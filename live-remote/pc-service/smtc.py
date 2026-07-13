# -*- coding: utf-8 -*-
"""
读取 Windows 系统媒体会话(SMTC)信息:当前 BGM 的歌名、播放位置、总时长、是否在播。
QQ音乐 不支持 seek,但支持被读取这些信息,用来在手机上显示一个只读进度条。
"""
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager)

# 播放状态枚举: 4=Playing, 5=Paused
_PLAYING = 4


async def snapshot():
    """返回当前媒体会话快照 dict,无会话返回 None。"""
    try:
        mgr = await MediaManager.request_async()
        s = mgr.get_current_session()
        if s is None:
            return None
        tl = s.get_timeline_properties()
        info = s.get_playback_info()
        try:
            playing = int(info.playback_status) == _PLAYING
        except Exception:
            playing = False
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
            "bgm_pos": round(tl.position.total_seconds(), 1),
            "bgm_dur": round(tl.end_time.total_seconds(), 1),
            "bgm_playing": playing,
        }
    except Exception:
        return None
