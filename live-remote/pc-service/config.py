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
# 用于 pycaw 单独调节音量。**重大坑(2026-07-14 实锤)**:MediaSDK_Server.exe 是腾讯**共用**
# 媒体进程——直播伴侣也会起同名子进程,并在 PLAYBACK 3/4(监听)+ VIRTUAL REC 3/4(推流主麦)
# 上挂着**麦克风链路**的音频会话!按进程名裸匹配会把它们当 QQ音乐 调音量:暂停 BGM 渐弱到 0
# = 把直播间主麦静音(即"点暂停 BGM 通道就静音"事故;VIRTUAL REC 3/4 音量莫名归零悬案同源)。
# 因此 server.py 匹配会话时做**归属校验**:QQMUSIC_OWNER_CHECK 里列出的共用进程,必须父进程链
# (向上 4 级)里含指定归属进程才算 QQ音乐;并优先只在 QQMUSIC_DEVICE_HINT 设备上找会话。
QQMUSIC_PROCS = ["MediaSDK_Server.exe", "QQMusic.exe"]
QQMUSIC_OWNER_CHECK = {"MediaSDK_Server.exe": "QQMusic.exe"}   # 共用进程 → 父链须含此进程
QQMUSIC_DEVICE_HINT = "PLAYBACK 1/2"   # BGM 设备白名单(FriendlyName 含此串);白名单上找不到
                                       # 任何会话时才退全设备搜(归属校验仍兜底,推流链不受伤)

# ── QQ音乐 传输控制(SMTC / winrt,有方向)────────────────
# winrt 子进程(smtc_helper.py)按 AUMID 从**所有**系统媒体会话里锁定 QQ音乐 自己的会话,
# 用它的 try_play/try_pause 做**有方向**控制(不再模拟全局媒体键——媒体键会被路由到"抢占
# 系统当前会话"的 App:WeSing/浏览器/直播伴侣,是"手机控制 QQ音乐 时好时坏、正在播的歌
# 不同步"的根因)。下面是会话 AUMID(source_app_user_model_id)的匹配片段(小写子串)。
# 若发现 BGM 始终锁不到 QQ音乐:看 server.log 里子进程启动时打的 `#SMTC 媒体会话 AUMID: [...]`
# 那行,把 QQ音乐 对应的那串(的稳定片段)填到这里。
QQMUSIC_SMTC_HINT = "qqmusic"

# bgm_vol 反向同步:后台周期回读 QQ音乐 音量的间隔(秒),把 PC 上手动改的音量同步回手机。
# 只在 BGM 在播且不渐变时读,走 pycaw 专线程。
BGM_VOL_POLL_INTERVAL = 4.0

# ── K歌播放器(独立子进程,由本服务拉起 + 按 HWND 显隐)──
import os as _os, sys as _sys
BASE_DIR = _os.path.dirname(_os.path.abspath(__file__))
KARAOKE_DIR   = _os.path.abspath(_os.path.join(BASE_DIR, "..", "..", "karaoke-player"))
PLAYER_PATH   = _os.path.join(KARAOKE_DIR, "player.py")
PLAYER_PYTHON = _sys.executable    # 与本服务同一解释器(已装 PySide6/sounddevice/numpy/audiotsm)
PLAYER_DEVICE = 27                 # 回退索引(名字解析失败时用;索引会随设备增减漂移,不可靠)
# 优先按名字实时解析输出设备(防漂移):找 WASAPI 下名字含此串、有输出通道的设备。
# 教训:接相机/ToDesk 虚拟音频等会新增音频端点,挤动 WASAPI 枚举顺序,写死索引 27 会指到别的设备
# (曾指到 ToDesk Virtual Audio,音乐灌进去听不到)。见 server.py _resolve_player_device。
PLAYER_DEVICE_NAME    = "PLAYBACK 1/2"   # ROUTIST R2 的伴奏/BGM 路由
PLAYER_DEVICE_HOSTAPI = "WASAPI"
PLAYER_TITLE  = "KaraokePlayer"    # 播放器窗口标题(player.py 里写死),karaoke_win 靠它找 hwnd

# ── 曲库导入(手动扫描窗口触发,不再后台轮询)────────────────
# PC 版 WeSing 缓存(LRU 只留最近几首)+ 手机版全民K歌(adb 拉取解密),扫描去重后勾选入库。
WESING_RES_DIR        = r"D:\WeSingCache\WeSingDL\Res"
KARAOKE_LIBRARY_DIR   = r"D:\KaraokeLibrary"
LIBRARY_JSON          = _os.path.join(KARAOKE_LIBRARY_DIR, "library.json")
LIBRARY_SUFFIXES      = ("_accompany.pcm", "_kongsinger.pcm", ".note", ".qrc")

# ── 手机版全民K歌导入(mobile_import.py + karaoke-player/mobile_convert.py)──
ADB_PATH            = r"D:\scrcpy-win64-v3.3.1\adb.exe"   # scrcpy 附带(见 CLAUDE.md)
MOBILE_PKG          = "com.tencent.karaoke"
MOBILE_FILES        = "/sdcard/Android/data/com.tencent.karaoke/files"  # qrc/note/obbligato 在此
MOBILE_STAGING_DIR  = _os.path.join(KARAOKE_LIBRARY_DIR, "_staging")    # 转换暂存
MOBILE_CONVERT_PATH = _os.path.join(KARAOKE_DIR, "mobile_convert.py")
MOBILE_TKM_WINDOW   = 180          # song↔tkm 按 mtime 聚类的时间窗(秒):qrc 与其两条 tkm 同批落盘
PREVIEW_PLAY_PATH   = _os.path.join(KARAOKE_DIR, "preview_play.py")  # 扫描窗口「试听」轻量预览播放器
PREVIEW_VOLUME      = 0.4           # 预览音量(走系统默认输出=自己听的通道,压低不吵)

# ── QQ音乐 导入(qqmusic_import.py:登录态 API 搜索 + 下明文伴奏/原唱,无音准)──
# 扫描窗口「QQ(无音准)」页签用:扫码登录一次 → 搜索 → 勾选 → 下载高质量原唱(明文 FLAC/MP3)
# → ffmpeg 转 PCM → **Demucs 人声分离出伴奏** → 写四件套(减 .note)入库。凭据存本机自用,勿外传。
QQ_CRED_PATH        = _os.path.join(KARAOKE_LIBRARY_DIR, "qq_cred.json")   # 登录凭据(cookie)
QQ_STAGING_DIR      = _os.path.join(KARAOKE_LIBRARY_DIR, "_qq_staging")    # 下载+转换暂存
QQ_ORIGINAL_QUALITY = ("FLAC", "MP3_320", "MP3_128")   # 原唱音质优先级(取第一个能出明文的)。
# 默认 **FLAC 无损优先**(~55MB,慢些):原唱既是切换参考、又是 Demucs 分离伴奏的输入源,质量要高;
# 拿不到无损再退 MP3_320/128。(QQ音乐 无可靠伴奏 stem,伴奏一律由此原唱经 Demucs 分离,见 qqmusic_import。)

# ── 礼物菜单(播放器绿幕左侧竖排"礼物→权益"引导条)──────────────
# 抖音礼物目录 API:匿名带 aid=1128 即返回 data.gifts[](id/name/diamond_count/icon.url_list)。
# 礼物列表基本静态:gifts.py 抓一次落盘缓存 + 下载图标 PNG,离线优先,refresh 才重新联网。
# 托盘"礼物菜单配置"窗据此列图标勾选;选中的 {id,自定义文字} 存 STATE["gifts"](跨重启缓存),
# 由 _push_gifts 解析成图标绝对路径 + 文字推给播放器绿幕竖排显示(见 server.py / player.py)。
GIFT_LIST_URL     = "https://live.douyin.com/webcast/gift/list/?aid=1128"
GIFT_CACHE_DIR    = _os.path.join(BASE_DIR, "gift_cache")
GIFT_CATALOG_JSON = _os.path.join(GIFT_CACHE_DIR, "gifts_catalog.json")
GIFT_ICONS_DIR    = _os.path.join(GIFT_CACHE_DIR, "icons")
