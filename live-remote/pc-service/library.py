# -*- coding: utf-8 -*-
"""曲库层:维护永久曲库(config.KARAOKE_LIBRARY_DIR)+ 清单 library.json,解 QRC 取歌名/歌手。
导入改为**手动扫描窗口触发**(不再后台轮询):
- scan_pc():列 PC 版 WeSing 缓存里库中没有的新歌候选(手机侧候选见 mobile_import.py);
- import_candidate(cand, title, artist, swap):把用户勾选的候选(PC 或手机来源)拷进曲库并登记,
  支持用户编辑歌名/歌手、对调伴奏/原唱。
start() 只载入清单 + 后台跑一次启动迁移(_remigrate,用当前清洗规则修旧脏标题)。"""
import os
import sys
import re
import zlib
import json
import time
import shutil
import threading

import config

# 复用 karaoke-player 的魔改 DES(只借这份易抄错的实现,不引 numpy)
sys.path.append(config.KARAOKE_DIR)
import tripledes  # noqa: E402

_QRC_KEY = b"!@#)(*$%123ZXC!@!@#)(NHL"

# ---------------------------------------------------- 歌名清洗
# WeSing 对 KTV/用户上传版本常把 QRC 的 [ti:] 填脏:带"-歌手-ktv"后缀,甚至整段是内部
# 数字ID(如成都存成"2422569")。干净歌名只在 WeSing 的 KSongsDataInfo.dat 里,但那是
# AES 级强加密(实测熵7.9、非ECB、KSongs DLL 无 TEA/明文key),解不动(见 DEV_LOG)。
# 故:能清洗的就地清洗(去后缀,救回"鼓楼-赵雷-ktv"→"鼓楼");纯数字/空的救不回,标
# needs_name=True 让托盘弹通知、用户点通知手动改名。
_JUNK_TOKEN = re.compile(
    r"^(ktv|live|现场|伴奏|demo|remix|cover|翻唱|原唱|清唱|纯享|高清|hq|hd|mv)$", re.I)
_JUNK_BRACKET = re.compile(
    r"[（(【\[]\s*(ktv|伴奏|现场|live|remix|demo|翻唱|cover|纯享|原唱|清唱|高清)"
    r"[^）)】\]]*[）)】\]]", re.I)


def _clean_title(ti, ar):
    """清洗 [ti:]:去版本注记括号、按分隔符切并丢掉=歌手或垃圾关键词的段。"""
    t = (ti or "").strip()
    t = _JUNK_BRACKET.sub("", t).strip()
    parts = re.split(r"\s*[-_/｜|、]\s*", t)
    if len(parts) > 1:
        ar_s = (ar or "").strip()
        kept = [p.strip() for p in parts if p.strip()
                and p.strip() != ar_s and not _JUNK_TOKEN.match(p.strip())]
        if kept:
            t = "-".join(kept)
    return t.strip()


def _is_junk_title(t):
    """标题是否是救不回的垃圾:空 / 纯数字(内部ID) / 纯符号。"""
    t = (t or "").strip()
    return (not t) or bool(re.fullmatch(r"\d+", t)) or bool(re.fullmatch(r"[\W_]+", t))

_state = None            # 注入的 server.STATE
_on_change = None        # 注入的"曲库变化"回调(刷托盘 + 推手机)
_on_import = None        # 注入的"单曲入库成功"回调 (mid, meta, 库存数) → 系统通知
_MANIFEST = {}           # 当前曲库清单 {mid: {title, artist, added}},供 server 读列表/取歌名


def manifest():
    """返回曲库清单副本(mid → {title, artist, added})。"""
    return dict(_MANIFEST)


def song_meta(mid):
    return _MANIFEST.get(mid)


def pending_rename():
    """待手动命名(needs_name)的 mid 列表——[ti:] 是纯数字/空、清洗救不回的那些。"""
    return [mid for mid, m in _MANIFEST.items() if m.get("needs_name")]


def bump_play(mid):
    """点歌一次 → 该曲"点歌次数"(plays)+1,持久化到 library.json 并触发刷新
    (手机端点歌列表默认按 plays 倒序,常点的歌浮到最前)。plays 只存清单、不写 meta.json
    (meta.json 会被启动迁移按 QRC 重写,存这里会被冲掉;清单才是列表的权威源)。"""
    ent = _MANIFEST.get(mid)
    if not ent:
        return
    ent["plays"] = int(ent.get("plays", 0)) + 1
    _save_manifest(_MANIFEST)
    if _on_change:
        _on_change()   # 刷托盘 + 推手机(顺序变了)
    return ent["plays"]


def get_key(mid):
    """某曲保存的默认调式(半音,无则 0=原调)。"""
    return int((_MANIFEST.get(mid) or {}).get("key", 0))


def set_key(mid, key):
    """设某曲的默认调式(半音,夹到 [-6,6]),持久化到 library.json。**不触发全量刷新**
    (托盘就地更新标签,避免重建列表丢滚动位置)。手机点到这首时由 server 载入时下发应用。"""
    ent = _MANIFEST.get(mid)
    if not ent:
        return None
    key = max(-6, min(6, int(key)))
    ent["key"] = key
    _save_manifest(_MANIFEST)
    return key


def rename(mid, title, artist):
    """手动订正歌名/歌手:更新内存清单 + library.json + meta.json,清 needs_name,触发刷新。
    供 server 的"点通知改名"用。返回是否成功。"""
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title or mid not in _MANIFEST:
        return False
    ent = _MANIFEST[mid]
    ent["title"], ent["artist"] = title, artist
    ent.pop("needs_name", None)
    ent["named"] = True          # 标记手动命名过:启动重迁移不再覆盖它
    _save_manifest(_MANIFEST)
    mp = os.path.join(config.KARAOKE_LIBRARY_DIR, mid, "meta.json")
    try:
        meta = json.load(open(mp, encoding="utf-8")) if os.path.isfile(mp) else {}
        meta.update({"title": title, "artist": artist})
        meta.pop("needs_name", None)
        json.dump(meta, open(mp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as e:
        print("[LIB] 写 meta.json 失败:", e)
    if _on_change:
        _on_change()   # 刷托盘 + 推手机
    return True


def delete(mid):
    """从曲库删除一首:删歌文件夹(四件套+meta)+ 清内存清单/library.json + 刷新(_on_change)。
    返回是否原本在库。歌单/队列的摘除由 server 侧 _lib_delete 先做(library 层不碰 server 状态)。"""
    existed = mid in _MANIFEST
    _MANIFEST.pop(mid, None)
    if existed:
        _save_manifest(_MANIFEST)
    dst = os.path.join(config.KARAOKE_LIBRARY_DIR, mid)
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)   # 删不掉(占用)也不抛,清单已清
    if _state is not None:
        _state["lib_count"] = len(_MANIFEST)
    if _on_change:
        _on_change()   # 刷托盘 + 推手机(库列表)
    return existed


# ---------------------------------------------------- QRC → 歌名/歌手
def _qrc_meta(qrc_path):
    """解 QRC 只取 [ti:]/[ar:]。镜像 karaoke-player/assets._qrc_decrypt。"""
    raw = open(qrc_path, "rb").read()
    s = raw.lstrip()
    if s[:5] == b"<?xml" or b"<QrcInfos" in raw[:200] or b"LyricContent=" in raw[:400]:
        xml = raw.decode("utf-8", "replace")                        # QQ音乐:已解密明文 QRC XML
    else:
        if all(c in b"0123456789abcdefABCDEF\r\n\t " for c in raw.strip()):
            cipher = bytearray.fromhex(raw.strip().decode("ascii"))     # 手机 hex 文本
        else:
            nl = raw.find(b"\n")                                        # PC:[offset:0]\n+裸密文
            cipher = bytearray(raw[nl + 1:] if nl != -1 else raw)
        sch = tripledes.tripledes_key_setup(_QRC_KEY, tripledes.DECRYPT)
        out = bytearray()
        for i in range(0, len(cipher), 8):
            out += tripledes.tripledes_crypt(cipher[i:], sch)
        xml = zlib.decompress(out).decode("utf-8", "replace")
    m = re.search(r'LyricContent="(.*?)"\s*/>', xml, re.S)
    body = m.group(1) if m else xml

    def g(k):
        mm = re.search(r"\[%s:(.*?)\]" % k, body)
        return mm.group(1).strip() if mm else ""

    ti, ar = g("ti"), g("ar")
    # 纯数字(如"0")/ 字面量 None|null = 垃圾歌手,清掉
    artist = "" if (re.fullmatch(r"\d+", ar) or ar.strip().lower() in ("none", "null")) else ar
    title = _clean_title(ti, ar)
    return {"title": title, "artist": artist, "needs_name": _is_junk_title(title)}


# ---------------------------------------------------- 文件判定
def _paths(root, mid):
    base = os.path.join(root, mid, mid)
    return [base + s for s in config.LIBRARY_SUFFIXES]


def _complete(root, mid):
    return all(os.path.isfile(p) for p in _paths(root, mid))


# ---------------------------------------------------- 清单
def _load_manifest():
    try:
        return json.load(open(config.LIBRARY_JSON, "r", encoding="utf-8"))
    except Exception:
        return {}   # {mid: {"title","artist","added"}}


def _save_manifest(man):
    os.makedirs(config.KARAOKE_LIBRARY_DIR, exist_ok=True)
    tmp = config.LIBRARY_JSON + ".tmp"
    json.dump(man, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, config.LIBRARY_JSON)   # 原子替换,防写一半


def _already(man, mid):
    dst = os.path.join(config.KARAOKE_LIBRARY_DIR, mid)
    return mid in man and os.path.isfile(os.path.join(dst, "meta.json"))


# ---------------------------------------------------- 扫描候选(不入库)
def scan_pc():
    """扫 PC 版 WeSing 缓存(WESING_RES_DIR),返回**库里没有的**新歌候选(不入库)。
    每个候选:{mid, source:"PC", src_root, title, artist, needs_name}。
    供扫描窗口列出、用户勾选后再经 import_candidate 入库。"""
    cands = []
    try:
        entries = os.listdir(config.WESING_RES_DIR)
    except Exception:
        return cands
    for mid in entries:
        if not os.path.isdir(os.path.join(config.WESING_RES_DIR, mid)):
            continue
        if not _complete(config.WESING_RES_DIR, mid):     # 四件套不全 → 无法导入,跳过
            continue
        already = _already(_MANIFEST, mid)                # 已入库:仍列出但标 in_library(UI 置灰禁选)
        try:
            meta = _qrc_meta(os.path.join(config.WESING_RES_DIR, mid, mid + ".qrc"))
        except Exception as e:
            if not already:
                print(f"[LIB] 跳过 {mid}(QRC 解析失败): {e}")
                continue
            meta = {"title": "", "artist": "", "needs_name": False}  # 已入库解析失败也照列(用库里名)
        if already:                                       # 已入库标题/歌手优先用库里存的(清洗+改名后)
            ent = _MANIFEST.get(mid, {})
            title = (ent.get("title") or meta["title"] or mid)
            artist = ent.get("artist", meta["artist"])
        else:
            title, artist = meta["title"], meta["artist"]
        try:
            mtime = os.path.getmtime(os.path.join(config.WESING_RES_DIR, mid))   # 缓存时间(排序用)
        except OSError:
            mtime = 0
        cands.append({"mid": mid, "source": "PC", "src_root": config.WESING_RES_DIR,
                      "title": title, "artist": artist,
                      "needs_name": (meta["needs_name"] and not already),
                      "in_library": already, "mtime": mtime})
    return cands


# ---------------------------------------------------- 入库(单个候选)
def import_candidate(cand, title=None, artist=None, swap=False):
    """把一个候选(PC 或手机来源)拷进永久曲库并登记。
    - cand["src_root"] 决定源目录(PC=WESING_RES_DIR;手机=已转换的暂存目录);
    - title/artist 非空则用**用户编辑值**覆盖(置 named=True,防启动 _remigrate 覆盖),否则解 QRC;
    - swap=True 把 _accompany.pcm / _kongsinger.pcm 对调(修正伴奏/原唱判别);
    - 重入库保留已有 plays / key。返回 meta。"""
    mid = cand["mid"]
    src_root = cand["src_root"]
    dst = os.path.join(config.KARAOKE_LIBRARY_DIR, mid)
    os.makedirs(dst, exist_ok=True)
    for p in _paths(src_root, mid):                  # 拷四件套(PCM 各 ~50MB)
        name = os.path.basename(p)
        if swap and name.endswith("_accompany.pcm"):
            name = mid + "_kongsinger.pcm"
        elif swap and name.endswith("_kongsinger.pcm"):
            name = mid + "_accompany.pcm"
        shutil.copy2(p, os.path.join(dst, name))

    title = (title or "").strip()
    artist = (artist or "").strip()
    if title:                                        # 用户编辑值优先
        meta = {"title": title, "artist": artist, "needs_name": False}
        named = True
    else:
        meta = _qrc_meta(os.path.join(dst, mid + ".qrc"))
        named = False
    json.dump({"title": meta["title"], "artist": meta["artist"]},
              open(os.path.join(dst, "meta.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    prev = _MANIFEST.get(mid, {})
    ent = {"title": meta["title"], "artist": meta["artist"],
           "added": time.time(),
           "plays": int(prev.get("plays", 0)),       # 重入库保留点歌次数
           "key": int(prev.get("key", 0))}           # 及默认调式
    if meta.get("needs_name"):
        ent["needs_name"] = True
    if named:
        ent["named"] = True                          # 手动命名过,_remigrate 不覆盖
    _MANIFEST[mid] = ent
    _save_manifest(_MANIFEST)
    if _state is not None:
        _state["lib_count"] = len(_MANIFEST)
    return meta


def _remigrate(man):
    """启动迁移:用当前清洗规则重刷旧条目的歌名/needs_name(修复前入库的脏标题,如
    "鼓楼-赵雷-ktv"→"鼓楼"、给纯数字标题补 needs_name)。清洗幂等,好标题重刷不变。
    **已手动命名(named=True)的跳过**,绝不覆盖用户订正。返回是否有改动。"""
    changed = False
    for mid, ent in man.items():
        if ent.get("named"):
            continue
        qp = os.path.join(config.KARAOKE_LIBRARY_DIR, mid, mid + ".qrc")
        if not os.path.isfile(qp):
            continue
        try:
            meta = _qrc_meta(qp)
        except Exception:
            continue
        if (ent.get("title") != meta["title"] or ent.get("artist") != meta["artist"]
                or bool(ent.get("needs_name")) != meta["needs_name"]):
            ent["title"], ent["artist"] = meta["title"], meta["artist"]
            if meta["needs_name"]:
                ent["needs_name"] = True
            else:
                ent.pop("needs_name", None)
            try:                                 # 同步 meta.json
                mp = os.path.join(config.KARAOKE_LIBRARY_DIR, mid, "meta.json")
                json.dump(dict(meta), open(mp, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=2)
            except Exception:
                pass
            changed = True
    if changed:
        _save_manifest(man)
    return changed


def _worker():
    # 启动迁移(读 QRC 慢)放后台跑一次,不阻塞启动;_MANIFEST 已在 start() 同步载好。
    # **不再周期轮询**:导入改为扫描窗口手动触发(scan_pc / mobile_import + import_candidate)。
    try:
        _remigrate(_MANIFEST)     # 用新清洗规则修旧条目(幂等,跳过手动命名的)
    except Exception as e:
        print(f"[LIB] 迁移异常: {e}")
    if _state is not None:
        _state["lib_count"] = len(_MANIFEST)
    if _on_change:
        _on_change()              # 迁移后回调(歌名已修正,server 侧会带修正名重推歌单)


def start(state, on_change=None, on_import=None):
    """载入曲库清单 + 后台跑一次启动迁移。state=server.STATE;on_change=曲库变化回调
    (刷托盘+推手机);on_import 保留兼容(手动扫描窗口自己反馈,不再用它弹单曲通知)。"""
    global _state, _on_change, _on_import, _MANIFEST
    _state, _on_change, _on_import = state, on_change, on_import
    _MANIFEST = _load_manifest()   # **同步载入**(小 json,快):确保随后 start_player 的
    state["lib_count"] = len(_MANIFEST)   # _push_setlist 能立即取到歌名(修歌单启动为空的竞态)
    threading.Thread(target=_worker, daemon=True).start()
