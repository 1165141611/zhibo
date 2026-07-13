# -*- coding: utf-8 -*-
"""发布 SMTC(系统媒体传输控件)会话,把本播放器伪装成"正在播放的音乐播放器",
让抖音直播伴侣的"歌词助手"识别歌名+进度、显示原生透明歌词。全同步,无需 asyncio。"""
import datetime

try:
    from winrt.windows.media.playback import MediaPlayer
    from winrt.windows.media import (
        MediaPlaybackType, MediaPlaybackStatus,
        SystemMediaTransportControlsTimelineProperties as TLProps)
    _OK = True
except Exception:
    _OK = False


class SmtcPublisher:
    def __init__(self):
        self.ok = _OK
        if not _OK:
            return
        self._player = MediaPlayer()
        self._smtc = self._player.system_media_transport_controls
        self._smtc.is_enabled = True
        self._smtc.is_play_enabled = True
        self._smtc.is_pause_enabled = True
        self._dur = 0.0

    def set_song(self, title, artist, duration_s):
        if not self.ok:
            return
        du = self._smtc.display_updater
        du.type = MediaPlaybackType.MUSIC
        du.music_properties.title = title or ""
        du.music_properties.artist = artist or ""
        du.update()
        self._dur = float(duration_s or 0)

    def set_playing(self, playing):
        if not self.ok:
            return
        self._smtc.playback_status = (
            MediaPlaybackStatus.PLAYING if playing else MediaPlaybackStatus.PAUSED)

    def update_position(self, pos_s):
        if not self.ok:
            return
        tl = TLProps()
        tl.start_time = datetime.timedelta(0)
        tl.end_time = datetime.timedelta(seconds=self._dur)
        tl.position = datetime.timedelta(
            seconds=max(0.0, min(float(pos_s), self._dur)))
        self._smtc.update_timeline_properties(tl)

    def close(self):
        if not self.ok:
            return
        try:
            self._smtc.playback_status = MediaPlaybackStatus.CLOSED
            self._smtc.is_enabled = False
        except Exception:
            pass


if __name__ == "__main__":
    import sys, time
    sys.stdout.reconfigure(encoding="utf-8")
    p = SmtcPublisher()
    print("winrt 可用:", p.ok)
    p.set_song("吉姆餐厅", "赵雷", 353)
    p.set_playing(True)
    p.update_position(72)
    print("已发布(纯同步,无 asyncio)。保活 3 秒…")
    time.sleep(3)
    print("OK — 同步方式可行" if p.ok else "winrt 不可用")
