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
import json
import time
import threading

import numpy as np
import sounddevice as sd
from PySide6 import QtCore, QtGui, QtWidgets

from assets import Song, SAMPLE_RATE, load_pcm
from audio_engine import AudioEngine
from smtc_publisher import SmtcPublisher

# ------------------------------------------------ 配置
# 输出设备:None=系统默认;或填设备索引(见 audio_test.py --list)。--device N 可覆盖。
OUTPUT_DEVICE = None

# 歌曲来源:优先永久曲库(pc-service 入库),回退 WeSing 缓存。IPC `load <mid>` 载入任意歌。
LIB_DIR = r"D:\KaraokeLibrary"
RES_DIR = r"D:\WeSingCache\WeSingDL\Res"
MID = "0039DPnd48clp5"        # 启动默认曲(吉姆餐厅);服务模式下 pc-service 会 IPC 载别的歌


def song_dir_for(mid):
    """给定 mid 返回它所在的目录:曲库优先,回退 Res。"""
    if os.path.isdir(os.path.join(LIB_DIR, mid)):
        return LIB_DIR
    return RES_DIR


class _NoSmtc:
    """SMTC 关闭时的空实现(接口与 SmtcPublisher 一致,全部 no-op)。"""
    ok = False
    def set_song(self, *a): pass
    def set_playing(self, *a): pass
    def update_position(self, *a): pass
    def close(self): pass


class _Ctl(QtCore.QObject):
    """服务模式下的控制桥:后台线程读 stdin 的指令,经信号回到 GUI 线程执行。"""
    cmd = QtCore.Signal(str)


class KaraokeWindow(QtWidgets.QWidget):
    BG_MODES = ["transparent", "chroma", "dark"]

    def __init__(self, song: Song, publish_smtc=True, service_mode=False):
        super().__init__()
        self.song = song
        self.service_mode = service_mode            # 由服务器托管时:Esc=隐藏而非退出
        self.accompany = song.accompany()          # 伴奏(key=0)
        self.kongsinger = None                      # 原唱,懒加载
        self._loading_vocal = False                 # 原唱是否正在后台线程加载(防重入 + GUI 不冻)
        self.use_vocal = False
        self.semitone = 0
        self.bg_mode = 1                            # 默认绿幕抠图模式(纯绿背景+描边文字)
        self.blank = False                          # 空白态:只画全绿背景(隐藏歌词时用,捕获帧=纯绿)
        self.status_text = ""
        self._drag_pos = None

        # 文字渲染缓存:整行预渲染成 QPixmap,每帧只 blit。不能每帧重建字形路径+描边
        # (那样一帧要 30ms+,GUI 线程占着 GIL,sounddevice 的 Python 回调抢不到 GIL
        #  → 设备缓冲(~46ms)耗尽 → 断续吱吱声,且只在窗口可见时出现)
        self.font_big = QtGui.QFont("Microsoft YaHei", 30, QtGui.QFont.Bold)
        self.font_small = QtGui.QFont("Microsoft YaHei", 20)
        self.font_status = QtGui.QFont("Microsoft YaHei", 11)
        self._line_h = QtGui.QFontMetrics(self.font_big).height()
        self._word_cache = {}    # (line.start, line.text) → 逐字行 base/hi 双 pixmap
        self._plain_cache = {}   # (text, 字号, 颜色) → 单 pixmap
        self._status_cache = None
        self._cache_dpr = 0.0
        # 预热字体光栅化(一次性~0.1s,此刻音频流还没开):否则首帧画字要 150ms+,
        # 音频回调跟着被拖,窗口显示瞬间就是一声吱
        fm = QtGui.QFontMetrics(self.font_big)
        self._make_line_pixmap([("预热", fm.horizontalAdvance("预热"))],
                               self.font_big, QtGui.QColor(0, 0, 0), 5)

        self.engine = AudioEngine(self.accompany, device=OUTPUT_DEVICE)

        # 发布 SMTC 会话(可选)。被 pc-service 托管时关掉,避免与其 smtc_helper 读会话打架。
        self.smtc = SmtcPublisher() if publish_smtc else _NoSmtc()
        self.smtc.set_song(song.title, song.artist, song.duration_ms() / 1000)
        if publish_smtc:
            print("SMTC 发布:", "已启用" if self.smtc.ok else "winrt不可用")

        # 音高范围(用于纵向映射),留 2 半音余量
        pit = [n.midi for n in song.notes] or [60]
        self.midi_lo = min(pit) - 2
        self.midi_hi = max(pit) + 2

        # 内容结束点(最后音符/歌词之后=尾奏),用于尾奏隐藏 playhead 竖线
        note_end = song.notes[-1].end if song.notes else 0
        line_end = song.lines[-1].end if song.lines else 0
        self.content_end = max(note_end, line_end)

        self._init_ui()
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick_paint)
        self.timer.start(16)                        # ~60fps(仅可见时真正重绘)
        # SMTC 进度/状态每 500ms 同步一次
        self.smtc_timer = QtCore.QTimer(self)
        self.smtc_timer.timeout.connect(self._smtc_tick)
        self.smtc_timer.start(500)

    def _init_ui(self):
        # 无边框(不置顶、不加 Qt.Tool;Tool 会导致不进任务栏/窗口捕获列表)
        self.setWindowFlags(QtCore.Qt.FramelessWindowHint)
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
            p.fillRect(self.rect(), QtGui.QColor(0, 255, 0))    # 纯绿,给绿幕抠图
        elif mode == "dark":
            p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 140))
        # transparent: 不填充

        if self.blank:      # 空白态:只留背景(全绿),不画歌词/音高/圆点/状态
            return

        dpr = self.devicePixelRatioF()
        if dpr != self._cache_dpr:      # 换屏/系统缩放变化:按新 DPR 重建文字缓存
            self._cache_dpr = dpr
            self._word_cache.clear()
            self._plain_cache.clear()
            self._status_cache = None

        now = self.engine.current_ms()
        pitch_h = int(h * 0.52)
        self._draw_pitch(p, 0, 0, w, pitch_h, now)
        self._draw_lyrics(p, 0, pitch_h, w, h - pitch_h, now)
        self._draw_status(p, w, h, now)

    def _tick_paint(self):
        """60fps 心跳:仅当窗口可见时才请求重绘。隐藏(服务托管默认态)时不排绘制,省 CPU。"""
        if self.isVisible():
            self.update()

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

        # playhead 竖线(不透明,带深色以便抠图);尾奏(过了最后音符/歌词)后隐藏
        if now <= self.content_end:
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255), 2))
            p.drawLine(int(playhead), y + 6, int(playhead), y + h - 6)

        outline = QtGui.QPen(QtGui.QColor(0, 0, 0), 2)   # 黑描边,抠图干净
        for n in self.song.notes:
            nx = playhead + (n.start - now) * ppms
            nw = max(3, n.dur * ppms)
            if nx + nw < x or nx > x + w:
                continue
            ny = self._midi_to_y(n.midi, top, bh)
            active = n.start <= now <= n.end
            bar_h = 9
            rect = QtCore.QRectF(nx, ny - bar_h / 2, nw, bar_h)
            p.setPen(outline)
            if active:
                p.setBrush(QtGui.QColor(80, 220, 255))       # 不透明青
                p.drawRoundedRect(rect, 4, 4)
                pr = float(np.clip((now - n.start) / max(1, n.dur), 0, 1))
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(QtGui.QColor(255, 255, 255))
                p.drawRoundedRect(QtCore.QRectF(nx, ny - bar_h / 2,
                                                nw * pr, bar_h), 4, 4)
            else:
                p.setBrush(QtGui.QColor(120, 170, 210))      # 不透明蓝
                p.drawRoundedRect(rect, 4, 4)

    # 引导圆点:仅前奏/长间奏出现(空档≥此值,单位ms);句间正常小停顿不显示
    GAP_FOR_DOTS = 8000
    LEAD = 3500          # 圆点倒计时提前量(ms)

    def _draw_lyrics(self, p, x, y, w, h, now):
        lines = self.song.lines
        big, small = self.font_big, self.font_small
        next_col = QtGui.QColor(220, 220, 220)
        line_h = self._line_h
        # 两行靠近,做直播底部字幕点缀(主体是主播画面)
        cy_big = y + h * 0.46
        cy_next = cy_big + line_h * 1.12

        # 正在唱(或刚唱完 300ms 内)的行
        cur = None
        for i, ln in enumerate(lines):
            if ln.start <= now < ln.end + 300:
                cur = i
            elif ln.start > now:
                break
        if cur is not None:
            self._draw_word_line(p, lines[cur], x, cy_big, w, big, now)
            if cur + 1 < len(lines):
                self._draw_plain_line(p, lines[cur + 1].text, x, cy_next, w,
                                      small, next_col)
            return

        # 无活跃行:即将唱的那行始终提前显示(等着);前奏显示歌名;长间奏末尾给圆点
        nxt = next((i for i, ln in enumerate(lines) if ln.start > now), None)
        if nxt is None:
            return
        remain = lines[nxt].start - now
        gap = (lines[nxt].start if nxt == 0
               else lines[nxt].start - lines[nxt - 1].end)   # 本行前的空档
        # 长间奏:整段就摆好圆点(远处全亮,最后 LEAD 秒才逐个熄灭)
        show_dots = gap >= self.GAP_FOR_DOTS
        # 前奏(首行前)整段显示 歌名 - 歌手
        if nxt == 0:
            self._draw_plain_line(p, "♪ %s - %s" % (self.song.title, self.song.artist),
                                  x, cy_big - line_h * 2.15, w, small,
                                  QtGui.QColor(120, 230, 255))
        if show_dots:
            self._draw_lead_dots(p, x, cy_big - line_h * 1.05, w, remain, self.LEAD)
        # now < 行首,逐字 frac=0 → 全底色,起唱瞬间无缝接上高亮
        self._draw_word_line(p, lines[nxt], x, cy_big, w, big, now)
        if nxt + 1 < len(lines):
            self._draw_plain_line(p, lines[nxt + 1].text, x, cy_next, w,
                                  small, next_col)

    def _draw_lead_dots(self, p, x, cy, w, remain, lead):
        """KTV 引导圆点:长间奏整段就摆好(remain≥lead 时全亮),最后 lead 秒内逐个熄灭。"""
        n = 4
        lit = n if remain >= lead else int(np.ceil(remain / lead * n))
        lit = max(0, min(n, lit))
        r = max(6, self.height() * 0.014)
        gap = r * 3.2
        sx = x + w / 2 - (n - 1) * gap / 2
        p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0), 2))
        for i in range(n):
            on = i < lit
            p.setBrush(QtGui.QColor(80, 220, 255) if on else QtGui.QColor(70, 70, 70))
            p.drawEllipse(QtCore.QPointF(sx + i * gap, cy), r, r)

    @staticmethod
    def _outlined(p, path, fill, ow=4):
        """描边(黑)+ 填充,抗锯齿边缘落在黑色上,绿幕可干净抠掉。"""
        pen = QtGui.QPen(QtGui.QColor(0, 0, 0), ow)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(QtCore.Qt.NoBrush)
        p.drawPath(path)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(fill)
        p.drawPath(path)

    PAD = 6   # 文字 pixmap 四周余量:容纳描边宽度+抗锯齿出血

    def _make_line_pixmap(self, words, font, fill, ow):
        """把一行字(黑描边+fill 填充)渲染成透明底 pixmap;words=[(文本, 步进宽)]。"""
        fm = QtGui.QFontMetrics(font)
        pad = self.PAD
        W = max(1, int(sum(a for _, a in words)) + 2 * pad)
        H = fm.height() + 2 * pad
        dpr = self.devicePixelRatioF() or 1.0
        pm = QtGui.QPixmap(int(W * dpr), int(H * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        path = QtGui.QPainterPath()
        x = pad
        for t, adv in words:
            path.addText(x, pad + fm.ascent(), font, t)
            x += adv
        self._outlined(p, path, fill, ow)
        p.end()
        return pm

    def _word_entry(self, line, font):
        """逐字行渲染缓存:base(未唱底色)/hi(已唱高亮)整行双 pixmap + 逐字步进宽。"""
        key = (line.start, line.text)
        ent = self._word_cache.get(key)
        if ent is None:
            fm = QtGui.QFontMetrics(font)
            words = [(wd.text, fm.horizontalAdvance(wd.text)) for wd in line.words]
            # hi(高亮版)懒构建:行先作为"即将唱"预告出现(只用 base),开始唱到才建 hi,
            # 把单行 10~35ms 的构建尖刺劈成两次,减小对音频回调的单次拖延
            ent = {"words": words,
                   "adv": [a for _, a in words],
                   "total": sum(a for _, a in words),
                   "ascent": fm.ascent(),
                   "H": fm.height() + 2 * self.PAD,
                   "base": self._make_line_pixmap(words, font,
                                                  QtGui.QColor(245, 245, 245), 5),
                   "hi": None, "font": font}
            self._word_cache[key] = ent
            while len(self._word_cache) > 8:     # 有界,防整首歌驻留(本机内存紧)
                del self._word_cache[next(iter(self._word_cache))]
        return ent

    def _draw_word_line(self, p, line, x, cy, w, font, now):
        ent = self._word_entry(line, font)
        left = x + (w - ent["total"]) / 2 - self.PAD
        top = cy + ent["ascent"] * 0.35 - ent["ascent"] - self.PAD
        p.drawPixmap(QtCore.QPointF(left, top), ent["base"])
        hi = 0.0                       # 已唱进度的像素宽(整行累计,词内按时间线性)
        for wd, adv in zip(line.words, ent["adv"]):
            if now >= wd.end:
                hi += adv
            else:
                if now > wd.start:
                    hi += adv * (now - wd.start) / max(1, wd.dur)
                break
        if hi > 0:      # 高亮版整行盖上,裁剪到进度线;两版描边一致,边界干净
            if ent["hi"] is None:
                ent["hi"] = self._make_line_pixmap(ent["words"], ent["font"],
                                                   QtGui.QColor(80, 220, 255), 5)
            p.save()
            p.setClipRect(QtCore.QRectF(left, top, self.PAD + hi, ent["H"]))
            p.drawPixmap(QtCore.QPointF(left, top), ent["hi"])
            p.restore()

    def _draw_plain_line(self, p, text, x, cy, w, font, col):
        key = (text, font.pointSizeF(), col.rgba())
        ent = self._plain_cache.get(key)
        if ent is None:
            fm = QtGui.QFontMetrics(font)
            total = fm.horizontalAdvance(text)
            ent = {"total": total, "ascent": fm.ascent(),
                   "pix": self._make_line_pixmap([(text, total)], font, col, 4)}
            self._plain_cache[key] = ent
            while len(self._plain_cache) > 8:
                del self._plain_cache[next(iter(self._plain_cache))]
        p.drawPixmap(QtCore.QPointF(x + (w - ent["total"]) / 2 - self.PAD,
                                    cy + ent["ascent"] * 0.35 - ent["ascent"] - self.PAD),
                     ent["pix"])

    def _draw_status(self, p, w, h, now):
        key = "%+d" % self.semitone if self.semitone else "原调"
        src = "原唱" if self.use_vocal else "伴奏"
        tip = "[%s] 调:%s 音源:%s  %d:%02d/%d:%02d" % (
            self.song.title, key, src,
            int(now // 60000), int(now // 1000) % 60,
            int(self.song.duration_ms() // 60000),
            int(self.song.duration_ms() // 1000) % 60)
        if self.status_text:
            tip = self.status_text + "   " + tip
        if self._status_cache is None or self._status_cache[0] != tip:
            fm = QtGui.QFontMetrics(self.font_status)   # 文案变了(约1次/秒)才重渲染
            self._status_cache = (tip, self._make_line_pixmap(
                [(tip, fm.horizontalAdvance(tip))], self.font_status,
                QtGui.QColor(230, 230, 230), 3))
        p.drawPixmap(QtCore.QPointF(10 - self.PAD, h - 26 - self.PAD),
                     self._status_cache[1])

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
            if self.service_mode:
                self.hide_lyrics()   # 服务模式:清空全绿后隐藏(进程不退,托盘可再显示)
            else:
                self.close()

    def set_vocal(self, want):
        """绝对切换 原唱/伴奏(want=True 用原唱引导声)。
        原唱首次使用需解码整段 PCM——放到**后台线程**做,绝不阻塞 GUI 线程
        (否则连续点"音源"会把窗口冻死)。加载完成后再切引擎缓冲(engine 自带锁,跨线程安全)。"""
        want = bool(want)
        if want == self.use_vocal:
            return
        self.use_vocal = want
        if not want:
            self.engine.swap_buffer(self.accompany)     # 切回伴奏:立即,无需加载
            return
        if self.kongsinger is not None:
            self.engine.swap_buffer(self.kongsinger)    # 已缓存:立即
            return
        if self._loading_vocal:
            return                                       # 后台已在加载,等它完成
        self._loading_vocal = True
        self.status_text = "加载原唱中…"
        path = self.song.kongsinger_path

        def _work():
            buf = None
            try:
                buf = load_pcm(path)
            except Exception as e:
                print("[VOCAL] 加载失败", e, flush=True)
            self._loading_vocal = False
            self.status_text = ""
            if buf is None:
                return
            self.kongsinger = buf
            if self.use_vocal:            # 加载期间没被切回伴奏,才真正应用(engine 内部有锁)
                self.engine.swap_buffer(buf)

        threading.Thread(target=_work, daemon=True).start()

    def _toggle_vocal(self):
        self.set_vocal(not self.use_vocal)

    def _change_key(self, semi):
        semi = int(np.clip(semi, -6, 6))
        if semi == self.semitone:
            return
        self.semitone = semi
        self.engine.set_semitones(semi)   # 实时秒切

    def load_song(self, mid):
        """载入曲库(或Res)里的任意一首歌:换全部数据 + 引擎归位、清调、暂停。"""
        try:
            song = Song(song_dir_for(mid), mid)
        except Exception as e:
            print("[LOAD] 失败", mid, e, flush=True)
            return False
        self.song = song
        self.accompany = song.accompany()
        self.kongsinger = None
        self.use_vocal = False
        self.semitone = 0
        pit = [n.midi for n in song.notes] or [60]
        self.midi_lo = min(pit) - 2
        self.midi_hi = max(pit) + 2
        note_end = song.notes[-1].end if song.notes else 0
        line_end = song.lines[-1].end if song.lines else 0
        self.content_end = max(note_end, line_end)
        self._word_cache.clear()             # 换歌清文字渲染缓存
        self._plain_cache.clear()
        self._status_cache = None
        self.engine.load(self.accompany)     # 换源、归位到0、清调、暂停
        self.smtc.set_song(song.title, song.artist, song.duration_ms() / 1000)
        print("[LOAD]", mid, song.title, "-", song.artist, flush=True)
        return True

    def _smtc_tick(self):
        """定时同步:SMTC(若启用)+ 服务模式下把播放状态/进度上报给 pc-service(stdout)。"""
        self.smtc.set_playing(self.engine.playing)
        self.smtc.update_position(self.engine.current_ms() / 1000)
        if self.service_mode:
            st = {"pos": round(self.engine.current_ms()),
                  "dur": round(self.song.duration_ms()),
                  "playing": bool(self.engine.playing),
                  "key": self.semitone, "vocal": self.use_vocal,
                  "vol": self.engine.volume_pct,
                  "mid": self.song.mid, "title": self.song.title,
                  "artist": self.song.artist}
            try:
                sys.stdout.write("STATE " + json.dumps(st, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except Exception:
                pass

    def mousePressEvent(self, e):
        if e.button() == QtCore.Qt.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None

    def show_lyrics(self):
        """显示歌词:恢复正常渲染并显示窗口。"""
        self.blank = False
        self.show()
        self.raise_()
        self.activateWindow()      # 取焦点,快捷键才生效

    def hide_lyrics(self):
        """隐藏歌词:先把内容清成全绿(同步画一帧,让捕获冻结帧=纯绿),稍后再隐藏窗口。"""
        self.blank = True
        self.repaint()             # 同步立刻画出全绿
        QtCore.QTimer.singleShot(80, self.hide)   # 留几帧时间给直播伴侣抓到绿帧,再隐藏

    def _report_vis(self, v):
        """服务模式下把可见性上报给 pc-service(stdout)。"""
        if self.service_mode:
            try:
                sys.stdout.write("VIS:%d\n" % (1 if v else 0))
                sys.stdout.flush()
            except Exception:
                pass

    def showEvent(self, e):
        super().showEvent(e)
        self._report_vis(True)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._report_vis(False)

    def closeEvent(self, e):
        # 服务模式:禁用手动关闭(任务栏/X 都不关也不隐藏),生命周期只由 pc-service/托盘控制
        if self.service_mode:
            e.ignore()
            return
        try:
            self.engine.close()
        except Exception:
            pass
        try:
            self.smtc.close()
        except Exception:
            pass
        e.accept()


def main():
    global OUTPUT_DEVICE
    # 关键:强制 stdout/stdin 用 UTF-8。被 pc-service 托管时,STATE/VIS 及启动打印含中文歌名,
    # Windows 下子进程默认 GBK(cp936)→ 父进程按 UTF-8 读会在第一行中文就 UnicodeDecodeError 崩掉
    # 读取线程,随后 stdout 管道写满、本进程 flush 阻塞 GUI 线程 → 对指令完全无响应(点歌不弹播放器)。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", newline="\n")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # GIL 切换间隔调小(默认5ms):GUI 线程绘制/stdout 上报期间,让 sounddevice 的
    # Python 音频回调更快抢到 GIL,减小回调抖动(欠载=吱吱声)
    sys.setswitchinterval(0.002)
    argv = sys.argv
    if "--device" in argv:
        OUTPUT_DEVICE = int(argv[argv.index("--device") + 1])
    hidden = "--hidden" in argv     # 隐藏启动(由服务器托管,靠托盘/HWND 显示)
    paused = "--paused" in argv     # 不自动播放
    no_smtc = "--no-smtc" in argv   # 关闭 SMTC 发布(避免与 pc-service 打架)
    default_dir = song_dir_for(MID)
    if not os.path.exists(os.path.join(default_dir, MID)):
        print("找不到默认歌曲:", os.path.join(default_dir, MID))
        return
    app = QtWidgets.QApplication(sys.argv)
    song = Song(default_dir, MID)
    print("载入:", song.title, "-", song.artist,
          "| 行", len(song.lines), "音符", len(song.notes))
    win = KaraokeWindow(song, publish_smtc=not no_smtc, service_mode=hidden)
    if hidden:
        # 服务模式:先完整 show 一次(建 HWND + 初始化绘制)再 hide,之后由 stdin 指令
        # 用 Qt 自己 show/hide(才会正确重绘;外部 win32 SW_SHOW 不会触发 Qt 重绘)。
        win.show()
        win.hide()

        def on_cmd(line):
            parts = line.split(None, 1)
            c = parts[0] if parts else ""
            arg = parts[1].strip() if len(parts) > 1 else None
            # 显隐
            if c == "hide":
                win.hide_lyrics()
            elif c == "show" or (c == "toggle" and not win.isVisible()):
                win.show_lyrics()
            elif c == "toggle" and win.isVisible():
                win.hide_lyrics()
            # 载歌
            elif c == "load" and arg:
                win.load_song(arg)
            # 播放控制
            elif c == "play":
                win.engine.set_playing(True)
            elif c == "pause":
                win.engine.set_playing(False)
            elif c == "playpause":
                win.engine.set_playing(not win.engine.is_playing())
            elif c == "seek" and arg is not None:
                win.engine.seek_to_ms(int(arg))
            # 升降调
            elif c == "key" and arg is not None:
                win._change_key(int(arg))          # 绝对半音
            elif c == "key+":
                win._change_key(win.semitone + 1)
            elif c == "key-":
                win._change_key(win.semitone - 1)
            # 原唱/伴奏
            elif c == "vocal" and arg is not None:
                win.set_vocal(arg == "1")
            elif c == "vocal_toggle":
                win._toggle_vocal()
            # 伴奏音量(0-100,手机音量键同步)
            elif c == "vol" and arg is not None:
                try:
                    win.engine.set_volume(int(arg))
                except ValueError:
                    pass
            # 父进程(pc-service)退出/崩溃 → stdin 关闭,本播放器自退,避免成为关不掉的孤儿
            elif c == "__quit__":
                QtWidgets.QApplication.quit()

        ctl = _Ctl()
        ctl.cmd.connect(on_cmd)

        def _stdin_reader():
            for line in sys.stdin:
                s = line.strip()
                if s:
                    ctl.cmd.emit(s)     # 跨线程 → 排队到 GUI 线程执行
            # 循环结束 = stdin EOF = 托管本进程的 pc-service 已退出(正常关会先 terminate 我们,
            # 走到这里通常是服务端异常崩溃)。服务模式下窗口禁用了手动关闭,若不自退就成了
            # 关不掉的隐藏进程 → 主动退出。先请 GUI 优雅退,再硬退兜底。
            ctl.cmd.emit("__quit__")
            time.sleep(1.5)
            os._exit(0)

        threading.Thread(target=_stdin_reader, daemon=True).start()
    else:
        win.show()
    if not paused:
        win.engine.toggle()     # 自动开始播放
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
