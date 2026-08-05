# -*- coding: utf-8 -*-
"""自制K歌播放器 Demo —— 透明置顶窗口。
数据来自 PC 版全民K歌(WeSing)缓存:伴奏PCM + 原唱PCM + 音高.note + 歌词QRC。
PC 播伴奏、自己当时钟 → 逐字高亮滚动歌词 + 原唱音高提示条 + 升降调。

热键:
  空格   播放/暂停
  ← →   快退/快进 5 秒
  ↑ ↓   升/降调(半音,实时秒切)
  R     伴奏 / 原唱引导声 切换
  P     音准线 显示 / 隐藏
  Q     歌词字体 循环切换(缓存,重启保持)
  O     顶端滚动歌单 显示 / 隐藏(缓存)
  Ctrl+↑↓ 顶端歌单上下移动(上不越窗顶,下不覆盖音轨;缓存位置)
  B     背景模式切换(透明 / 洋红抠像 / 半透黑)
  ↑↓拖动 鼠标拖动窗口
  Esc   退出
"""
import sys
import os
import json
import math
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
    # 歌词字体循环(Q 键切换,pc-service 缓存):(显示名, 字体族, 是否加粗)。
    # bold=False 的是字体名已含字重(Black/Medium)的,不再叠加粗——与选字时的示例一致。
    FONTS = [
        ("微软雅黑", "Microsoft YaHei", True),
        ("黑体", "SimHei", True),
        ("思源黑Black", "Noto Sans SC Black", False),
        ("思源黑Medium", "Noto Sans SC Medium", False),
        ("思源宋", "Noto Serif SC", True),
        ("思源宋Black", "Noto Serif SC Black", False),
        ("楷体", "KaiTi", True),
    ]
    MARQUEE_SPEED = 45          # 顶端歌单滚动速度(像素/秒)

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
        self.show_pitch = True                      # 音准线显隐(IPC `pitch 0/1` 控;pc-service 缓存)
        self.show_setlist = True                    # 顶端滚动歌单显隐(O 键;同 P 一起缓存)
        self.setlist_titles = []                    # 歌单歌名(pc-service 据曲库勾选推来)
        self.setlist_y = 24                         # 歌单竖直位置(Ctrl+↑↓ 移动;缓存)
        self._setlist_pix = None                    # 歌单滚动条 pixmap(内容/字体变时重建)
        # 礼物菜单:绿幕左侧竖排"礼物→权益"引导条(如 🎈点歌 / 🍰插队)。内容由 pc-service
        # 「礼物菜单配置」窗选礼物+填自定义文字后经 IPC `gifts` 推来;G 键/IPC 显隐;鼠标可单独拖动
        # (命中检测:按在礼物条上拖它、否则拖整窗),位置/显隐经 STATE 上报 pc-service 缓存。
        self.show_gifts = True                      # 礼物菜单显隐(G 键 / IPC gifts_show;缓存)
        self.gift_items = []                        # 礼物条内容 [{icon,text}](顺序即显示顺序)
        self.gift_x = 24                            # 礼物条左上角 x(鼠标拖动;缓存)
        self.gift_y = 300                           # 左上角 y(默认左侧偏上,避开顶端歌单带)
        self.gift_scale = 1.0                       # 礼物菜单整体缩放(配置窗滑块调,IPC gift_scale;缓存)
        self.gift_outline = self.GIFT_OUTLINE_DEF   # 描边宽(配置窗滑块;IPC gift_outline;缓存)
        self.gift_gap = self.GIFT_GAP_DEF           # 卡片竖直间距(配置窗滑块;IPC gift_gap;缓存)
        self.gift_color = QtGui.QColor(self.GIFT_COLOR_DEF)  # 描边颜色(配置窗取色器;IPC gift_color;缓存)
        self._gift_pix = None                       # 预合成卡片 pixmap 缓存(内容/字体/DPR/尺寸/描边 变时重建)
        self._gift_drag = False                     # 是否正在拖礼物条(vs 拖整窗)
        self._gift_drag_off = (0, 0)                # 拖动时鼠标相对礼物条左上角的偏移
        self.blank = False                          # 空白态:只画全绿背景(隐藏歌词时用,捕获帧=纯绿)
        # 未演唱态:歌曲已载入但主播还没开唱(进度 0、从未播放)。此时绿幕只出纯绿背景,
        # 不画歌词/音准线——队列点的第一首、唱完切到的下一首都装在开头暂停(见 pc-service
        # k_enqueue / k_advance_paused),避免观众提前看到还没开唱那首的词和音高。开唱即解除。
        self._ever_played = False
        self.status_text = ""
        self._drag_pos = None
        # 窗口桌面位置记忆:拖动/显隐时记下,下次 show 恢复到关闭时的位置。服务模式下隐藏会销毁
        # 原生 HWND(见 _hide_and_release)、再 show 时 Qt 会重建到默认位置,故必须显式 move 回来;
        # 位置经 STATE 上报给 pc-service 存 state_cache.json,服务重启后拉起播放器时再下发 `pos x y`。
        self._saved_pos = None
        # 演唱者(主播名):开头标题卡显示"演唱:<名>";pc-service 托盘可改、缓存下发,默认兜底。
        self.performer = "八门官上"
        self._title_card = None          # 标题卡 pixmap 缓存(歌名/原唱/演唱/字体 变时重建)
        self._title_dur = 0              # 本歌标题卡时长(ms;据首句起点算,load 时定)
        self._dot_y = None               # 音高游标当前 y(平滑滑动;None=未初始化,load 重置)

        # 文字渲染缓存:整行预渲染成 QPixmap,每帧只 blit。不能每帧重建字形路径+描边
        # (那样一帧要 30ms+,GUI 线程占着 GIL,sounddevice 的 Python 回调抢不到 GIL
        #  → 设备缓冲(~46ms)耗尽 → 断续吱吱声,且只在窗口可见时出现)
        self.font_status = QtGui.QFont("Microsoft YaHei", 11)   # 状态/快捷键固定雅黑,不随 Q 切换
        self._word_cache = {}    # (line.start, line.text) → 逐字行 base/hi 双 pixmap
        self._plain_cache = {}   # (text, 字号, 颜色) → 单 pixmap
        self._status_cache = None
        self._hotkey_pix = None   # 右下角快捷键提示(静态文案,建一次缓存)
        self._cache_dpr = 0.0
        self.font_idx = 0
        self._apply_font(0)      # 设 font_big/font_small/_line_h(默认微软雅黑;pc-service 会按缓存下发)
        # 预热字体光栅化(一次性~0.1s,此刻音频流还没开):否则首帧画字要 150ms+,
        # 音频回调跟着被拖,窗口显示瞬间就是一声吱
        fm = QtGui.QFontMetrics(self.font_big)
        self._make_line_pixmap([("预热", fm.horizontalAdvance("预热"))],
                               self.font_big, QtGui.QColor(0, 0, 0), 1)

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
        self._compute_slots()               # 预计算 KTV 双行槽位(乐句首在上排)
        self._compute_title_dur()           # 据首句起点定开头标题卡时长

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
        # 竖屏 3:4 窗:配竖屏直播;字幕不强行铺满窗口,绿幕抠图后手动裁剪即可(多余留白无所谓)
        self.resize(720, 960)
        self.setWindowTitle("KaraokePlayer")

    def _apply_font(self, idx):
        """切换歌词字体(font_big/small),重算行高,清文字缓存(旧字体 pixmap 作废)。
        Q 键 / IPC `font <idx>` / 启动恢复都走这里。越界取模,安全。"""
        idx %= len(self.FONTS)
        self.font_idx = idx
        _disp, fam, bold = self.FONTS[idx]
        self.font_big = QtGui.QFont(fam, 30)
        self.font_big.setBold(bold)
        self.font_small = QtGui.QFont(fam, 20)
        self.font_small.setBold(bold)
        self._line_h = QtGui.QFontMetrics(self.font_big).height()
        self._word_cache.clear()
        self._plain_cache.clear()
        self._setlist_pix = None        # 歌单用 font_small,字体变了要重建
        self._title_card = None         # 标题卡用同族字体,字体变了要重建
        self._gift_pix = None           # 礼物卡片文字用同族字体,字体变了要重建

    def _flash_status(self, text, ms=2000):
        """左下角状态栏临时提示(如切字体),ms 后若没被新提示替换则清掉。"""
        self.status_text = text
        QtCore.QTimer.singleShot(
            ms, lambda: setattr(self, "status_text", "") if self.status_text == text else None)

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
            self._hotkey_pix = None
            self._setlist_pix = None
            self._title_card = None
            self._gift_pix = None

        now = self.engine.current_ms()
        if self.engine.playing:             # 一旦开唱就记住:之后暂停在半途仍算"演唱中",照常显示
            self._ever_played = True
        unsung = not self._ever_played      # 未演唱(载入待唱):不画歌词/音准线,只留背景
        # 开头标题卡阶段:开唱后前几秒(now<title_dur)居中显示 歌名/原唱/演唱,渐隐后再出歌词+音准线
        in_title = (not unsung) and self._title_dur > 0 and now < self._title_dur
        pitch_top, pitch_h, cy_top = self._layout()
        if in_title:
            self._draw_title_card(p, w, h, self._title_alpha(now))
        elif not unsung:
            if self.show_pitch:             # 音准线可显隐(手机遥控 + 缓存)
                self._draw_pitch(p, 0, pitch_top, w, pitch_h, now)
            self._draw_lyrics(p, 0, cy_top, w, now)
        if self.show_setlist:               # 顶端滚动歌单(O 键;下边界=音轨顶,不覆盖)
            self._draw_setlist(p, w)
        if self.show_gifts:                 # 礼物菜单(绿幕左侧竖排;独立拖动,pc-service 配置窗选礼物)
            self._draw_gifts(p, w, h)
        self._draw_status(p, w, h, now)
        self._draw_hotkeys(p, w, h)

    def _layout(self):
        """主题(音准带+两行歌词)底部锚定:落在最下方信息/快捷栏上方。返回 (pitch_top, pitch_h, cy_top)。"""
        h = self.height()
        lh = self._line_h
        cy_top = (h - 26) - lh * 2.35        # 两行歌词(cy_bot=cy_top+1.15lh)落在状态栏上方
        pitch_h = int(lh * 2)                # 音准区≤两行歌词高,扁平
        pitch_top = int(cy_top - lh * 1.4 - pitch_h)   # 音准带在歌词上方,留圆点空间
        return pitch_top, pitch_h, cy_top

    def _setlist_entry(self):
        """歌单滚动条:歌名用空格连接、尾部补空格(循环无缝)→ 一张 pixmap,内容/字体变时重建。"""
        if self._setlist_pix is None:
            if not self.setlist_titles:
                return None
            sep = "　"             # 一个全角空格(原两个,间隔缩小一半)
            text = sep.join(self.setlist_titles) + sep
            fm = QtGui.QFontMetrics(self.font_small)
            total = fm.horizontalAdvance(text)
            # 样式统一为"未唱歌词"款:白底 + 黑描边(比原 1px 更像 KTV 字幕),描边宽按字号
            # 从歌词的 OW_BLACK 等比缩小(font_small 比 font_big 小),视觉权重与歌词一致。
            ow = max(2, round(self.OW_BLACK * self.font_small.pointSizeF()
                              / max(1.0, self.font_big.pointSizeF())))
            self._setlist_pix = (total, fm.height(),
                                 self._make_line_pixmap([(text, total)], self.font_small,
                                                        self.COL_UNSUNG, ow))
        return self._setlist_pix

    def _setlist_h(self):
        return QtGui.QFontMetrics(self.font_small).height() + 2 * self.PAD

    def _draw_setlist(self, p, w):
        """顶端横向循环滚动歌单(只歌名,空格分隔)。pixmap 平铺两份+裁剪,时钟驱动无缝滚。"""
        ent = self._setlist_entry()
        if not ent:
            return
        period, fh, pix = ent
        y = self.setlist_y
        off = (time.monotonic() * self.MARQUEE_SPEED) % period
        p.save()
        p.setClipRect(QtCore.QRectF(0, y, w, fh + 2 * self.PAD))
        x = -off
        while x < w:
            p.drawPixmap(QtCore.QPointF(x - self.PAD, y), pix)
            x += period
        p.restore()

    def set_setlist(self, titles):
        new = [str(t) for t in titles]
        if new == self.setlist_titles:
            return                          # ★ 内容没变就别动:重置 _setlist_pix 会让下一帧在 GUI 线程
                                            #   重建滚动 pixmap(字体渲染重活),与音频回调抢 GIL → 正在播的
                                            #   歌卡顿一下。pc-service 每次"点歌入队"都会重推一次歌单(内容
                                            #   其实没变,只是 plays 次数变了触发了曲库回调),故必须在此挡掉。
        self.setlist_titles = new
        self._setlist_pix = None            # 内容变了才重建

    def _move_setlist(self, d):
        """Ctrl+↑↓ 移歌单:上不越窗顶(0),下不覆盖音轨(音准带顶)。"""
        pitch_top, _, _ = self._layout()
        hi = max(0, pitch_top - self._setlist_h())
        self.setlist_y = int(max(0, min(hi, self.setlist_y + d * 24)))

    # ---------------------------------------------------- 礼物菜单
    def set_gifts(self, items):
        """收到 pc-service 推的礼物条内容([{icon,text}],顺序即显示顺序)。内容变了才重建
        pixmap(同 set_setlist 的 GIL 守卫:重建卡片=drawText+图标缩放,GUI 线程重活,内容没变
        别白重建抢音频回调 GIL)。"""
        new = [{"icon": str(it.get("icon", "")), "text": str(it.get("text", ""))}
               for it in (items or [])]
        if new == self.gift_items:
            return
        self.gift_items = new
        self._gift_pix = None            # 内容变了才重建

    def _build_gift_pix(self):
        """把每个礼物预合成一张卡片 pixmap(图标 + 白字,**无底板**,各自描一圈黑边),
        一次性做好缓存;paintEvent 只 blit。统一卡片宽(取最宽的)竖排更整齐。填 self._gift_pix
        = [(pixmap, 宽, 高)]。尺寸随 gift_scale 缩放。
        描边法(绿幕干净抠):先把图标+白字画到透明"内容层"(白字用 drawText,emoji 出彩色);
        再取内容层 alpha 填黑得"黑剪影",在 8 个方向各偏移 r 画一遍 = 一圈黑轮廓(抗锯齿边缘落黑上);
        最后把内容层盖上。等价歌词/音准线的黑 keyline,只是描的是任意位图剪影而非字形路径。"""
        self._gift_pix = []
        if not self.gift_items:
            return
        dpr = self.devicePixelRatioF() or 1.0
        s = max(self.GIFT_SCALE_MIN, min(self.GIFT_SCALE_MAX, self.gift_scale))
        icon = max(1, int(round(self.GIFT_ICON * s)))
        gap = int(round(self.GIFT_ICON_GAP * s))
        pt = max(6, int(round(self.GIFT_TEXT_PT * s)))
        r = max(0.0, min(self.GIFT_OUTLINE_MAX, self.gift_outline) * s)   # 描边宽(可调,0=不描)
        pad = int(math.ceil(r)) + 2                        # 四周留描边出血
        tf = QtGui.QFont(self.font_big.family(), pt)
        tf.setBold(True)
        fm = QtGui.QFontMetrics(tf)
        loaded, max_tw = [], 0
        for it in self.gift_items:                 # 预载图标 + 量文字,先算统一卡片宽
            ic = QtGui.QPixmap(it["icon"]) if it.get("icon") else QtGui.QPixmap()
            if not ic.isNull():
                ic = ic.scaled(int(icon * dpr), int(icon * dpr),
                               QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
                ic.setDevicePixelRatio(dpr)
            txt = it.get("text", "")
            max_tw = max(max_tw, fm.horizontalAdvance(txt) if txt else 0)
            loaded.append((ic, txt))
        has_text = max_tw > 0
        content_h = max(icon, fm.height())
        card_w = pad + icon + (gap + max_tw if has_text else 0) + pad
        card_h = pad + content_h + pad
        for ic, txt in loaded:
            # 1) 内容层(透明底):图标(左,竖直居中)+ 白字(右,竖直居中,含彩色 emoji)
            content = QtGui.QPixmap(int(card_w * dpr), int(card_h * dpr))
            content.setDevicePixelRatio(dpr)
            content.fill(QtCore.Qt.transparent)
            cp = QtGui.QPainter(content)
            cp.setRenderHint(QtGui.QPainter.Antialiasing)
            cp.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
            if not ic.isNull():
                iw, ih = ic.width() / dpr, ic.height() / dpr
                cp.drawPixmap(QtCore.QPointF(pad + (icon - iw) / 2, (card_h - ih) / 2), ic)
            if has_text and txt:
                cp.setPen(QtGui.QColor(255, 255, 255))
                cp.setFont(tf)
                cp.drawText(QtCore.QRectF(pad + icon + gap, pad, max_tw + 2, content_h),
                            int(QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft), txt)
            cp.end()
            # 2) 描边剪影:取内容层 alpha 填成**描边色**(opaque=绿幕干净;淡靠选浅灰,不用半透明)
            sil = None
            if r > 0:
                sil = QtGui.QPixmap(content.size())
                sil.setDevicePixelRatio(dpr)
                sil.fill(QtCore.Qt.transparent)
                sp = QtGui.QPainter(sil)
                sp.drawPixmap(0, 0, content)
                sp.setCompositionMode(QtGui.QPainter.CompositionMode_SourceIn)
                sp.fillRect(sil.rect(), self.gift_color)
                sp.end()
            # 3) 合成:描边剪影 8 方向各偏移 r 画一圈(r>0 才描)+ 内容层盖上
            pm = QtGui.QPixmap(content.size())
            pm.setDevicePixelRatio(dpr)
            pm.fill(QtCore.Qt.transparent)
            p = QtGui.QPainter(pm)
            if sil is not None:
                for ox, oy in self._GIFT_OUTLINE_OFFS:
                    p.drawPixmap(QtCore.QPointF(ox * r, oy * r), sil)
            p.drawPixmap(QtCore.QPointF(0, 0), content)
            p.end()
            self._gift_pix.append((pm, card_w, card_h))

    def _draw_gifts(self, p, w, h):
        """从 (gift_x, gift_y) 竖排 blit 各礼物卡片。pixmap 预合成(见 _build_gift_pix),此处零重活。"""
        if self._gift_pix is None:
            self._build_gift_pix()
        gv = max(0, min(self.GIFT_GAP_MAX, self.gift_gap))   # 间距绝对 px(不随尺寸缩放,直控更直观)
        y = self.gift_y
        for pm, cw, ch in self._gift_pix:
            p.drawPixmap(QtCore.QPointF(self.gift_x, y), pm)
            y += ch + gv

    def _gift_bbox(self):
        """礼物条整体外接矩形(鼠标命中检测拖动用);显隐关或无内容返回 None。"""
        if not self.show_gifts:
            return None
        if self._gift_pix is None:
            self._build_gift_pix()
        if not self._gift_pix:
            return None
        bw = max(cw for _, cw, _ in self._gift_pix)
        gv = max(0, min(self.GIFT_GAP_MAX, self.gift_gap))
        bh = sum(ch for _, _, ch in self._gift_pix) + gv * (len(self._gift_pix) - 1)
        return QtCore.QRectF(self.gift_x, self.gift_y, bw, bh)

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
        pad = max(4, int(h * 0.12))
        top, bh = y + pad, h - 2 * pad
        # 音准线只表示原唱旋律"趋势",不随升降调移动;整体压扁在≤两行歌词高内。
        # 起唱竖线已移除,改为下面的"音高游标"白色亮点。

        # 音准块描边:黑,粗细与歌词笔画看齐(不再淡化);未唱=白、已唱(播放头左侧)=蓝
        outline = QtGui.QPen(QtGui.QColor(0, 0, 0), self.PITCH_OW)
        outline.setJoinStyle(QtCore.Qt.RoundJoin)
        white = QtGui.QColor(245, 245, 245)          # 未唱=白(同歌词底色)
        blue = QtGui.QColor(80, 220, 255)            # 已唱=蓝(旋律高亮,暂沿用青)
        bar_h = self.PITCH_BAR_H
        for n in self.song.notes:
            nx = playhead + (n.start - now) * ppms
            nw = max(3, n.dur * ppms)
            if nx + nw < x or nx > x + w:
                continue
            ny = self._midi_to_y(n.midi, top, bh)
            rect = QtCore.QRectF(nx, ny - bar_h / 2, nw, bar_h)
            # 像歌词:底色全白 + 描边;播放头左侧(已唱)裁出来染蓝
            p.setPen(outline)
            p.setBrush(white)
            p.drawRoundedRect(rect, 3, 3)
            if nx < playhead:                        # 有已唱部分 → 染蓝到播放头
                p.save()
                p.setClipRect(QtCore.QRectF(nx, ny - bar_h / 2, playhead - nx, bar_h))
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(blue)
                p.drawRoundedRect(rect, 3, 3)
                p.restore()
        # 音高游标:白色圆形亮点 + 柔和光晕,高度随当前音符上下移动,无音符落到底部等待
        self._draw_pitch_cursor(p, playhead, top, bh, bar_h, now)

    def _draw_pitch_cursor(self, p, cx, top, bh, bar_h, now):
        """播放头处的白色圆形"音高游标":直径稍大于音准线粗细,带柔和光晕(径向渐变)。
        - **有音符**(唱着):加**轻阻尼**滑向该音符音高(跟手但不生硬,不再瞬吸);
        - **句末/间奏无音符**:加**更慢阻尼**缓缓回落到底部,不直接砸底;
        - **演唱结束**(过了最后一个音符=没音准线了):**隐藏游标**(不再画)。"""
        notes = self.song.notes
        last_end = notes[-1].end if notes else 0
        if now > last_end:                           # 全部音符已过=演唱结束、没音准线 → 隐藏
            self._dot_y = None                       # 复位:下次(如 seek 回去)有音符时重新吸附
            return
        cur = None
        for n in notes:
            if n.start <= now < n.end:
                cur = n
                break
            if n.start > now:                        # 音符按 start 排序,过了当前时刻即停
                break
        if cur is not None:                          # 有音符:轻阻尼滑向音高
            target = self._midi_to_y(cur.midi, top, bh)
            damp = self.PITCH_DOT_DAMP_NOTE
        else:                                        # 句末/间奏无音符:更慢阻尼回落到底部
            target = top + bh
            damp = self.PITCH_DOT_DAMP_FALL
        if self._dot_y is None:
            self._dot_y = target                     # 首帧直接就位,不从别处滑入
        else:
            self._dot_y += (target - self._dot_y) * damp
        dy = self._dot_y
        thick = bar_h + self.PITCH_OW                # 音准线总粗
        r_dot = thick * 0.65                         # 直径≈1.3×总粗,稍大于音准线粗细
        glow_r = r_dot * 2.6
        grad = QtGui.QRadialGradient(cx, dy, glow_r)  # 白亮心 → 外圈透明的柔和光晕
        grad.setColorAt(0.0, QtGui.QColor(255, 255, 255, 210))
        grad.setColorAt(0.32, QtGui.QColor(255, 255, 255, 120))
        grad.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QBrush(grad))
        p.drawEllipse(QtCore.QPointF(cx, dy), glow_r, glow_r)
        p.setBrush(QtGui.QColor(255, 255, 255))       # 实心白亮点(不透明,绿幕干净抠)
        p.drawEllipse(QtCore.QPointF(cx, dy), r_dot, r_dot)

    # ---------------------------------------------------- 开头标题卡
    def _compute_title_dur(self):
        """标题卡时长:据首句起点定,标题卡在首句前 ~600ms 之前收完(不压到第一句歌词);
        无歌词则用 TITLE_MAX。首句太早(<~600ms)则不显标题(=0)。"""
        first = self.song.lines[0].start if self.song.lines else None
        if first is None:
            self._title_dur = self.TITLE_MAX
        else:
            self._title_dur = min(self.TITLE_MAX, max(0, first - 600))

    def _title_alpha(self, now):
        """标题卡透明度:淡入 → 常显 → 淡出(据 now 在 [0, title_dur] 的位置)。"""
        d = self._title_dur
        if d <= 0:
            return 0.0
        fi = min(self.TITLE_FADE_IN, d * 0.3)
        fo = min(self.TITLE_FADE_OUT, d * 0.4)
        if fi > 0 and now < fi:
            return now / fi
        if fo > 0 and now > d - fo:
            return max(0.0, (d - now) / fo)
        return 1.0

    def _title_entry(self):
        """构建/缓存标题卡各行 pixmap(经典 KTV 蓝底白描边,同已唱歌词;字号比歌词放大)。
        行 = 歌名(大) / 原唱:<歌手>(有才显) / 演唱:<主播名>。返回 {items, total_h}。"""
        if self._title_card is not None:
            return self._title_card
        title = (self.song.title or "").strip()
        if not title:
            return None
        fam = self.font_big.family()
        bold = self.font_big.bold()
        tf = QtGui.QFont(fam, 46); tf.setBold(bold)     # 歌名(大)
        cf = QtGui.QFont(fam, 26); cf.setBold(bold)     # 原唱/演唱(中)

        def mk(text, font):
            fm = QtGui.QFontMetrics(font)
            total = fm.horizontalAdvance(text)
            scale = font.pointSizeF() / max(1.0, self.font_big.pointSizeF())
            ob = max(2, round(self.OW_BLACK * scale))   # 描边宽按字号等比放大
            ow = max(1, round(self.OW_WHITE * scale))
            pix = self._make_line_pixmap(
                [(text, total)], font, self.COL_SUNG,
                strokes=[(self.COL_OUTLINE, ob), (self.COL_SUNG_OUTLINE, ow)])
            return [pix, total, fm.height()]

        items = []                                       # [pix, 宽, 高, 行后步进(下一行 y 增量)]
        t = mk(title, tf)
        items.append(t + [t[2] * 1.15])                  # 标题占满一行高 + 半行间隔再接原唱
        artist = (self.song.artist or "").strip()
        if artist:
            c = mk("原唱：" + artist, cf)
            items.append(c + [c[2] * 1.08])
        perf = (self.performer or "").strip() or "八门官上"
        c = mk("演唱：" + perf, cf)
        items.append(c + [c[2] * 1.08])
        total_h = sum(it[3] for it in items[:-1]) + items[-1][2]   # 末行不计行后步进
        self._title_card = {"items": items, "total_h": total_h}
        return self._title_card

    def _main_area(self):
        """主区域(顶端歌单下沿 ~ 底部歌词上沿)的竖直范围 (top, bottom)——标题卡据此居中,
        而非整窗居中,也不是到音准带顶(那会偏上)。直播捕获的就是歌单与底部歌词之间这整块,
        标题卡阶段歌词/音准线都不画,故下界取歌词行顶 `cy_top`,居中才落在画面中间。"""
        if self.show_setlist and self.setlist_titles:
            top = self.setlist_y + self._setlist_h()
        else:
            top = 24
        _, _, cy_top = self._layout()
        return top, max(top + 1, cy_top)

    def _draw_title_card(self, p, w, h, alpha):
        """在**主区域**(歌单下沿 ~ 歌词上沿)内**居中**画标题卡,整体按 alpha 渐隐
        (绿幕下靠直播伴侣抠像羽化合成软淡入淡出)。"""
        if alpha <= 0:
            return
        ent = self._title_entry()
        if not ent:
            return
        area_top, area_bottom = self._main_area()
        avail = area_bottom - area_top
        yy = area_top + max(0.0, (avail - ent["total_h"]) * 0.5)   # 主区域正中
        p.save()
        p.setOpacity(alpha)
        for pix, pw, ph, adv in ent["items"]:
            p.drawPixmap(QtCore.QPointF((w - pw) / 2 - self.PAD, yy - self.PAD), pix)
            yy += adv
        p.restore()

    # 引导圆点:仅前奏/长间奏出现(空档≥此值,单位ms);句间正常小停顿不显示
    GAP_FOR_DOTS = 8000
    LEAD = 3500          # 圆点倒计时提前量(ms)
    PHRASE_GAP = 4000    # 空档≥此值=新乐句起点(其首行回到上排,阅读顺序才顺)

    @staticmethod
    def _align_x(x, w, total, align, margin):
        """按对齐方式算内容左边界:left=靠左留 margin,right=靠右留 margin,center=居中。"""
        if align == "left":
            return x + margin
        if align == "right":
            return x + w - margin - total
        return x + (w - total) / 2

    def _compute_slots(self):
        """预计算每行的槽位与"乐句起点":大空档(≥PHRASE_GAP)后回到上排(0),句内左右交替。
        这样一段的首句总在上排(顺读)、句内两行永远异槽(不重叠)、起唱不跳位。"""
        lines = self.song.lines
        self._line_slot = [0] * len(lines)     # 0=上行/左对齐,1=下行/右对齐
        self._line_pstart = [False] * len(lines)   # 是否新乐句起点(大空档后)
        for i, ln in enumerate(lines):
            if i == 0:
                self._line_pstart[i] = True
                self._line_slot[i] = 0
            elif ln.start - lines[i - 1].end >= self.PHRASE_GAP:
                self._line_pstart[i] = True
                self._line_slot[i] = 0                       # 乐句首→上排
            else:
                self._line_slot[i] = 1 - self._line_slot[i - 1]   # 句内交替

    def _draw_lyrics(self, p, x, cy_top, w, now):
        lines = self.song.lines
        if not lines:
            return
        font = self.font_big                 # 上下两行同尺寸(不再上大下小)
        lh = self._line_h
        margin = w * 0.06
        cy_bot = cy_top + lh * 1.15          # 下行略低于上行,左右错开
        dots_dy = lh * 0.9                   # 圆点在所属行顶部上方

        def pos(i):                          # 行 i 的槽位(用预计算表:乐句首在上排)
            return (cy_top, "left") if self._line_slot[i] == 0 else (cy_bot, "right")

        # 当前活跃行(唱中或刚唱完 300ms 内)
        cur = None
        for i, ln in enumerate(lines):
            if ln.start <= now < ln.end + 300:
                cur = i
            elif ln.start > now:
                break

        if cur is not None:
            cy, align = pos(cur)
            self._draw_word_line(p, lines[cur], x, cy, w, font, now, align, margin)
            j = cur + 1                       # 后一行仅"同乐句"才提前显示(远句不显,避免重叠)
            if j < len(lines) and not self._line_pstart[j]:
                cy2, align2 = pos(j)
                self._draw_word_line(p, lines[j], x, cy2, w, font, now, align2, margin)
            return

        # 无活跃行:即将唱的行 nxt(+同乐句后一行)提前显示;前奏显歌名;前奏/长间奏给圆点
        nxt = next((i for i, ln in enumerate(lines) if ln.start > now), None)
        if nxt is None:
            return
        remain = lines[nxt].start - now
        gap = (lines[nxt].start if nxt == 0
               else lines[nxt].start - lines[nxt - 1].end)
        cy, align = pos(nxt)                  # nxt 若为乐句首(大空档后)→ 上排,顺读
        # (前奏歌名不再在此显示——开头已有居中"标题卡"承载歌名/原唱/演唱,旧的 ♪歌名-歌手 预告已移除)
        self._draw_word_line(p, lines[nxt], x, cy, w, font, now, align, margin)
        j = nxt + 1
        if j < len(lines) and not self._line_pstart[j]:
            cy2, align2 = pos(j)
            self._draw_word_line(p, lines[j], x, cy2, w, font, now, align2, margin)
        if gap >= self.GAP_FOR_DOTS:          # 长间奏/前奏:此时 nxt 必是乐句首=上排,圆点贴其句首上方
            ent = self._word_entry(lines[nxt], font)
            lx = self._align_x(x, w, ent["total"], align, margin)
            self._draw_lead_dots(p, lx, cy - dots_dy, remain, self.LEAD)

    def _draw_lead_dots(self, p, left_x, cy, remain, lead):
        """KTV 引导圆点:长间奏整段就摆好 n 个,最后 lead 秒内**逐个消失**(不是变色/变填充)——
        剩余越少画得越少,还亮着的那些原样保留、位置不动。**开唱前留一个空拍**:把 lead 分成 n 个
        圆点 + 1 个空拍(slot=lead/(n+1)),所有圆点在开唱前 slot ms 就消失完,空一拍后才起唱
        (不再是"最后一个圆点消失与第一句开始重叠")。配色同已唱歌词:蓝底 + 白描边 + 黑 keyline
        (最外黑,绿幕干净抠)。大小按行高约 0.55 倍直径,与图片中圆点/文字比例一致。
        从 left_x 起横排(放在即将唱那行的顶部、与行首对齐)。"""
        n = 4
        slot = lead / (n + 1)              # n 个圆点 + 1 个空拍;圆点在开唱前 slot ms 全部消失
        eff = remain - slot                # 空拍:开唱前 slot 内不显圆点
        if eff <= 0:
            lit = 0
        elif eff >= lead - slot:
            lit = n
        else:
            lit = int(np.ceil(eff / (lead - slot) * n))
        lit = max(0, min(n, lit))
        r = self._line_h * 0.28                 # 半径≈0.28×行高 → 直径≈0.55×行高(仿图片比例)
        gap = r * 2.9                           # 圆心间距(留出白描边+黑边不相撞)
        kw = max(1.0, r * 0.12)                 # 黑 keyline 宽(外圈)
        ww = max(1.5, r * 0.22)                 # 白描边宽(中圈)
        p.setPen(QtCore.Qt.NoPen)
        for i in range(lit):                    # 只画还亮着的,其余不画=已消失
            c = QtCore.QPointF(left_x + r + i * gap, cy)
            p.setBrush(self.COL_OUTLINE);       p.drawEllipse(c, r + ww + kw, r + ww + kw)
            p.setBrush(self.COL_SUNG_OUTLINE);  p.drawEllipse(c, r + ww, r + ww)
            p.setBrush(self.COL_SUNG);          p.drawEllipse(c, r, r)

    # ── 经典 KTV 字幕配色(2026-07-15)──────────────────────────────────────
    # 未唱=白底黑描边;已唱=蓝底白描边。绿幕边界:最外一圈始终是黑(抗锯齿边缘落在黑上,
    # 绿幕可干净抠),已唱的白描边内嵌在黑 keyline 里(否则白直接贴绿,抠像会留绿边)。
    # 两态外轮廓总宽相同(都是 OW_BLACK),逐字擦除的裁剪边界才对得齐、无台阶。
    COL_UNSUNG = QtGui.QColor(255, 255, 255)        # 未唱填充:白
    COL_SUNG = QtGui.QColor(28, 42, 205)            # 已唱填充:经典 KTV 蓝
    COL_OUTLINE = QtGui.QColor(0, 0, 0)             # 黑(未唱主描边 / 已唱最外 keyline)
    COL_SUNG_OUTLINE = QtGui.QColor(255, 255, 255)  # 已唱描边:白
    OW_BLACK = 6                                     # 黑描边/keyline 总宽(px,居中于字形路径)
    OW_WHITE = 4                                     # 白描边总宽(<OW_BLACK,外圈才露出黑 keyline)
    # 音准线:粗细/描边与歌词笔画看齐(黑描边,不再淡化);总粗≈bar_h+ow
    PITCH_BAR_H = 6.0        # 音准块填充高
    PITCH_OW = 2            # 音准块黑描边宽
    # 音高游标阻尼(每帧朝目标走的比例,越小越慢/越"阻尼"):有音符时响应式滑向音高(不生硬),
    # 句末无音符时更慢地阻尼回落到底部
    PITCH_DOT_DAMP_NOTE = 0.3   # 有音符:跟随旋律滑到音高(比瞬吸柔和,又够跟手)
    PITCH_DOT_DAMP_FALL = 0.08  # 句末/间奏:缓缓回落到底部
    # 开头标题卡(歌名/原唱/演唱):前几秒居中显示后渐隐,再出歌词/音准线
    TITLE_MAX = 5500        # 最长显示(ms);实际取 min(此值, 首句起点-600)
    TITLE_FADE_IN = 400
    TITLE_FADE_OUT = 1200
    # ── 礼物菜单(绿幕左侧竖排"礼物→权益"引导条)──────────────────────
    # 每张卡片 = 礼物图标(左) + 自定义文字(右,白),**无底板**——各自描一圈边(贴纸式轮廓)。
    # **必须描边**:礼物图/文字直接贴绿会被抠像留绿边(半透明边与绿混合);描边法把内容的
    # 剪影(alpha)填成描边色、在 8 个方向偏移各画一遍,再把内容盖上——等价歌词的黑 keyline:
    # 抗锯齿边缘落描边色上、绿幕干净抠。emoji 也走剪影(不受"字形路径不出彩色"限制)。
    # **描边宽/间距/颜色 现为可调实例属性**(配置窗滑块+取色器控;IPC gift_outline/gift_gap/gift_color),
    # 下面只是默认值与上限。描边色用**不透明**深灰:opaque=绿幕干净不留暗绿边,淡化靠选浅灰而非半透明。
    GIFT_ICON = 48          # 图标显示边长(px,base;实际乘 gift_scale)
    GIFT_ICON_GAP = 8       # 图标与文字间距(base)
    GIFT_TEXT_PT = 16       # 自定义文字字号(base)
    GIFT_GAP_DEF = 4        # 卡片竖直间距默认(px,绝对;配置窗滑块可调)
    GIFT_GAP_MAX = 24
    GIFT_OUTLINE_DEF = 1.0  # 描边宽默认(px,剪影外扩;0=不描边)
    GIFT_OUTLINE_MAX = 3.0
    GIFT_COLOR_DEF = "#333333"   # 描边颜色默认(深灰;opaque 绿幕干净,淡化选浅灰)
    GIFT_SCALE_MIN = 0.4    # 尺寸下限(配置窗滑块 40%;可缩得更小)
    GIFT_SCALE_MAX = 2.0    # 尺寸上限(配置窗滑块 200%)
    # 描边的 8 个偏移方向(×描边宽 r):内容剪影填色在各方向画一遍 = 一圈轮廓
    _GIFT_OUTLINE_OFFS = ((-1, -1), (0, -1), (1, -1), (-1, 0),
                          (1, 0), (-1, 1), (0, 1), (1, 1))

    @staticmethod
    def _outlined(p, path, fill, strokes):
        """按 strokes(由外到内=由宽到窄的 [(颜色,线宽)])逐层描边,最后填 fill。
        最外层(最宽)保持黑,让抗锯齿边缘落在黑上、绿幕干净抠;更窄的内层在边缘露出一圈
        (用于'蓝底白描边+黑 keyline'的经典 KTV 描边)。单色黑描边则传单元素 strokes。"""
        p.setBrush(QtCore.Qt.NoBrush)
        for col, w in strokes:
            pen = QtGui.QPen(col, w)
            pen.setJoinStyle(QtCore.Qt.RoundJoin)
            p.setPen(pen)
            p.drawPath(path)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(fill)
        p.drawPath(path)

    PAD = 6   # 文字 pixmap 四周余量:容纳描边宽度+抗锯齿出血

    def _make_line_pixmap(self, words, font, fill, ow=1, strokes=None):
        """把一行字(描边+fill 填充)渲染成透明底 pixmap;words=[(文本, 步进宽)]。
        strokes 给出则按其分层描边(经典 KTV 双色描边);否则退化为单层黑描边(线宽 ow)。"""
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
        self._outlined(p, path, fill,
                       strokes if strokes is not None else [(QtGui.QColor(0, 0, 0), ow)])
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
                   # 未唱:白底 + 黑描边(经典 KTV;外轮廓宽=OW_BLACK,与 hi 对齐)
                   "base": self._make_line_pixmap(words, font,
                                                  self.COL_UNSUNG, self.OW_BLACK),
                   "hi": None, "font": font}
            self._word_cache[key] = ent
            while len(self._word_cache) > 8:     # 有界,防整首歌驻留(本机内存紧)
                del self._word_cache[next(iter(self._word_cache))]
        return ent

    def _draw_word_line(self, p, line, x, cy, w, font, now, align="center", margin=0):
        ent = self._word_entry(line, font)
        left = self._align_x(x, w, ent["total"], align, margin) - self.PAD
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
        if hi > 0:      # 高亮版整行盖上,裁剪到进度线;两版外轮廓同宽,边界干净
            if ent["hi"] is None:
                # 已唱:蓝底 + 白描边 + 黑 keyline(最外黑,保绿幕边界干净)
                ent["hi"] = self._make_line_pixmap(
                    ent["words"], ent["font"], self.COL_SUNG,
                    strokes=[(self.COL_OUTLINE, self.OW_BLACK),
                             (self.COL_SUNG_OUTLINE, self.OW_WHITE)])
            p.save()
            p.setClipRect(QtCore.QRectF(left, top, self.PAD + hi, ent["H"]))
            p.drawPixmap(QtCore.QPointF(left, top), ent["hi"])
            p.restore()

    def _draw_plain_line(self, p, text, x, cy, w, font, col, align="center", margin=0):
        key = (text, font.pointSizeF(), col.rgba())
        ent = self._plain_cache.get(key)
        if ent is None:
            fm = QtGui.QFontMetrics(font)
            total = fm.horizontalAdvance(text)
            ent = {"total": total, "ascent": fm.ascent(),
                   "pix": self._make_line_pixmap([(text, total)], font, col, 1)}
            self._plain_cache[key] = ent
            while len(self._plain_cache) > 8:
                del self._plain_cache[next(iter(self._plain_cache))]
        lx = self._align_x(x, w, ent["total"], align, margin)
        p.drawPixmap(QtCore.QPointF(lx - self.PAD,
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

    def _draw_hotkeys(self, p, w, h):
        """右下角快捷键提示,与左下播放信息同款(font_status + 白字黑描边)。两行,叠在状态栏上方
        (右对齐,不与左下播放信息横向撞)。静态文案,建一次缓存。"""
        if self._hotkey_pix is None:
            lines = ["←→ 步退/进   ↑↓ 升降调   R 原唱/伴奏",
                     "P 音准线   Q 字体   O 歌单   G 礼物   Ctrl+↑↓ 移歌单"]
            fm = QtGui.QFontMetrics(self.font_status)
            self._hotkey_pix = [(fm.horizontalAdvance(s), self._make_line_pixmap(
                [(s, fm.horizontalAdvance(s))], self.font_status,
                QtGui.QColor(230, 230, 230), 3)) for s in lines]
        fh = QtGui.QFontMetrics(self.font_status).height()
        n = len(self._hotkey_pix)
        base = h - 26               # 最下一行快捷键与左下播放信息同底,其余行叠其上
        for i, (total, pix) in enumerate(self._hotkey_pix):
            y = base - (n - 1 - i) * fh
            p.drawPixmap(QtCore.QPointF(w - total - 10 - self.PAD, y - self.PAD), pix)

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
            if e.modifiers() & QtCore.Qt.ControlModifier:
                self._move_setlist(-1)              # Ctrl+↑ 歌单上移
            else:
                self._change_key(self.semitone + 1)
        elif k == QtCore.Qt.Key_Down:
            if e.modifiers() & QtCore.Qt.ControlModifier:
                self._move_setlist(1)               # Ctrl+↓ 歌单下移
            else:
                self._change_key(self.semitone - 1)
        elif k == QtCore.Qt.Key_R:
            self._toggle_vocal()
        elif k == QtCore.Qt.Key_P:
            self.show_pitch = not self.show_pitch   # 音准线显隐(pc-service 经 STATE 回读同步手机)
        elif k == QtCore.Qt.Key_O:
            self.show_setlist = not self.show_setlist   # 顶端歌单显隐(pc-service 回读缓存,同 P)
        elif k == QtCore.Qt.Key_G:
            self.show_gifts = not self.show_gifts       # 礼物菜单显隐(pc-service 回读缓存,同 P/O)
        elif k == QtCore.Qt.Key_Q:
            self._apply_font(self.font_idx + 1)     # 循环切歌词字体(pc-service 经 STATE 回读缓存)
            self._flash_status("字体: " + self.FONTS[self.font_idx][0])
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
        self._ever_played = False            # 新歌载入=未演唱:开唱前绿幕不显歌词/音准线
        pit = [n.midi for n in song.notes] or [60]
        self.midi_lo = min(pit) - 2
        self.midi_hi = max(pit) + 2
        note_end = song.notes[-1].end if song.notes else 0
        line_end = song.lines[-1].end if song.lines else 0
        self.content_end = max(note_end, line_end)
        self._compute_slots()                # 换歌:重算 KTV 双行槽位
        self._compute_title_dur()            # 换歌:重算开头标题卡时长
        self._word_cache.clear()             # 换歌清文字渲染缓存
        self._plain_cache.clear()
        self._status_cache = None
        self._title_card = None              # 换歌:标题卡(歌名/原唱)作废,重建
        self._dot_y = None                   # 换歌:音高游标复位(下次绘制吸附到目标)
        self.engine.load(self.accompany)     # 换源、归位到0、清调、暂停
        self.smtc.set_song(song.title, song.artist, song.duration_ms() / 1000)
        print("[LOAD]", mid, song.title, "-", song.artist, flush=True)
        return True

    def _smtc_tick(self):
        """定时同步:SMTC(若启用)+ 服务模式下把播放状态/进度上报给 pc-service(stdout)。"""
        self.smtc.set_playing(self.engine.playing)
        self.smtc.update_position(self.engine.current_ms() / 1000)
        if self.engine.playing:      # 隐藏时也追踪:开唱后再显示窗口不会闪一帧空白
            self._ever_played = True
        if self.isVisible():         # 可见时持续记录桌面位置(捕获经其它途径的移动;隐藏时保留旧值)
            self._saved_pos = (self.x(), self.y())
        if self.service_mode:
            st = {"pos": round(self.engine.current_ms()),
                  "dur": round(self.song.duration_ms()),
                  "playing": bool(self.engine.playing),
                  "key": self.semitone, "vocal": self.use_vocal,
                  "vol": self.engine.volume_pct, "pitch": self.show_pitch,
                  "font": self.font_idx,
                  "setlist_show": self.show_setlist, "setlist_y": self.setlist_y,
                  "gifts_show": self.show_gifts,
                  "gift_x": int(self.gift_x), "gift_y": int(self.gift_y),
                  "gift_scale": round(self.gift_scale, 3),
                  "gift_outline": round(self.gift_outline, 2),
                  "gift_gap": int(self.gift_gap), "gift_color": self.gift_color.name(),
                  "mid": self.song.mid, "title": self.song.title,
                  "artist": self.song.artist}
            if self._saved_pos is not None:   # 有真实位置(显示过 / pc-service 已下发)才上报,免把 (0,0) 缓存进去
                st["win_x"], st["win_y"] = int(self._saved_pos[0]), int(self._saved_pos[1])
            try:
                sys.stdout.write("STATE " + json.dumps(st, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            except Exception:
                pass

    def mousePressEvent(self, e):
        if e.button() == QtCore.Qt.LeftButton:
            # 命中检测:按在礼物条上 → 单独拖礼物条;否则 → 拖整窗(摆捕获区,原行为)
            lp = e.position().toPoint()
            box = self._gift_bbox()
            if box is not None and box.contains(QtCore.QPointF(lp)):
                self._gift_drag = True
                self._gift_drag_off = (lp.x() - self.gift_x, lp.y() - self.gift_y)
            else:
                self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._gift_drag:                          # 拖礼物条:改 gift_x/gift_y(夹在窗内,不拖丢)
            lp = e.position().toPoint()
            self.gift_x = int(max(0, min(self.width() - 24, lp.x() - self._gift_drag_off[0])))
            self.gift_y = int(max(0, min(self.height() - 24, lp.y() - self._gift_drag_off[1])))
        elif self._drag_pos is not None:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None
        self._gift_drag = False
        if self.isVisible():
            self._saved_pos = (self.x(), self.y())   # 拖动结束即记住位置(下次 show 恢复)

    def show_lyrics(self):
        """显示歌词:恢复正常渲染并显示窗口,并把窗口恢复到上次关闭时的桌面位置。
        服务模式隐藏会销毁原生 HWND,再 show 时 Qt 会放到默认位置,故显式 move 回来。"""
        self.blank = False
        self.show()
        if self._saved_pos is not None:
            self.move(*self._saved_pos)
        self.raise_()
        self.activateWindow()      # 取焦点,快捷键才生效

    def hide_lyrics(self):
        """隐藏歌词:先把内容清成全绿(同步画一帧,让捕获冻结帧=纯绿),稍后隐藏并销毁原生窗口。"""
        self.blank = True
        self.repaint()             # 同步立刻画出全绿
        QtCore.QTimer.singleShot(80, self._hide_and_release)   # 留几帧给直播伴侣抓到绿帧

    def _hide_and_release(self):
        """隐藏后销毁原生 HWND(Qt 对象和状态保留,下次 show() 自动重建窗口)。
        本窗口是 WA_TranslucentBackground 的逐像素 alpha 分层窗口,实测只要它的 HWND
        存在——哪怕 SW_HIDE 隐藏——ToDesk 远程会话里光标每移动一次,DWM 就要多做一次
        全窗合成(dwm.exe +18~25% 单核,远端视频帧率骤降),表现为远程鼠标严重拖影卡顿;
        普通 LWA_ALPHA 分层窗口无此问题。销毁 HWND 后开销归零;直播伴侣的窗口捕捉源
        按标题匹配,重新 show 后会自动重挂。"""
        self.hide()
        self.destroy()

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
        # 服务模式:不预建窗口——原生 HWND 只在 show 时由 Qt 创建、hide 后销毁
        # (见 _hide_and_release):这种逐像素 alpha 分层窗口只要存在(即使隐藏),
        # ToDesk 远程会话里光标一动 DWM 就多合成一次 → 远程鼠标严重拖影。
        # 显隐一律由 stdin 指令走 Qt 自己的 show/hide(外部 win32 SW_SHOW 不会触发 Qt 重绘)。

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
            # 音准线显隐(手机遥控 + 缓存)
            elif c == "pitch" and arg is not None:
                win.show_pitch = (arg == "1")
            # 歌词字体(Q 键循环 + pc-service 缓存恢复)
            elif c == "font" and arg is not None:
                try:
                    win._apply_font(int(arg))
                except ValueError:
                    pass
            # 顶端滚动歌单:内容(pc-service 据曲库勾选推来)/ 显隐 / 竖直位置
            elif c == "setlist" and arg is not None:
                try:
                    win.set_setlist(json.loads(arg))
                except Exception:
                    pass
            elif c == "setlist_show" and arg is not None:
                win.show_setlist = (arg == "1")
            elif c == "setlist_y" and arg is not None:
                try:
                    win.setlist_y = int(arg)
                except ValueError:
                    pass
            # 礼物菜单:内容(pc-service 配置窗选礼物+自定义文字推来)/ 显隐 / 位置
            elif c == "gifts" and arg is not None:
                try:
                    win.set_gifts(json.loads(arg))
                except Exception:
                    pass
            elif c == "gifts_show" and arg is not None:
                win.show_gifts = (arg == "1")
            elif c == "gift_pos" and arg is not None:
                try:
                    xs, ys = arg.split()
                    win.gift_x, win.gift_y = int(xs), int(ys)
                except Exception:
                    pass
            elif c == "gift_scale" and arg is not None:
                try:
                    v = max(win.GIFT_SCALE_MIN, min(win.GIFT_SCALE_MAX, float(arg)))
                    if v != win.gift_scale:
                        win.gift_scale = v
                        win._gift_pix = None        # 尺寸变了重建卡片
                except ValueError:
                    pass
            elif c == "gift_outline" and arg is not None:
                try:
                    v = max(0.0, min(win.GIFT_OUTLINE_MAX, float(arg)))
                    if v != win.gift_outline:
                        win.gift_outline = v
                        win._gift_pix = None        # 描边宽变了重建卡片
                except ValueError:
                    pass
            elif c == "gift_gap" and arg is not None:
                try:
                    win.gift_gap = max(0, min(win.GIFT_GAP_MAX, int(float(arg))))
                except ValueError:
                    pass                            # 间距只影响竖排,不重建 pixmap
            elif c == "gift_color" and arg is not None:
                col = QtGui.QColor(arg.strip())
                if col.isValid() and col.name() != win.gift_color.name():
                    win.gift_color = col
                    win._gift_pix = None            # 描边色变了重建卡片
            # 窗口桌面位置(pc-service 据 state_cache.json 在拉起时下发,恢复上次关闭时的位置)
            elif c == "pos" and arg is not None:
                try:
                    xs, ys = arg.split()
                    win._saved_pos = (int(xs), int(ys))
                    if win.isVisible():
                        win.move(*win._saved_pos)
                except Exception:
                    pass
            # 演唱者(主播名):pc-service 托盘可改,开头标题卡显示"演唱:<名>"
            elif c == "performer" and arg is not None:
                win.performer = arg
                win._title_card = None          # 重建标题卡
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
