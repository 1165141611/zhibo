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

# ── scrcpy 无线投屏 ───────────────────────────────────
# 进入悬浮态时,PC 自动经无线 adb 连手机并拉起 scrcpy(只投屏、不带声音)。
# 采用【安卓11无线调试】(端口动态,靠 adb mDNS 发现)——需先手动配对过一次:
#     adb pair 手机IP:配对端口     (配对码见手机"无线调试"页)
ADB_PATH = "adb"          # 在 PATH 里,直接用命令名
SCRCPY_PATH = "scrcpy"    # 在 PATH 里

# 无线连接方式:两条路都会试,谁先连上用谁。
#  A)固定端口(推荐,免配对码):先用数据线执行一次 `adb tcpip 5555`,
#    之后拔线,PC 直接 `adb connect 手机IP:5555`。手机重启后需再执行一次 tcpip。
#    把 SCRCPY_FIXED_PORT 设为 0 可关闭这条路。
#  B)安卓11无线调试(动态端口):需先 `adb pair 手机IP:配对端口` 配对一次,
#    之后端口每次随机,PC 靠 adb mDNS 自动发现。配对可持久(手机重启也在)。
SCRCPY_FIXED_PORT = 5555  # A 方案端口;0 = 停用,只走 mDNS 动态发现
SCRCPY_USE_MDNS = True    # B 方案:找不到固定端口时,用 mDNS 发现动态端口

# scrcpy 启动参数。--no-audio = 不抓手机声音;可按需加裁剪/窗口标题等,例如:
#   "--window-title=手机投屏", "--crop=1080:1230:0:1500", "--stay-awake"
SCRCPY_ARGS = ["--no-audio", "--window-title=手机投屏"]
