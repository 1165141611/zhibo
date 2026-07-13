# -*- coding: utf-8 -*-
"""自制K歌播放器 Demo —— 透明置顶窗口。
数据来自 PC 版全民K歌(WeSing)缓存:伴奏PCM + 原唱PCM + 音高.note + 歌词QRC。
PC 播伴奏、自己当时钟 → 逐字高亮滚动歌词 + 原唱音高提示条 + 升降调。

热键:
  空格   播放/暂停
  ← →   快退/快进 5 秒
  ↑ ↓   升/降调(半音,实时秒切)
  R     伴奏 / 原唱引导声 切换
  B     背景模式切换(透明 / 洋红抠像 / 半透黑)
  ↑↓拖动 鼠标拖动窗口
  Esc   退出
"""
import sys
import os
import threading

import numpy as np
import sounddevice as sd
from PySide6 import QtCore, QtGui, QtWidgets

from assets import Song, SAMPLE_RATE, load_pcm
from audio_engine import AudioEngine

# ------------------------------------------------ 配置:吉姆餐厅
# 输出设备:None=系统默认;或填设备索引(见 audio_test.py --list)。
# 命令行 --device N 可覆盖。建议选伴奏该去的那条声卡路由(和QQ音乐BGM同一条)。
OUTPUT_DEVICE = None

RES_DIR = r"D:\WeSingCache\WeSingDL\Res"
MID = "0039DPnd48clp5"
QRC_PATH = (r"C:\Users\11651\AppData\Local\Temp\claude"
            r"\E--bianchengwenjian-cursor-zhibo"
            r"\d7766944-3a42-4c60-b30c-450affd70042\scratchpad\qrc"
            r"\0039DPnd48clp5_original.qrc")


class KaraokeWindow(QtWidgets.QWidget):
    BG_MODES = ["transparent", "chroma", "dark"]

    def __init__(self, song: Song):
        super().__init__()
        self.song = song
        self.accompany = song.accompany()          # 伴奏(key=0)
        self.kongsinger = None                      # 原唱,懒加载
        self.use_vocal = False
        self.semitone = 0
        self.bg_mode = 2                            # 默认半透黑,便于捕获
        self.status_text = ""
        self._drag_pos = None

        self.engine = AudioEngine(self.accompany, device=OUTPUT_DEVICE)

        # 音高范围(用于纵向映射),留 2 半音余量
        pit = [n.midi for n in song.notes] or [60]
        self.midi_lo = min(pit) - 2
        self.midi_hi = max(pit) + 2

        self._init_ui()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(16)                        # ~60fps

    def _init_ui(self):
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint |
                            QtCore.Qt.WindowStaysOnTopHint |
                            QtCore.Qt.Tool)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.resize(1100, 420)
        self.setWindowTitle("KaraokePlayer")

    # ---------------------------------------------------- 绘制
    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setRenderHint(QtGui.QPainter.TextAntialiasing)
        w, h = self.width(), self.height()

        mode = self.BG_MODES[self.bg_mode]
        if mode == "chroma":
            p.fillRect(self.rect(), QtGui.QColor(255, 0, 255))
        elif mode == "dark":
            p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 140))
        # transparent: 不填充

        now = self.engine.current_ms()
        pitch_h = int(h * 0.52)
        self._draw_pitch(p, 0, 0, w, pitch_h, now)
        self._draw_lyrics(p, 0, pitch_h, w, h - pitch_h, now)
        self._draw_status(p, w, h, now)

    def _midi_to_y(self, midi, top, height):
        span = max(1, self.midi_hi - self.midi_lo)
        r = (midi - self.midi_lo) / span
        return top + height - r * height

    def _draw_pitch(self, p, x, y, w, h, now):
        playhead = x + w * 0.28
        ppms = 0.18                                 # 像素/毫秒
        pad = 14
        top, bh = y + pad, h - 2 * pad
        # 音准线只表示原唱旋律"趋势",不随升降调移动

        # playhead 竖线
        p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 120), 2))
        p.drawLine(int(playhead), y + 6, int(playhead), y + h - 6)

        for n in self.song.notes:
            nx = playhead + (n.start - now) * ppms
            nw = max(3, n.dur * ppms)
            if nx + nw < x or nx > x + w:
                continue
            ny = self._midi_to_y(n.midi, top, bh)
            active = n.start <= now <= n.end
            bar_h = 9
            rect = QtCore.QRectF(nx, ny - bar_h / 2, nw, bar_h)
            if active:
                p.setBrush(QtGui.QColor(80, 220, 255))
                p.setPen(QtCore.Qt.NoPen)
                # 已唱进度
                pr = np.clip((now - n.start) / max(1, n.dur), 0, 1)
                p.drawRoundedRect(rect, 4, 4)
                p.setBrush(QtGui.QColor(255, 255, 255))
                p.drawRoundedRect(QtCore.QRectF(nx, ny - bar_h / 2,
                                                nw * pr, bar_h), 4, 4)
            else:
                p.setBrush(QtGui.QColor(120, 170, 210, 200))
                p.setPen(QtCore.Qt.NoPen)
                p.drawRoundedRect(rect, 4, 4)

    def _draw_lyrics(self, p, x, y, w, h, now):
        lines = self.song.lines
        cur = self._current_line_idx(now)
        big = QtGui.QFont("Microsoft YaHei", 30, QtGui.QFont.Bold)
        small = QtGui.QFont("Microsoft YaHei", 20)

        # 当前行(逐字高亮) + 下一行
        if cur is not None:
            self._draw_word_line(p, lines[cur], x, y + h * 0.34, w, big, now)
            if cur + 1 < len(lines):
                self._draw_plain_line(p, lines[cur + 1].text,
                                      x, y + h * 0.74, w, small,
                                      QtGui.QColor(210, 210, 210, 220))
        else:
            # 前奏:显示即将到来的第一行
            nxt = next((i for i, ln in enumerate(lines) if ln.start > now), None)
            if nxt is not None:
                secs = (lines[nxt].start - now) / 1000
                self._draw_plain_line(p, "♪ %.0f秒后: %s" % (secs, lines[nxt].text),
                                      x, y + h * 0.5, w, small,
                                      QtGui.QColor(200, 200, 200, 200))

    def _draw_word_line(self, p, line, x, cy, w, font, now):
        p.setFont(font)
        fm = QtGui.QFontMetrics(font)
        total = fm.horizontalAdvance(line.text)
        sx = x + (w - total) / 2
        base_col = QtGui.QColor(235, 235, 235)
        hi_col = QtGui.QColor(80, 220, 255)
        cx = sx
        for wd in line.words:
            wtext = wd.text
            ww = fm.horizontalAdvance(wtext)
            if now < wd.start:
                frac = 0.0
            elif now >= wd.end:
                frac = 1.0
            else:
                frac = (now - wd.start) / max(1, wd.dur)
            # 底色字
            p.setPen(base_col)
            p.drawText(QtCore.QRectF(cx, cy - 40, ww + 4, 60),
                       QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, wtext)
            # 高亮字(按 frac 裁剪宽度)
            if frac > 0:
                p.save()
                p.setClipRect(QtCore.QRectF(cx, cy - 42, ww * frac, 64))
                p.setPen(hi_col)
                p.drawText(QtCore.QRectF(cx, cy - 40, ww + 4, 60),
                           QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, wtext)
                p.restore()
            cx += ww

    def _draw_plain_line(self, p, text, x, cy, w, font, col):
        p.setFont(font)
        p.setPen(col)
        p.drawText(QtCore.QRectF(x, cy - 30, w, 50),
                   QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter, text)

    def _draw_status(self, p, w, h, now):
        p.setFont(QtGui.QFont("Microsoft YaHei", 11))
        key = "%+d" % self.semitone if self.semitone else "原调"
        src = "原唱" if self.use_vocal else "伴奏"
        tip = "[%s] 调:%s 音源:%s  %d:%02d/%d:%02d" % (
            self.song.title, key, src,
            int(now // 60000), int(now // 1000) % 60,
            int(self.song.duration_ms() // 60000),
            int(self.song.duration_ms() // 1000) % 60)
        if self.status_text:
            tip = self.status_text + "   " + tip
        p.setPen(QtGui.QColor(255, 255, 255, 180))
        p.drawText(QtCore.QRectF(10, h - 26, w - 20, 22),
                   QtCore.Qt.AlignLeft, tip)

    def _current_line_idx(self, now):
        cur = None
        for i, ln in enumerate(self.song.lines):
            if ln.start <= now < ln.end + 400:
                cur = i
            elif ln.start > now:
                break
        # 行间空档:保持上一行到下一行开始前
        if cur is None:
            prev = None
            for i, ln in enumerate(self.song.lines):
                if ln.end <= now:
                    prev = i
                else:
                    break
            if prev is not None and prev + 1 < len(self.song.lines) \
                    and self.song.lines[prev + 1].start - now < 3000:
                return prev + 1
        return cur

    # ---------------------------------------------------- 交互
    def keyPressEvent(self, e):
        k = e.key()
        if k == QtCore.Qt.Key_Space:
            self.engine.toggle()
        elif k == QtCore.Qt.Key_Left:
            self.engine.seek_ms(-5000)
        elif k == QtCore.Qt.Key_Right:
            self.engine.seek_ms(5000)
        elif k == QtCore.Qt.Key_Up:
            self._change_key(self.semitone + 1)
        elif k == QtCore.Qt.Key_Down:
            self._change_key(self.semitone - 1)
        elif k == QtCore.Qt.Key_R:
            self._toggle_vocal()
        elif k == QtCore.Qt.Key_B:
            self.bg_mode = (self.bg_mode + 1) % len(self.BG_MODES)
        elif k == QtCore.Qt.Key_Escape:
            self.close()

    def _toggle_vocal(self):
        self.use_vocal = not self.use_vocal
        if self.use_vocal and self.kongsinger is None:
            self.status_text = "加载原唱中…"
            self.repaint()
            self.kongsinger = load_pcm(self.song.kongsinger_path)
            self.status_text = ""
        self.engine.swap_buffer(
            self.kongsinger if self.use_vocal else self.accompany)

    def _change_key(self, semi):
        semi = int(np.clip(semi, -6, 6))
        if semi == self.semitone:
            return
        self.semitone = semi
        self.engine.set_semitones(semi)   # 实时秒切

    def mousePressEvent(self, e):
        if e.button() == QtCore.Qt.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def closeEvent(self, e):
        try:
            self.engine.close()
        except Exception:
            pass
        e.accept()


def main():
    global OUTPUT_DEVICE
    if "--device" in sys.argv:
        OUTPUT_DEVICE = int(sys.argv[sys.argv.index("--device") + 1])
    if not os.path.exists(os.path.join(RES_DIR, MID)):
        print("找不到歌曲缓存:", os.path.join(RES_DIR, MID))
        return
    app = QtWidgets.QApplication(sys.argv)
    song = Song(RES_DIR, MID, qrc_path=QRC_PATH)
    print("载入:", song.title, "-", song.artist,
          "| 行", len(song.lines), "音符", len(song.notes))
    win = KaraokeWindow(song)
    win.show()
    win.engine.toggle()  # 自动开始播放
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
