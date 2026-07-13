# -*- coding: utf-8 -*-
"""
直播遥控 · 电脑后台服务
=====================================
职责:
  1. 收手机(悬浮窗 App / 测试网页)通过 WebSocket 发来的指令
  2. 声卡音轨   → 通过 loopMIDI 虚拟口发 MIDI 给 Studio One
  3. 背景音乐   → 系统媒体键控制 QQ音乐 播放,pycaw 单独调 QQ音乐 音量
  4. 把当前状态实时推回手机(当前场景 / BGM 音量 / 连接状态)

运行: python server.py
依赖: pip install -r requirements.txt  (还需另装 loopMIDI 虚拟 MIDI 驱动)
"""

import os
import sys
import json
import socket
import ctypes
import asyncio
import threading
import subprocess
import warnings

warnings.filterwarnings("ignore")  # 屏蔽 pycaw 对未接入设备的无害 COMError 警告

import comtypes
from comtypes import CLSCTX_ALL, cast, POINTER
import psutil
from pycaw.pycaw import (AudioUtilities, ISimpleAudioVolume,
                         IAudioSessionManager2, IAudioSessionControl2)

import winmm_midi
import studio_win
import scrcpy_win

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.staticfiles import StaticFiles

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# pythonw(无窗口)下 sys.stdout/stderr 为 None,print 和 uvicorn 日志会报错导致后台线程起不来。
# 重定向到日志文件,既不报错,出问题也能查 server.log。
if sys.stdout is None or sys.stderr is None:
    try:
        _logf = open(os.path.join(BASE_DIR, "server.log"), "a", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = _logf
        if sys.stderr is None:
            sys.stderr = _logf
    except Exception:
        pass

# ── 全局状态(推给手机)──────────────────────────────────
STATE = {
    "scene": None,             # 当前声卡场景 id
    "bgm_vol": None,           # QQ音乐 音量 0-100
    "studio_connected": False, # MIDI 口是否打开成功
    "bgm_title": "",           # BGM 歌名(来自 winrt 子进程)
    "bgm_artist": "",          # 歌手
    "bgm_pos": 0,              # 播放位置(秒)
    "bgm_dur": 0,              # 总时长(秒)
    "bgm_playing": True,       # 播放状态(渐变方向 + 进度条;假定启动时在播)
    "studio_visible": True,    # Studio One 窗口是否显示
    "scrcpy_ok": False,        # scrcpy 无线投屏是否已就绪
    "scrcpy_msg": "",          # scrcpy 最近一次尝试的说明(失败原因等)
}

# ══════════════════════════════════════════════════════════
#  1) 声卡音轨 —— MIDI 发送
# ══════════════════════════════════════════════════════════
_midi_out = None


def open_midi():
    """打开 loopMIDI 虚拟端口。返回是否成功。"""
    global _midi_out
    names = winmm_midi.list_output_names()
    out = winmm_midi.MidiOut()
    matched = out.open_by_name(config.MIDI_PORT_NAME)
    if matched is None:
        print(f"[MIDI] 未找到端口 '{config.MIDI_PORT_NAME}'。现有端口: {names}")
        print("[MIDI] 请先安装 loopMIDI 并创建同名端口。")
        return False
    _midi_out = out
    print(f"[MIDI] 已连接: {matched}")
    return True


# 4 条人声通道的静音状态(note -> True=已静音)。约定:启动/归位时全部"不静音"。
# 只要直播前把 4 条通道的 M 都关掉,这里的记录就和 Studio One 实际一致,之后切换一直准。
_mute_state = {note: False for note in config.VOCAL_MUTE_NOTES.values()}
_midi_in = None


def reset_mute_state():
    """归位:把记录重置为"全不静音"。配合"把 4 条 M 都关掉"使用,恢复同步。"""
    for note in config.VOCAL_MUTE_NOTES.values():
        _mute_state[note] = False
    STATE["scene"] = None


def _on_feedback(status, d1, d2):
    """Studio One 回传的 Mackie 消息。静音 LED = Note On(0x90) 音符16-23,力度>0 表示已静音。"""
    if (status & 0xF0) == 0x90 and d1 in config.VOCAL_MUTE_NOTES.values():
        _mute_state[d1] = (d2 > 0)


def open_feedback():
    """打开回传端口,开始读 Studio One 的静音状态。"""
    global _midi_in
    _midi_in = winmm_midi.MidiIn()
    matched = _midi_in.open_by_name(config.MIDI_FEEDBACK_NAME, _on_feedback)
    if matched:
        print(f"[MIDI] 状态回读已连接: {matched}")
        return True
    print(f"[MIDI] 未找到回读端口 '{config.MIDI_FEEDBACK_NAME}'")
    return False


def _toggle_mute(note):
    """Mackie 静音切换 = 音符按下(vel127)+ 松开(vel0)。"""
    _midi_out.note_on(note, 127, config.MIDI_CHANNEL)
    _midi_out.note_on(note, 0, config.MIDI_CHANNEL)


def learn_states():
    """启动时用"双翻转"探明每条通道的真实静音状态(净效果为零,只为触发回传)。"""
    import time
    for note in config.VOCAL_MUTE_NOTES.values():
        _toggle_mute(note)
        time.sleep(0.2)
        _toggle_mute(note)
        time.sleep(0.2)


def send_scene(scene_id):
    """切换声卡场景:让 active 那条人声"未静音"、其余"静音";闭麦(active=None)全部静音。
    依据 Studio One 回传的真实状态,只翻转需要改变的通道。"""
    sc = config.SCENES.get(scene_id)
    if _midi_out is None or sc is None:
        return False
    active = sc.get("active")
    try:
        for name, note in config.VOCAL_MUTE_NOTES.items():
            target_muted = (name != active)   # active 那条=False(不静音),其余=True;None 时全 True
            if _mute_state.get(note) != target_muted:
                _toggle_mute(note)
                _mute_state[note] = target_muted   # 乐观更新,随后回传会再校正
        return True
    except Exception as e:
        print(f"[MIDI] 发送失败: {e}")
        return False


# ══════════════════════════════════════════════════════════
#  2) 背景音乐 —— 系统媒体键
# ══════════════════════════════════════════════════════════
KEYEVENTF_KEYUP = 0x0002
VK_MEDIA_NEXT = 0xB0
VK_MEDIA_PREV = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3


def _tap_media(vk):
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


# ══════════════════════════════════════════════════════════
#  3) 背景音乐 —— QQ音乐 单独音量 (pycaw)
# ══════════════════════════════════════════════════════════
_tls = threading.local()


def _ensure_com():
    """pycaw 走 COM,每个线程首次调用前需 CoInitialize。"""
    if not getattr(_tls, "inited", False):
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        _tls.inited = True


# 这台机器是 ROUTIST R2 声卡,QQ音乐 会同时出现在多个虚拟路由(设备)上。
# 所以要跨"所有活跃设备"找出全部 QQ音乐 会话,音量一起调,谁喂给监听/直播都同步。
_qq_vols = []  # 缓存:所有匹配到的 ISimpleAudioVolume


def _proc_name(pid):
    try:
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def _resolve_qq_sessions():
    """跨所有活跃设备,收集所有 QQ音乐 相关会话的音量接口。"""
    _ensure_com()
    targets = [p.lower() for p in config.QQMUSIC_PROCS]
    vols = []
    try:
        for d in AudioUtilities.GetAllDevices():
            if "Active" not in str(d.state):
                continue
            try:
                mgr = cast(d._dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None),
                           POINTER(IAudioSessionManager2))
                senum = mgr.GetSessionEnumerator()
                for i in range(senum.GetCount()):
                    ctl = senum.GetSession(i)
                    ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                    if _proc_name(ctl2.GetProcessId()) in targets:
                        vols.append(ctl.QueryInterface(ISimpleAudioVolume))
            except Exception:
                continue
    except Exception as e:
        print(f"[VOL] 枚举设备失败: {e}")
    return vols


def _qq_vols_cached():
    global _qq_vols
    if not _qq_vols:
        _qq_vols = _resolve_qq_sessions()
    return _qq_vols


def set_qq_volume(pct):
    global _qq_vols
    pct = max(0, min(100, pct))
    vols = _qq_vols_cached()
    ok, stale = False, False
    for v in vols:
        try:
            v.SetMasterVolume(pct / 100.0, None)
            ok = True
        except Exception:
            stale = True
    if stale or not vols:
        # 会话失效(QQ音乐重启/切换设备)→ 重新解析一次再试
        _qq_vols = _resolve_qq_sessions()
        for v in _qq_vols:
            try:
                v.SetMasterVolume(pct / 100.0, None)
                ok = True
            except Exception:
                pass
    return ok


def get_qq_volume():
    global _qq_vols
    for attempt in range(2):
        for v in _qq_vols_cached():
            try:
                return int(round(v.GetMasterVolume() * 100))
            except Exception:
                continue
        _qq_vols = _resolve_qq_sessions()  # 第一次读失败就重解析后再读
    return None


# ══════════════════════════════════════════════════════════
#  4) WebSocket 服务
# ══════════════════════════════════════════════════════════
app = FastAPI()
_clients = set()
_loop = None            # uvicorn 的事件循环(供子进程读取线程跨线程广播)
_smtc_proc = None       # winrt 子进程


@app.on_event("startup")
async def _capture_loop():
    global _loop
    _loop = asyncio.get_running_loop()


def start_smtc_reader():
    """启动 winrt 子进程,后台线程读它的 stdout(歌名/进度),更新状态并推给手机。"""
    def worker():
        global _smtc_proc
        helper = os.path.join(BASE_DIR, "smtc_helper.py")
        while True:
            try:
                _smtc_proc = subprocess.Popen(
                    [sys.executable, helper],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                    text=True, encoding="utf-8",
                )
            except Exception as e:
                print(f"[BGM] 启动 winrt 子进程失败: {e}")
                return
            for line in _smtc_proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    snap = json.loads(line)
                except Exception:
                    continue
                changed = any(STATE.get(k) != v for k, v in snap.items())
                STATE.update(snap)
                if changed and _clients and _loop is not None:
                    asyncio.run_coroutine_threadsafe(_broadcast(), _loop)
            # 子进程退出了,稍等重启
            import time
            time.sleep(2)

    threading.Thread(target=worker, daemon=True).start()


async def _broadcast():
    msg = json.dumps({"type": "state", **STATE})
    dead = []
    for ws in _clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for d in dead:
        _clients.discard(d)


_fading = False


async def _fade_out_pause():
    """渐弱到 0 → 暂停 → 恢复会话音量(此时已静默)。"""
    global _fading
    if _fading:
        return
    _fading = True
    try:
        full = get_qq_volume() or STATE.get("bgm_vol") or 60
        steps = 20
        dt = config.FADE_SECONDS / steps if config.FADE_SECONDS > 0 else 0
        for i in range(steps - 1, -1, -1):
            set_qq_volume(round(full * i / steps))
            if dt:
                await asyncio.sleep(dt)
        _tap_media(VK_MEDIA_PLAY_PAUSE)     # 暂停
        await asyncio.sleep(0.1)
        set_qq_volume(full)                 # 恢复音量(已暂停,无声)
        STATE["bgm_vol"] = full
        STATE["bgm_playing"] = False
    finally:
        _fading = False
    await _broadcast()


async def _play_fade_in():
    """会话音量设 0 → 播放 → 渐强回原值。"""
    global _fading
    if _fading:
        return
    _fading = True
    try:
        full = get_qq_volume() or STATE.get("bgm_vol") or 60
        set_qq_volume(0)
        _tap_media(VK_MEDIA_PLAY_PAUSE)     # 播放
        steps = 20
        dt = config.FADE_SECONDS / steps if config.FADE_SECONDS > 0 else 0
        for i in range(1, steps + 1):
            set_qq_volume(round(full * i / steps))
            if dt:
                await asyncio.sleep(dt)
        STATE["bgm_vol"] = full
        STATE["bgm_playing"] = True
    finally:
        _fading = False
    await _broadcast()


async def _handle_cmd(data, phone_ip=None):
    cmd = data.get("cmd")
    if cmd == "ensure_scrcpy":
        # 进入悬浮态时由网页触发:确保无线 adb 已连 + scrcpy 在跑。
        # 阻塞的 adb/scrcpy 调用丢到线程池,别卡住事件循环。
        ok, msg = await asyncio.to_thread(scrcpy_win.ensure_scrcpy, phone_ip)
        STATE["scrcpy_ok"] = ok
        STATE["scrcpy_msg"] = msg
        print(f"[SCRCPY] ip={phone_ip} ok={ok} {msg}")
    elif cmd == "scene":
        sid = int(data.get("id"))
        if send_scene(sid):
            STATE["scene"] = sid
    elif cmd == "bgm":
        action = data.get("action")
        if action == "next":
            _tap_media(VK_MEDIA_NEXT)
        elif action == "prev":
            _tap_media(VK_MEDIA_PREV)
        elif action == "playpause":
            # 正在播放→渐弱暂停;已暂停→播放渐强。后台跑,不阻塞。
            if STATE.get("bgm_playing"):
                asyncio.create_task(_fade_out_pause())
            else:
                asyncio.create_task(_play_fade_in())
    elif cmd == "reset_scene":
        reset_mute_state()   # 归位:记录重置为全不静音(需你先把 4 条 M 都关掉)
    elif cmd == "studio_toggle":
        if STATE.get("studio_visible", True):
            if studio_win.hide():
                STATE["studio_visible"] = False
        else:
            if studio_win.show():
                STATE["studio_visible"] = True
    elif cmd == "bgm_vol":
        v = int(data.get("value"))
        if set_qq_volume(v):
            STATE["bgm_vol"] = v
    elif cmd == "ping":
        # 刷新一下音量读数(QQ音乐 可能刚开)
        v = get_qq_volume()
        if v is not None:
            STATE["bgm_vol"] = v
    await _broadcast()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    phone_ip = ws.client.host if ws.client else None   # 手机局域网 IP,供 scrcpy 匹配
    # 首次连接时读一次 QQ音乐 音量(pycaw 在本线程,已无 winrt 冲突,安全)
    if STATE.get("bgm_vol") is None:
        v = get_qq_volume()
        if v is not None:
            STATE["bgm_vol"] = v
    await ws.send_text(json.dumps({"type": "state", **STATE}))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                await _handle_cmd(json.loads(raw), phone_ip)
            except Exception as e:
                print(f"[WS] 处理指令出错: {e}  raw={raw!r}")
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


# App 进悬浮前的闸门:问 PC 现在能否经无线 adb 连上这台手机(投屏前提)。
# 用请求对端 IP 作为手机 IP,避免手机端硬编码。必须在 StaticFiles 挂载前注册。
@app.get("/scrcpy/check")
async def scrcpy_check(request: Request):
    ip = request.client.host if request.client else None
    ok, msg = await asyncio.to_thread(scrcpy_win.can_reach, ip)
    return {"reachable": ok, "phone_ip": ip, "msg": msg}


# 测试网页(static/index.html)挂在根路径。ws / scrcpy 路由已先注册,不受影响。
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


# ══════════════════════════════════════════════════════════
#  启动 + 托盘
# ══════════════════════════════════════════════════════════
def get_lan_ip():
    """优先返回家用内网地址(192.168 > 10 > 172.16-31),避免被 VPN/虚拟网卡抢占。"""
    def score(ip):
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            try:
                if 16 <= int(ip.split(".")[1]) <= 31:
                    return 2
            except Exception:
                pass
        return 9

    candidates = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                candidates.append(ip)
    except Exception:
        pass
    private = sorted([c for c in candidates if score(c) < 9], key=score)
    if private:
        return private[0]

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def run_server():
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


def run_tray(url):
    import pystray
    from PIL import Image, ImageDraw

    ico = os.path.join(BASE_DIR, "ico.ico")
    if os.path.exists(ico):
        image = Image.open(ico)
    else:
        image = Image.new("RGB", (16, 16), "black")
        d = ImageDraw.Draw(image)
        d.rectangle((2, 2, 14, 14), fill="red")
        d.rectangle((4, 4, 12, 12), fill="white")

    def do_quit(icon, item):
        icon.stop()
        if _smtc_proc is not None:
            try:
                _smtc_proc.terminate()
            except Exception:
                pass
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem(f"遥控地址: {url}", None, enabled=False),
        pystray.MenuItem(
            f"Studio One MIDI: {'已连接' if STATE['studio_connected'] else '未连接'}",
            None, enabled=False),
        pystray.MenuItem("退出", do_quit),
    )
    pystray.Icon("live_remote", image, "直播遥控 · 后台服务", menu).run()


def _port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((config.HOST, port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def main():
    if _port_in_use(config.PORT):
        try:
            ctypes.windll.user32.MessageBoxW(
                0,
                f"端口 {config.PORT} 已被占用——很可能已经有一个『直播遥控』"
                "在托盘里运行了。\n\n请先在右下角托盘图标上右键→退出,再启动这一个。",
                "直播遥控", 0x40)
        except Exception:
            pass
        return

    STATE["studio_connected"] = open_midi()
    # 说明:Studio One 的 Mackie 不回传静音状态,故不用回读,改为"纯记录 + 归位"。
    # 注意:不要在主线程调 get_qq_volume()——pycaw 的 COM 会和主线程托盘冲突导致崩溃。
    # bgm_vol 由后台轮询线程(_bgm_poller)首次读取,见下。

    start_smtc_reader()   # winrt 子进程(歌名/进度),与主进程 COM 隔离

    ip = get_lan_ip()
    url = f"http://{ip}:{config.PORT}"
    print("=" * 50)
    print("  直播遥控 · 电脑后台服务已启动")
    print(f"  手机 App / 网页填:   {ip}:{config.PORT}")
    print(f"  Studio One MIDI:     {'已连接' if STATE['studio_connected'] else '未连接(检查 loopMIDI)'}")
    print("  已在右下角托盘常驻。看完地址可直接关掉本窗口,")
    print("  服务不受影响;要停服务请右键托盘图标→退出。")
    print("=" * 50)

    # 脱离控制台:关掉黑窗口不会杀掉服务(托盘继续挂着,只能从托盘退出)。
    # banner 已打印可见;之后的输出转入 server.log。
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        _logf = open(os.path.join(BASE_DIR, "server.log"), "a", encoding="utf-8")
        sys.stdout = _logf
        sys.stderr = _logf
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass

    # 服务跑在后台线程,托盘跑主线程。winrt 已移到子进程,pystray+pycaw 同为 STA,安全。
    threading.Thread(target=run_server, daemon=True).start()
    run_tray(url)


if __name__ == "__main__":
    main()
