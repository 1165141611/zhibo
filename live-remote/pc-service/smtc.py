# -*- coding: utf-8 -*-
"""
读取 Windows 系统媒体会话(SMTC):QQ音乐 的歌名/播放位置/总时长/是否在播,并对其做
**有方向**的传输控制(play/pause/next/prev)。

关键设计(2026-07 重构,治"手机控制 QQ音乐 时好时坏 + 正在播的歌不同步"):
  - **按 AUMID 精确锁定 QQ音乐 会话**,不再用 get_current_session()。老写法读"系统当前会话",
    直播时 WeSing / 浏览器 / 直播伴侣 随时会抢走 current session,后果:
      ① 手机上显示的 BGM 歌名/状态变成别的 App(或该 App 无歌名 → 快照 None → STATE 冻结在旧值);
      ② 手机点播放/暂停时,老代码模拟全局媒体键,被 Windows 路由到抢了会话的那个 App,
         QQ音乐 纹丝不动——显示错和控制失效是同一个根因,所以常常一起出现。
    现按 source_app_user_model_id 含 hint(config.QQMUSIC_SMTC_HINT)从**所有**会话里锁定 QQ音乐。
  - **有方向控制**:用会话自己的 try_play/try_pause 而非无方向的全局媒体键,
    彻底消除"方向反打"(媒体键无方向,状态一旦漂移就越点越乱)。
  - 锁不到 QQ音乐 会话时退回 current session(保底,行为不劣于老版)。
"""
import time as _time

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager)

# GlobalSystemMediaTransportControlsSessionPlaybackStatus:
#   0=Closed 1=Opened 2=Changing 3=Stopped 4=Playing 5=Paused
_ST_PLAYING = 4
_ST_NOT_PLAYING = (0, 3, 5)   # Closed / Stopped / Paused —— 确定"没在播"

# QQ音乐 的 SMTC 播放状态**不可靠**:实测它在播放时常年卡在 2(Changing)甚至 1(Opened),
# 只有偶尔正确报 4(Playing)。若照字面 status==4 判断,"正在播的歌"会被判成暂停(bgm_playing=False),
# 手机上状态错、演唱联动/幂等 play 逻辑跟着乱。**兜底:遇到含糊状态(1/2)就看进度是否在推进**
# ——position 在走 = 在播,冻结 = 没播。QQ音乐 的 position 一直可靠更新,是比 status 更硬的信号。
_last = {"pos": None, "at": 0.0, "playing": False}


def _infer_playing(status, pos):
    """综合 SMTC 状态枚举 + 进度推进判断是否在播(治 QQ音乐 status 卡在 Changing)。"""
    if status == _ST_PLAYING:
        return True
    if status in _ST_NOT_PLAYING:
        return False
    # 含糊态(Opened/Changing):看两帧间 position 是否推进
    now = _time.monotonic()
    prev_pos, prev_at = _last["pos"], _last["at"]
    if prev_pos is not None and (now - prev_at) > 0.3 and pos is not None:
        return (pos - prev_pos) > 0.15     # 进度前进 → 在播;冻结/回退 → 没播
    return _last["playing"]                # 信息不足(首帧/间隔太短)→ 维持上次判断

# QQ音乐 会话的 AUMID 匹配片段(小写子串)。smtc_helper 启动时会用 config 覆盖。
_HINT = "qqmusic"


def set_hint(hint):
    """由子进程注入 config 里配置的匹配片段(小写化)。"""
    global _HINT
    if hint:
        _HINT = str(hint).lower()


async def _qq_session(mgr):
    """在所有媒体会话里按 AUMID 锁定 QQ音乐;找不到则退回系统当前会话(保底)。"""
    try:
        sessions = list(mgr.get_sessions())
    except Exception:
        sessions = []
    for s in sessions:
        try:
            aumid = (s.source_app_user_model_id or "").lower()
        except Exception:
            aumid = ""
        if _HINT and _HINT in aumid:
            return s
    try:
        return mgr.get_current_session()
    except Exception:
        return None


async def list_aumids():
    """列出当前所有媒体会话的 AUMID(诊断用:帮作者确认 QQMUSIC_SMTC_HINT 该填什么)。"""
    try:
        mgr = await MediaManager.request_async()
        out = []
        for s in mgr.get_sessions():
            try:
                out.append(s.source_app_user_model_id or "")
            except Exception:
                pass
        return out
    except Exception:
        return []


async def get_manager():
    """请求一次 SMTC 会话管理器。**由调用方(smtc_helper)缓存复用**——绝不能每帧 request_async:
    它会重新枚举整个 SMTC 基础设施,配合每帧新建 asyncio 事件循环,会让 winrt 线程池膨胀到数百线程、
    反复 churn,拖垮系统调度器 → 全桌面 UI 卡顿(用户态 CPU 却不高)。见 smtc_helper 的常驻循环。"""
    return await MediaManager.request_async()


async def snapshot(mgr):
    """返回 QQ音乐 会话快照 dict,无会话返回 None。mgr 由调用方缓存复用(不要每帧 request_async)。"""
    try:
        s = await _qq_session(mgr)
        if s is None:
            return None
        tl = s.get_timeline_properties()
        info = s.get_playback_info()
        pos = round(tl.position.total_seconds(), 1)
        try:
            status = int(info.playback_status)
        except Exception:
            status = -1
        playing = _infer_playing(status, pos)
        _last.update(pos=pos, at=_time.monotonic(), playing=playing)   # 供下帧进度推进判断
        title, artist = "", ""
        try:
            media = await s.try_get_media_properties_async()
            title = media.title or ""
            artist = media.artist or ""
        except Exception:
            pass
        return {
            "bgm_title": title,
            "bgm_artist": artist,
            "bgm_pos": pos,
            "bgm_dur": round(tl.end_time.total_seconds(), 1),
            "bgm_playing": playing,
        }
    except Exception:
        return None


async def control(mgr, action):
    """对 QQ音乐 会话做有方向的传输控制(play/pause/next/prev/toggle)。返回是否成功下发。
    mgr 由调用方缓存复用(不要每次 request_async)。"""
    try:
        s = await _qq_session(mgr)
        if s is None:
            return False
        if action == "play":
            await s.try_play_async()
        elif action == "pause":
            await s.try_pause_async()
        elif action == "next":
            await s.try_skip_next_async()
        elif action == "prev":
            await s.try_skip_previous_async()
        elif action == "toggle":
            await s.try_toggle_play_pause_async()
        else:
            return False
        return True
    except Exception:
        return False
