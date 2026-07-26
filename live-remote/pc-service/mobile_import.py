# -*- coding: utf-8 -*-
"""手机版全民K歌导入编排(手机侧,轻量:只用 adb + 调 mobile_convert 子进程)。

流程(scan_phone):
1. `adb shell stat` 列手机 `files/{qrc,note,obbligato}` 的文件名 + mtime;
2. 每个 `qrc/<song_mid>_original.qrc` 的音高是 `note/<song_mid>.oke`(**同名直配**);伴奏/原唱两条
   `obbligato/<file_mid>.tkm` 与 qrc 不同名,按 **mtime 聚类**(点一首歌时四件同批落盘)取最近两条;
3. 去重:song_mid 已在曲库则跳过;
4. `adb pull` 拉 qrc/oke/两 tkm 到暂存 → 调 `karaoke-player/mobile_convert.py` 子进程转成 PC 四件套
   (写 `MOBILE_STAGING_DIR/<mid>/<mid>_*`),读进度行回调;
5. 返回候选 [{mid, source:"手机", src_root=MOBILE_STAGING_DIR, title, artist, needs_name}],
   交给扫描窗口勾选、library.import_candidate 入库。

无 adb / 无设备 / 无手机资源:抛异常或返回空,由扫描窗口降级处理(只扫 PC)。
"""
import os
import re
import time
import secrets
import subprocess

import config
import library

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW:pythonw 下不弹黑框


# ---------------------------------------------------------------- adb 基础
def _adb(args, serial=None, timeout=120):
    cmd = [config.ADB_PATH]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=_NO_WINDOW, timeout=timeout)


def list_devices():
    """返回在线设备序列号列表(state==device)。adb 不存在/异常 → []。"""
    try:
        r = _adb(["devices"], timeout=15)
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines()[1:]:          # 跳过 "List of devices attached"
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            out.append(parts[0])
    return out


def device_label(serial):
    """设备友好名:'<model> (<serial>)',取不到型号就用序列号。"""
    try:
        r = _adb(["shell", "getprop", "ro.product.model"], serial=serial, timeout=10)
        model = r.stdout.strip()
        return f"{model} ({serial})" if model else serial
    except Exception:
        return serial


# ---------------------------------------------------------------- 无线 ADB 配对(扫码连接)
def make_pair_payload():
    """生成配对二维码内容。手机『开发者选项→无线调试→用二维码配对设备』扫描后,会以 name 为实例名
    广播 `_adb-tls-pairing._tcp` mDNS 服务,主机据此发现并 `adb pair`。返回 (name, code, qr_data)。"""
    name = "studio-" + secrets.token_hex(3)      # 实例名(Android Studio 用 studio- 前缀)
    code = "%06d" % secrets.randbelow(1000000)   # 6 位配对码
    return name, code, "WIFI:T:ADB;S:%s;P:%s;;" % (name, code)


def _mdns_services():
    """`adb mdns services` → [(instance, type, ip:port)]。"""
    try:
        r = _adb(["mdns", "services"], timeout=10)
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1].startswith("_adb"):
            out.append((parts[0], parts[1], parts[2]))
    return out


def wait_and_pair(name, code, progress_cb=None, stop=None, timeout=150):
    """轮询 mDNS 等手机扫码 → `adb pair` 配对 → 找 connect 服务 `adb connect`。
    成功返回连上的 serial(`ip:port`),取消/超时返回 None。"""
    def prog(m):
        if progress_cb:
            progress_cb(m)

    def stopped():
        return bool(stop and stop())

    _adb(["start-server"], timeout=15)
    prog("等待手机扫码配对…")
    paired_ip, deadline = None, time.monotonic() + timeout
    while time.monotonic() < deadline and not stopped():
        pairing = [s for s in _mdns_services() if "_adb-tls-pairing" in s[1]]
        # 优先按二维码里的实例名匹配;匹配不到但只有一个配对服务时也用它(用户刚扫的就是我们这个)
        cand = [s for s in pairing if s[0].startswith(name)] or (pairing if len(pairing) == 1 else [])
        if cand:
            addr = cand[0][2]
            prog("发现手机,配对中… %s" % addr)
            try:
                r = _adb(["pair", addr, code], timeout=25)
            except Exception as e:
                prog("配对超时:%s" % e); time.sleep(1); continue
            if r.returncode == 0 and "paired" in (r.stdout + r.stderr).lower():
                paired_ip = addr.rsplit(":", 1)[0]
                break
            prog("配对失败,请核对二维码/重扫…")
        time.sleep(1)
    if not paired_ip:
        return None

    prog("已配对,连接中…")
    for _ in range(25):
        if stopped():
            return None
        conn = [s for s in _mdns_services()
                if "_adb-tls-connect" in s[1] and s[2].startswith(paired_ip + ":")]
        if conn:
            addr = conn[0][2]
            try:
                r = _adb(["connect", addr], timeout=15)
            except Exception:
                r = None
            if r and "connected" in (r.stdout + r.stderr).lower():
                return addr
        time.sleep(1)
    return None


# ---------------------------------------------------------------- 列文件 + mtime
def _stat_dir(serial, remote_dir, pattern):
    """`stat -c '%Y %n'` 列一个目录里匹配 pattern 的文件 → {basename: mtime_epoch}。
    目录空 / 不存在 → {}(stat 的报错落 stderr,不影响)。"""
    cmd = "stat -c '%%Y %%n' %s/%s" % (remote_dir, pattern)
    try:
        r = _adb(["shell", cmd], serial=serial, timeout=30)
    except Exception:
        return {}
    files = {}
    for line in r.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue                                # 跳过 "stat: cannot stat ..." 之类
        mt, path = int(parts[0]), parts[1].strip()
        files[path.rsplit("/", 1)[-1]] = mt
    return files


def _pull(serial, remote, local):
    r = _adb(["pull", remote, local], serial=serial, timeout=120)
    if r.returncode != 0 or not os.path.isfile(local):
        raise RuntimeError("adb pull 失败: %s\n%s" % (remote, (r.stderr or r.stdout)[:200]))


# ---------------------------------------------------------------- 转换子进程
def _run_convert(mid, out_dir, qrc, oke, tkms, progress_cb):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        [config.PLAYER_PYTHON, config.MOBILE_CONVERT_PATH, mid, out_dir, qrc, oke, *tkms],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=_NO_WINDOW, env=env)
    err = None
    for line in proc.stdout:
        line = line.rstrip()
        if line.startswith("PROGRESS "):
            if progress_cb:
                progress_cb("  " + line[9:])
        elif line.startswith("ERR "):
            err = line[4:]
    proc.wait()
    if proc.returncode != 0 or err:
        raise RuntimeError("转换失败(%s): %s" % (mid, err or "退出码 %d" % proc.returncode))


# ---------------------------------------------------------------- 主扫描
def _song_mid(qrc_name):
    """`<song_mid>[_<n>]_original.qrc` → song_mid(去尾部版本号 _N)。"""
    base = qrc_name[:-len("_original.qrc")] if qrc_name.endswith("_original.qrc") else qrc_name
    return re.sub(r"_\d+$", "", base)


def scan_phone(serial, progress_cb=None, on_candidate=None, known_mids=None):
    """扫手机、拉取、转换,返回库里没有的新歌候选。serial 必填(由扫描窗口从 list_devices 选)。
    on_candidate(cand):每转换好一首**立即**回调一次(供 UI 扫出一首显示一首),函数末尾仍返回全部。"""
    def prog(msg):
        if progress_cb:
            progress_cb(msg)

    if not serial:
        raise RuntimeError("未选择手机设备")
    known = set(known_mids if known_mids is not None else library.manifest().keys())

    prog("列手机资源…")
    qrcs = _stat_dir(serial, config.MOBILE_FILES + "/qrc", "*_original.qrc")
    notes = _stat_dir(serial, config.MOBILE_FILES + "/note", "*.oke")
    tkms = _stat_dir(serial, config.MOBILE_FILES + "/obbligato", "*.tkm")
    if not qrcs:
        prog("手机上没有可导入的歌(先在全民K歌里点开唱一首)")
        return []

    # song_mid → qrc mtime / 文件名(同 mid 多版本取最新)
    song_mt, song_qname = {}, {}
    for qname, qmt in qrcs.items():
        mid = _song_mid(qname)
        if mid not in song_mt or qmt > song_mt[mid]:
            song_mt[mid], song_qname[mid] = qmt, qname

    # **每条 tkm 归属 mtime 最近的那首歌**(比"固定窗口内任取"稳:某首只缓存了歌词、没下伴奏时,
    # 不会误抢邻近歌的 tkm——因为那些 tkm 离它们自己的 qrc 更近)。窗口仅作离谱防呆上限。
    tkm_by_song = {}
    for tname, tmt in tkms.items():
        mid = min(song_mt, key=lambda m: abs(song_mt[m] - tmt))
        if abs(song_mt[mid] - tmt) <= config.MOBILE_TKM_WINDOW:
            tkm_by_song.setdefault(mid, []).append((tname, tmt))

    todo = []
    for mid, qmt in song_mt.items():
        if mid in known:
            continue
        if (mid + ".oke") not in notes:
            prog("跳过 %s(无音高数据)" % mid)
            continue
        pool = sorted(tkm_by_song.get(mid, []), key=lambda kv: abs(kv[1] - qmt))
        chosen = [n for n, _ in pool][:2]
        if not chosen:
            prog("跳过 %s(伴奏未下载)" % mid)
            continue
        todo.append((mid, song_qname[mid], mid + ".oke", chosen))

    if not todo:
        prog("没有库里缺的新歌")
        return []

    os.makedirs(config.MOBILE_STAGING_DIR, exist_ok=True)
    cands = []
    for i, (mid, qname, nname, tkm_names) in enumerate(todo):
        prog("转换 %d/%d:%s" % (i + 1, len(todo), mid))
        raw = os.path.join(config.MOBILE_STAGING_DIR, "_raw", mid)
        os.makedirs(raw, exist_ok=True)
        try:
            qrc_l = os.path.join(raw, "q.qrc")
            oke_l = os.path.join(raw, "n.oke")
            _pull(serial, config.MOBILE_FILES + "/qrc/" + qname, qrc_l)
            _pull(serial, config.MOBILE_FILES + "/note/" + nname, oke_l)
            tkm_ls = []
            for j, tn in enumerate(tkm_names):
                tp = os.path.join(raw, "t%d.tkm" % j)
                _pull(serial, config.MOBILE_FILES + "/obbligato/" + tn, tp)
                tkm_ls.append(tp)
            _run_convert(mid, config.MOBILE_STAGING_DIR, qrc_l, oke_l, tkm_ls, progress_cb)
            meta = library._qrc_meta(os.path.join(config.MOBILE_STAGING_DIR, mid, mid + ".qrc"))
            cand = {"mid": mid, "source": "手机", "src_root": config.MOBILE_STAGING_DIR,
                    "title": meta["title"], "artist": meta["artist"],
                    "needs_name": meta["needs_name"]}
            cands.append(cand)
            if on_candidate:
                on_candidate(cand)                 # 扫出一首立即回调 → UI 增量显示
        except Exception as e:
            prog("× %s 失败:%s" % (mid, e))
    return cands
