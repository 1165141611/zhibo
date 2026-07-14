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
import time
import socket
import ctypes
import asyncio
import threading
import subprocess
import warnings
import concurrent.futures

warnings.filterwarnings("ignore")  # 屏蔽 pycaw 对未接入设备的无害 COMError 警告

import comtypes
from comtypes import CLSCTX_ALL, cast, POINTER
import psutil
from pycaw.pycaw import (AudioUtilities, ISimpleAudioVolume,
                         IAudioSessionManager2, IAudioSessionControl2)

# ── 关键防崩:全局禁用 comtypes 的 GC 自动 Release ──────────────────────────
# 本进程 comtypes 只用于 pycaw(WASAPI 会话音量)。实测(server.log + Windows 事件日志):
# QQ音乐 暂停/切歌/会话变化后,pycaw 拿到的 COM 指针会悬空,GC 触发 comtypes
# `_compointer_base.__del__ → Release()` 就是对已释放内存读写:
#   - 有时被 ctypes SEH 兜住 → server.log 里 "Exception ignored ... access violation
#     writing ..."(但"写"类 AV 落在可写页上会**静默改坏堆**,之后在随机位置崩——
#     实录 ucrtbase c0000409 failfast);
#   - 有时兜不住 → 进程直接闪退(实录 _ctypes.pyd+0x8535 c0000005,7/13-7/14 四次同签名;
#     开曲库管理窗(大量建 tk 控件触发分代 GC,__del__ 就在 tk 线程执行)或点歌开唱
#     (BGM 联动渐变频繁解析/作废会话)时最易命中)。
# 旧"指针坟场"(_qq_graveyard)只保住了缓存的 ISimpleAudioVolume;每次解析会话还会产生
# 几十个临时包装(设备/SessionManager/枚举器/SessionControl),这里一并兜底:把 __del__
# 换成 no-op,**所有 COM 包装都不再由 GC Release**(故意泄漏,单个几十字节,常驻服务
# 无所谓——稳定性优先,与坟场同一哲学)。
try:
    from comtypes._post_coinit.unknwn import _compointer_base as _ct_ptr_base
    _ct_ptr_base.__del__ = lambda self: None
    print("[COM] 已禁用 comtypes GC Release(防悬空 COM 指针闪退)")
except Exception as _e:      # comtypes 版本升级找不到该私有类时不拦启动,仅提示
    print(f"[COM] 禁用 comtypes GC Release 失败(继续运行,注意崩溃风险): {_e}")

import winmm_midi
import studio_win
import karaoke_win
import library
import karaoke_data

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

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
    "player_visible": False,   # K歌歌词窗口是否显示(初始隐藏,由服务器托管)
    "pitch_visible": True,     # 音准线是否显示(手机遥控 + 跨重启缓存)
    "lib_count": 0,            # 曲库已入库首数
    "watcher_running": False,  # 曲库监听线程是否在跑
    # ── K歌播放器状态(来自播放器 stdout 的 STATE 上报)──
    "k_playing": False, "k_pos": 0, "k_dur": 0, "k_key": 0, "k_vocal": False,
    "k_vol": 100,              # 伴奏音量 0-100(手机音量键同步)
    "k_font": 0,               # 歌词字体索引(播放器 Q 键循环,跨重启缓存)
    "setlist": [],             # 歌单 mid 列表(曲库管理页勾选,跨重启缓存;推给播放器顶端滚动字幕)
    "setlist_visible": True,   # 顶端歌单显隐(播放器 O 键,跨重启缓存)
    "setlist_y": 24,           # 顶端歌单竖直位置(播放器 Ctrl+↑↓,跨重启缓存)
    "k_mid": "", "k_title": "", "k_artist": "",
    "now": None,               # 正在唱 {mid,title,artist} 或 None(空闲)
    "queue": [],               # 等待队列 [{mid,title,artist}...]
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

# ── 跨重启持久缓存:声卡场景 + 静音记录 + 演唱(伴奏)音量 ──────────────
# Studio One 和播放器在服务重启期间状态不变,所以把"场景/静音记录"原样存盘、启动时恢复记录
# (**不发 MIDI**,记录=现实即可继续准确切换);k_vol 恢复后在拉起播放器时下发一次。
PERSIST_PATH = os.path.join(BASE_DIR, "state_cache.json")
_persist_lock = threading.Lock()


def _save_persist():
    """把需继承的状态写盘(整写小 JSON,先写临时文件再原子替换;失败只打日志不影响运行)。"""
    try:
        with _persist_lock:
            data = {
                "scene": STATE.get("scene"),
                "mute_state": {str(k): bool(v) for k, v in _mute_state.items()},
                "k_vol": int(STATE.get("k_vol", 100)),
                "studio_visible": bool(STATE.get("studio_visible", True)),
                "pitch_visible": bool(STATE.get("pitch_visible", True)),
                "k_font": int(STATE.get("k_font", 0)),
                "setlist": list(STATE.get("setlist", [])),
                "setlist_visible": bool(STATE.get("setlist_visible", True)),
                "setlist_y": int(STATE.get("setlist_y", 24)),
            }
            tmp = PERSIST_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, PERSIST_PATH)
    except Exception as e:
        print(f"[PERSIST] 写入失败: {e}")


def _restore_persist():
    """启动时恢复上次的场景/静音记录/演唱音量(文件缺失或损坏则保持默认)。"""
    try:
        with open(PERSIST_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    ms = data.get("mute_state") or {}
    for note in list(_mute_state.keys()):
        if str(note) in ms:
            _mute_state[note] = bool(ms[str(note)])
    if data.get("scene") is not None:
        try:
            STATE["scene"] = int(data["scene"])
        except Exception:
            pass
    if data.get("k_vol") is not None:
        try:
            STATE["k_vol"] = max(0, min(100, int(data["k_vol"])))
        except Exception:
            pass
    if data.get("studio_visible") is not None:
        sv = bool(data["studio_visible"])
        STATE["studio_visible"] = sv
        if not sv:               # 上次是隐藏 → 重新隐藏,保持一致(可见则不动,避免抢焦点)
            try:
                studio_win.hide()
            except Exception:
                pass
    if data.get("pitch_visible") is not None:
        STATE["pitch_visible"] = bool(data["pitch_visible"])   # 播放器拉起后统一下发,见 start_player
    if data.get("k_font") is not None:
        try:
            STATE["k_font"] = int(data["k_font"])              # 播放器拉起后统一下发
        except Exception:
            pass
    if isinstance(data.get("setlist"), list):
        STATE["setlist"] = [str(m) for m in data["setlist"]]
    if data.get("setlist_visible") is not None:
        STATE["setlist_visible"] = bool(data["setlist_visible"])
    if data.get("setlist_y") is not None:
        try:
            STATE["setlist_y"] = int(data["setlist_y"])
        except Exception:
            pass
    print(f"[PERSIST] 已恢复: scene={STATE['scene']} k_vol={STATE['k_vol']} "
          f"studio_visible={STATE['studio_visible']} pitch_visible={STATE['pitch_visible']} "
          f"mute={_mute_state}")


def reset_mute_state():
    """归位:把记录重置为"全不静音"。配合"把 4 条 M 都关掉"使用,恢复同步。"""
    for note in config.VOCAL_MUTE_NOTES.values():
        _mute_state[note] = False
    STATE["scene"] = None
    _save_persist()


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
    """pycaw 走 COM,每个线程首次调用前需 CoInitialize(专用线程用 MTA,见 _pycaw_thread_init)。"""
    if not getattr(_tls, "inited", False):
        try:
            comtypes.CoInitialize()
        except Exception:
            pass
        _tls.inited = True


# ── 专用 pycaw/COM 工作线程(关键:防段错误) ──────────────────
# 所有 pycaw(Windows Core Audio COM)操作都丢到这**一条**线程串行执行,且该线程用
# **MTA(多线程套间)**初始化。原来 pycaw 直接跑在 uvicorn 异步线程(STA、无消息泵),
# 一旦 QQ音乐 有活跃会话、演唱↔BGM 联动触发音量渐变(连发 20 次 SetMasterVolume),
# 就会因套间/消息泵问题原生崩溃(用户点歌开唱瞬间服务端闪退即此)。丢到独立 MTA 线程后
# COM 调用无需泵消息、无跨套间、天然串行,彻底规避。
def _pycaw_thread_init():
    try:
        comtypes.CoInitializeEx(getattr(comtypes, "COINIT_MULTITHREADED", 0x0))
    except Exception:
        pass   # 已被初始化过(RPC_E_CHANGED_MODE)则沿用现状
    _tls.inited = True


_pycaw_exec = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="pycaw", initializer=_pycaw_thread_init)


def _pycaw_call(fn, *args):
    """把一个 pycaw 操作调度到专用 MTA 线程执行并等结果。异常/超时返回 None,绝不让 COM 崩主进程。"""
    try:
        return _pycaw_exec.submit(fn, *args).result(timeout=8)
    except Exception as e:
        print(f"[VOL] pycaw 调用异常: {e}")
        return None


# 这台机器是 ROUTIST R2 声卡,QQ音乐 会同时出现在多个虚拟路由(设备)上。
# 所以要跨"所有活跃设备"找出全部 QQ音乐 会话,音量一起调,谁喂给监听/直播都同步。
_qq_vols = []  # 缓存:所有匹配到的 ISimpleAudioVolume
# 指针坟场:被丢弃的会话指针**永久持有引用、绝不释放**。QQ暂停/切歌后其 COM 指针悬空,
# 若任由 GC 触发 comtypes __del__→Release(),就是对已释放内存写 → access violation 连环崩
# (2026-07-13 实崩:server.log 连环 "access violation writing ..." 后进程死)。
# 每个指针就几十字节,常驻服务漏这点无所谓,稳定性优先。
_qq_graveyard = []


def _proc_name(pid):
    try:
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def _qq_running():
    """QQ音乐相关进程是否存在。不存在就别去枚举 35 个音频设备(白折腾 COM,还添崩溃面)。"""
    targets = [p.lower() for p in config.QQMUSIC_PROCS]
    try:
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() in targets:
                return True
    except Exception:
        pass
    return False


def _resolve_qq_sessions():
    """跨所有活跃设备,收集所有 QQ音乐 相关会话的音量接口。"""
    if not _qq_running():
        return []
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


_QQ_RESOLVE_INTERVAL = 1.5   # 缓存为空时最短重解析间隔(秒)。防高频枚举 35 个设备把 COM 打崩
_qq_resolve_at = 0.0         # 上次解析时刻(monotonic)


def _qq_vols_cached():
    """返回缓存的 QQ音乐 音量会话。为空且距上次解析≥间隔才重解析(限频,防枚举风暴)。"""
    global _qq_vols, _qq_resolve_at
    if not _qq_vols:
        now = time.monotonic()
        if now - _qq_resolve_at >= _QQ_RESOLVE_INTERVAL:
            _qq_resolve_at = now
            _qq_vols = _resolve_qq_sessions()
    return _qq_vols


def _clear_qq_cache(allow_reresolve=True):
    """丢弃缓存的会话指针(QQ暂停/切歌后其 COM 指针会悬空,继续调用会段错误)。
    丢弃 = 移进坟场永久持有,**绝不能让 GC Release 悬空指针**(见 _qq_graveyard 注释)。
    allow_reresolve=True 时顺带清空限频计时,允许下次立即重解析(暂停↔播放切换需要)。"""
    global _qq_vols, _qq_resolve_at
    _qq_graveyard.extend(_qq_vols)
    _qq_vols = []
    if allow_reresolve:
        _qq_resolve_at = 0.0


def _set_qq_volume_impl(pct):
    """设 QQ音乐 音量。**只用缓存会话、单次尝试**;失败即清缓存(交给下次限频重解析),
    绝不在一次调用里反复枚举设备——高频枚举正是段错误根源。"""
    pct = max(0, min(100, pct))
    ok = False
    for v in list(_qq_vols_cached()):
        try:
            v.SetMasterVolume(pct / 100.0, None)
            ok = True
        except Exception:
            _clear_qq_cache(allow_reresolve=False)   # 指针失效 → 清缓存,下次限频再解析
            break
    return ok


def _get_qq_volume_impl():
    for v in list(_qq_vols_cached()):
        try:
            return int(round(v.GetMasterVolume() * 100))
        except Exception:
            _clear_qq_cache(allow_reresolve=False)
            break
    return None


def set_qq_volume(pct):
    """公开接口:调度到专用 pycaw 线程执行(防 COM 段错误)。返回是否成功。"""
    return bool(_pycaw_call(_set_qq_volume_impl, pct))


def get_qq_volume():
    """公开接口:调度到专用 pycaw 线程执行。返回 0-100 或 None。"""
    return _pycaw_call(_get_qq_volume_impl)


# ── 非阻塞音量:coalesced(合并),绝不在事件循环里 .result() 等 pycaw ──────────
# 老写法在 _handle_cmd 里 set_qq_volume(v) 会 .result(timeout=8) 阻塞事件循环;若此刻
# 渐变正占着单线程 pycaw 执行器,整个循环卡死→手机按钮全失灵、状态不再广播。
# 现改为:STATE 乐观更新(UI 立即跟手)+ 把"最新目标值"丢到 pycaw 线程,连拖只应用最后一档。
_qq_vol_lock = threading.Lock()
_qq_vol_target = None      # 待应用的最新音量(0-100);None=无待办
_qq_vol_running = False    # pump 是否已在 pycaw 线程上跑


def _qq_vol_pump():
    """在 pycaw 线程上把待办音量应用完(期间又来新值就应用最新的),排空后退出。"""
    global _qq_vol_target, _qq_vol_running
    while True:
        with _qq_vol_lock:
            tgt = _qq_vol_target
            _qq_vol_target = None
            if tgt is None:
                _qq_vol_running = False
                return
        try:
            _set_qq_volume_impl(tgt)
        except Exception:
            pass


def schedule_qq_volume(v):
    """把设音量丢到 pycaw 线程(合并高频拖动),不阻塞事件循环。"""
    global _qq_vol_target, _qq_vol_running
    v = max(0, min(100, int(v)))
    with _qq_vol_lock:
        _qq_vol_target = v
        if _qq_vol_running:
            return          # pump 还在跑,它会读走最新 target
        _qq_vol_running = True
    try:
        _pycaw_exec.submit(_qq_vol_pump)
    except Exception:
        with _qq_vol_lock:
            _qq_vol_running = False


def schedule_qq_volume_read():
    """异步读一次 QQ音乐 音量,读到就更新 STATE 并推手机。不阻塞事件循环。"""
    def work():
        v = _get_qq_volume_impl()
        if v is not None and STATE.get("bgm_vol") != v:
            STATE["bgm_vol"] = v
            _threadsafe_broadcast()
    try:
        _pycaw_exec.submit(work)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
#  4) WebSocket 服务
# ══════════════════════════════════════════════════════════
app = FastAPI()
_clients = set()
_loop = None            # uvicorn 的事件循环(供子进程读取线程跨线程广播)
_smtc_proc = None       # winrt 子进程
_player_proc = None     # K歌播放器子进程
_player_lock = threading.Lock()   # 串行化对播放器 stdin 的写(读取线程自动下一首 vs 事件循环手动切歌)
# 专用单线程 IO 执行器:所有发往播放器 stdin 的写都丢到这里 FIFO 有序执行,
# 让事件循环**永不**因阻塞的管道写而卡住(手机连点时尤其关键)。
_player_io_exec = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="player-io")
_tray_icon = None       # pystray 托盘图标(供刷新)
_tray_thread_id = None  # 托盘消息泵所在线程 id;update_menu 只能在该线程调(跨线程改 Win32 菜单会崩)
_tray_hwnd = None       # 托盘消息窗 HWND(PostMessage 跨线程唤醒托盘线程刷新用)
_last_import_mid = None  # 最近一首入库的 mid(=当前气泡通知对应的歌;点气泡→改它)
# 自定义窗口消息:WM_USER(0x400) pystray 自用 +10(STOP)/+11(NOTIFY);我们用 +20 触发刷新
WM_TRAY_REFRESH = 0x400 + 20
NIN_BALLOONUSERCLICK = 0x0405   # 用户点了气泡通知(经托盘回调消息 lParam 送达)
_queue = []             # 点歌等待队列(mid 列表)
_now_mid = None         # 正在唱的 mid(None=空闲)


@app.on_event("startup")
async def _capture_loop():
    global _loop
    _loop = asyncio.get_running_loop()


def start_smtc_reader():
    """启动 winrt 子进程,后台线程读它的 stdout(歌名/进度),更新状态并推给手机。"""
    def worker():
        global _smtc_proc
        helper = os.path.join(BASE_DIR, "smtc_helper.py")
        _env = dict(os.environ, PYTHONIOENCODING="utf-8")   # 子进程 std 流强制 UTF-8(BGM 中文歌名)
        while True:
            try:
                _smtc_proc = subprocess.Popen(
                    [sys.executable, helper],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                    text=True, encoding="utf-8", errors="replace", env=_env,
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


# 关键:整段渐变**都在专用 pycaw 线程一次跑完**(解析一次会话、用缓存指针逐步调、线程内 sleep),
# 而不是从异步线程连发几十次 set_qq_volume(每次可能重枚举设备)——后者正是"点歌开唱瞬间服务端闪退"的根源。
def _fade_out_pause_impl():
    full = _get_qq_volume_impl() or STATE.get("bgm_vol") or 60
    steps = 20
    dt = config.FADE_SECONDS / steps if config.FADE_SECONDS > 0 else 0
    _qq_vols_cached()                       # 解析一次(填缓存),后续步骤复用,不再枚举
    for i in range(steps - 1, -1, -1):
        _set_qq_volume_impl(round(full * i / steps))
        if dt:
            time.sleep(dt)
    _tap_media(VK_MEDIA_PLAY_PAUSE)         # 暂停
    _clear_qq_cache()                       # 暂停后会话指针悬空 → 丢弃,严禁再用(会崩)
    return full


def _play_fade_in_impl():
    full = STATE.get("bgm_vol") or _get_qq_volume_impl() or 60
    _clear_qq_cache()
    _tap_media(VK_MEDIA_PLAY_PAUSE)         # 播放(QQ 起新会话)
    time.sleep(0.2)
    _clear_qq_cache()                       # 用播放后的新会话
    steps = 20
    dt = config.FADE_SECONDS / steps if config.FADE_SECONDS > 0 else 0
    _qq_vols_cached()
    for i in range(1, steps + 1):
        _set_qq_volume_impl(round(full * i / steps))
        if dt:
            time.sleep(dt)
    return full


async def _run_pycaw(fn):
    """把整段(阻塞的)pycaw 渐变丢到专用线程执行,事件循环不阻塞。"""
    return await asyncio.get_running_loop().run_in_executor(_pycaw_exec, fn)


async def _fade_out_pause():
    """渐弱 → 暂停 QQ音乐(整段在 pycaw 线程执行,防 COM 段错误)。"""
    global _fading
    if _fading:
        return
    _fading = True
    try:
        full = await _run_pycaw(_fade_out_pause_impl)
        STATE["bgm_vol"] = full or STATE.get("bgm_vol") or 60
        STATE["bgm_playing"] = False
    finally:
        _fading = False
    await _broadcast()


async def _play_fade_in():
    """播放 QQ音乐 → 渐强(整段在 pycaw 线程执行)。"""
    global _fading
    if _fading:
        return
    _fading = True
    try:
        full = await _run_pycaw(_play_fade_in_impl)
        STATE["bgm_vol"] = full or STATE.get("bgm_vol") or 60
        STATE["bgm_playing"] = True
    finally:
        _fading = False
    await _broadcast()


async def _handle_cmd(data):
    cmd = data.get("cmd")
    if cmd == "scene":
        sid = int(data.get("id"))
        if send_scene(sid):
            STATE["scene"] = sid
            _save_persist()
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
                _save_persist()      # 显隐状态跨重启持久(同场景/音量)
        else:
            if studio_win.show():
                STATE["studio_visible"] = True
                _save_persist()
    elif cmd == "player_toggle":
        toggle_player()
    elif cmd == "pitch_toggle":
        v = not STATE.get("pitch_visible", True)
        STATE["pitch_visible"] = v
        _player_send("pitch " + ("1" if v else "0"))
        _save_persist()          # 音准线显隐跨重启持久(同场景/音量/Studio显隐)
    elif cmd == "setlist_toggle":
        v = not STATE.get("setlist_visible", True)
        STATE["setlist_visible"] = v
        _player_send("setlist_show " + ("1" if v else "0"))
        _save_persist()          # 顶端歌单显隐跨重启持久(同 O 键回读那套)
    # ── K歌:点歌队列 + 播放控制 ──
    elif cmd == "kqueue_add":
        k_enqueue(data.get("mid"))
    elif cmd == "kqueue_remove":
        k_remove(int(data.get("idx", -1)))
    elif cmd == "kqueue_next":
        k_play_next()
    elif cmd == "kqueue_clear":
        k_clear()
    elif cmd == "kqueue_move":
        k_move(int(data.get("from", -1)), int(data.get("to", -1)))
    elif cmd == "kplay":
        _player_send("play")
    elif cmd == "kpause":
        _player_send("pause")
    elif cmd == "kplaypause":
        _player_send("playpause")
    elif cmd == "kkey":
        _player_send("key " + str(int(data.get("semi", 0))))
    elif cmd == "kvocal":
        _player_send("vocal " + ("1" if data.get("on") else "0"))
    elif cmd == "kvol":
        v = max(0, min(100, int(data.get("value", 100))))
        STATE["k_vol"] = v            # 乐观更新,播放器 STATE 上报会再校正
        _save_persist()
        _player_send("vol " + str(v))
    elif cmd == "kseek":
        _player_send("seek " + str(int(data.get("ms", 0))))
    elif cmd == "kshow":
        _player_send("show")
    elif cmd == "khide":
        _player_send("hide")
    elif cmd == "bgm_vol":
        v = max(0, min(100, int(data.get("value"))))
        STATE["bgm_vol"] = v          # 乐观更新:滑条立即跟手
        schedule_qq_volume(v)         # 实际设音量丢到 pycaw 线程,合并高频拖动,不阻塞循环
    elif cmd == "ping":
        # 异步刷新一下音量读数(QQ音乐 可能刚开),不阻塞事件循环
        schedule_qq_volume_read()
    await _broadcast()


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    # 首次连接时异步读一次 QQ音乐 音量(不阻塞握手;读到后会自动再推一帧 state)
    if STATE.get("bgm_vol") is None:
        schedule_qq_volume_read()
    await ws.send_text(json.dumps({"type": "state", **STATE}))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                await _handle_cmd(json.loads(raw))
            except Exception as e:
                print(f"[WS] 处理指令出错: {e}  raw={raw!r}")
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


# 曲库列表(手机点歌用):返回 [{mid,title,artist}]。必须在 StaticFiles 挂载前注册。
@app.get("/library")
async def get_library():
    man = library.manifest()
    songs = [{"mid": m, "title": v.get("title", ""), "artist": v.get("artist", "")}
             for m, v in man.items()]
    songs.sort(key=lambda s: (s["title"] or ""))
    return {"count": len(songs), "songs": songs}


# 卡拉OK数据(手机演唱页用):某首歌的 QRC 逐字时间轴 + .note 音高线(归一化)。
@app.get("/song/{mid}/karaoke")
async def get_song_karaoke(mid: str):
    data = karaoke_data.song_karaoke(mid)
    if data is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return data


# 测试网页(static/index.html)挂在根路径。ws / library 路由已先注册,不受影响。
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


# ── K歌播放器子进程 + 托盘刷新 ────────────────────────
def _player_reader(proc):
    """读播放器 stdout:VIS:0/1(可见性)、STATE {json}(进度/播放/调/原唱)。
    更新 STATE + 刷托盘 + 推手机;并做"唱完自动下一首"。"""
    prev_playing = False
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("VIS:"):
                STATE["player_visible"] = (line[4:] == "1")
                refresh_tray()
                _threadsafe_broadcast()
            elif line.startswith("STATE "):
                try:
                    st = json.loads(line[6:])
                except Exception:
                    continue
                vol_before = STATE.get("k_vol")
                pitch_before = STATE.get("pitch_visible")
                font_before = STATE.get("k_font")
                slv_before = STATE.get("setlist_visible")
                sly_before = STATE.get("setlist_y")
                STATE.update({
                    "k_pos": st["pos"], "k_dur": st["dur"], "k_playing": st["playing"],
                    "k_key": st["key"], "k_vocal": st["vocal"], "k_mid": st["mid"],
                    "k_vol": st.get("vol", STATE.get("k_vol", 100)),
                    # 回读音准线/字体/歌单显隐+位置:让播放器 P/Q/O/Ctrl+↑↓ 也同步缓存(同 key/vocal)
                    "pitch_visible": st.get("pitch", STATE.get("pitch_visible", True)),
                    "k_font": st.get("font", STATE.get("k_font", 0)),
                    "setlist_visible": st.get("setlist_show", STATE.get("setlist_visible", True)),
                    "setlist_y": st.get("setlist_y", STATE.get("setlist_y", 24)),
                    "k_title": st["title"], "k_artist": st["artist"],
                })
                if (STATE.get("k_vol") != vol_before                # 有变才写盘(上报每 500ms 一次)
                        or STATE.get("pitch_visible") != pitch_before
                        or STATE.get("k_font") != font_before
                        or STATE.get("setlist_visible") != slv_before
                        or STATE.get("setlist_y") != sly_before):
                    _save_persist()
                # 结束检测:曾在播、现在停、且已到尾 → 切到下一首**开头并暂停**(不自动开唱)。
                # 只在"结束的正是当前曲"时才自动切,避免与手动切歌(事件循环线程)撞车导致跳一首。
                ended = (prev_playing and not st["playing"]
                         and st["dur"] > 0 and st["pos"] >= st["dur"] - 800)
                prev_playing = st["playing"]
                if ended and _now_mid is not None and st.get("mid") == _now_mid:
                    k_advance_paused()
                _threadsafe_broadcast()
    except Exception:
        pass


def start_player():
    """拉起 K歌播放器子进程:隐藏、暂停、关 SMTC、指定声卡。stdin 收指令 / stdout 报可见性。
    不自动重启(面向用户,可能主动关)。"""
    global _player_proc
    try:
        # PYTHONIOENCODING=utf-8:强制子进程 std 流用 UTF-8(否则 Windows 默认 GBK,STATE/VIS 中文歌名
        # 会让下面 encoding="utf-8" 的读取在第一行就 UnicodeDecodeError 崩掉读取线程 → 管道写满 → 播放器
        # GUI 卡死无响应)。errors="replace":父进程侧再加一层兜底,任何杂字节也绝不崩读取循环。
        _env = dict(os.environ, PYTHONIOENCODING="utf-8")
        _player_proc = subprocess.Popen(
            [config.PLAYER_PYTHON, config.PLAYER_PATH,
             "--device", str(config.PLAYER_DEVICE),
             "--hidden", "--paused", "--no-smtc"],
            creationflags=0x08000000,        # CREATE_NO_WINDOW
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=_env,
        )
        STATE["player_visible"] = False
        threading.Thread(target=_player_reader, args=(_player_proc,), daemon=True).start()
        # 把持久缓存里的演唱音量、音准线显隐、歌词字体、顶端歌单下发过去(重启继承播放器默认值)
        _player_send("vol " + str(int(STATE.get("k_vol", 100))))
        _player_send("pitch " + ("1" if STATE.get("pitch_visible", True) else "0"))
        _player_send("font " + str(int(STATE.get("k_font", 0))))
        _player_send("setlist_show " + ("1" if STATE.get("setlist_visible", True) else "0"))
        _player_send("setlist_y " + str(int(STATE.get("setlist_y", 24))))
        _push_setlist()          # 歌单内容(据缓存的 mid 列表 → 歌名)
        print("[PLAYER] 已拉起 K歌播放器(隐藏)")
    except Exception as e:
        print(f"[PLAYER] 启动失败: {e}")


def toggle_player():
    """显隐 K歌播放器窗口。发显式 show/hide 给播放器(它用 Qt 自己显隐才会正确重绘),
    并**立即在当前(托盘主)线程**更新 STATE + 刷托盘,不等异步 VIS 往返(避免勾选卡住)。
    VIS 上报仍保留,用于 ESC/关窗等外部隐藏的兜底同步。进程死则懒重建。"""
    global _player_proc
    if _player_proc is None or _player_proc.poll() is not None:
        start_player()               # 进程死了/没拉起 → 重新拉起(隐藏启动)
        STATE["player_visible"] = False
        refresh_tray()
        return
    want = not STATE.get("player_visible")
    try:
        _player_proc.stdin.write(("show" if want else "hide") + "\n")
        _player_proc.stdin.flush()
        STATE["player_visible"] = want
        refresh_tray()
        if _clients and _loop is not None:
            asyncio.run_coroutine_threadsafe(_broadcast(), _loop)
    except Exception as e:
        print(f"[PLAYER] 发送指令失败: {e}")


def refresh_tray():
    """STATE 变化后刷新托盘菜单(曲库数/监听/勾选)。
    **update_menu 只能在托盘线程调**——跨线程改 Win32 菜单会段错误(切歌时播放器上报 VIS
    从读取线程刷托盘导致主程序闪退,即此坑)。且实测 pystray-win32 右键弹菜单用的是**缓存的
    菜单句柄、不会在打开时重新求值动态 lambda**——所以不主动 update_menu 的话,曲库数/勾选
    变化后**永远不刷新**(旧代码"下次打开自动刷新"的假设是错的)。故:托盘线程直接调;其它
    线程 PostMessage 唤醒托盘线程去调(WM_TRAY_REFRESH 由 _dispatcher 在托盘线程分发)。"""
    if _tray_icon is None:
        return
    if _tray_thread_id is None or threading.get_ident() == _tray_thread_id:
        try:
            _tray_icon.update_menu()
        except Exception:
            pass
        return
    if _tray_hwnd:                    # 非托盘线程:marshal 回托盘线程
        try:
            ctypes.windll.user32.PostMessageW(_tray_hwnd, WM_TRAY_REFRESH, 0, 0)
        except Exception:
            pass


def _threadsafe_broadcast():
    """从任意线程把 STATE 推给手机(WS)。"""
    if _clients and _loop is not None:
        asyncio.run_coroutine_threadsafe(_broadcast(), _loop)


def _on_lib_change():
    """曲库变化:刷托盘 + 推手机 + 重推歌单(歌名可能被迁移修正,或新歌入库)。"""
    refresh_tray()
    _threadsafe_broadcast()
    _push_setlist()


def _on_lib_import(mid, meta, count):
    """单曲入库成功 → 系统通知(托盘气泡):什么歌导入成功、现在库存几首。**每首都记 _last_import_mid**
    (=当前气泡对应的歌),点气泡即弹它的改名框——不管歌名准不准都能点改。needs_name(如成都存成
    数字ID)额外提示"点此改名"。pystray 的 notify 底层是 Shell_NotifyIcon(NIF_INFO),无 update_menu
    的线程亲和限制,可从 library 监听线程直接调;托盘没起(--headless)就跳过。"""
    global _last_import_mid
    if _tray_icon is None:
        return
    _last_import_mid = mid       # 每次入库都更新:点气泡永远编辑"这条通知的歌",不再串到上一首
    title = (meta.get("title") or "").strip() or mid
    artist = (meta.get("artist") or "").strip()
    name = f"{title} - {artist}" if artist else title
    if meta.get("needs_name"):
        msg = f"《{name}》已入库(歌名可能不准)。点此通知修改歌名/歌手。曲库 {count} 首"
    else:
        msg = f"《{name}》已入库,曲库现有 {count} 首。点此可改名"
    try:
        _tray_icon.notify(msg, "K歌曲库 · 导入成功")
    except Exception as e:
        print(f"[LIB] 入库通知失败: {e}")


def _build_edit_form(container, mid, on_saved):
    """在 container(Tk 或 Toplevel)里搭"歌名/歌手"两栏编辑 + 保存/取消。保存→library.rename
    →on_saved()→销毁 container。供"点通知改名"和"曲库管理里双击改"共用。"""
    import tkinter as tk
    meta = library.song_meta(mid) or {}
    tk.Label(container, text="歌名:").grid(row=0, column=0, padx=10, pady=(12, 6), sticky="e")
    e_t = tk.Entry(container, width=32)
    e_t.grid(row=0, column=1, padx=10, pady=(12, 6))
    e_t.insert(0, (meta.get("title") or "").strip())
    tk.Label(container, text="歌手:").grid(row=1, column=0, padx=10, pady=6, sticky="e")
    e_a = tk.Entry(container, width=32)
    e_a.grid(row=1, column=1, padx=10, pady=6)
    e_a.insert(0, (meta.get("artist") or "").strip())

    def _ok(*_):
        t, a = e_t.get().strip(), e_a.get().strip()
        if t:
            library.rename(mid, t, a)      # 更新曲库 + 刷托盘 + 推手机
            if on_saved:
                on_saved()
        container.destroy()

    tk.Button(container, text="取消", width=8, command=container.destroy).grid(
        row=2, column=0, padx=10, pady=(6, 12))
    tk.Button(container, text="保存", width=10, command=_ok).grid(
        row=2, column=1, padx=10, pady=(6, 12), sticky="e")
    container.bind("<Return>", _ok)
    container.after(100, lambda: (e_t.focus_set(),))


def _push_setlist():
    """把歌单(mid→歌名)推给播放器顶端滚动字幕。曲库勾选变化 / 播放器拉起时调。"""
    titles = []
    for m in STATE.get("setlist", []):
        t = (library.song_meta(m) or {}).get("title", "").strip()
        if t:
            titles.append(t)
    _player_send("setlist " + json.dumps(titles, ensure_ascii=False))


def set_setlist_member(mid, on):
    """曲库管理页勾选/取消一首歌进歌单:更新 STATE + 存盘 + 推播放器。"""
    sl = list(STATE.get("setlist", []))
    if on and mid not in sl:
        sl.append(mid)
    elif not on and mid in sl:
        sl.remove(mid)
    STATE["setlist"] = sl
    _save_persist()
    _push_setlist()
    _threadsafe_broadcast()


def _open_rename_dialog(mid):
    """点通知气泡 → 独立窗口改一首歌名。tkinter 在**独立线程**跑自己的 mainloop——绝不阻塞托盘
    消息泵(同 do_quit 教训:在托盘回调里开模态框会占死消息泵)。"""
    if not mid:
        return

    def _dlg():
        try:
            import tkinter as tk
            root = tk.Tk()
            root.title("修改歌名 · K歌曲库")
            root.attributes("-topmost", True)
            root.resizable(False, False)
            _build_edit_form(root, mid, on_saved=None)
            root.after(120, root.focus_force)
            root.mainloop()
        except Exception as ex:
            print(f"[LIB] 改名对话框失败: {ex}")

    threading.Thread(target=_dlg, daemon=True).start()


def _open_library_browser(selftest=False):
    """点托盘"曲库: N 首" → 曲库管理窗(高性能版):
    - 搜索框 **200ms 防抖**(老版每敲一键全量重建所有行,正是卡顿主因之一);
    - **Live 筛选**:全部 / 只看Live / 排除Live(约定:歌名含 "live" 即视为 Live 版);
    - **排序**:最新入库(默认) / 未勾选在前 / 已勾选在前(勾选=在歌单里;组内仍按入库时间倒序);
    - **触底分页渲染**:每批 60 行,滚动到底自动续批。不再一次性把几百行 tk 控件全建出来;
      内容不足一屏时 yscrollcommand 同样会触发(last=1.0),自动续批直到填满或渲完;
    - 每行:勾选=加歌单、编辑/播放按钮、斑马纹+悬停+选中态,交互与旧版一致。
    独立线程单 Tk 根;编辑用子 Toplevel(同根同线程,稳)。
    selftest=True 供 headless 自检:自动滚底驱动分页并打印进度,渲完自毁,不影响正常使用。"""

    def _win():
        try:
            import tkinter as tk
            from tkinter import ttk
            root = tk.Tk()
            root.title("K歌曲库管理")
            root.geometry("640x500")
            root.attributes("-topmost", True)

            top = tk.Frame(root)
            top.pack(fill="x", padx=10, pady=(10, 2))
            tk.Label(top, text="搜索:").pack(side="left")
            q = tk.StringVar()
            tk.Entry(top, textvariable=q).pack(side="left", fill="x", expand=True, padx=6)
            count_var = tk.StringVar()
            tk.Label(top, textvariable=count_var).pack(side="left")

            # 筛选 + 排序(选择即刷新)
            bar2 = tk.Frame(root)
            bar2.pack(fill="x", padx=10, pady=(2, 0))
            tk.Label(bar2, text="筛选:").pack(side="left")
            f_var = tk.StringVar(value="全部")
            cb_f = ttk.Combobox(bar2, textvariable=f_var, state="readonly", width=9,
                                values=("全部", "只看Live", "排除Live"))
            cb_f.pack(side="left", padx=(4, 14))
            tk.Label(bar2, text="排序:").pack(side="left")
            s_var = tk.StringVar(value="最新入库")
            cb_s = ttk.Combobox(bar2, textvariable=s_var, state="readonly", width=11,
                                values=("最新入库", "未勾选在前", "已勾选在前"))
            cb_s.pack(side="left", padx=4)

            tk.Label(root, text="☑ 勾选 = 加入歌单(播放器顶端滚动显示)", anchor="w",
                     fg="#888888").pack(fill="x", padx=12)

            # 列表区:Canvas + 内嵌 Frame + 右侧滚动条(为了每行放真按钮,不用 Treeview)
            body = tk.Frame(root)
            body.pack(fill="both", expand=True, padx=(10, 0), pady=4)
            canvas = tk.Canvas(body, highlightthickness=0)
            vsb = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
            vsb.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            inner = tk.Frame(canvas, bg="#ffffff")
            win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
            inner.bind("<Configure>",
                       lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.bind("<Configure>",
                        lambda e: canvas.itemconfig(win_id, width=e.width))
            canvas.bind_all("<MouseWheel>",         # 滚轮(本解释器独享,窗口关即失效)
                            lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

            # 行配色:斑马纹交替底色 + 悬停高亮 + 点击选中态(纯 tk 无 Treeview 选中样式,手动画)
            C_EVEN, C_ODD, C_HOVER, C_SEL = "#ffffff", "#f4f5f7", "#eaf1fb", "#c8e0f8"
            PAGE = 60                     # 触底分页:每批渲染行数
            view = {"items": [], "pos": 0, "queued": False}  # items=[(mid,meta)];pos=已渲染数
            rows = []                # 已渲染行 [{paint, mid}](重画选中/悬停态用)
            sel = {"mid": None}      # 当前选中的 mid(跨搜索/续批保留)

            def _edit(mid):
                dlg = tk.Toplevel(root)
                dlg.title("修改歌名")
                dlg.attributes("-topmost", True)
                dlg.resizable(False, False)
                dlg.grab_set()                      # 模态(同根,安全)
                _build_edit_form(dlg, mid, on_saved=refresh)

            def _play(mid):
                sel["mid"] = mid                    # 播放即选中,反馈更明确
                for r in rows:
                    r["paint"]()
                k_play_mid(mid)                     # 立即 load+play,有歌在播则切歌

            def _select(mid):
                sel["mid"] = mid
                for r in rows:
                    r["paint"]()

            def _add_row(mid, m, idx):
                """渲染一行(idx 定斑马纹底色)。行结构/交互与旧版完全一致。"""
                title = (m.get("title") or "").strip()
                artist = (m.get("artist") or "").strip()
                ts = m.get("added", 0)
                tstr = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else ""
                base = C_EVEN if idx % 2 == 0 else C_ODD
                rf = tk.Frame(inner, bg=base)
                rf.pack(fill="x")
                rf.columnconfigure(1, weight=1, minsize=160)   # 歌名列伸缩(col1)
                # col0:勾选框 = 加入歌单(默认按 STATE["setlist"];顶端滚动字幕)
                var = tk.BooleanVar(value=(mid in STATE.get("setlist", [])))
                chk = tk.Checkbutton(
                    rf, variable=var, bg=base, activebackground=base,
                    command=lambda mid=mid, var=var: set_setlist_member(mid, var.get()))
                chk.grid(row=0, column=0, padx=(6, 0))
                name_fg = "#c0392b" if m.get("needs_name") else "#111111"
                l_name = tk.Label(rf, text=title or "(待命名)", anchor="w",
                                  bg=base, fg=name_fg, padx=8, pady=7)
                l_name.grid(row=0, column=1, sticky="w")
                l_art = tk.Label(rf, text=artist, anchor="w", width=11,
                                 bg=base, fg="#333333")
                l_art.grid(row=0, column=2, sticky="w", padx=4)
                l_time = tk.Label(rf, text=tstr, anchor="w", width=11,
                                  bg=base, fg="#999999")
                l_time.grid(row=0, column=3, sticky="w", padx=4)
                tk.Button(rf, text="编辑", width=5,
                          command=lambda mid=mid: _edit(mid)).grid(
                    row=0, column=4, padx=(6, 2), pady=3)
                tk.Button(rf, text="播放", width=5,
                          command=lambda mid=mid: _play(mid)).grid(
                    row=0, column=5, padx=(2, 8), pady=3)
                cells = (l_name, l_art, l_time)

                def _mk_paint(rf=rf, cells=cells, chk=chk, base=base, mid=mid):
                    def paint(hover=False):
                        c = (C_SEL if sel["mid"] == mid
                             else (C_HOVER if hover else base))
                        rf.configure(bg=c)
                        for lb in cells:
                            lb.configure(bg=c)
                        chk.configure(bg=c, activebackground=c)
                    return paint

                paint = _mk_paint()
                for w in (rf, l_name, l_art, l_time):   # 整行(含各格)都响应悬停/点击
                    w.bind("<Enter>", lambda e, p=paint: p(True))
                    w.bind("<Leave>", lambda e, p=paint: p(False))
                    w.bind("<Button-1>", lambda e, mid=mid: _select(mid))
                chk.bind("<Enter>", lambda e, p=paint: p(True))   # 勾选框上也保持悬停色
                chk.bind("<Leave>", lambda e, p=paint: p(False))
                rows.append({"paint": paint, "mid": mid})

            def _render_more():
                """追加渲染下一批 PAGE 行,更新"已显示/总数"计数。"""
                view["queued"] = False
                items, start = view["items"], view["pos"]
                end = min(start + PAGE, len(items))
                for i in range(start, end):
                    _add_row(items[i][0], items[i][1], i)
                view["pos"] = end
                count_var.set(f"已显示 {end} / 共 {len(items)} 首")

            def _on_scroll(first, last):
                """yscrollcommand:既喂滚动条,也做触底检测(视野越过 94% 即续批)。
                内容不满一屏时 last=1.0 同样触发 → 自动续批填满首屏。
                after_idle 出回调再渲染,queued 防重复排队。"""
                vsb.set(first, last)
                if (float(last) > 0.94 and not view["queued"]
                        and view["pos"] < len(view["items"])):
                    view["queued"] = True
                    root.after_idle(_render_more)

            canvas.configure(yscrollcommand=_on_scroll)

            def refresh():
                """重算 筛选+排序 结果集,清掉已渲染行,渲首批(其余滚动触底续批)。"""
                for w in inner.winfo_children():
                    w.destroy()
                rows.clear()
                kw = q.get().strip().lower()
                flt, srt = f_var.get(), s_var.get()
                setlist = set(STATE.get("setlist", []))
                items = []
                for mid, m in library.manifest().items():
                    title = (m.get("title") or "").strip().lower()
                    artist = (m.get("artist") or "").strip().lower()
                    if kw and kw not in title and kw not in artist:
                        continue
                    if flt != "全部":
                        is_live = "live" in title       # 歌名含 live 即视为 Live 版
                        if (flt == "只看Live") != is_live:
                            continue
                    items.append((mid, m))
                if srt == "未勾选在前":        # 按勾选态分组,组内仍按入库时间倒序
                    items.sort(key=lambda kv: (kv[0] in setlist,
                                               -(kv[1].get("added") or 0)))
                elif srt == "已勾选在前":
                    items.sort(key=lambda kv: (kv[0] not in setlist,
                                               -(kv[1].get("added") or 0)))
                else:                          # 最新入库(默认,同旧版)
                    items.sort(key=lambda kv: kv[1].get("added") or 0, reverse=True)
                view["items"], view["pos"] = items, 0
                canvas.yview_moveto(0)
                _render_more()    # 首批;不满一屏时 _on_scroll 会自动续批

            # 搜索防抖:停敲 200ms 才刷新
            _deb = {"id": None}

            def _on_query(*_a):
                if _deb["id"] is not None:
                    try:
                        root.after_cancel(_deb["id"])
                    except Exception:
                        pass
                _deb["id"] = root.after(200, refresh)

            q.trace_add("write", _on_query)
            cb_f.bind("<<ComboboxSelected>>", lambda e: refresh())
            cb_s.bind("<<ComboboxSelected>>", lambda e: refresh())

            bar = tk.Frame(root)
            bar.pack(fill="x", padx=10, pady=(0, 10))
            tk.Button(bar, text="关闭", width=8, command=root.destroy).pack(side="right")

            refresh()
            root.after(120, root.focus_force)
            if selftest:   # headless 自检:反复滚到底驱动分页,打印进度,渲完自毁
                def _auto(i=0):
                    try:
                        canvas.yview_moveto(1.0)
                        print(f"[LIBWIN-TEST] shown={view['pos']}/{len(view['items'])}",
                              flush=True)
                        if view["pos"] >= len(view["items"]) or i >= 80:
                            root.after(300, root.destroy)
                        else:
                            root.after(150, lambda: _auto(i + 1))
                    except Exception:
                        pass
                root.after(600, _auto)
            root.mainloop()
        except Exception as ex:
            print(f"[LIB] 曲库管理窗失败: {ex}")

    threading.Thread(target=_win, daemon=True).start()


# ── K歌:发指令给播放器 + 点歌队列 ──────────────────────
def _player_write(cmd):
    """真正的阻塞写(在 _player_io_exec 单线程上执行,加锁串行防写坏管道)。"""
    if _player_proc is None or _player_proc.poll() is not None:
        return
    try:
        with _player_lock:
            _player_proc.stdin.write(cmd + "\n")
            _player_proc.stdin.flush()
    except Exception:
        pass


def _player_send(cmd):
    """异步、有序地把指令投递给播放器:提交到单线程 IO 执行器,立即返回,不阻塞调用方。
    这样事件循环(_handle_cmd)和读取线程(k_play_next 自动下一首)都不会被管道背压卡住。
    单线程 FIFO 天然保证 load→show→play 等指令顺序不乱。"""
    try:
        _player_io_exec.submit(_player_write, cmd)
    except Exception:
        pass
    return True


def _sync_queue_state():
    """把 _now_mid/_queue 同步进 STATE(带歌名/歌手,供手机显示)。"""
    STATE["now"] = ({"mid": _now_mid, **(library.song_meta(_now_mid) or {})}
                    if _now_mid else None)
    STATE["queue"] = [{"mid": m, **(library.song_meta(m) or {})} for m in _queue]


def k_advance_paused():
    """自然唱完时调用:把下一首装载到**开头并保持暂停**(等主播手动开唱),队空则清空当前曲。
    这样歌曲间歇 BGM 能顶上——手机端"演唱↔BGM 联动"看到演唱停止,会自动恢复它暂停过的 QQ音乐;
    主播按播放开唱下一首时,联动又会把 BGM 渐弱暂停。"""
    global _now_mid
    if _queue:
        _now_mid = _queue.pop(0)
        _player_send("load " + _now_mid)   # load 自带:归位到 0、清调、切回伴奏、暂停
    else:
        _now_mid = None
        _player_send("pause")
    _sync_queue_state()


def k_play_next():
    """播放队列下一首(队空则空闲暂停)。静默切歌:不发 show,
    窗口显隐维持原状,由用户经托盘/遥控手动控制。"""
    global _now_mid
    if _queue:
        _now_mid = _queue.pop(0)
        _player_send("load " + _now_mid)
        _player_send("play")
    else:
        _now_mid = None
        _player_send("pause")
    _sync_queue_state()


def k_play_mid(mid):
    """从曲库直接播放某首:立即 `load+play`——**有歌在播则=切歌**(静默,不发 show)。
    不入队,`_queue` 保持不变(本首自然唱完后仍按队列续)。供曲库管理窗"播放"按钮用。"""
    global _now_mid
    if not mid:
        return
    _now_mid = mid
    _player_send("load " + mid)
    _player_send("play")
    _sync_queue_state()


def k_enqueue(mid):
    """点歌:入队;若当前空闲则立即开唱。"""
    if not mid:
        return
    _queue.append(mid)
    if _now_mid is None:
        k_play_next()
    else:
        _sync_queue_state()


def k_remove(idx):
    if 0 <= idx < len(_queue):
        _queue.pop(idx)
    _sync_queue_state()


def k_clear():
    _queue.clear()
    _sync_queue_state()


def k_move(frm, to):
    """队列重排:把第 frm 首移到第 to 位(手机长按拖动 / 置顶)。"""
    if 0 <= frm < len(_queue) and 0 <= to < len(_queue) and frm != to:
        _queue.insert(to, _queue.pop(frm))
    _sync_queue_state()


def run_tray(url):
    global _tray_icon, _tray_thread_id
    _tray_thread_id = threading.get_ident()   # 记录托盘线程,refresh_tray 只在此线程改菜单
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

    def _really_quit(icon):
        icon.stop()
        for p in (_smtc_proc, _player_proc):     # 收尾两个子进程
            try:
                if p is not None:
                    p.terminate()
            except Exception:
                pass
        os._exit(0)

    def do_quit(icon, item):
        # 退出前弹窗确认,防误点(托盘一停,服务、子进程全收尾,不可逆)。
        # 关键:弹窗必须在独立线程弹——直接在托盘菜单回调里 MessageBox 会占住
        # pystray 的 Win32 消息泵,导致弹窗按钮点了没反应、整个托盘卡死。
        def _confirm():
            try:
                import ctypes
                MB_YESNO, MB_ICONQUESTION, MB_TOPMOST, IDYES = 0x4, 0x20, 0x40000, 6
                answer = ctypes.windll.user32.MessageBoxW(
                    0,
                    "确定要退出直播遥控后台服务吗?\n退出后遥控、曲库监听、K歌播放器都会停止。",
                    "直播遥控 · 退出确认",
                    MB_YESNO | MB_ICONQUESTION | MB_TOPMOST,
                )
                if answer != IDYES:
                    return
            except Exception:
                pass   # 弹窗失败(非 Windows/无 GUI)时按老行为直接退出
            _really_quit(icon)

        threading.Thread(target=_confirm, daemon=True).start()

    def on_toggle_karaoke(icon, item):
        toggle_player()

    def on_open_library(icon, item):
        _open_library_browser()        # 打开曲库管理窗(倒序列表 + 搜索 + 编辑)

    # 菜单项文本用可调用;但 pystray-win32 右键弹的是缓存菜单、**不会**在打开时重新求值,
    # 需靠 refresh_tray()→update_menu() 主动刷(见 refresh_tray 注释)。
    menu = pystray.Menu(
        pystray.MenuItem(f"遥控地址: {url}", None, enabled=False),
        pystray.MenuItem(
            lambda i: f"Studio One MIDI: {'已连接' if STATE['studio_connected'] else '未连接'}",
            None, enabled=False),
        # 曲库:可点击 → 曲库管理窗(倒序/搜索/编辑歌名歌手)
        pystray.MenuItem(lambda i: f"曲库: {STATE['lib_count']} 首 — 点击管理", on_open_library),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "K歌歌词", on_toggle_karaoke,
            checked=lambda i: STATE["player_visible"]),   # 用权威状态(player 经VIS上报),不重读win32免竞态
        pystray.MenuItem("退出", do_quit),
    )
    _tray_icon = pystray.Icon("live_remote", image, "直播遥控 · 后台服务", menu)

    # 注册自定义窗口消息处理(经 _dispatcher 在托盘线程分发,update_menu 在此线程调安全):
    # ① WM_TRAY_REFRESH:非托盘线程 PostMessage 来唤醒托盘线程 update_menu(曲库数/勾选刷新);
    # ② 包裹托盘回调 WM_NOTIFY(=WM_USER+11):点气泡通知(NIN_BALLOONUSERCLICK)→ 弹改名框,
    #    其它 lParam(左键/右键点图标)照旧交回原处理。
    _orig_on_notify = _tray_icon._on_notify

    def _on_tray_notify(wparam, lparam):
        if lparam == NIN_BALLOONUSERCLICK:
            _open_rename_dialog(_last_import_mid)   # 点气泡→编辑该通知对应的歌
            return 0
        return _orig_on_notify(wparam, lparam)

    _tray_icon._message_handlers[WM_TRAY_REFRESH] = lambda w, l: _tray_icon.update_menu()
    _tray_icon._message_handlers[0x400 + 11] = _on_tray_notify   # WM_NOTIFY(pystray 托盘回调)

    def _tray_setup(icon):
        global _tray_hwnd
        icon.visible = True                       # 自定义 setup 须自己置可见
        _tray_hwnd = getattr(icon, "_hwnd", None)  # loop 已起,_hwnd 已建

    _tray_icon.run(setup=_tray_setup)


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

    _restore_persist()   # 先恢复上次的场景/静音记录/演唱音量,再起各子系统
    STATE["studio_connected"] = open_midi()
    # 说明:Studio One 的 Mackie 不回传静音状态,故不用回读,改为"纯记录 + 归位"。
    # 注意:不要在主线程调 get_qq_volume()——pycaw 的 COM 会和主线程托盘冲突导致崩溃。
    # bgm_vol 由后台轮询线程(_bgm_poller)首次读取,见下。

    start_smtc_reader()   # winrt 子进程(歌名/进度),与主进程 COM 隔离
    library.start(STATE, _on_lib_change, _on_lib_import)  # 曲库监听(WeSing缓存→永久曲库,入库弹通知)
    start_player()        # 拉起 K歌播放器子进程(隐藏+暂停)

    ip = get_lan_ip()
    url = f"http://{ip}:{config.PORT}"
    print("=" * 50)
    print("  直播遥控 · 电脑后台服务已启动")
    print(f"  手机 App / 网页填:   {ip}:{config.PORT}")
    print(f"  Studio One MIDI:     {'已连接' if STATE['studio_connected'] else '未连接(检查 loopMIDI)'}")
    print("  已在右下角托盘常驻。看完地址可直接关掉本窗口,")
    print("  服务不受影响;要停服务请右键托盘图标→退出。")
    print("=" * 50)

    # --headless:无托盘,uvicorn 跑主线程阻塞。供无界面/自动化测试(pystray 需交互桌面,
    # 分离进程里 icon.run() 会立即返回致主线程结束、服务随之退出;headless 绕开)。
    headless = "--headless" in sys.argv
    if headless:
        # 服务器仍跑后台线程(与正常路径一致,避免主线程 uvicorn 与 COM/pycaw 冲突段错误),
        # 主线程仅阻塞等待(替代托盘)。供无界面/自动化测试。
        print("[headless] 无托盘模式,服务后台线程运行(Ctrl+C 退出)", flush=True)
        threading.Thread(target=run_server, daemon=True).start()
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return

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
