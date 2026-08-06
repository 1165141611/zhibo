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
import gc
import queue
import concurrent.futures

warnings.filterwarnings("ignore")  # 屏蔽 pycaw 对未接入设备的无害 COMError 警告

import comtypes
from comtypes import CLSCTX_ALL, cast, POINTER
import psutil
from pycaw.pycaw import (AudioUtilities, ISimpleAudioVolume,
                         IAudioSessionManager2, IAudioSessionControl2,
                         IAudioEndpointVolume)

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
import mobile_import
import karaoke_data
import gifts

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ── 多线程 tkinter 防崩:Tk 窗口开着期间禁用 GC ──────────────────────────
# 本服务的 Tk 窗口(曲库管理/扫描导入/改名)各在**自己的后台线程**跑独立 Tk 根。Python 的循环 GC
# 会在**任意线程**触发,一旦在非 Tk 线程去 finalize 窗口里的 tk 对象(BooleanVar/PhotoImage 等),
# 就从错误线程调 Tcl → `tcl86t.dll` **Tcl_Panic(0x80000003)硬崩溃闪退**(曲库窗建 100+ BooleanVar
# 最易触发:单开不崩、多线程服务里必崩)。**修法(业界通用)**:窗口开着就禁 GC,全关了再恢复。
# 引用计数守卫支持多窗口同时开;`_tk_window_thread(fn)` 把窗口线程体一包即可。
_tk_gc_lock = threading.Lock()
_tk_gc_depth = 0


def _tk_gc_enter():
    global _tk_gc_depth
    with _tk_gc_lock:
        if _tk_gc_depth == 0:
            gc.disable()
        _tk_gc_depth += 1


def _tk_gc_exit():
    global _tk_gc_depth
    with _tk_gc_lock:
        _tk_gc_depth = max(0, _tk_gc_depth - 1)
        if _tk_gc_depth == 0:
            gc.enable()


# ── 常驻 UI 根:所有 Tk 窗口共用**一个隐藏根 + 单 mainloop**(自己的守护线程),各窗口都做它的
#    Toplevel。旧版每开一个窗都新建 `tk.Tk()`(冷启动 ~170ms/次 + 首个窗还要付 Tcl/Tk DLL、系统
#    字体枚举的一次性开销),且多 Tk() 根跨解释器易踩坑(故到处 `master=root`)。改为常驻单根后:
#    ①开窗只建 Toplevel(几十 ms),再开近乎瞬时;②全进程只有一个解释器,跨根隐患消失。
#    **跨线程调度只用队列**:托盘线程/后台线程把"建/毁窗口"的可调用丢进 _ui_queue,由 UI 线程
#    自己的周期 after 定时器抽取执行——绝不从别的线程直接碰 Tcl(那正是 Tcl_Panic 的来源)。
#    GC 崩溃防护沿用引用计数(见 _tk_gc_enter/exit):窗口一开就禁 GC、全关了在 UI 线程 collect 再恢复。
_ui_root = None
_ui_thread_id = None
_ui_queue = queue.Queue()
_ui_ready = threading.Event()


def _ui_thread_main():
    global _ui_root, _ui_thread_id
    import tkinter as tk
    _ui_root = tk.Tk()
    _ui_root.withdraw()                     # 根本身永不显示,只做所有窗口的宿主
    _ui_thread_id = threading.get_ident()

    def _drain():
        while True:                         # 抽干队列里排到的建/毁窗口任务(都在本 UI 线程跑)
            try:
                fn = _ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                print("[UI] 窗口任务异常:", e)
        _ui_root.after(30, _drain)
    _ui_root.after(30, _drain)
    _ui_ready.set()
    _ui_root.mainloop()


def _start_ui_thread():
    """启动常驻 UI 线程(建隐藏根 + 单 mainloop)。幂等:已起则直接返回。"""
    if _ui_ready.is_set():
        return
    threading.Thread(target=_ui_thread_main, daemon=True, name="tk-ui").start()
    _ui_ready.wait(5)


def _ui_post(fn):
    """把一个建/毁 Tk 窗口的可调用投到常驻 UI 线程执行(唯一合法的跨线程入口)。"""
    _start_ui_thread()
    _ui_queue.put(fn)


def _tk_win_close_guard(root, on_closed):
    """给一个窗口 Toplevel 装"关闭善后":窗一开就 _tk_gc_enter()(禁 GC),真正销毁(root 自身
    <Destroy>)时在**本 UI 线程** gc.collect() 清掉本窗遗留的 tk 循环、再 _tk_gc_exit() 恢复引用计数,
    并跑 on_closed(清全局勾选态 + 刷托盘)。**只认 root 自身的 <Destroy>**(过滤子控件销毁事件)。
    调用时机:窗口所有控件建好后调一次即可。"""
    _tk_gc_enter()
    done = {"v": False}

    def _on_destroy(e):
        if e.widget is not root or done["v"]:
            return
        done["v"] = True
        try:
            on_closed()
        except Exception as ex:
            print("[UI] 关窗善后异常:", ex)
        try:
            gc.collect()                    # 本 UI 线程回收本窗 tk 循环(别留给后台线程 → 免 Tcl_Panic)
        except Exception:
            pass
        _tk_gc_exit()
    root.bind("<Destroy>", _on_destroy)


def _prewarm_qqmusic():
    """启动时后台预热 `import qqmusic_import`(连带 numpy/niquests/qqmusic_api,冷导入合计 ~0.7s+,
    实运行 GIL 负载下更久)。预热后填进 sys.modules,QQ 页签首次构建时的 import 变成瞬时命中,
    不再让扫描窗在 Tk 线程上同步等这一串导入(那正是"打开扫描窗要卡好几秒"的主因)。"""
    try:
        import qqmusic_import  # noqa: F401
    except Exception as e:
        print("[QQ] 预热导入失败(打开 QQ 页签时再试):", e)


def _tk_window_thread(fn):
    """把一个建/跑 Tk 根的函数包成守卫线程体:进禁 GC、退恢复(引用计数,多窗口安全)。
    退出时先**在本 Tk 线程上** `gc.collect()`——本窗遗留的 tk 循环在自己线程回收(其 __del__ 有
    _tkinter 的线程守卫、只抛被忽略的 RuntimeError,不会 Tcl_Panic),不留给后台线程去回收(那才会崩)。"""
    def _runner():
        _tk_gc_enter()
        try:
            fn()
        finally:
            try:
                gc.collect()          # 显式 collect 无视 disable;就地清掉本窗 tk 循环
            except Exception:
                pass
            _tk_gc_exit()
    return _runner
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
    # ── 礼物菜单(绿幕左侧竖排"礼物→权益"引导条;托盘配置窗选礼物+自定义文字)──
    "gifts": [],               # [{id, text}] 按显示顺序(配置窗选,跨重启缓存;_push_gifts 解析成图标推播放器)
    "gifts_visible": True,     # 礼物菜单显隐(播放器 G 键 / 手机遥控,跨重启缓存)
    "gift_x": 24,              # 礼物条左上角 x(播放器鼠标拖动,跨重启缓存)
    "gift_y": 300,             # 礼物条左上角 y(同上)
    "gift_scale": 1.0,         # 礼物菜单整体缩放 0.4~2.0(配置窗滑块,跨重启缓存)
    "gift_outline": 1.0,       # 礼物描边宽 0~3px(配置窗滑块,跨重启缓存)
    "gift_gap": 4,             # 礼物卡片竖直间距 0~24px(配置窗滑块,跨重启缓存)
    "gift_color": "#333333",   # 礼物描边颜色(配置窗取色器,跨重启缓存)
    # ── 歌单/歌词样式(绿幕样式控制窗;字体大小/描边粗细/描边颜色/左右边距)──
    "setlist_pt": 20, "setlist_outline": 4, "setlist_color": "#000000", "setlist_margin": 40,
    "lyric_pt": 30, "lyric_outline": 6, "lyric_color": "#000000", "lyric_margin": 43,
    "player_x": None, "player_y": None,   # K歌播放器窗口桌面位置(拖动记忆,跨重启缓存)
    "performer": "八门官上",   # 演唱者(主播名):开头标题卡"演唱:<名>";托盘可改,跨重启缓存
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
# 只有走过启动 _restore_persist(把真实状态载入 STATE)之后才允许写盘。防"某进程 import server 后
# 在默认空 STATE 上调到 _save_persist(如测试调 _toggle_studio_visible / set_setlist_member)→
# 用默认空值覆盖掉用户真实 state_cache.json(曾把 setlist 清空)"。生产 main() 必先 _restore_persist。
_persist_ready = False


def _save_persist():
    """把需继承的状态写盘(整写小 JSON,先写临时文件再原子替换;失败只打日志不影响运行)。"""
    if not _persist_ready:           # 未经启动恢复就别写盘(防默认空 STATE 覆盖真实缓存)
        return
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
                "gifts": list(STATE.get("gifts", [])),
                "gifts_visible": bool(STATE.get("gifts_visible", True)),
                "gift_x": int(STATE.get("gift_x", 24)),
                "gift_y": int(STATE.get("gift_y", 300)),
                "gift_scale": float(STATE.get("gift_scale", 1.0)),
                "gift_outline": float(STATE.get("gift_outline", 1.0)),
                "gift_gap": int(STATE.get("gift_gap", 4)),
                "gift_color": str(STATE.get("gift_color", "#333333")),
                **{k: STATE.get(k) for k in _STYLE_KEYS},   # 歌单/歌词样式
                "player_x": STATE.get("player_x"),
                "player_y": STATE.get("player_y"),
                "performer": STATE.get("performer", "八门官上"),
            }
            tmp = PERSIST_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, PERSIST_PATH)
    except Exception as e:
        print(f"[PERSIST] 写入失败: {e}")


def _restore_persist():
    """启动时恢复上次的场景/静音记录/演唱音量(文件缺失或损坏则保持默认)。"""
    global _persist_ready
    _persist_ready = True            # 走过恢复流程(哪怕文件缺失=首次运行),之后才允许写盘
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
    if isinstance(data.get("gifts"), list):          # 礼物菜单内容(拉起播放器后统一推,见 start_player)
        STATE["gifts"] = [{"id": int(g["id"]), "text": str(g.get("text", ""))}
                          for g in data["gifts"] if g.get("id") is not None]
    if data.get("gifts_visible") is not None:
        STATE["gifts_visible"] = bool(data["gifts_visible"])
    for _k in ("gift_x", "gift_y"):
        if data.get(_k) is not None:
            try:
                STATE[_k] = int(data[_k])
            except Exception:
                pass
    if data.get("gift_scale") is not None:
        try:
            STATE["gift_scale"] = max(0.4, min(2.0, float(data["gift_scale"])))
        except Exception:
            pass
    if data.get("gift_outline") is not None:
        try:
            STATE["gift_outline"] = max(0.0, min(3.0, float(data["gift_outline"])))
        except Exception:
            pass
    if data.get("gift_gap") is not None:
        try:
            STATE["gift_gap"] = max(0, min(24, int(data["gift_gap"])))
        except Exception:
            pass
    if isinstance(data.get("gift_color"), str) and data["gift_color"].strip():
        STATE["gift_color"] = data["gift_color"].strip()
    for _k in _STYLE_KEYS:                            # 歌单/歌词样式(拉起播放器后统一下发)
        if data.get(_k) is not None:
            STATE[_k] = data[_k]
    for _k in ("player_x", "player_y"):          # 播放器窗口位置(拉起播放器后统一下发,见 start_player)
        if data.get(_k) is not None:
            try:
                STATE[_k] = int(data[_k])
            except Exception:
                pass
    if (data.get("performer") or "").strip():    # 演唱者(主播名),拉起播放器后下发
        STATE["performer"] = data["performer"].strip()
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
#  2) 背景音乐 —— QQ音乐 传输控制(有方向,经 winrt 子进程)
# ══════════════════════════════════════════════════════════
def _smtc_send(cmd):
    """把有方向的传输指令(play/pause/next/prev/toggle)写给 winrt 子进程 stdin。
    子进程按 AUMID 锁定 QQ音乐 会话做 try_play/try_pause 等——**不再用无方向的全局媒体键**:
    老媒体键会被 Windows 路由到"抢占系统当前会话"的 App(WeSing/浏览器/直播伴侣),
    正是"手机控制 QQ音乐 时好时坏、方向反打"的根因。子进程死/重启窗口内丢失一次指令可接受
    (2s 内自动重拉),换来的是有方向、指定目标、绝不打到别的 App。"""
    p = _smtc_proc
    if p is None or p.poll() is not None or p.stdin is None:
        return False
    try:
        p.stdin.write(cmd + "\n")
        p.stdin.flush()
        return True
    except Exception:
        return False


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
# ── 设备级缓存:哪些设备上见过 QQ音乐 会话 ──────────────────────────────
# SessionManager 和端点音量接口都是**设备级**的,不随会话销毁失效(ROUTIST 虚拟设备也不会拔插),
# 可以在"QQ暂停、会话已死"期间提前拿着用。两个用途(都为治"恢复播放炸响"):
#   ① 恢复播放时只扫这几个设备等新会话(毫秒级),不再全量枚举 35 个设备(一轮几百 ms,慢半拍);
#   ② 恢复播放**之前**先把这些设备端点静音——新会话纵然默认 100%,也一个采样都放不出来,
#     等压到 0 再解除静音。全量解析(_resolve_qq_sessions)时顺带刷新这两个缓存。
_qq_dev_mgrs = []   # [IAudioSessionManager2] 见过 QQ 会话的设备的会话管理器
_qq_dev_eps = []    # [IAudioEndpointVolume]  同一批设备的端点音量接口(mute 保险丝用)
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


# pid → (是否QQ音乐, 过期时刻)。归属校验要 psutil 爬父链,较贵;快速轮询 80ms 一拍高频调用,
# 必须缓存。TTL 60s 防 pid 复用造成的陈旧判定。
_qq_pid_cache = {}


def _is_qq_pid(pid):
    """该 pid 是否真属于 QQ音乐。**不能只看进程名**:MediaSDK_Server.exe 是腾讯共用进程,
    直播伴侣也用它挂推流麦克风链路的会话(实锤:按名裸匹配把主麦当 BGM 压到 0 = 直播间静音)。
    QQMUSIC_OWNER_CHECK 里的共用进程,必须父链(向上 4 级)含指定归属进程(QQMusic.exe)才算。"""
    hit = _qq_pid_cache.get(pid)
    now = time.monotonic()
    if hit is not None and hit[1] > now:
        return hit[0]
    name = _proc_name(pid)
    ok = name in [p.lower() for p in config.QQMUSIC_PROCS]
    if ok:
        need = None
        for shared, owner_ in getattr(config, "QQMUSIC_OWNER_CHECK", {}).items():
            if name == shared.lower():
                need = owner_.lower()
                break
        if need:
            ok = False
            try:
                q = psutil.Process(pid)
                for _ in range(4):
                    q = q.parent()
                    if q is None:
                        break
                    if q.name().lower() == need:
                        ok = True
                        break
            except Exception:
                ok = False
    _qq_pid_cache[pid] = (ok, now + 60.0)
    return ok


def _qq_running():
    """QQ音乐 是否在跑(带归属校验)。不在就别去枚举 35 个音频设备(白折腾 COM,还添崩溃面)。"""
    try:
        for p in psutil.process_iter(["name", "pid"]):
            n = (p.info.get("name") or "").lower()
            if n == "qqmusic.exe":
                return True
            if n in [t.lower() for t in config.QQMUSIC_PROCS] and _is_qq_pid(p.info["pid"]):
                return True
    except Exception:
        pass
    return False


def _resolve_qq_sessions():
    """收集 QQ音乐 会话的音量接口(**带归属校验**,直播伴侣的同名 MediaSDK_Server 绝不入选);
    优先只搜 BGM 设备白名单(QQMUSIC_DEVICE_HINT,如 PLAYBACK 1/2),白名单一无所获才退回
    全设备搜(应对用户改了 QQ 输出设备;归属校验仍兜底,推流链路设备上不会误伤)。
    顺带刷新设备级缓存(_qq_dev_mgrs/_qq_dev_eps,供恢复播放时快速轮询 + 端点静音保险丝)。"""
    global _qq_dev_mgrs, _qq_dev_eps
    if not _qq_running():
        return []
    _ensure_com()
    hint = (getattr(config, "QQMUSIC_DEVICE_HINT", "") or "").lower()

    def scan(devices):
        vols, mgrs, eps = [], [], []
        for d in devices:
            try:
                mgr = cast(d._dev.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None),
                           POINTER(IAudioSessionManager2))
                senum = mgr.GetSessionEnumerator()
                found = False
                for i in range(senum.GetCount()):
                    ctl = senum.GetSession(i)
                    ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                    if _is_qq_pid(ctl2.GetProcessId()):
                        vols.append(ctl.QueryInterface(ISimpleAudioVolume))
                        found = True
                if found:        # 该设备上有 QQ 会话(哪怕 Inactive 残留)→ 记住设备
                    mgrs.append(mgr)
                    try:
                        eps.append(cast(d._dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
                                        POINTER(IAudioEndpointVolume)))
                    except Exception:
                        pass
            except Exception:
                continue
        return vols, mgrs, eps

    vols, mgrs, eps = [], [], []
    try:
        devs = [d for d in AudioUtilities.GetAllDevices() if "Active" in str(d.state)]
        hinted = [d for d in devs if hint and hint in str(d.FriendlyName).lower()]
        if hinted:
            vols, mgrs, eps = scan(hinted)
        if not vols:                       # 白名单缺失/一无所获 → 全设备搜兜底
            vols, mgrs, eps = scan(devs)
    except Exception as e:
        print(f"[VOL] 枚举设备失败: {e}")
    if mgrs:   # 这轮真的见到 QQ 会话才更新设备缓存;否则保留旧的(QQ 暂停期会话可能全无)
        _qq_graveyard.extend(_qq_dev_mgrs)
        _qq_graveyard.extend(_qq_dev_eps)
        _qq_dev_mgrs, _qq_dev_eps = mgrs, eps
    return vols


def _mute_qq_endpoints():
    """恢复播放前的"保险丝":把见过 QQ 会话的设备端点静音,返回 [(ep, 原mute)] 供恢复。
    **仅在伴奏静默(k_playing=False)时用**——端点静音会连带掐掉共用 PLAYBACK 1/2 上的伴奏。
    用途:QQ音乐 恢复播放时会把自己的会话音量**重置回 100%**(QQ 自身行为),端点先静音,
    等它重置完、我们压回目标音量、再解除静音,恢复瞬间一个 100% 采样都放不出去(防炸响)。"""
    if not _qq_dev_eps:
        _resolve_qq_sessions()
    saved = []
    for ep in _qq_dev_eps:
        try:
            saved.append((ep, bool(ep.GetMute())))
            ep.SetMute(True, None)
        except Exception:
            continue
    return saved


def _restore_qq_endpoints(saved):
    """恢复端点静音原状(必须在 finally 里调——绝不能把用户设备留在静音态)。"""
    for ep, was in saved:
        try:
            ep.SetMute(was, None)
        except Exception:
            pass


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


def _get_qq_volume_impl(resolve=True):
    """读 QQ音乐 音量。resolve=False 时**只读已有缓存指针,绝不触发设备枚举**——本机 35 个虚拟
    设备,一次全量 _resolve_qq_sessions 可达数秒~25s,会把单条 pycaw 线程整段占死、拖垮所有
    播放/暂停(bgm_vol 周期轮询就必须用 resolve=False,不能每 4s 引发一次枚举风暴)。"""
    vols = _qq_vols_cached() if resolve else list(_qq_vols)
    for v in list(vols):
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


_qq_vol_set_at = 0.0       # 最近一次"手机端设音量"的时刻(monotonic);反向轮询据此让路


def schedule_qq_volume(v):
    """把设音量丢到 pycaw 线程(合并高频拖动),不阻塞事件循环。"""
    global _qq_vol_target, _qq_vol_running, _qq_vol_set_at
    v = max(0, min(100, int(v)))
    _qq_vol_set_at = time.monotonic()      # 记一笔,抑制反向轮询回读旧值(见 schedule_qq_volume_read)
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
    """异步读一次 QQ音乐 音量,读到就更新 STATE 并推手机。不阻塞事件循环。
    **手机端刚设过音量(3s 内)或还有待应用的设值时不回读**:否则会读到"设置尚未落地的旧值",
    把手机滑条硬拽回旧数字(用户设 40 → 轮询读到旧 100 → 滑条弹回 100)。反向同步只该同步
    "PC 上手动改的",不该和手机自己的前向设置打架。"""
    def work():
        if _qq_vol_target is not None or time.monotonic() - _qq_vol_set_at < 3.0:
            return                               # 有待应用的设值 / 刚设过 → 让路,不回读
        v = _get_qq_volume_impl(resolve=False)   # 只读缓存,绝不引发设备枚举(防拖垮 pycaw 线程)
        # **≥98 的读数不反向同步**:QQ音乐 换歌/恢复时会把会话音量重置回 100,那不是"PC 上手动
        # 改的",若采信会把 STATE.bgm_vol 冲成 100、连累自动切歌接管拿错目标。用户想要满音量走
        # 手机滑条前向设(STATE.bgm_vol 照样能到 100),不影响。
        if v is not None and v < 98 and STATE.get("bgm_vol") != v:
            STATE["bgm_vol"] = v
            _threadsafe_broadcast()
    try:
        _pycaw_exec.submit(work)
    except Exception:
        pass


def _reassert_bgm_vol():
    """自动切歌/换歌后补一次音量接管:QQ 把会话音量重置回 100,这里在 pycaw 专线程把它压回
    用户设定值(STATE.bgm_vol),~1.8s 内反复设几拍盖过 QQ 的重置(重置是一次性的,设后稳)。
    自动切歌时新歌已在播、无法预先静音,只能尽快压回缩短炸响窗(配合 smtc_helper 的快速换歌检测)。"""
    global _qq_vol_set_at
    target = STATE.get("bgm_vol")
    if not target or _fading:
        return
    _qq_vol_set_at = time.monotonic()   # 抑制反向轮询把 QQ 重置的 100 当 PC 端改动回读
    def work():
        for _ in range(6):
            _set_qq_volume_impl(target)
            time.sleep(0.3)
    try:
        _pycaw_exec.submit(work)
    except Exception:
        pass


def _start_bgm_vol_poller():
    """后台线程:周期性回读 QQ音乐 音量,把**在 PC 上手动改的**音量反向同步到手机。
    只在 BGM 在播且不在渐变时读(渐变自己会管音量,别插一脚);实际读走安全的 pycaw 专线程
    (schedule_qq_volume_read → 提交给单线程 MTA 执行器),本线程绝不直接碰 COM,与全局
    '不在主线程调 pycaw' 的稳定性纪律一致。间隔见 config.BGM_VOL_POLL_INTERVAL。
    读到变化才更新+广播,静止时零额外流量。"""
    def worker():
        interval = getattr(config, "BGM_VOL_POLL_INTERVAL", 4.0)
        while True:
            time.sleep(interval)
            if _fading or not STATE.get("bgm_playing"):
                continue
            schedule_qq_volume_read()
    threading.Thread(target=worker, daemon=True).start()


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
_lib_root = None        # 曲库管理窗 Tk 根(None=未开):托盘勾选反映开关态 + 单实例 + 再点即关
_scan_root = None       # 扫描导入窗 Tk 根(同上)
_gift_root = None       # 礼物菜单配置窗 Tk 根(同上)
_style_root = None      # 绿幕样式控制窗 Tk 根(同上)
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
                    stdin=subprocess.PIPE,   # 父进程经 stdin 下发有方向的传输控制(_smtc_send)
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
                if line.startswith("#"):   # 诊断日志(如会话 AUMID 列表),只记不解析
                    print(line)
                    continue
                try:
                    snap = json.loads(line)
                except Exception:
                    continue
                # 渐变刚按过媒体键 → 抑制窗内不采信快照里的 bgm_playing(可能是按键前的旧状态,
                # 会把乐观更新翻回去,导致 playpause 方向反打)。窗外恢复 SMTC 权威。
                if time.monotonic() < _bgm_smtc_mute_until:
                    snap.pop("bgm_playing", None)
                # 自动切歌检测:歌名变 / 进度大幅回退(且在播、不在渐变=非手机点的 next/prev)。
                # QQ音乐 自动换下一首时同样会把会话音量重置回 100% → 补一次音量接管(手机点的
                # next/prev 走 _bgm_switch,那时 _fading=True,这里不重复插手)。
                old_title = STATE.get("bgm_title")
                old_pos = STATE.get("bgm_pos")
                new_title = snap.get("bgm_title")
                new_pos = snap.get("bgm_pos")
                auto_switch = (
                    not _fading and STATE.get("bgm_playing") and snap.get("bgm_playing", True)
                    and ((new_title and old_title and new_title != old_title)
                         or (new_pos is not None and old_pos is not None and new_pos < old_pos - 5))
                )
                changed = any(STATE.get(k) != v for k, v in snap.items())
                STATE.update(snap)
                if changed and _clients and _loop is not None:
                    asyncio.run_coroutine_threadsafe(_broadcast(), _loop)
                if auto_switch:
                    _reassert_bgm_vol()   # 自动切歌 → 抢回用户音量(不然新歌 100% 炸响)
            # 子进程退出了,稍等重启(time 用模块级导入;函数内**绝不能** import time,
            # 否则 time 变函数局部名,上面 time.monotonic() 会 UnboundLocalError 崩掉读取线程)
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

# SMTC 覆盖抑制窗:渐变刚按下媒体键后,winrt 子进程可能还推来一帧**按键前**的旧快照,
# 把我们乐观更新的 bgm_playing 翻回去 → 之后 playpause 方向判断反打(媒体键本身无方向,
# 反一次后播放/暂停全乱,表现为"手动暂停后自动化失灵")。在窗内丢弃快照里的 bgm_playing。
_bgm_smtc_mute_until = 0.0   # monotonic 时刻;在此之前 SMTC 快照的 bgm_playing 不采信


# ── QQ音乐 播放/暂停:在**持久会话**上做纯音量渐变(整段在专用 pycaw 线程一次跑完)──────────
# 关键事实(2026-07-16 实测):**有方向 SMTC 控制下,QQ音乐 暂停并不销毁音频会话**——它只是转
# Inactive、音量原样保留,恢复播放沿用同一会话(转回 Active);持有的 ISimpleAudioVolume 指针
# 暂停期间依旧可读可写(实测:暂停时把它设 30,播放后仍是 30)。所以老那套"暂停即销毁会话→恢复
# 新会话默认 100% 炸响→端点静音保险丝 + 轮询新会话 + 指针坟场"已无必要(那是**无方向媒体键**
# 时代 QQ 的行为),而且正是它把这条路搞坏的:
#   ① 播放要死等"新 Active 会话"轮询 ~3s,整段 ~6s,期间 _fading 把用户后续点击**全丢**
#      → "暂停后连点播放没反应";
#   ② 老会话被 graveyard、重解析成默认值 → 音量被打回 100%。
# 但**实测另一条铁律**:QQ音乐 恢复播放时会把自己的会话音量**重置回 100%**(一次性,恢复后
# ~1~1.5s 发生;之后再设就稳)。老代码"轮询到会话就压 0 再渐强"压得太早——渐强跑完了 QQ 才重置到
# 100,于是**最终卡在 100、用户设的音量丢失 + 炸响**(正是本次要修的症状)。
# 现方案:会话持久(指针不失效),播放时① 先静音设备端点当保险丝(仅伴奏静默时,别掐共用通道的伴奏);
# ② 有方向 play;③ **等 QQ 那次"重置回100"真的发生**(会话音量跳到≥90 即知),再压 0 接管;
# ④ 解除端点静音;⑤ 渐强到 full;⑥ 再补设两拍兜底。整段在 pycaw 专线程串行跑。
def _bgm_fade_impl(target):
    """在持久的 QQ音乐 会话上做音量渐变。target=True 播放渐强,False 暂停渐弱。返回目标音量 full。"""
    global _bgm_smtc_mute_until
    steps = 20
    dt = config.FADE_SECONDS / steps if config.FADE_SECONDS > 0 else 0
    full = STATE.get("bgm_vol") or _get_qq_volume_impl() or 60
    if target:
        _bgm_smtc_mute_until = time.monotonic() + config.FADE_SECONDS + 6.0
        _qq_vols_cached()                        # 确保缓存(持久会话指针)
        guards = _mute_qq_endpoints() if not STATE.get("k_playing") else []
        try:
            _smtc_send("play")                   # 有方向播放(沿用同一持久会话)
            deadline = time.monotonic() + 2.5    # 等 QQ 的"重置回100"发生(端点已静音,它重置也无声)
            while time.monotonic() < deadline:
                v = _get_qq_volume_impl(resolve=False)
                if v is not None and v >= 90:
                    break
                time.sleep(0.1)
            _set_qq_volume_impl(0)               # 重置已发生 → 压 0 接管(端点还静着,无声)
        finally:
            _restore_qq_endpoints(guards)        # 解除端点静音(会话已 0,解除也不炸)
        for i in range(1, steps + 1):            # 渐强 0→full(QQ 不会再重置,渐强稳稳到位)
            _set_qq_volume_impl(round(full * i / steps))
            if dt:
                time.sleep(dt)
        _set_qq_volume_impl(full)
        for _ in range(2):                       # 补设两拍:盖过任何迟到的重置(极少见)
            time.sleep(0.3)
            _set_qq_volume_impl(full)
    else:
        _bgm_smtc_mute_until = time.monotonic() + config.FADE_SECONDS + 4.0
        _qq_vols_cached()                        # 解析一次填缓存,后续步骤复用
        for i in range(steps - 1, -1, -1):
            _set_qq_volume_impl(round(full * i / steps))
            if dt:
                time.sleep(dt)
        _smtc_send("pause")                      # 渐弱到 0 后有方向暂停(会话转 Inactive,不销毁)
        _bgm_smtc_mute_until = time.monotonic() + 4.0
    return full


async def _run_pycaw(fn):
    """把整段(阻塞的)pycaw 渐变丢到专用线程执行,事件循环不阻塞。"""
    return await asyncio.get_running_loop().run_in_executor(_pycaw_exec, fn)


_bgm_desired = None   # 最新期望播放态(True/False);渐变期间来的新指令只更新它,结束后自动续做


async def _bgm_apply(target):
    """把 BGM 收敛到 target(True=播放渐强 / False=暂停渐弱)。bgm_playing **先乐观翻转并广播**
    (手机端按钮/联动立刻看到正确方向),音量渐变在 pycaw 线程做。
    **正在渐变时绝不丢弃后续指令**:只更新期望 _bgm_desired,当前渐变收尾后自动续到最新期望
    ——治老代码"_fading 期间直接 return 把用户后续点击全丢"导致的"暂停后连点播放没反应、
    几秒后又自动回到暂停"(那几秒是老的 ~6s 慢渐变 + 丢指令 + SMTC 抑制窗过期后把真实态翻回)。"""
    global _fading, _bgm_desired, _bgm_smtc_mute_until
    _bgm_desired = target
    # ★ 抑制窗必须**在事件循环线程同步设好**(早于把渐变丢进 pycaw 执行器):_pycaw_exec 是单线程
    #   (max_workers=1),被 _reassert_bgm_vol(自动切歌占 1.8s)/音量泵/回读占着时,_bgm_fade_impl
    #   要排队,若抑制窗留到它里面才设,这段排队延迟内 winrt 每 0.35~1s 推来的真实 bgm_playing 快照
    #   会把刚乐观翻转的 STATE["bgm_playing"] 冲回去并广播 → 手机按钮"按了2s又弹回"、要点好几次。
    #   在此同步设窗,快照处理时窗已生效,乐观态不再被冲。用 max 不缩短已有更长的窗。渐变真正开跑时
    #   _bgm_fade_impl 会再按"实际起点"续设(覆盖排队更久的极端情况)。
    _bgm_smtc_mute_until = max(_bgm_smtc_mute_until,
                              time.monotonic() + config.FADE_SECONDS + (6.0 if target else 4.0))
    if STATE.get("bgm_playing") != target:
        STATE["bgm_playing"] = target
        await _broadcast()
    if _fading:
        return                          # 正在渐变 → 结束时会 reconcile 到最新 _bgm_desired
    _fading = True
    try:
        while True:
            tgt = _bgm_desired
            full = await _run_pycaw(lambda t=tgt: _bgm_fade_impl(t))
            if tgt:                     # 播放收尾才记目标音量;暂停不动 bgm_vol,滑条保持用户设定
                STATE["bgm_vol"] = full or STATE.get("bgm_vol") or 60
            if _bgm_desired == tgt:     # 期间无新指令 → 收敛完成
                break
    finally:
        _fading = False
    await _broadcast()


# 切歌(next/prev)也会触发 QQ音乐 那次"会话音量重置回 100%"(实测:切歌后会话音量从 40 变 100
# 并保持)。老代码切歌只发 transport、不管音量 → 新歌以 100% 炸响、手机音量条也弹回 100。
# 处理:静音端点当保险丝(仅伴奏静默时)→ 有方向切歌 → 等 QQ 重置发生 → 压回用户音量 → 解静音。
_bgm_switch_again = False   # 连点切歌:渐变/切歌进行中又来切歌 → 置位,收尾补一次纯音量接管


def _bgm_switch_impl(direction):
    """切歌 + 音量接管。direction 给出则有方向切歌;为 None 只补做音量接管(不再切歌)。返回 full。"""
    global _bgm_smtc_mute_until
    full = STATE.get("bgm_vol") or _get_qq_volume_impl() or 60
    _bgm_smtc_mute_until = time.monotonic() + 5.0
    _qq_vols_cached()                            # 确保缓存(持久会话指针)
    guards = _mute_qq_endpoints() if not STATE.get("k_playing") else []
    try:
        if direction:
            _smtc_send(direction)                # 有方向切歌
        deadline = time.monotonic() + 2.5        # 等 QQ 的"重置回100"发生(端点已静音,新歌炸不出来)
        while time.monotonic() < deadline:
            v = _get_qq_volume_impl(resolve=False)
            if v is not None and v >= 90:
                break
            time.sleep(0.1)
        _set_qq_volume_impl(full)                # 压回用户音量(端点还静着,无声切换)
    finally:
        _restore_qq_endpoints(guards)            # 解静音(会话已是 full,新歌以正确音量出声)
    for _ in range(2):                           # 补设两拍:盖过任何迟到的重置
        time.sleep(0.3)
        _set_qq_volume_impl(full)
    return full


async def _bgm_switch(direction):
    """切歌(next/prev):有方向切 + 接管音量(治切歌后音量被 QQ 重置回 100%)。
    正忙(渐变/切歌中)时直接发 transport 立即切,并置 _bgm_switch_again,收尾补一次音量接管。"""
    global _fading, _bgm_switch_again
    if _fading:
        _smtc_send(direction)
        _bgm_switch_again = True
        return
    _fading = True
    try:
        full = await _run_pycaw(lambda: _bgm_switch_impl(direction))
        STATE["bgm_vol"] = full or STATE.get("bgm_vol") or 60
        await _broadcast()
        while _bgm_switch_again:                  # 期间又切了(bare)→ 补一次纯音量接管
            _bgm_switch_again = False
            full = await _run_pycaw(lambda: _bgm_switch_impl(None))
            STATE["bgm_vol"] = full or STATE.get("bgm_vol") or 60
            await _broadcast()
    finally:
        _fading = False


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
            asyncio.create_task(_bgm_switch("next"))
        elif action == "prev":
            asyncio.create_task(_bgm_switch("prev"))
        elif action == "playpause":
            # 正在播放→渐弱暂停;已暂停→播放渐强。后台跑,不阻塞;指令永不丢(见 _bgm_apply)。
            asyncio.create_task(_bgm_apply(not STATE.get("bgm_playing")))
        elif action == "pause":
            # 有方向的暂停(演唱↔BGM 联动用)。_bgm_apply 内部幂等 + 合并,重复/连点都安全。
            asyncio.create_task(_bgm_apply(False))
        elif action == "play":
            # 有方向的播放(联动恢复用),同上。
            asyncio.create_task(_bgm_apply(True))
    elif cmd == "reset_scene":
        reset_mute_state()   # 归位:记录重置为全不静音(需你先把 4 条 M 都关掉)
    elif cmd == "studio_toggle":
        _toggle_studio_visible()     # 托盘/App 共用:show/hide + 存盘 + 刷托盘勾选 + 推 App 同步
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
    elif cmd == "gifts_toggle":
        v = not STATE.get("gifts_visible", True)
        STATE["gifts_visible"] = v
        _player_send("gifts_show " + ("1" if v else "0"))
        _save_persist()          # 礼物菜单显隐跨重启持久(同 G 键回读那套)
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
    songs = [{"mid": m, "title": v.get("title", ""), "artist": v.get("artist", ""),
              "plays": int(v.get("plays", 0))}
             for m, v in man.items()]
    # 默认按点歌次数倒序(常点的浮到最前),同次数按歌名升序稳定排列
    songs.sort(key=lambda s: (-s["plays"], s["title"] or ""))
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


# 歌单/歌词样式 STATE 键(回读/推送/存盘统一遍历;字体 pt 的 IPC 命令名是 *_font,见 _GP_CMD)
_STYLE_KEYS = ("setlist_pt", "setlist_outline", "setlist_color", "setlist_margin",
               "lyric_pt", "lyric_outline", "lyric_color", "lyric_margin")
# 绿幕样式控制:STATE 键 → 播放器 IPC 命令名(字体大小命令名是 *_font,其余同名)
_GP_CMD = {
    "gift_scale": "gift_scale", "gift_outline": "gift_outline", "gift_gap": "gift_gap",
    "gift_color": "gift_color",
    "setlist_pt": "setlist_font", "setlist_outline": "setlist_outline",
    "setlist_color": "setlist_color", "setlist_margin": "setlist_margin",
    "lyric_pt": "lyric_font", "lyric_outline": "lyric_outline",
    "lyric_color": "lyric_color", "lyric_margin": "lyric_margin",
}
# STATE 键 → (kind, lo, hi):kind 'f'=float / 'i'=int / 'c'=颜色(#rrggbb)
_GP_RANGE = {
    "gift_scale": ("f", 0.4, 2.0), "gift_outline": ("f", 0.0, 3.0), "gift_gap": ("i", 0, 24),
    "gift_color": ("c", 0, 0),
    "setlist_pt": ("i", 8, 40), "setlist_outline": ("f", 0.0, 8.0),
    "setlist_color": ("c", 0, 0), "setlist_margin": ("i", 0, 320),
    "lyric_pt": ("i", 16, 56), "lyric_outline": ("f", 0.0, 10.0),
    "lyric_color": ("c", 0, 0), "lyric_margin": ("i", 0, 320),
}


def set_style(key, v, save=True):
    """绿幕样式统一设定:夹取 → 更新 STATE → 推播放器(命令名查 _GP_CMD)→ 可选存盘+广播。
    滑块拖动 save=False 只 live 推(预览),松手/选色 save=True 存盘。"""
    cmd, spec = _GP_CMD.get(key), _GP_RANGE.get(key)
    if cmd is None or spec is None:
        return
    kind, lo, hi = spec
    if kind == "c":
        s = (str(v) or "").strip()
        if not s:
            return
        STATE[key] = s
        _player_send(cmd + " " + s)
    else:
        try:
            n = float(v)
        except Exception:
            return
        n = max(lo, min(hi, n))
        if kind == "i":
            n = int(round(n))
        STATE[key] = n
        _player_send(cmd + ((" %.3f" % n) if kind == "f" else (" " + str(n))))
    if save:
        _save_persist()
        _threadsafe_broadcast()


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
                gv_before = STATE.get("gifts_visible")
                gx_before = STATE.get("gift_x")
                gy_before = STATE.get("gift_y")
                gs_before = STATE.get("gift_scale")
                go_before = STATE.get("gift_outline")
                gg_before = STATE.get("gift_gap")
                gc_before = STATE.get("gift_color")
                style_before = {k: STATE.get(k) for k in _STYLE_KEYS}
                px_before = STATE.get("player_x")
                py_before = STATE.get("player_y")
                STATE.update({
                    "k_pos": st["pos"], "k_dur": st["dur"], "k_playing": st["playing"],
                    "k_key": st["key"], "k_vocal": st["vocal"], "k_mid": st["mid"],
                    "k_vol": st.get("vol", STATE.get("k_vol", 100)),
                    # 回读音准线/字体/歌单显隐+位置:让播放器 P/Q/O/Ctrl+↑↓ 也同步缓存(同 key/vocal)
                    "pitch_visible": st.get("pitch", STATE.get("pitch_visible", True)),
                    "k_font": st.get("font", STATE.get("k_font", 0)),
                    "setlist_visible": st.get("setlist_show", STATE.get("setlist_visible", True)),
                    "setlist_y": st.get("setlist_y", STATE.get("setlist_y", 24)),
                    # 回读礼物菜单显隐(G 键)+ 位置(鼠标拖动):同步缓存 + 推手机
                    "gifts_visible": st.get("gifts_show", STATE.get("gifts_visible", True)),
                    "gift_x": st.get("gift_x", STATE.get("gift_x", 24)),
                    "gift_y": st.get("gift_y", STATE.get("gift_y", 300)),
                    "gift_scale": max(0.4, min(2.0, float(st.get("gift_scale", STATE.get("gift_scale", 1.0))))),
                    "gift_outline": max(0.0, min(3.0, float(st.get("gift_outline", STATE.get("gift_outline", 1.0))))),
                    "gift_gap": max(0, min(24, int(st.get("gift_gap", STATE.get("gift_gap", 4))))),
                    "gift_color": str(st.get("gift_color", STATE.get("gift_color", "#333333"))),
                    # 歌单/歌词样式回读(播放器鼠标拖动/样式窗改动都同步缓存)
                    "setlist_pt": int(st.get("setlist_pt", STATE.get("setlist_pt", 20))),
                    "setlist_outline": float(st.get("setlist_outline", STATE.get("setlist_outline", 4))),
                    "setlist_color": str(st.get("setlist_color", STATE.get("setlist_color", "#000000"))),
                    "setlist_margin": int(st.get("setlist_margin", STATE.get("setlist_margin", 40))),
                    "lyric_pt": int(st.get("lyric_pt", STATE.get("lyric_pt", 30))),
                    "lyric_outline": float(st.get("lyric_outline", STATE.get("lyric_outline", 6))),
                    "lyric_color": str(st.get("lyric_color", STATE.get("lyric_color", "#000000"))),
                    "lyric_margin": int(st.get("lyric_margin", STATE.get("lyric_margin", 43))),
                    # 播放器窗口桌面位置(拖动记忆,跨重启缓存;仅缓存,不推手机——纯 PC 侧信息)
                    "player_x": st.get("win_x", STATE.get("player_x")),
                    "player_y": st.get("win_y", STATE.get("player_y")),
                    "k_title": st["title"], "k_artist": st["artist"],
                })
                if (STATE.get("k_vol") != vol_before                # 有变才写盘(上报每 500ms 一次)
                        or STATE.get("pitch_visible") != pitch_before
                        or STATE.get("k_font") != font_before
                        or STATE.get("setlist_visible") != slv_before
                        or STATE.get("setlist_y") != sly_before
                        or STATE.get("gifts_visible") != gv_before
                        or STATE.get("gift_x") != gx_before
                        or STATE.get("gift_y") != gy_before
                        or STATE.get("gift_scale") != gs_before
                        or STATE.get("gift_outline") != go_before
                        or STATE.get("gift_gap") != gg_before
                        or STATE.get("gift_color") != gc_before
                        or {k: STATE.get(k) for k in _STYLE_KEYS} != style_before
                        or STATE.get("player_x") != px_before
                        or STATE.get("player_y") != py_before):
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


def _resolve_player_device():
    """按名字实时解析播放器输出设备索引(防设备枚举漂移)。找 WASAPI 下名字含 PLAYER_DEVICE_NAME
    且有输出通道的设备;找不到回退 config.PLAYER_DEVICE。教训见 config.py PLAYER_DEVICE_NAME。"""
    try:
        import sounddevice as sd
        want = config.PLAYER_DEVICE_NAME
        cands = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] > 0 and want in d["name"]:
                ha = sd.query_hostapis(d["hostapi"])["name"]
                cands.append((i, ha, d["name"]))
        for i, ha, nm in cands:                     # 优先 WASAPI(伴奏走这条)
            if config.PLAYER_DEVICE_HOSTAPI in ha:
                print(f"[PLAYER] 输出设备按名解析 → [{i}] {nm} ({ha})")
                return i
        if cands:
            print(f"[PLAYER] 未找到 {config.PLAYER_DEVICE_HOSTAPI},用 [{cands[0][0]}] {cands[0][2]}")
            return cands[0][0]
    except Exception as e:
        print(f"[PLAYER] 设备名解析失败,回退索引 {config.PLAYER_DEVICE}: {e}")
    print(f"[PLAYER] 未匹配到 '{config.PLAYER_DEVICE_NAME}',回退索引 {config.PLAYER_DEVICE}")
    return config.PLAYER_DEVICE


def start_player():
    """拉起 K歌播放器子进程:隐藏、暂停、关 SMTC、指定声卡。stdin 收指令 / stdout 报可见性。
    不自动重启(面向用户,可能主动关)。"""
    global _player_proc
    try:
        # PYTHONIOENCODING=utf-8:强制子进程 std 流用 UTF-8(否则 Windows 默认 GBK,STATE/VIS 中文歌名
        # 会让下面 encoding="utf-8" 的读取在第一行就 UnicodeDecodeError 崩掉读取线程 → 管道写满 → 播放器
        # GUI 卡死无响应)。errors="replace":父进程侧再加一层兜底,任何杂字节也绝不崩读取循环。
        _env = dict(os.environ, PYTHONIOENCODING="utf-8")
        dev_idx = _resolve_player_device()
        _player_proc = subprocess.Popen(
            [config.PLAYER_PYTHON, config.PLAYER_PATH,
             "--device", str(dev_idx),
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
        # 礼物菜单:显隐 + 位置 + 内容(拉起必推,绕过去重缓存)
        _player_send("gifts_show " + ("1" if STATE.get("gifts_visible", True) else "0"))
        _player_send("gift_pos %d %d" % (int(STATE.get("gift_x", 24)), int(STATE.get("gift_y", 300))))
        _player_send("gift_scale %.3f" % float(STATE.get("gift_scale", 1.0)))
        _player_send("gift_outline %.2f" % float(STATE.get("gift_outline", 1.0)))
        _player_send("gift_gap " + str(int(STATE.get("gift_gap", 4))))
        _player_send("gift_color " + str(STATE.get("gift_color", "#333333")))
        # 歌单/歌词样式(字体 pt 的 IPC 命令名是 *_font)
        for _k in _STYLE_KEYS:
            _player_send(_GP_CMD[_k] + " " + str(STATE.get(_k)))
        _push_gifts(force=True)
        if STATE.get("player_x") is not None and STATE.get("player_y") is not None:
            _player_send("pos %d %d" % (int(STATE["player_x"]), int(STATE["player_y"])))  # 恢复上次窗口位置
        _player_send("performer " + str(STATE.get("performer", "八门官上")))   # 演唱者(开头标题卡)
        _push_setlist(force=True)   # 歌单内容(据缓存的 mid 列表 → 歌名);拉起必推,绕过去重缓存
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


def _toggle_studio_visible():
    """切 Studio One 窗口显隐——**托盘菜单 与 App 的 `studio_toggle` 共用同一套**:show/hide + 更新
    STATE + 存盘 + 刷托盘勾选 + 推手机(让 App 端显隐开关同步)。返回新的可见态。"""
    if STATE.get("studio_visible", True):
        if studio_win.hide():
            STATE["studio_visible"] = False
    else:
        if studio_win.show():
            STATE["studio_visible"] = True
    _save_persist()
    refresh_tray()               # 托盘勾选(App 触发时也刷,双向同步)
    _threadsafe_broadcast()      # 推 App(托盘触发时让 App UI 同步;从事件循环触发=多推一次,无害)
    return STATE["studio_visible"]


def _safe_destroy(w):
    """在 UI 线程上安全销毁一个窗口(已销毁/无效则忽略)。"""
    try:
        w.destroy()
    except Exception:
        pass


def _toggle_library_window(icon=None, item=None):
    """托盘「曲库」:未开→开;已开→关窗(单实例,绝不重复开)。勾选态随 _lib_root 反映(见 _win)。
    开/关都投到常驻 UI 线程执行(不再从托盘线程跨线程碰 Tcl)。"""
    r = _lib_root
    if r is not None:
        _ui_post(lambda w=r: _safe_destroy(w))   # 关窗:<Destroy> 善后会清 _lib_root + 刷托盘
    else:
        _open_library_browser()


def _toggle_scan_window(icon=None, item=None):
    """托盘「扫描导入歌曲」:未开→开;已开→关窗(单实例)。勾选态随 _scan_root 反映。"""
    r = _scan_root
    if r is not None:
        _ui_post(lambda w=r: _safe_destroy(w))
    else:
        _open_scan_window()


def _toggle_gift_window(icon=None, item=None):
    """托盘「礼物菜单配置」:未开→开;已开→关窗(单实例)。勾选态随 _gift_root 反映。"""
    r = _gift_root
    if r is not None:
        _ui_post(lambda w=r: _safe_destroy(w))
    else:
        _open_gift_window()


def _toggle_style_window(icon=None, item=None):
    """托盘「绿幕样式控制」:未开→开;已开→关窗(单实例)。勾选态随 _style_root 反映。"""
    r = _style_root
    if r is not None:
        _ui_post(lambda w=r: _safe_destroy(w))
    else:
        _open_style_window()


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


_last_setlist_pushed = None   # 上次已推给播放器的歌单(去重用)


def _push_setlist(force=False):
    """把歌单(mid→歌名)推给播放器顶端滚动字幕。曲库勾选变化 / 播放器拉起时调。
    ★ 内容没变则跳过(force=True 强推,仅播放器拉起时用):否则每次"点歌入队"触发曲库回调
    (bump_play→_on_lib_change)都会重推同样内容,播放器白白重建滚动 pixmap(GUI 线程字体渲染)、
    与音频回调抢 GIL → 正在播的歌卡顿一下。播放器侧也有同样守卫,双保险。"""
    global _last_setlist_pushed
    titles = []
    for m in STATE.get("setlist", []):
        t = (library.song_meta(m) or {}).get("title", "").strip()
        if t:
            titles.append(t)
    if not force and titles == _last_setlist_pushed:
        return
    _last_setlist_pushed = titles
    _player_send("setlist " + json.dumps(titles, ensure_ascii=False))


_last_gifts_pushed = None   # 上次已推给播放器的礼物条(去重用)


def _push_gifts(force=False):
    """把礼物菜单(STATE["gifts"]=[{id,text}])解析成 [{icon:图标绝对路径, text}] 推给播放器。
    配置窗保存 / 播放器拉起时调。★ 内容没变则跳过(force 仅拉起时用):同 _push_setlist,
    避免白重推让播放器重建卡片 pixmap(drawText+图标缩放,GUI 线程重活)抢音频回调 GIL。"""
    global _last_gifts_pushed
    try:
        resolved = gifts.resolve(STATE.get("gifts", []))
    except Exception as e:
        print(f"[GIFTS] 解析失败: {e}")
        return
    if not force and resolved == _last_gifts_pushed:
        return
    _last_gifts_pushed = resolved
    _player_send("gifts " + json.dumps(resolved, ensure_ascii=False))


def set_gift_config(items):
    """礼物菜单配置窗「保存」:items=[{id,text}] 按显示顺序。更新 STATE + 存盘 + 推播放器 + 推手机。"""
    clean = []
    for g in items or []:
        try:
            clean.append({"id": int(g["id"]), "text": str(g.get("text", "")).strip()})
        except Exception:
            continue
    STATE["gifts"] = clean
    _save_persist()
    _push_gifts(force=True)          # 内容确实变了,强推
    _threadsafe_broadcast()


def set_gift_scale(v, save=True):
    """礼物菜单尺寸(0.6~2.0)。配置窗滑块调:拖动时 save=False 只 live 推播放器(preview),
    松手 save=True 存盘 + 广播。夹在上下限内。"""
    try:
        s = max(0.4, min(2.0, float(v)))
    except Exception:
        return
    STATE["gift_scale"] = s
    _player_send("gift_scale %.3f" % s)
    if save:
        _save_persist()
        _threadsafe_broadcast()


def set_gift_outline(v, save=True):
    """礼物描边宽(0~3px)。滑块拖动 save=False live 推、松手 save=True 存盘。"""
    try:
        w = max(0.0, min(3.0, float(v)))
    except Exception:
        return
    STATE["gift_outline"] = w
    _player_send("gift_outline %.2f" % w)
    if save:
        _save_persist(); _threadsafe_broadcast()


def set_gift_gap(v, save=True):
    """礼物卡片竖直间距(0~24px)。"""
    try:
        g = max(0, min(24, int(float(v))))
    except Exception:
        return
    STATE["gift_gap"] = g
    _player_send("gift_gap " + str(g))
    if save:
        _save_persist(); _threadsafe_broadcast()


def set_gift_color(hexcol, save=True):
    """礼物描边颜色(#rrggbb,不透明)。取色器选定即调。"""
    hexcol = (hexcol or "").strip()
    if not hexcol:
        return
    STATE["gift_color"] = hexcol
    _player_send("gift_color " + hexcol)
    if save:
        _save_persist(); _threadsafe_broadcast()


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
            root = tk.Toplevel(_ui_root)
            root.title("修改歌名 · K歌曲库")
            root.attributes("-topmost", True)
            root.resizable(False, False)
            _build_edit_form(root, mid, on_saved=None)
            _tk_win_close_guard(root, on_closed=lambda: None)
            root.after(120, root.focus_force)
        except Exception as ex:
            print(f"[LIB] 改名对话框失败: {ex}")

    _ui_post(_dlg)


def _open_performer_dialog():
    """托盘"演唱者"菜单 → 独立窗口改主播名。保存后更新 STATE + 存盘 + 下发播放器(开头标题卡)+ 刷托盘。
    独立线程跑 mainloop,绝不阻塞托盘消息泵(同 _open_rename_dialog)。"""
    def _dlg():
        try:
            import tkinter as tk
            root = tk.Toplevel(_ui_root)
            root.title("演唱者 · 直播")
            root.attributes("-topmost", True)
            root.resizable(False, False)
            tk.Label(root, text="演唱者(主播名):").grid(row=0, column=0, padx=10, pady=(14, 8))
            e = tk.Entry(root, width=24)
            e.grid(row=0, column=1, padx=10, pady=(14, 8))
            e.insert(0, STATE.get("performer", "八门官上"))

            def _ok(*_):
                name = e.get().strip()
                if name:
                    STATE["performer"] = name
                    _save_persist()
                    _player_send("performer " + name)   # 开头标题卡即时用新名
                    refresh_tray()
                    _threadsafe_broadcast()
                root.destroy()

            tk.Button(root, text="取消", width=8, command=root.destroy).grid(
                row=1, column=0, padx=10, pady=(0, 12))
            tk.Button(root, text="保存", width=10, command=_ok).grid(
                row=1, column=1, padx=10, pady=(0, 12), sticky="e")
            root.bind("<Return>", _ok)
            _tk_win_close_guard(root, on_closed=lambda: None)
            root.after(120, root.focus_force)
            root.after(150, e.focus_set)
        except Exception as ex:
            print(f"[PERF] 演唱者对话框失败: {ex}")

    _ui_post(_dlg)


def _fmt_key(k):
    """默认调式标签:0=原调,正负半音带符号。"""
    return "原调" if int(k) == 0 else ("%+d" % int(k))


def _open_pair_dialog(parent, on_connected):
    """无线 ADB 配对二维码框(与 parent 同线程 Toplevel)。手机『设置→开发者选项→无线调试→用二维码
    配对设备』扫码 → 后台 adb mdns 发现 → adb pair → adb connect,成功回调 on_connected(serial)。"""
    import tkinter as tk
    try:
        import qrcode
        from PIL import ImageTk
    except Exception as e:
        from tkinter import messagebox
        messagebox.showerror("缺少依赖", "无线配对二维码需要 qrcode 库:\npip install qrcode\n\n%s" % e)
        return

    name, code, data = mobile_import.make_pair_payload()
    dlg = tk.Toplevel(parent)
    dlg.title("无线 ADB 配对")
    dlg.attributes("-topmost", True); dlg.resizable(False, False); dlg.grab_set()
    st = {"stop": False, "serial": None, "done": False,
          "msg": "手机:设置 → 开发者选项 → 无线调试 → 用二维码配对设备,扫下方二维码"}

    img = qrcode.make(data)
    pil = (img.get_image() if hasattr(img, "get_image") else img).resize((240, 240))
    photo = ImageTk.PhotoImage(pil, master=dlg)   # master=dlg 防多窗口时绑错解释器(二维码不显示)
    lbl_img = tk.Label(dlg, image=photo); lbl_img.image = photo   # 保引用防 GC
    lbl_img.pack(padx=18, pady=(16, 6))
    tk.Label(dlg, text="配对码:%s" % code, fg="#888888").pack()
    status = tk.Label(dlg, text=st["msg"], wraplength=300, justify="center")
    status.pack(padx=16, pady=10)

    def _close():
        st["stop"] = True
        try:
            dlg.destroy()
        except Exception:
            pass
    tk.Button(dlg, text="取消", width=8, command=_close).pack(pady=(0, 14))
    dlg.protocol("WM_DELETE_WINDOW", _close)

    def _work():
        try:
            st["serial"] = mobile_import.wait_and_pair(
                name, code, progress_cb=lambda m: st.update(msg=m), stop=lambda: st["stop"])
        except Exception as e:
            st["msg"] = "出错:%s" % e
        st["done"] = True
    threading.Thread(target=_work, daemon=True).start()

    def _poll():
        if st["stop"]:
            return
        status.config(text=st["msg"])
        if st["done"]:
            if st["serial"]:
                status.config(text="✅ 已连接 %s" % st["serial"], fg="#1a7f37")
                try:
                    on_connected(st["serial"])
                except Exception:
                    pass
                dlg.after(900, _close)
            else:
                status.config(text=st["msg"] + "(可取消重试)", fg="#c0392b")
            return
        dlg.after(300, _poll)
    _poll()
    dlg.after(120, dlg.focus_force)


_preview_proc = None


def _preview(cand):
    """扫描窗口「试听」:子进程拉起 preview_play.py 播该曲伴奏(**系统默认输出**=自己听的通道)
    + 纯文本歌词窗(←→ 步退进、Esc 退出)。单实例:再点/换歌先杀掉上一个。
    cand['src_root']/'mid' 指向已转换好的四件套(手机=暂存目录,PC=WeSing 缓存)。"""
    global _preview_proc
    if _preview_proc is not None and _preview_proc.poll() is None:
        try:
            _preview_proc.terminate()
        except Exception:
            pass
    try:
        _preview_proc = subprocess.Popen(
            [config.PLAYER_PYTHON, config.PREVIEW_PLAY_PATH, cand["src_root"], cand["mid"],
             "--volume", str(config.PREVIEW_VOLUME)],
            creationflags=0x08000000,       # CREATE_NO_WINDOW(抑制控制台;Tk 预览窗照常显示)
            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    except Exception as e:
        print("[SCAN] 试听启动失败:", e)


def _open_scan_window():
    """扫描导入窗:PC 缓存 + 手机全民K歌**双端扫描** → 去重 → 多选可编辑表格 → 勾选入库。
    自带线程 + 自建 Tk 根(仿 _open_library_browser)。慢扫描(adb 拉取 + ffmpeg 转换,可能 ~30s)
    放 worker 线程,共享态 st 由 root.after 轮询读,刷 loading 动画;扫完渲染表格。"""
    NO_DEV = "(未检测到手机)"

    def _win():
        global _scan_root
        import tkinter as tk
        from tkinter import ttk

        root = tk.Toplevel(_ui_root)   # 常驻根的 Toplevel(免每次新建 tk.Tk() 冷启动)
        root.title("扫描导入歌曲")
        root.geometry("720x560")
        root.attributes("-topmost", True)
        _scan_root = root

        def _on_closed():
            global _scan_root
            _scan_root = None          # 关窗 → 去托盘勾选
            refresh_tray()
        _tk_win_close_guard(root, _on_closed)
        refresh_tray()                 # 反映"已打开"的托盘勾选

        # 双页签:K歌(带音准)=本地缓存扫描;QQ(无音准)=登录态在线搜索
        nb = ttk.Notebook(root); nb.pack(fill="both", expand=True)
        kge = tk.Frame(nb); nb.add(kge, text="K歌（带音准）")
        qqf = tk.Frame(nb); nb.add(qqf, text="QQ（无音准）")

        st = {"phase": "idle", "msg": "", "results": None, "error": None,
              "gen": 0, "serial": None, "import_done": None, "op_msg": None}
        dev_map = {}   # 下拉显示名 -> serial

        # ============ K歌页签:扫描来源(电脑/手机 二选一)+ 设备 + 重新扫描 ============
        # 一次只扫一端,避免两端数据互相干扰。
        scan_mode = tk.StringVar(master=root, value="pc")
        top = tk.Frame(kge); top.pack(fill="x", padx=10, pady=(10, 2))
        tk.Label(top, text="扫描来源:").pack(side="left")
        tk.Radiobutton(top, text="电脑缓存", variable=scan_mode, value="pc",
                       command=lambda: _on_mode()).pack(side="left")
        tk.Radiobutton(top, text="手机全民K歌", variable=scan_mode, value="phone",
                       command=lambda: _on_mode()).pack(side="left", padx=(0, 8))
        rescan_btn = tk.Button(top, text="重新扫描"); rescan_btn.pack(side="right")
        # 检索框:短、置于「重新扫描」左侧同一行(过滤扫描结果;结果按缓存时间倒序)
        search_var = tk.StringVar(master=root)
        tk.Entry(top, textvariable=search_var, width=16).pack(side="right", padx=(0, 6))
        tk.Label(top, text="检索:").pack(side="right")

        top2 = tk.Frame(kge); top2.pack(fill="x", padx=10, pady=(0, 4))
        dev_lbl = tk.Label(top2, text="手机设备:"); dev_lbl.pack(side="left")
        dev_var = tk.StringVar(master=root)
        dev_cb = ttk.Combobox(top2, textvariable=dev_var, state="readonly", width=26)
        dev_cb.pack(side="left", padx=6)
        pair_btn = tk.Button(top2, text="📶 扫码连接",
                             command=lambda: _open_pair_dialog(root, _on_paired))
        pair_btn.pack(side="left")

        def _on_mode():
            """切来源:手机模式才启用设备行;并刷新状态提示(不自动扫描,由用户点『重新扫描』)。"""
            phone = scan_mode.get() == "phone"
            dev_cb.config(state="readonly" if phone else "disabled")
            pair_btn.config(state="normal" if phone else "disabled")
            dev_lbl.config(fg="#111111" if phone else "#999999")
            status_lbl.config(
                text=("选好手机设备后点『重新扫描』(只扫手机全民K歌)。" if phone
                      else "点『重新扫描』(只扫电脑 WeSing 缓存)。"), fg="#666666")

        status_lbl = tk.Label(kge, text="", anchor="w", fg="#666666")
        status_lbl.pack(fill="x", padx=12)
        prog = ttk.Progressbar(kge, mode="indeterminate")   # loading(动态显隐)

        # 表格区:Canvas + 内嵌 Frame + 滚动条
        body = tk.Frame(kge); body.pack(fill="both", expand=True, padx=(10, 0), pady=4)
        canvas = tk.Canvas(body, highlightthickness=0)
        vsb = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        vsb.pack(side="right", fill="y"); canvas.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(canvas, bg="#ffffff")
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))
        def _on_wheel(e):
            # 内容比视口短就锁定顶部、不滚动(否则上滚会把短内容推下去、顶部露出一截空白);超出视口才滚。
            if inner.winfo_height() <= canvas.winfo_height():
                canvas.yview_moveto(0)
                return
            canvas.yview_scroll(int(-e.delta / 120), "units")
        # 两页签各自的滚动区都想收滚轮:按鼠标进出该 canvas 时才 bind_all,避免两页签抢滚轮。
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        rows = []   # [{cand, chk, e_title, e_artist}]

        # 底部按钮
        bar = tk.Frame(kge); bar.pack(fill="x", padx=10, pady=(2, 10))

        def _set_all(v):
            for r in rows:
                # 已入库禁选跳过;只对当前检索可见的行生效(全选=选中你看到的)
                if not r.get("in_library") and r["rf"].winfo_ismapped():
                    r["chk"].set(v)
        tk.Button(bar, text="全选", width=6, command=lambda: _set_all(True)).pack(side="left")
        tk.Button(bar, text="全不选", width=7, command=lambda: _set_all(False)).pack(side="left", padx=4)
        confirm_btn = tk.Button(bar, text="确认入库"); confirm_btn.pack(side="right")
        tk.Button(bar, text="取消", width=7, command=root.destroy).pack(side="right", padx=6)

        C_EVEN, C_ODD = "#ffffff", "#f4f5f7"
        _TITLE_PH = "（待命名，点此输入）"    # 空标题占位,别显示成空白行;导入时视为空(仍走 needs_name)

        def _row_title(r):
            """取行内歌名:占位没动过 → 视为空(让 import_candidate 回退 QRC / 标 needs_name)。"""
            w = r["e_title"]; t = w.get()
            return "" if (getattr(w, "_ph", None) and t == w._ph) else t

        def _clear_rows():
            for w in inner.winfo_children():
                w.destroy()
            rows.clear()

        def _preview_kge(cand):
            """K歌试听:手机新歌先解密转码(needs_convert)再播;PC/已转换的直接 _preview。
            转好后清 needs_convert,之后入库/再试听直接复用暂存。"""
            if not cand.get("needs_convert"):
                _preview(cand); return

            def _work():
                try:
                    st["op_msg"] = "试听:解密转码 " + (cand.get("title") or cand["mid"]) + "…"
                    mobile_import.convert_phone_song(
                        cand, progress_cb=lambda m: st.update(op_msg="试听转码 · " + m.strip()))
                    cand["needs_convert"] = False
                    _preview(cand)
                    st["op_msg"] = "试听中(伴奏,系统默认输出、不进直播)。"
                except Exception as e:
                    st["op_msg"] = "试听失败:%s" % e
            threading.Thread(target=_work, daemon=True).start()

        def _add_row(cand, idx):
            base = C_EVEN if idx % 2 == 0 else C_ODD
            in_lib = cand.get("in_library")
            rf = tk.Frame(inner, bg=base); rf.pack(fill="x")
            rf.columnconfigure(1, weight=1, minsize=150)
            chk = tk.BooleanVar(master=root, value=False)   # 默认全不选;master=root 防多窗口串解释器
            cb = tk.Checkbutton(rf, variable=chk, bg=base, activebackground=base)
            if in_lib:
                cb.config(state="disabled")          # 已入库:禁止勾选
            cb.grid(row=0, column=0, padx=(6, 2))
            e_t = tk.Entry(rf, width=24)
            title = (cand.get("title") or "").strip()
            if title:
                e_t.insert(0, title)
            elif not in_lib:                       # 空标题(待命名):灰红占位(已入库有库里名,不会走这)
                e_t.insert(0, _TITLE_PH); e_t.config(fg="#c0392b"); e_t._ph = _TITLE_PH

                def _clear_ph(ev, w=e_t):          # 点击/聚焦即清占位,可直接输入
                    if getattr(w, "_ph", None) and w.get() == w._ph:
                        w.delete(0, "end"); w.config(fg="#111111"); w._ph = None
                e_t.bind("<FocusIn>", _clear_ph)
            e_t.grid(row=0, column=1, sticky="we", padx=2, pady=4)
            e_a = tk.Entry(rf, width=12)
            e_a.insert(0, cand.get("artist") or "")
            e_a.grid(row=0, column=2, padx=2)
            if in_lib:                             # 已入库:置灰、禁编辑、标"已入库"、无试听
                e_t.config(state="disabled", disabledforeground="#9aa0a6")
                e_a.config(state="disabled", disabledforeground="#9aa0a6")
                tk.Label(rf, text="已入库", bg=base, fg="#9aa0a6", width=6).grid(row=0, column=3, padx=(2, 8))
            else:
                tk.Label(rf, text=cand["source"], bg=base, fg="#888888", width=4).grid(row=0, column=3, padx=(2, 8))
                tk.Button(rf, text="▶ 试听", command=lambda c=cand: _preview_kge(c)).grid(row=0, column=4, padx=(2, 6))
            # 伴奏/原唱结构固定、自动判别即准,不提供交换按钮(确认一次即可)。
            rows.append({"cand": cand, "chk": chk, "e_title": e_t, "e_artist": e_a,
                         "in_library": in_lib, "rf": rf})

        def _match(r, kw):
            return (not kw) or (kw in (r["e_title"].get() or "").lower()
                                or kw in (r["e_artist"].get() or "").lower())

        def _apply_filter(*_):
            """检索:隐藏不匹配的行、按行序重排可见行(保留勾选/改名,不重建行)。"""
            kw = search_var.get().strip().lower()
            for r in rows:
                r["rf"].pack_forget()
            for r in rows:                          # rows 已是排序后的顺序,依序 pack 保持有序
                if _match(r, kw):
                    r["rf"].pack(fill="x")

        def _resort_render():
            """扫描完成后:按缓存时间**倒序**(mtime desc)重排全部结果重渲,再套用当前检索。"""
            _clear_rows()
            items = sorted(st["results"] or [], key=lambda c: c.get("mtime", 0), reverse=True)
            for i, c in enumerate(items):
                _add_row(c, i)
            _apply_filter()

        def _render_new():
            """扫描进行中的增量渲染(扫出一首显示一首,暂按扫描顺序;完成时 _resort_render 再按时间倒序)。"""
            results = st["results"] or []
            kw = search_var.get().strip().lower()
            for i in range(len(rows), len(results)):
                _add_row(results[i], i)
                if kw and not _match(rows[-1], kw):   # 有检索词时新行不匹配即隐藏
                    rows[-1]["rf"].pack_forget()

        _filt_deb = {"id": None}

        def _on_search(*_):
            if _filt_deb["id"]:
                try:
                    root.after_cancel(_filt_deb["id"])
                except Exception:
                    pass
            _filt_deb["id"] = root.after(200, _apply_filter)   # 200ms 防抖
        search_var.trace_add("write", _on_search)

        # ---- 扫描 worker(**只扫选中的那一端**;手机侧慢:adb 拉取 + ffmpeg 转换,边转边显示)----
        def _do_scan(gen, serial, mode):
            def pcb(msg):
                if st["gen"] == gen:
                    st["msg"] = msg

            def add(c):
                if st["gen"] == gen:
                    st["results"].append(c)         # 追加到 _start_scan 建好的同一 list

            try:
                if mode == "phone":
                    if not serial:
                        raise RuntimeError("未选择手机设备(先连上手机或用『扫码连接』)")
                    st["msg"] = "扫描手机全民K歌…"
                    mobile_import.scan_phone(serial, progress_cb=pcb, on_candidate=add)
                else:
                    st["msg"] = "扫描电脑 WeSing 缓存…"
                    for c in library.scan_pc():
                        add(c)
                if st["gen"] == gen:
                    st["phase"] = "done"
            except Exception as e:                   # 异常:已铺的部分结果保留
                if st["gen"] == gen:
                    st["error"], st["phase"] = str(e), "done"

        def _start_scan():
            mode = scan_mode.get()
            st["gen"] += 1
            st.update(phase="scanning", msg="准备…", results=[], error=None)
            st["serial"] = dev_map.get(dev_var.get())
            _clear_rows(); canvas.yview_moveto(0)
            prog.pack(fill="x", padx=12, pady=4, before=body); prog.start(12)
            confirm_btn.config(state="disabled"); rescan_btn.config(state="disabled")
            threading.Thread(target=_do_scan, args=(st["gen"], st["serial"], mode), daemon=True).start()

        def _poll():
            _render_new()                            # 扫出一首渲一首
            n = len(st["results"] or [])
            if st["phase"] == "scanning":
                status_lbl.config(text="扫描中… %s(已发现 %d 首)" % (st.get("msg", ""), n), fg="#666666")
            elif st["phase"] == "done":
                prog.stop(); prog.pack_forget()
                confirm_btn.config(state="normal"); rescan_btn.config(state="normal")
                _resort_render()                     # 扫完按缓存时间倒序重排 + 套用检索
                if st["error"]:
                    status_lbl.config(text="⚠ 扫描失败:%s%s" % (
                        st["error"], "(已列出已转换的)" if n else ""), fg="#c0392b")
                elif n:
                    new_n = sum(1 for c in (st["results"] or []) if not c.get("in_library"))
                    status_lbl.config(
                        text="扫描到 %d 首(灰色=已入库,%d 首新歌);勾选要导入的,可改歌名/原唱,确认入库。" % (n, new_n),
                        fg="#666666")
                else:
                    status_lbl.config(text="没扫到歌。", fg="#666666")
                st["phase"] = "idle"
            elif st.get("op_msg"):                   # 入库/试听的转换进度(手机音频延迟转码)
                status_lbl.config(text=st["op_msg"], fg="#666666")
            root.after(150, _poll)

        # ---- 确认入库(后台线程做拷贝,完成后关窗)----
        def _confirm():
            picked = [r for r in rows if r["chk"].get() and not r.get("in_library")]
            if not picked:
                root.destroy(); return
            confirm_btn.config(state="disabled"); rescan_btn.config(state="disabled")
            status_lbl.config(text="正在入库 %d 首…" % len(picked), fg="#666666")
            st["import_done"] = None

            def _work():
                n = 0
                for i, r in enumerate(picked):
                    cand = r["cand"]
                    head = "入库 %d/%d:%s " % (i + 1, len(picked), _row_title(r) or cand["mid"])
                    try:
                        if cand.get("needs_convert"):   # 手机新歌:入库时才解密转码(重活)
                            st["op_msg"] = head + "解密转码中…"
                            mobile_import.convert_phone_song(
                                cand, progress_cb=lambda m, h=head: st.update(op_msg=h + m.strip()))
                        else:
                            st["op_msg"] = head
                        library.import_candidate(cand, _row_title(r), r["e_artist"].get())
                        n += 1
                    except Exception as e:
                        print("[SCAN] 入库失败 %s: %s" % (cand["mid"], e))
                try:
                    _on_lib_change()               # 刷托盘 + 推手机 + 推歌单
                except Exception:
                    pass
                st["op_msg"] = None
                st["import_done"] = n
            threading.Thread(target=_work, daemon=True).start()

            def _wait():
                if st["import_done"] is None:
                    root.after(150, _wait); return
                root.destroy()
            root.after(150, _wait)

        confirm_btn.config(command=_confirm)
        rescan_btn.config(command=_start_scan)

        # 设备下拉(默认选第一台;扫码连上后选中新设备);换设备即重扫
        def _reload_devices(select_serial=None):
            devs = mobile_import.list_devices()
            dev_map.clear()
            if devs:
                for d in devs:
                    dev_map[mobile_import.device_label(d)] = d
                labels = list(dev_map.keys())
                dev_cb["values"] = labels
                dev_var.set(next((l for l, s in dev_map.items() if s == select_serial), labels[0]))
            else:
                dev_cb["values"] = [NO_DEV]; dev_var.set(NO_DEV)

        def _on_paired(serial):           # 扫码配对成功:切手机模式、刷新设备、选中新机、重扫
            scan_mode.set("phone"); _on_mode()
            _reload_devices(select_serial=serial)
            _start_scan()

        _reload_devices()
        # 换设备只在手机模式下自动重扫(电脑模式与设备无关)
        dev_cb.bind("<<ComboboxSelected>>", lambda e: scan_mode.get() == "phone" and _start_scan())

        _poll()
        _on_mode()                        # 初始:电脑模式(禁用设备行)+ 相应提示,不自动扫描

        # ================= QQ（无音准）页签(**延迟构建**)=================
        # 只在用户第一次切到 QQ 页签时才搭这套控件 + 起 _qpoll 轮询,并触发 `import qqmusic_import`
        # (已在启动时后台预热 → 此处瞬时命中)。窗口打开只需先把 K歌 页签建好,即时可见、不再卡。
        _qq_built = {"done": False}

        def _build_qq_tab():
            if _qq_built["done"]:
                return
            _qq_built["done"] = True
            _build_qq_tab_impl()

        def _build_qq_tab_impl():
            import base64 as _b64
            try:
                import qqmusic_import
            except Exception as _qe:        # 依赖没装好时降级:页签可见但提示
                qqmusic_import = None
                print("[QQ] qqmusic_import 导入失败:", _qe)

            qst = {"phase": "idle", "msg": None, "results": None, "error": None, "gen": 0,
               "qr_png": None, "login_msg": None, "login_done": None, "import_done": None,
               "dl_pct": None}   # dl_pct:None=不确定(转圈);0-100=下载百分比(确定进度条)
            qrows = []

            qtop = tk.Frame(qqf); qtop.pack(fill="x", padx=10, pady=(10, 4))
            qlogin_lbl = tk.Label(qtop, text="", fg="#666666"); qlogin_lbl.pack(side="left")
            qtype = ttk.Combobox(qtop, values=["QQ", "微信", "QQ音乐App"], state="readonly", width=9)
            qtype.set("QQ")
            qlogin_btn = tk.Button(qtop, text="登录")
            qlogout_btn = tk.Button(qtop, text="退出登录")
            qsearch_var = tk.StringVar(master=root)
            qsearch_entry = tk.Entry(qtop, textvariable=qsearch_var, width=20)
            qsearch_btn = tk.Button(qtop, text="搜索")

            qqr_lbl = tk.Label(qqf); qqr_ref = {"img": None}    # 登录时显示二维码
            qstatus = tk.Label(qqf, text="", anchor="w", fg="#666666"); qstatus.pack(fill="x", padx=12)
            qprog = ttk.Progressbar(qqf, mode="indeterminate")

            qbody = tk.Frame(qqf); qbody.pack(fill="both", expand=True, padx=(10, 0), pady=4)
            qcanvas = tk.Canvas(qbody, highlightthickness=0)
            qvsb = ttk.Scrollbar(qbody, orient="vertical", command=qcanvas.yview)
            qvsb.pack(side="right", fill="y"); qcanvas.pack(side="left", fill="both", expand=True)
            qcanvas.configure(yscrollcommand=qvsb.set)
            qinner = tk.Frame(qcanvas, bg="#ffffff")
            qwin = qcanvas.create_window((0, 0), window=qinner, anchor="nw")
            qinner.bind("<Configure>", lambda e: qcanvas.configure(scrollregion=qcanvas.bbox("all")))
            qcanvas.bind("<Configure>", lambda e: qcanvas.itemconfig(qwin, width=e.width))

            def _qwheel(e):
                if qinner.winfo_height() <= qcanvas.winfo_height():
                    qcanvas.yview_moveto(0); return
                qcanvas.yview_scroll(int(-e.delta / 120), "units")
            qcanvas.bind("<Enter>", lambda e: qcanvas.bind_all("<MouseWheel>", _qwheel))
            qcanvas.bind("<Leave>", lambda e: qcanvas.unbind_all("<MouseWheel>"))

            qbar = tk.Frame(qqf); qbar.pack(fill="x", padx=10, pady=(2, 10))
            tk.Button(qbar, text="全选", width=6,
                      command=lambda: [r["chk"].set(True) for r in qrows if not r.get("in_library")]).pack(side="left")
            tk.Button(qbar, text="全不选", width=7,
                      command=lambda: [r["chk"].set(False) for r in qrows if not r.get("in_library")]).pack(side="left", padx=4)
            qconfirm_btn = tk.Button(qbar, text="确认入库"); qconfirm_btn.pack(side="right")
            tk.Button(qbar, text="关闭", width=7, command=root.destroy).pack(side="right", padx=6)

            def _qclear():
                for w in qinner.winfo_children():
                    w.destroy()
                qrows.clear()

            def _qadd_row(cand, idx):
                base = C_EVEN if idx % 2 == 0 else C_ODD
                in_lib = cand.get("in_library")
                rf = tk.Frame(qinner, bg=base); rf.pack(fill="x")
                rf.columnconfigure(1, weight=1, minsize=150)
                chk = tk.BooleanVar(master=root, value=False)   # 默认全不选;master=root 防多窗口串解释器
                cb = tk.Checkbutton(rf, variable=chk, bg=base, activebackground=base)
                if in_lib:
                    cb.config(state="disabled")          # 已入库:禁止勾选
                cb.grid(row=0, column=0, padx=(6, 2))
                e_t = tk.Entry(rf, width=20); e_t.insert(0, cand.get("title") or "")
                e_t.grid(row=0, column=1, sticky="we", padx=2, pady=4)
                e_a = tk.Entry(rf, width=10); e_a.insert(0, cand.get("artist") or "")
                e_a.grid(row=0, column=2, padx=2)
                iv = cand.get("interval", 0) or 0
                tk.Label(rf, text="%d:%02d" % (iv // 60, iv % 60), bg=base, fg="#999999", width=5).grid(row=0, column=3, padx=2)
                if in_lib:                               # 已入库:置灰、禁编辑、标"已入库"、无试听
                    e_t.config(state="disabled", disabledforeground="#9aa0a6")
                    e_a.config(state="disabled", disabledforeground="#9aa0a6")
                    tk.Label(rf, text="已入库", bg=base, fg="#9aa0a6", width=6).grid(row=0, column=4, padx=(2, 6))
                else:
                    tk.Label(rf, text="QQ", bg=base, fg="#888888", width=3).grid(row=0, column=4, padx=(2, 4))
                    tk.Button(rf, text="▶ 试听", command=lambda c=cand: _qpreview(c)).grid(row=0, column=5, padx=(2, 6))
                qrows.append({"cand": cand, "chk": chk, "e_title": e_t, "e_artist": e_a, "in_library": in_lib})

            def _qrender_new():
                results = qst["results"] or []
                for i in range(len(qrows), len(results)):
                    _qadd_row(results[i], i)

            def _refresh_login_ui():
                if qqmusic_import is None:
                    qlogin_lbl.config(text="✗ QQ模块未就绪(装 qqmusic-api-python)", fg="#c0392b")
                    qlogin_btn.config(state="disabled"); qsearch_btn.config(state="disabled")
                    qlogout_btn.config(state="disabled"); return
                if qqmusic_import.logged_in():
                    qlogin_lbl.config(text="✓ 已登录", fg="#27ae60"); qlogin_btn.config(text="重新登录")
                    qlogout_btn.config(state="normal")
                else:
                    qlogin_lbl.config(text="○ 未登录", fg="#c0392b"); qlogin_btn.config(text="登录")
                    qlogout_btn.config(state="disabled")

            def _do_login():
                lt = {"QQ": "QQ", "微信": "WX", "QQ音乐App": "MOBILE"}.get(qtype.get(), "QQ")
                try:
                    ok = qqmusic_import.login_qr(
                        lt, on_qr=lambda p: qst.update(qr_png=p),
                        progress=lambda s: qst.update(login_msg=s), timeout=180)
                    qst["login_done"] = bool(ok)
                except Exception as e:
                    qst["login_msg"] = "登录出错:%s" % e; qst["login_done"] = False

            def _start_login():
                if qqmusic_import is None:
                    return
                qst.update(qr_png=None, login_msg="生成二维码…", login_done=None)
                qprog.pack(fill="x", padx=12, pady=4, before=qbody); qprog.start(12)
                qlogin_btn.config(state="disabled")
                threading.Thread(target=_do_login, daemon=True).start()

            def _do_search(gen, kw):
                try:
                    res = qqmusic_import.search(kw, num=20)
                    if qst["gen"] == gen:
                        qst["results"] = res; qst["phase"] = "done"
                except Exception as e:
                    if qst["gen"] == gen:
                        qst["error"] = str(e); qst["phase"] = "done"

            def _start_search():
                if qqmusic_import is None:
                    return
                if not qqmusic_import.logged_in():
                    qstatus.config(text="请先登录 QQ音乐（点上方“登录”扫码）", fg="#c0392b"); return
                kw = qsearch_var.get().strip()
                if not kw:
                    return
                qst["gen"] += 1; qst.update(phase="searching", results=[], error=None)
                _qclear(); qcanvas.yview_moveto(0)
                qprog.pack(fill="x", padx=12, pady=4, before=qbody); qprog.start(12)
                qconfirm_btn.config(state="disabled"); qsearch_btn.config(state="disabled")
                threading.Thread(target=_do_search, args=(qst["gen"], kw), daemon=True).start()

            def _qconfirm():
                picked = [r for r in qrows if r["chk"].get() and not r.get("in_library")]
                if not picked:
                    return
                qconfirm_btn.config(state="disabled"); qsearch_btn.config(state="disabled")
                qst["import_done"] = None
                qprog.pack(fill="x", padx=12, pady=4, before=qbody); qprog.start(12)

                def _work():
                    n = 0
                    for i, r in enumerate(picked):
                        c = r["cand"]
                        head = "入库 %d/%d " % (i + 1, len(picked))
                        try:
                            qst.update(msg=head + r["e_title"].get(), dl_pct=None)
                            qqmusic_import.prepare(
                                c,
                                progress_cb=lambda m, h=head: qst.update(msg=h + m, dl_pct=None),
                                pct_cb=lambda label, pct, h=head: qst.update(msg="%s%s %d%%" % (h, label, pct), dl_pct=pct))
                            library.import_candidate(c, r["e_title"].get().strip(), r["e_artist"].get().strip())
                            n += 1
                        except Exception as e:
                            print("[QQ] 入库失败 %s: %s" % (c.get("mid"), e))
                    try:
                        _on_lib_change()
                    except Exception:
                        pass
                    qst["import_done"] = n
                threading.Thread(target=_work, daemon=True).start()

            def _qpreview(cand):
                """QQ 试听:worker 里**只下伴奏一轨**(preview=True)到暂存 → 复用 _preview 子进程播放
                (系统默认输出=自己听,不进直播)。之后勾选入库会再下全量覆盖。"""
                if qqmusic_import is None:
                    return

                def _work():
                    try:
                        qst.update(msg="试听:%s" % (cand.get("title") or ""), dl_pct=None)
                        qqmusic_import.prepare(
                            cand, preview=True,
                            progress_cb=lambda m: qst.update(msg="试听 " + m, dl_pct=None),
                            pct_cb=lambda label, pct: qst.update(msg="试听 %s %d%%" % (label, pct), dl_pct=pct))
                        qst.update(msg="试听中(伴奏,系统默认输出、不进直播)。", dl_pct=None)
                        _preview(cand)                    # src_root=QQ_STAGING_DIR,已备好伴奏
                    except Exception as e:
                        qst.update(msg="试听失败:%s" % e, dl_pct=None)
                threading.Thread(target=_work, daemon=True).start()

            def _qpoll():
                # 进度条:显示时按 dl_pct 切「转圈(不确定)」/「百分比(确定)」
                if qprog.winfo_ismapped():
                    pct = qst.get("dl_pct")
                    if pct is None:
                        if str(qprog["mode"]) != "indeterminate":
                            qprog.config(mode="indeterminate"); qprog.start(12)
                    else:
                        if str(qprog["mode"]) != "determinate":
                            qprog.stop(); qprog.config(mode="determinate", maximum=100)
                        qprog["value"] = pct
                if qst.get("qr_png") is not None:
                    png = qst["qr_png"]; qst["qr_png"] = None
                    try:
                        img = tk.PhotoImage(master=root, data=_b64.b64encode(png).decode("ascii"))
                        qqr_ref["img"] = img; qqr_lbl.config(image=img)
                        if not qqr_lbl.winfo_ismapped():
                            qqr_lbl.pack(pady=4, before=qstatus)
                    except Exception:            # Tk 不认该 PNG 时退回:存临时文件用系统看图器打开
                        try:
                            import tempfile
                            fp = os.path.join(tempfile.gettempdir(), "qq_login_qr.png")
                            open(fp, "wb").write(png); os.startfile(fp)
                        except Exception:
                            pass
                if qst.get("login_msg"):
                    qstatus.config(text="登录:%s" % qst["login_msg"], fg="#666666"); qst["login_msg"] = None
                if qst.get("login_done") is not None:
                    done = qst["login_done"]; qst["login_done"] = None
                    qprog.stop(); qprog.pack_forget(); qprog.config(mode="indeterminate"); qst["dl_pct"] = None
                    qqr_lbl.pack_forget(); qqr_ref["img"] = None
                    qlogin_btn.config(state="normal"); _refresh_login_ui()
                    qstatus.config(text="登录成功,可以搜索了。" if done else "登录未完成(超时/取消/拒绝),可重试。",
                                   fg="#27ae60" if done else "#c0392b")
                _qrender_new()
                if qst["phase"] == "searching":
                    qstatus.config(text="搜索中…", fg="#666666")
                elif qst["phase"] == "done":
                    qprog.stop(); qprog.pack_forget(); qprog.config(mode="indeterminate"); qst["dl_pct"] = None
                    qconfirm_btn.config(state="normal"); qsearch_btn.config(state="normal")
                    n = len(qst["results"] or [])
                    if qst.get("error"):
                        qstatus.config(text="搜索失败:%s" % qst["error"], fg="#c0392b")
                    elif n:
                        new_n = sum(1 for c in (qst["results"] or []) if not c.get("in_library"))
                        qstatus.config(text="搜到 %d 首(灰色=已入库,%d 首可导入);勾选新歌,确认入库(下载解码较慢)。" % (n, new_n),
                                       fg="#666666")
                    else:
                        qstatus.config(text="没有结果。", fg="#666666")
                    qst["phase"] = "idle"; qst["error"] = None
                if qst.get("msg"):
                    qstatus.config(text=qst["msg"], fg="#666666"); qst["msg"] = None
                if qst.get("import_done") is not None:
                    nn = qst["import_done"]; qst["import_done"] = None
                    qprog.stop(); qprog.pack_forget(); qprog.config(mode="indeterminate"); qst["dl_pct"] = None
                    qconfirm_btn.config(state="normal"); qsearch_btn.config(state="normal")
                    qstatus.config(text="已入库 %d 首(切到 K歌曲库管理可见)。" % nn, fg="#27ae60")
                root.after(150, _qpoll)

            qlogin_btn.config(command=_start_login)
            qlogout_btn.config(command=lambda: (qqmusic_import and qqmusic_import.logout(), _refresh_login_ui()))
            qsearch_btn.config(command=_start_search)
            qconfirm_btn.config(command=_qconfirm)      # ← 之前漏接:确认入库按钮
            qsearch_entry.bind("<Return>", lambda e: _start_search())
            qtype.pack(side="left", padx=4); qlogin_btn.pack(side="left")
            qlogout_btn.pack(side="left", padx=4)
            qsearch_btn.pack(side="right"); qsearch_entry.pack(side="right", padx=6)
            tk.Label(qtop, text="搜索:", fg="#666666").pack(side="right")
            _refresh_login_ui(); _qpoll()

        # 切到 QQ 页签(index 1)时才构建它;之后重复切换不再重建
        nb.bind("<<NotebookTabChanged>>",
                lambda e: (nb.index(nb.select()) == 1) and _build_qq_tab())

        root.after(120, root.focus_force)
        # 关窗善后(清 _scan_root + 刷托盘)已由 _tk_win_close_guard 接管;无独立 mainloop(用常驻根)

    _ui_post(_win)


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
        global _lib_root
        try:
            import tkinter as tk
            from tkinter import ttk
            # 正常路径:常驻根的 Toplevel(免 tk.Tk() 冷启动)+ close_guard 善后。
            # selftest:仍用独立 tk.Tk()+自带 mainloop(headless 自检,不依赖常驻 UI 线程)。
            root = tk.Tk() if selftest else tk.Toplevel(_ui_root)
            root.title("K歌曲库管理")
            root.geometry("720x500")
            _lib_root = root
            if not selftest:
                def _on_closed():
                    global _lib_root
                    _lib_root = None
                    refresh_tray()
                _tk_win_close_guard(root, _on_closed)
                refresh_tray()         # 反映"已打开"的托盘勾选(selftest 不碰托盘)
            root.attributes("-topmost", True)

            top = tk.Frame(root)
            top.pack(fill="x", padx=10, pady=(10, 2))
            tk.Label(top, text="搜索:").pack(side="left")
            q = tk.StringVar(master=root)
            tk.Entry(top, textvariable=q).pack(side="left", fill="x", expand=True, padx=6)
            count_var = tk.StringVar(master=root)
            tk.Label(top, textvariable=count_var).pack(side="left")

            # 筛选 + 排序(选择即刷新)
            bar2 = tk.Frame(root)
            bar2.pack(fill="x", padx=10, pady=(2, 0))
            tk.Label(bar2, text="筛选:").pack(side="left")
            f_var = tk.StringVar(master=root, value="全部")
            cb_f = ttk.Combobox(bar2, textvariable=f_var, state="readonly", width=9,
                                values=("全部", "只看Live", "排除Live"))
            cb_f.pack(side="left", padx=(4, 14))
            tk.Label(bar2, text="排序:").pack(side="left")
            s_var = tk.StringVar(master=root, value="最新入库")
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
            def _on_wheel(e):                       # 滚轮
                # 内容比视口短就锁顶不滚,防上滚把短内容推下、顶部露白;超出才滚。
                if inner.winfo_height() <= canvas.winfo_height():
                    canvas.yview_moveto(0)
                    return
                canvas.yview_scroll(int(-e.delta / 120), "units")
            # 常驻单根下全进程共用一个解释器,`bind_all` 是应用级的——多窗同开会互相顶掉滚轮绑定。
            # 故与扫描窗一致:鼠标进本 canvas 才 bind_all、离开即 unbind_all,滚轮归属当前悬停窗口。
            canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_wheel))
            canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

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

            def _delete(mid):
                """删除一首:**二次确认弹窗** → 出歌单/队列 + 删库文件 + 刷新列表。不可恢复。"""
                from tkinter import messagebox
                m = library.manifest().get(mid, {})
                name = (m.get("title") or "").strip() or mid
                if not messagebox.askyesno(
                        "删除确认",
                        "确定从曲库删除《%s》?\n将删除该歌的全部文件(伴奏/原唱/歌词等),不可恢复。" % name,
                        icon="warning", parent=root):
                    return
                try:
                    _lib_delete(mid)
                except Exception as e:
                    messagebox.showerror("删除失败", str(e), parent=root)
                    return
                if sel["mid"] == mid:
                    sel["mid"] = None
                refresh()                           # 重算结果集重渲(该行消失)

            def _select(mid):
                sel["mid"] = mid
                for r in rows:
                    r["paint"]()

            def _bump_key(mid, delta, label, holder):
                """曲库管理页 −/+ 调默认调式:夹到 [-12,12],持久化(library.set_key),就地更新标签;
                若这首正在唱(_now_mid)则**实时下发**边调边听。改的是"下次点到这首用的默认调"。"""
                nv = max(-12, min(12, holder["k"] + delta))
                if nv == holder["k"]:
                    return
                holder["k"] = nv
                library.set_key(mid, nv)
                try:
                    label.config(text=_fmt_key(nv))
                except Exception:
                    pass
                if mid == _now_mid:
                    _player_send("key " + str(nv))

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
                var = tk.BooleanVar(master=root, value=(mid in STATE.get("setlist", [])))
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
                # 默认调式:−  <调>  +(左减右加,中间显示当前默认;点即存,正在唱的实时应用)
                kf = tk.Frame(rf, bg=base)
                kf.grid(row=0, column=4, padx=(6, 2))
                kh = {"k": int(m.get("key", 0))}
                tk.Button(kf, text="−", width=2, takefocus=0,
                          command=lambda mid=mid: _bump_key(mid, -1, kl, kh)).pack(side="left")
                kl = tk.Label(kf, text=_fmt_key(kh["k"]), width=4, bg=base,
                              fg="#111111", font=("", 9, "bold"))
                kl.pack(side="left")
                tk.Button(kf, text="+", width=2, takefocus=0,
                          command=lambda mid=mid: _bump_key(mid, +1, kl, kh)).pack(side="left")
                tk.Button(rf, text="编辑", width=5,
                          command=lambda mid=mid: _edit(mid)).grid(
                    row=0, column=5, padx=(6, 2), pady=3)
                tk.Button(rf, text="播放", width=5,
                          command=lambda mid=mid: _play(mid)).grid(
                    row=0, column=6, padx=(2, 2), pady=3)
                tk.Button(rf, text="删除", width=5, fg="#c0392b", activeforeground="#c0392b",
                          command=lambda mid=mid: _delete(mid)).grid(
                    row=0, column=7, padx=(2, 8), pady=3)
                cells = (l_name, l_art, l_time, kf, kl)

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

            # 先让窗壳 + 工具栏立即出图,首批 60 行放到 idle 再渲(避免开窗前同步建 360+ 控件卡首帧)
            root.after_idle(refresh)
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
                root.mainloop()    # selftest 独立根:自带 mainloop 驱动;正常路径用常驻根,不 mainloop
        except Exception as ex:
            print(f"[LIB] 曲库管理窗失败: {ex}")
            if not selftest:
                try:
                    root.destroy()     # 触发 close_guard 善后(gc 恢复 + 清 _lib_root + 刷托盘)
                except Exception:
                    _lib_root = None
                    refresh_tray()

    if selftest:
        threading.Thread(target=_tk_window_thread(_win), daemon=True).start()
    else:
        _ui_post(_win)


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


def _player_load(mid):
    """载入某曲并应用它保存的**默认调式**:player 的 `load` 会归位清调到 0(原调),
    随后按曲库存的默认 key 补下发 `key <n>`(非 0 才发),这样手机点到这首就是调好的调,
    不必每次手动升降。player 端 stdin 单线程 FIFO,load→key 顺序不乱。"""
    _player_send("load " + mid)
    k = library.get_key(mid)
    if k:
        _player_send("key " + str(k))


def k_advance_paused():
    """自然唱完时调用:把下一首装载到**开头并保持暂停**(等主播手动开唱),队空则清空当前曲。
    这样歌曲间歇 BGM 能顶上——手机端"演唱↔BGM 联动"看到演唱停止,**缓冲 2 秒后**自动恢复它
    暂停过的 QQ音乐(发有方向的 `bgm play`);主播按播放开唱下一首时,联动又发 `bgm pause`
    把 BGM 渐弱暂停(缓冲期内开唱则直接取消恢复)。联动可在手机 BGM 悬浮面板一键关闭。"""
    global _now_mid
    if _queue:
        _now_mid = _queue.pop(0)
        _player_load(_now_mid)             # load + 应用曲库存的默认调式
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
        _player_load(_now_mid)
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
    _player_load(mid)
    _player_send("play")
    _sync_queue_state()


def k_enqueue(mid):
    """点歌:入队;若当前空闲则把这首装到**开头并暂停**(不自动开唱),等主播手动开唱。
    与"唱完切下一首暂停"(k_advance_paused)一致:队列空时新点的第一首也不自动播,
    保持 BGM 顶着,由主播按播放键开唱。"""
    global _now_mid
    if not mid:
        return
    library.bump_play(mid)                 # 点歌次数 +1(手机端点歌列表默认按次数倒序)
    _queue.append(mid)
    if _now_mid is None:
        _now_mid = _queue.pop(0)
        _player_load(_now_mid)             # load + 应用曲库存的默认调式
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


def _lib_delete(mid):
    """曲库管理窗「删除」:先把该 mid 从歌单/队列摘掉,再删库文件+清单+刷新推送。
    正在唱的歌(_now_mid)已把音频载进播放器内存,删文件不影响当前这遍(只是不再在库/队列)。"""
    if mid in STATE.get("setlist", []):
        set_setlist_member(mid, False)          # 出歌单 + 存盘 + 推播放器/手机
    if mid in _queue:
        _queue[:] = [m for m in _queue if m != mid]
        _sync_queue_state()                     # 推队列(手机点歌抽屉)
    library.delete(mid)                         # 删文件 + 清单 + _on_lib_change(刷托盘 + 推库列表)


def _open_gift_window(selftest=False):
    """托盘「礼物菜单配置」:选礼物 + 填自定义文字 + 排序,推给播放器绿幕左侧竖排显示。
    - 左:礼物目录(搜索过滤;每行 名 + 抖币价 +「＋加入」)。目录 1373 项,只渲染过滤后前 120 条,
      靠搜索缩小(不做全量分页——礼物按名找即可)。「刷新目录」后台重抓 gifts.fetch_catalog。
    - 右:已选(=显示顺序);每行 缩略图 + 名 + 自定义文字输入框 + ↑ ↓ 排序 + ✕ 删除。
      缩略图只对已选的几个下载(gifts.icon_path 用到才下),不批量拉 1373 张。
    - 保存:set_gift_config([{id,text}]) → 存盘 + 推播放器 + 推手机。
    独立性同曲库/扫描窗:常驻根 Toplevel + close_guard 善后 + 单实例(_gift_root)。"""

    def _win():
        global _gift_root
        try:
            import tkinter as tk
            from tkinter import ttk, colorchooser
            from PIL import Image, ImageTk

            root = tk.Tk() if selftest else tk.Toplevel(_ui_root)
            root.title("礼物菜单配置")
            root.geometry("720x560")
            _gift_root = root
            if not selftest:
                def _on_closed():
                    global _gift_root
                    _gift_root = None
                    refresh_tray()
                _tk_win_close_guard(root, _on_closed)
                refresh_tray()
            root.attributes("-topmost", True)

            catalog = gifts.fetch_catalog()                 # [{id,name,diamond,icon_url}]
            cat_by_id = {int(it["id"]): it for it in catalog if it.get("id") is not None}
            # 工作副本:已选礼物 [{id,name,text}](名取自目录,缺失回退保存值)
            sel = []
            for g in STATE.get("gifts", []):
                gid = int(g["id"])
                nm = (cat_by_id.get(gid) or {}).get("name", "")
                sel.append({"id": gid, "name": nm, "text": str(g.get("text", ""))})
            thumbs = {}           # id → ImageTk.PhotoImage(防 GC)
            text_vars = {}        # id → StringVar(自定义文字输入)

            # ── 顶部:搜索 + 刷新 + 计数 ──
            top = tk.Frame(root); top.pack(fill="x", padx=10, pady=(10, 4))
            tk.Label(top, text="搜索礼物:").pack(side="left")
            q = tk.StringVar(master=root)
            tk.Entry(top, textvariable=q, width=18).pack(side="left", padx=6)
            cnt = tk.StringVar(master=root)
            tk.Label(top, textvariable=cnt, fg="#888888").pack(side="left", padx=6)

            def _refresh_catalog():
                def _work():
                    gifts.fetch_catalog(refresh=True)
                    try:
                        root.after(0, lambda: (_reload_cat(), None))
                    except Exception:
                        pass
                threading.Thread(target=_work, daemon=True).start()
            tk.Button(top, text="刷新目录", command=_refresh_catalog).pack(side="right")

            body = tk.Frame(root); body.pack(fill="both", expand=True, padx=10, pady=4)
            # 左:目录列表(Canvas 滚动)
            left = tk.LabelFrame(body, text="礼物目录(点「＋加入」)")
            left.pack(side="left", fill="both", expand=True)
            lc = tk.Canvas(left, highlightthickness=0, width=320)
            lvsb = ttk.Scrollbar(left, orient="vertical", command=lc.yview)
            lvsb.pack(side="right", fill="y"); lc.pack(side="left", fill="both", expand=True)
            l_inner = tk.Frame(lc, bg="#ffffff")
            l_wid = lc.create_window((0, 0), window=l_inner, anchor="nw")
            l_inner.bind("<Configure>", lambda e: lc.configure(scrollregion=lc.bbox("all")))
            lc.bind("<Configure>", lambda e: lc.itemconfig(l_wid, width=e.width))
            lc.bind("<Enter>", lambda e: lc.bind_all(
                "<MouseWheel>", lambda ev: lc.yview_scroll(int(-ev.delta / 120), "units")))
            lc.bind("<Leave>", lambda e: lc.unbind_all("<MouseWheel>"))

            # 右:已选(顺序 + 文字 + 排序 + 删)
            right = tk.LabelFrame(body, text="已选(=显示顺序,上→下)")
            right.pack(side="right", fill="both", expand=True, padx=(8, 0))
            rc = tk.Canvas(right, highlightthickness=0, width=360)
            rvsb = ttk.Scrollbar(right, orient="vertical", command=rc.yview)
            rvsb.pack(side="right", fill="y"); rc.pack(side="left", fill="both", expand=True)
            r_inner = tk.Frame(rc, bg="#ffffff")
            r_wid = rc.create_window((0, 0), window=r_inner, anchor="nw")
            r_inner.bind("<Configure>", lambda e: rc.configure(scrollregion=rc.bbox("all")))
            rc.bind("<Configure>", lambda e: rc.itemconfig(r_wid, width=e.width))
            rc.bind("<Enter>", lambda e: rc.bind_all(
                "<MouseWheel>", lambda ev: rc.yview_scroll(int(-ev.delta / 120), "units")))
            rc.bind("<Leave>", lambda e: rc.unbind_all("<MouseWheel>"))

            def _thumb(gid):
                """已选项缩略图(40px);下载/解码失败返回 None(只显文字)。缓存防 GC。"""
                if gid in thumbs:
                    return thumbs[gid]
                url = (cat_by_id.get(gid) or {}).get("icon_url", "")
                p = gifts.icon_path(gid, url)
                if not p:
                    return None
                try:
                    im = Image.open(p).convert("RGBA")
                    im.thumbnail((40, 40))
                    bg = Image.new("RGBA", (40, 40), (255, 255, 255, 0))
                    bg.paste(im, ((40 - im.width) // 2, (40 - im.height) // 2), im)
                    ph = ImageTk.PhotoImage(bg)
                    thumbs[gid] = ph
                    return ph
                except Exception:
                    return None

            def _sync_text_vars():
                for it in sel:
                    if it["id"] in text_vars:
                        it["text"] = text_vars[it["id"]].get()

            def _add(gid):
                _sync_text_vars()
                if any(s["id"] == gid for s in sel):
                    return
                nm = (cat_by_id.get(gid) or {}).get("name", "")
                sel.append({"id": gid, "name": nm, "text": ""})
                _render_sel()

            def _remove(gid):
                _sync_text_vars()
                sel[:] = [s for s in sel if s["id"] != gid]
                _render_sel()

            def _swap(i, j):
                _sync_text_vars()
                if 0 <= i < len(sel) and 0 <= j < len(sel):
                    sel[i], sel[j] = sel[j], sel[i]
                    _render_sel()

            def _render_sel():
                for w in r_inner.winfo_children():
                    w.destroy()
                text_vars.clear()
                for i, it in enumerate(sel):
                    gid = it["id"]
                    rf = tk.Frame(r_inner, bg="#ffffff", bd=1, relief="solid")
                    rf.pack(fill="x", padx=4, pady=3)
                    ph = _thumb(gid)
                    if ph is not None:
                        tk.Label(rf, image=ph, bg="#ffffff").grid(row=0, column=0, rowspan=2, padx=4, pady=4)
                    else:
                        tk.Label(rf, text="?", width=4, bg="#eeeeee").grid(
                            row=0, column=0, rowspan=2, padx=4, pady=4)
                    tk.Label(rf, text=it["name"] or "(礼物%d)" % gid, anchor="w",
                             bg="#ffffff", fg="#111111", font=("", 9, "bold")).grid(
                        row=0, column=1, sticky="w")
                    tv = tk.StringVar(master=root, value=it["text"])
                    text_vars[gid] = tv
                    ef = tk.Frame(rf, bg="#ffffff"); ef.grid(row=1, column=1, sticky="w")
                    tk.Label(ef, text="文字:", bg="#ffffff", fg="#666666").pack(side="left")
                    tk.Entry(ef, textvariable=tv, width=16).pack(side="left")
                    bf = tk.Frame(rf, bg="#ffffff"); bf.grid(row=0, column=2, rowspan=2, padx=4)
                    tk.Button(bf, text="↑", width=2, takefocus=0,
                              command=lambda i=i: _swap(i, i - 1)).pack(side="left")
                    tk.Button(bf, text="↓", width=2, takefocus=0,
                              command=lambda i=i: _swap(i, i + 1)).pack(side="left")
                    tk.Button(bf, text="✕", width=2, fg="#c0392b", takefocus=0,
                              command=lambda gid=gid: _remove(gid)).pack(side="left", padx=(4, 0))
                rc.yview_moveto(0)

            def _render_cat():
                for w in l_inner.winfo_children():
                    w.destroy()
                kw = q.get().strip().lower()
                items = [it for it in catalog
                         if not kw or kw in (it.get("name", "").lower())]
                shown = items[:120]
                cnt.set("共 %d,显示 %d" % (len(items), len(shown)))
                for it in shown:
                    gid = int(it["id"])
                    rf = tk.Frame(l_inner, bg="#ffffff")
                    rf.pack(fill="x")
                    tk.Button(rf, text="＋加入", width=6, takefocus=0,
                              command=lambda gid=gid: _add(gid)).pack(side="left", padx=4, pady=2)
                    tk.Label(rf, text=it.get("name", "") or ("礼物%d" % gid), anchor="w",
                             bg="#ffffff", fg="#111111").pack(side="left")
                    tk.Label(rf, text="  %d 抖币" % int(it.get("diamond", 0)), anchor="e",
                             bg="#ffffff", fg="#999999").pack(side="right", padx=6)

            def _reload_cat():
                nonlocal catalog, cat_by_id
                catalog = gifts.fetch_catalog()
                cat_by_id = {int(it["id"]): it for it in catalog if it.get("id") is not None}
                for it in sel:                       # 名字可能刷新
                    it["name"] = (cat_by_id.get(it["id"]) or {}).get("name", it["name"])
                _render_cat(); _render_sel()

            _deb = {"id": None}

            def _on_query(*_a):
                if _deb["id"] is not None:
                    try:
                        root.after_cancel(_deb["id"])
                    except Exception:
                        pass
                _deb["id"] = root.after(200, _render_cat)
            q.trace_add("write", _on_query)

            # ── 底部:提示 + 保存/关闭(样式调节已移到托盘「绿幕样式控制」窗)──
            bar = tk.Frame(root); bar.pack(fill="x", padx=10, pady=(2, 10))
            tk.Label(bar, text="保存后即推送到播放器绿幕(左侧竖排);位置/样式在「绿幕样式控制」窗与播放器内调。",
                     fg="#888888", wraplength=360, justify="left").pack(side="left")

            def _save():
                _sync_text_vars()
                set_gift_config([{"id": s["id"], "text": s["text"]} for s in sel])
                if not selftest:
                    root.destroy()
            tk.Button(bar, text="关闭", width=8, command=root.destroy).pack(side="right")
            tk.Button(bar, text="保存", width=10, command=_save).pack(side="right", padx=6)

            root.after_idle(lambda: (_render_cat(), _render_sel()))
            root.after(120, root.focus_force)
            if selftest:
                def _auto():
                    print("[GIFTWIN-TEST] catalog=%d sel=%d" % (len(catalog), len(sel)), flush=True)
                    if catalog:
                        _add(int(catalog[0]["id"]))
                        print("[GIFTWIN-TEST] after add sel=%d" % len(sel), flush=True)
                    root.after(300, root.destroy)
                root.after(600, _auto)
                root.mainloop()
        except Exception as ex:
            print(f"[GIFT] 礼物配置窗失败: {ex}")
            if not selftest:
                try:
                    root.destroy()
                except Exception:
                    _gift_root = None
                    refresh_tray()

    if selftest:
        threading.Thread(target=_tk_window_thread(_win), daemon=True).start()
    else:
        _ui_post(_win)


def _open_style_window(selftest=False):
    """托盘「绿幕样式控制」:统一调 礼物菜单 / 歌单 / 歌词 三块的样式——字体大小 / 描边粗细 /
    描边颜色 / 左右边距(歌单歌词=居中带宽度)/ 礼物尺寸间距。滑块拖动 live 预览、松手/选色即存
    (`set_style`),经 IPC 推播放器 + 缓存。歌单竖直位置在**播放器里鼠标拖动**(本窗不含);歌词固定底部。"""

    def _win():
        global _style_root
        try:
            import tkinter as tk
            from tkinter import colorchooser
            root = tk.Tk() if selftest else tk.Toplevel(_ui_root)
            root.title("绿幕样式控制")
            root.geometry("470x560")
            _style_root = root
            if not selftest:
                def _on_closed():
                    global _style_root
                    _style_root = None
                    refresh_tray()
                _tk_win_close_guard(root, _on_closed)
                refresh_tray()
            root.attributes("-topmost", True)

            def slider_row(parent, label, key, lo, hi, res, is_pct=False):
                fr = tk.Frame(parent); fr.pack(fill="x", padx=8, pady=2)
                tk.Label(fr, text=label, width=8, anchor="w").pack(side="left")
                cur = STATE.get(key)
                vlbl = tk.Label(fr, width=5, text=("%d%%" % round(cur * 100)) if is_pct
                                else (("%.1f" % cur) if res < 1 else str(int(cur))))
                vlbl.pack(side="right")

                def _on(v, key=key, is_pct=is_pct, res=res, vlbl=vlbl):
                    x = float(v)
                    if is_pct:
                        vlbl.config(text="%d%%" % int(x)); set_style(key, x / 100.0, save=False)
                    else:
                        vlbl.config(text=("%.1f" % x) if res < 1 else str(int(x)))
                        set_style(key, x, save=False)
                s = tk.Scale(fr, from_=lo, to=hi, orient="horizontal", resolution=res,
                             showvalue=False, command=_on)
                s.set(round(cur * 100) if is_pct else cur)
                s.pack(side="left", fill="x", expand=True, padx=6)
                s.bind("<ButtonRelease-1>", lambda e, s=s, key=key, is_pct=is_pct:
                       set_style(key, (s.get() / 100.0 if is_pct else s.get()), save=True))

            def color_row(parent, label, key):
                fr = tk.Frame(parent); fr.pack(fill="x", padx=8, pady=(2, 4))
                tk.Label(fr, text=label, width=8, anchor="w").pack(side="left")
                hold = {"v": str(STATE.get(key, "#000000"))}
                sw = tk.Label(fr, width=4, bg=hold["v"], relief="solid", bd=1)
                sw.pack(side="left", padx=4)

                def _pick(key=key, hold=hold, sw=sw, label=label):
                    res = colorchooser.askcolor(color=hold["v"], parent=root, title=label)
                    if res and res[1]:
                        hold["v"] = res[1]; sw.config(bg=res[1]); set_style(key, res[1], save=True)
                tk.Button(fr, text="选色", command=_pick).pack(side="left")

            g = tk.LabelFrame(root, text="礼物菜单(播放器内鼠标拖动摆位)")
            g.pack(fill="x", padx=10, pady=(8, 2))
            slider_row(g, "尺寸", "gift_scale", 40, 200, 5, is_pct=True)
            slider_row(g, "描边粗细", "gift_outline", 0.0, 3.0, 0.1)
            slider_row(g, "卡片间距", "gift_gap", 0, 24, 1)
            color_row(g, "描边颜色", "gift_color")

            sl = tk.LabelFrame(root, text="歌单(播放器内鼠标竖直拖动位置)")
            sl.pack(fill="x", padx=10, pady=2)
            slider_row(sl, "字体大小", "setlist_pt", 8, 40, 1)
            slider_row(sl, "描边粗细", "setlist_outline", 0.0, 8.0, 0.5)
            slider_row(sl, "左右边距", "setlist_margin", 0, 320, 4)
            color_row(sl, "描边颜色", "setlist_color")

            ly = tk.LabelFrame(root, text="歌词(固定底部,不调位置)")
            ly.pack(fill="x", padx=10, pady=2)
            slider_row(ly, "字体大小", "lyric_pt", 16, 56, 1)
            slider_row(ly, "描边粗细", "lyric_outline", 0.0, 10.0, 0.5)
            slider_row(ly, "左右边距", "lyric_margin", 0, 320, 4)
            color_row(ly, "描边颜色", "lyric_color")

            bar = tk.Frame(root); bar.pack(fill="x", padx=10, pady=8)
            tk.Label(bar, text="拖动即实时预览、松手/选色即存盘。左右边距=离窗口两侧距离(居中带宽度)。",
                     fg="#888888", wraplength=330, justify="left").pack(side="left")
            tk.Button(bar, text="关闭", width=8, command=root.destroy).pack(side="right")

            root.after(120, root.focus_force)
            if selftest:
                def _auto():
                    set_style("lyric_pt", 40); set_style("setlist_margin", 100)
                    print("[STYLEWIN-TEST] lyric_pt=%s setlist_margin=%s" %
                          (STATE.get("lyric_pt"), STATE.get("setlist_margin")), flush=True)
                    root.after(300, root.destroy)
                root.after(600, _auto); root.mainloop()
        except Exception as ex:
            print("[STYLE] 样式窗失败: %s" % ex)
            if not selftest:
                try:
                    root.destroy()
                except Exception:
                    _style_root = None; refresh_tray()

    if selftest:
        threading.Thread(target=_tk_window_thread(_win), daemon=True).start()
    else:
        _ui_post(_win)


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

    def on_toggle_studio(icon, item):
        _toggle_studio_visible()       # Studio One 显隐(与 App 同步,见 _toggle_studio_visible)

    def on_toggle_gifts(icon, item):
        v = not STATE.get("gifts_visible", True)
        STATE["gifts_visible"] = v
        _player_send("gifts_show " + ("1" if v else "0"))
        _save_persist()
        refresh_tray()
        _threadsafe_broadcast()

    def on_edit_performer(icon, item):
        _open_performer_dialog()       # 编辑演唱者(主播名,开头标题卡用)

    # 菜单项文本用可调用;但 pystray-win32 右键弹的是缓存菜单、**不会**在打开时重新求值,
    # 需靠 refresh_tray()→update_menu() 主动刷(见 refresh_tray 注释)。
    menu = pystray.Menu(
        pystray.MenuItem(f"遥控地址: {url}", None, enabled=False),
        pystray.MenuItem(
            lambda i: f"Studio One MIDI: {'已连接' if STATE['studio_connected'] else '未连接'}",
            None, enabled=False),
        # 曲库:勾选式开关——打开=打钩、关闭=去钩,开着再点即关窗(单实例,不重复开)
        pystray.MenuItem(lambda i: f"曲库: {STATE['lib_count']} 首 — 点击管理", _toggle_library_window,
                         checked=lambda i: _lib_root is not None),
        # 扫描导入:同款勾选式开关
        pystray.MenuItem("扫描导入歌曲", _toggle_scan_window,
                         checked=lambda i: _scan_root is not None),
        # 礼物菜单配置:同款勾选式开关(选礼物+自定义文字,推播放器绿幕竖排)
        pystray.MenuItem("礼物菜单配置", _toggle_gift_window,
                         checked=lambda i: _gift_root is not None),
        # 绿幕样式控制:礼物/歌单/歌词的字体大小/描边/颜色/边距统一调
        pystray.MenuItem("绿幕样式控制", _toggle_style_window,
                         checked=lambda i: _style_root is not None),
        # 演唱者(主播名):点击编辑,开头标题卡"演唱:<名>"用
        pystray.MenuItem(lambda i: f"演唱者：{STATE.get('performer', '八门官上')}", on_edit_performer),
        pystray.Menu.SEPARATOR,
        # Studio One 显隐:勾选=当前显示;与 App 的显隐开关双向同步(见 _toggle_studio_visible)
        pystray.MenuItem("Studio One 显示", on_toggle_studio,
                         checked=lambda i: STATE.get("studio_visible", True)),
        pystray.MenuItem(
            "K歌歌词", on_toggle_karaoke,
            checked=lambda i: STATE["player_visible"]),   # 用权威状态(player 经VIS上报),不重读win32免竞态
        pystray.MenuItem("礼物菜单显示", on_toggle_gifts,
                         checked=lambda i: STATE.get("gifts_visible", True)),
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
    # bgm_vol 改由 _start_bgm_vol_poller() 后台线程周期回读(走 pycaw 专线程,不碰主线程 COM),
    # 首次连接也会异步读一次(见 ws_endpoint)。

    start_smtc_reader()   # winrt 子进程(歌名/进度 + 有方向传输控制),与主进程 COM 隔离
    library.start(STATE, _on_lib_change, _on_lib_import)  # 曲库监听(WeSing缓存→永久曲库,入库弹通知)
    start_player()        # 拉起 K歌播放器子进程(隐藏+暂停)
    _start_bgm_vol_poller()   # 周期回读 QQ音乐 音量 → 反向同步到手机(PC 上手动改音量也能同步)
    _start_ui_thread()    # 常驻隐藏 Tk 根 + 单 mainloop:曲库/扫描/改名等窗口都做它的 Toplevel(开窗更快)
    threading.Thread(target=_prewarm_qqmusic, daemon=True).start()  # 预热 QQ 导入(消除首开扫描窗几秒卡顿)
    try:
        _pycaw_exec.submit(_resolve_qq_sessions)   # 后台预热会话缓存:让首次播放/暂停渐变不必现场枚举
    except Exception:
        pass

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
