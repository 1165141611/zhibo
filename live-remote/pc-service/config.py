# -*- coding: utf-8 -*-
"""直播遥控 · 电脑后台服务配置"""

# ── 网络 ──────────────────────────────────────────────
HOST = "0.0.0.0"          # 监听所有网卡,手机才能连进来
PORT = 8765               # 手机 App / 测试网页连这个端口

# ── Studio One 声卡音轨(MIDI 控制)────────────────────
# 需与 loopMIDI 里创建的虚拟端口名一致(部分匹配即可,不区分大小写)
MIDI_PORT_NAME = "loopMIDI Port"   # 发给 Studio One(Mackie 接收自)
MIDI_FEEDBACK_NAME = "MackieFB"    # Studio One 回传静音状态(Mackie 发送到),服务读它
MIDI_CHANNEL = 0          # MIDI 通道 (0 = 通道 1)

# Studio One 用 Mackie Control 设备接收(接收自 loopMIDI Port)。
# Mackie 协议里,每条通道的"静音"按钮 = 固定 MIDI 音符:
#   通道1=16 2=17 3=18 4=19 5=20 6=21 7=22 8=23
# 我们的 4 条人声通道 = 混音台第 3~6 条 = 音符 18~21。
# 发"音符按下(vel127)+松开(vel0)"= 切换该通道静音(Mackie 是开关式)。
VOCAL_MUTE_NOTES = {
    "chat": 18,   # 通道3 聊天
    "wet":  19,   # 通道4 唱歌(湿)
    "dry":  20,   # 通道5 唱歌(干)
    "horn": 21,   # 通道6 喇叭
}

# 场景 id → {label, active}
# active = 该场景要"取消静音(开)"的那条人声通道;其余人声通道全部静音。
# 闭麦(active=None):4 条人声全部静音,BGM 不受影响。
SCENES = {
    1: {"label": "聊天", "active": "chat"},
    2: {"label": "湿唱", "active": "wet"},
    3: {"label": "干唱", "active": "dry"},
    4: {"label": "喇叭", "active": "horn"},
    5: {"label": "闭麦", "active": None},
}

# 播放/暂停 的渐强/渐弱时长(秒)。改大更柔和,改 0 则接近瞬时。
FADE_SECONDS = 1.0

# ── 背景音乐(QQ音乐)─────────────────────────────────
# 用于 pycaw 单独调节音量。QQ音乐 新版声音走的是 MediaSDK_Server.exe 这个音频进程,
# 老版可能是 QQMusic.exe,这里列一组候选,谁在出声就调谁。
QQMUSIC_PROCS = ["MediaSDK_Server.exe", "QQMusic.exe"]

# ── K歌播放器(独立子进程,由本服务拉起 + 按 HWND 显隐)──
import os as _os, sys as _sys
BASE_DIR = _os.path.dirname(_os.path.abspath(__file__))
KARAOKE_DIR   = _os.path.abspath(_os.path.join(BASE_DIR, "..", "..", "karaoke-player"))
PLAYER_PATH   = _os.path.join(KARAOKE_DIR, "player.py")
PLAYER_PYTHON = _sys.executable    # 与本服务同一解释器(已装 PySide6/sounddevice/numpy/audiotsm)
PLAYER_DEVICE = 27                 # sounddevice 输出设备索引(已验证=ROUTIST PLAYBACK 1/2)
PLAYER_TITLE  = "KaraokePlayer"    # 播放器窗口标题(player.py 里写死),karaoke_win 靠它找 hwnd

# ── 自动曲库导入器 ────────────────────────────────────
# 监听 WeSing 缓存(LRU 只留最近几首),把唱过的歌四件套拷进永久曲库,防被清掉。
WESING_RES_DIR        = r"D:\WeSingCache\WeSingDL\Res"
KARAOKE_LIBRARY_DIR   = r"D:\KaraokeLibrary"
LIBRARY_JSON          = _os.path.join(KARAOKE_LIBRARY_DIR, "library.json")
LIBRARY_SCAN_INTERVAL = 10.0       # 轮询间隔(秒)
LIBRARY_SUFFIXES      = ("_accompany.pcm", "_kongsinger.pcm", ".note", ".qrc")
