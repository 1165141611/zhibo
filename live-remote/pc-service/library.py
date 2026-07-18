# -*- coding: utf-8 -*-
"""自动曲库导入器:监听 WeSing 缓存(Res\\,LRU 只留最近几首),把唱过的歌四件套
拷进永久曲库(config.KARAOKE_LIBRARY_DIR),解 QRC 取歌名/歌手写 meta.json,维护 library.json。
后台 daemon 线程:启动先 backfill 全量补齐,之后每 LIBRARY_SCAN_INTERVAL 轮询;
文件连续两轮 (size,mtime) 签名一致(=写完)才入库。"""
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


# ---------------------------------------------------- QRC → 歌名/歌手
def _qrc_meta(qrc_path):
    """解 QRC 只取 [ti:]/[ar:]。镜像 karaoke-player/assets._qrc_decrypt。"""
    raw = open(qrc_path, "rb").read()
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
    artist = "" if re.fullmatch(r"\d+", ar) else ar     # 纯数字歌手(如"0")=垃圾,清掉
    title = _clean_title(ti, ar)
    return {"title": title, "artist": artist, "needs_name": _is_junk_title(title)}


# ---------------------------------------------------- 文件判定
def _paths(root, mid):
    base = os.path.join(root, mid, mid)
    return [base + s for s in config.LIBRARY_SUFFIXES]


def _complete(root, mid):
    return all(os.path.isfile(p) for p in _paths(root, mid))


def _signature(root, mid):
    """四文件 (size, mtime) 元组;两轮一致 = 写完。"""
    return tuple((os.stat(p).st_size, int(os.stat(p).st_mtime))
                 for p in _paths(root, mid))


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


# ---------------------------------------------------- 入库
def _import_one(mid, man):
    dst = os.path.join(config.KARAOKE_LIBRARY_DIR, mid)
    os.makedirs(dst, exist_ok=True)
    for p in _paths(config.WESING_RES_DIR, mid):     # 拷 4 文件(PCM 各 ~78MB)
        shutil.copy2(p, os.path.join(dst, os.path.basename(p)))
    meta = _qrc_meta(os.path.join(dst, mid + ".qrc"))   # QRC 未下完会抛异常 → 本首不落库,下轮重试
    json.dump(meta, open(os.path.join(dst, "meta.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    man[mid] = {**meta, "added": time.time(),
                "plays": int(man.get(mid, {}).get("plays", 0))}   # 重入库保留已有点歌次数
    _save_manifest(man)
    return meta


def _scan_once(man, pending):
    """扫一遍 Res:齐全且未入库的记签名,连续两轮一致才入库。返回本轮新入库数。"""
    added = 0
    try:
        entries = os.listdir(config.WESING_RES_DIR)
    except Exception:
        return 0
    for mid in entries:
        if not os.path.isdir(os.path.join(config.WESING_RES_DIR, mid)):
            continue
        if _already(man, mid) or not _complete(config.WESING_RES_DIR, mid):
            pending.pop(mid, None)
            continue
        try:
            sig = _signature(config.WESING_RES_DIR, mid)
        except Exception:
            continue
        if pending.get(mid) == sig:              # 稳定 → 入库
            try:
                meta = _import_one(mid, man)
                added += 1
                if _on_import:
                    try:                         # 通知失败绝不影响入库循环
                        _on_import(mid, meta, len(man))
                    except Exception:
                        pass
            except Exception as e:
                print(f"[LIB] 导入 {mid} 失败(下轮重试): {e}")
            pending.pop(mid, None)
        else:
            pending[mid] = sig                   # 记签名,下轮再比
    return added


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
    # 迁移(读 QRC 慢)放后台,不阻塞启动;_MANIFEST 已在 start() 同步载好。
    try:
        _remigrate(_MANIFEST)     # 用新清洗规则修旧条目(幂等,跳过手动命名的)
    except Exception as e:
        print(f"[LIB] 迁移异常: {e}")
    if _state is not None:
        _state["lib_count"] = len(_MANIFEST)
    if _on_change:
        _on_change()              # 迁移后回调(歌名已修正,server 侧会带修正名重推歌单)
    pending = {}
    while True:
        try:
            added = _scan_once(_MANIFEST, pending)   # 就地更新 _MANIFEST
        except Exception as e:
            print(f"[LIB] 扫描异常: {e}")
            added = 0
        if added and _state is not None:
            _state["lib_count"] = len(_MANIFEST)
            if _on_change:
                _on_change()                         # 刷托盘 + 推手机(歌单已更新)
        time.sleep(config.LIBRARY_SCAN_INTERVAL)


def start(state, on_change=None, on_import=None):
    """启动后台监听线程。state=server.STATE;on_change=曲库变化回调(刷托盘+推手机);
    on_import=单曲入库成功回调 (mid, meta, 库存数),供 server 弹系统通知。"""
    global _state, _on_change, _on_import, _MANIFEST
    _state, _on_change, _on_import = state, on_change, on_import
    _MANIFEST = _load_manifest()   # **同步载入**(小 json,快):确保随后 start_player 的
    state["lib_count"] = len(_MANIFEST)   # _push_setlist 能立即取到歌名(修歌单启动为空的竞态)
    state["watcher_running"] = True
    threading.Thread(target=_worker, daemon=True).start()
