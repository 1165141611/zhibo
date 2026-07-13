# 自制K歌播放器 (karaoke-player)

直播用的**干净滚动歌词 + 原唱音高提示 + 可升降调伴奏**播放器。从 PC 版全民K歌(WeSing)扒出歌曲数据,自己当时钟播放渲染,投进直播伴侣,替代原来"投屏 K歌歌词 + 绿幕抠图"的烂方案。

> 属于 `zhibo` 直播项目的子项目之一。总览见上级目录 [../README.md](../README.md);跨项目规则见 [../CLAUDE.md](../CLAUDE.md)。

---

## 现状(2026-07-13)

**单曲 Demo 已全通**(写死《吉姆餐厅》),四件套都验证成功:

| 功能 | 实现 |
|---|---|
| 逐字高亮滚动歌词 | QRC 解码(三重魔改DES + zlib) |
| 原唱音高提示线 | `.note` 纯文本(`起始ms 时长ms MIDI音高`) |
| 干净伴奏 | 破解 WeSing 的加密PCM(静态256字节XOR) |
| 升降调 | 实时时域 WSOLA(秒切、无杂音、音量不损) |
| 伴奏/原唱切换 | 两条 PCM 缓冲切换 |

**未做(直播真正可用前的 TODO):**
- 曲库导入器:WeSing 缓存 `Res\` 只留最近约4首(LRU),需自动监视把唱过的歌四件套拷进永久曲库。
- 多曲支持 + 选歌界面(现在写死单曲)。
- 验证直播伴侣能否捕获透明置顶窗(捕不到就改普通窗 + `B` 键洋红抠像)。
- 界面打磨。

---

## 数据来源:PC 版 WeSing 缓存

- 进程 `WeSing.exe`(本机在 `D:\WeSing\WeSing.exe`)。
- 缓存根:`D:\WeSingCache\WeSingDL\Res\<songmid>\`,每首歌四件套:
  - `<mid>_accompany.pcm` —— 伴奏,**加密PCM**(见下),44.1kHz/16bit/立体声。
  - `<mid>_kongsinger.pcm` —— 原唱引导声,同格式同加密。
  - `<mid>.note` —— 原唱音高线,**纯文本**,每行 `起始ms 时长ms MIDI音高`。
  - `<mid>.qrc` —— 歌词,PC 版是 `[offset:0]\n` 明文头 + 裸密文(也可用手机版 hex-QRC)。
- **约束**:`Res\` 是 LRU,只留最近约 4 首,且只有"唱过一次"的歌才落盘。

### 两处加密(都已破解)

1. **歌词 QRC** —— hex 文本 → 三重魔改DES(ECB, key=`!@#)(*$%123ZXC!@!@#)(NHL`)→ zlib。
   实现见 `tripledes.py` + `qmc1.py`(移植自开源 LDDC 项目)。标准 3DES 解不了。
2. **音频 PCM** —— **静态 256 字节重复 XOR**(所有歌、伴奏和原唱共用同一密钥)。
   密钥存 `wesing_pcm_key.py`。解密 = `raw XOR tile(key,256)` 再当 int16 立体声。
   > 破解经过:字节熵 7.998 看似强加密,但"最长连续相同块 run=1"排除了块加密(AES/SM4 的
   > ECB 静音段会有长串相同块);自相关在 256/512/1024 显著、44100 处随机 → 周期 256 的 XOR;
   > 从歌曲开头静音段直接读出密钥。验证:解密后前 1 秒精确静音、整轨 RMS 0.2 = 真音乐。
   > (曾误判 AES 去读 WeSing 内存暴破,是弯路。)

---

## 代码结构

| 文件 | 作用 |
|---|---|
| `player.py` | 主程序:PySide6 透明置顶窗 + 逐字歌词/音高条渲染 + 热键。**顶部写死当前歌配置**。 |
| `audio_engine.py` | 实时音频引擎:后台生产者线程做**连续流式 WSOLA 变调**喂队列,sd 回调只取。 |
| `assets.py` | 数据加载:QRC 歌词解码、`.note` 音高解析、加密PCM 解密加载(`load_pcm`)。 |
| `tripledes.py` / `qmc1.py` | QRC 的魔改 DES 解密(被 assets 引用)。 |
| `wesing_pcm_key.py` | 伴奏/原唱 PCM 的静态 256 字节 XOR 密钥。 |
| `audio_test.py` | 声卡输出设备排查工具(`--list` 列设备,`<index>` 放测试音)。 |

### 关键技术决策(别再走弯路)

- **升降调必须是实时时域 WSOLA,不能用相位声码器,不能分块**:
  - 相位声码器(stftpitchshift)有**金属声/机械失真**;
  - **分块独立处理**对 WSOLA/相位声码器都不行——短块拉伸不准(音高错)+ 相位重置的周期性机械感;
  - 正解 = **单个持续状态的 WSOLA 贯穿整流**。用 `audiotsm.wsola` 的 `read_from/write_to`(维护
    状态)+ `set_speed`(动态变调),后台线程连续喂 source → 拉伸 → 连续重采样 → 队列。
  - 切调 = 在当前所听位置刷新队列 + 清管线用新调重启(响应 ~0.15s)。`semitones=0` 直通源。
- **音准线不随升降调移动**(用户要求:只表示原唱旋律趋势)。不显示录音准度(只要原唱音高提示)。
- **音频输出走声卡 ROUTIST 的 `PLAYBACK 1/2`**(WASAPI 设备索引 27,=用户 BGM 那条路由)。
  **不能**走默认的 `PLAYBACK 3/4`(那是麦克风监听通道,会回授炸麦)。
- **低内存**:机器 RAM 紧张(WeSing+StudioOne+QQ音乐全开时连 59MB 分配都失败),避免整首一次性
  分配大数组——流式处理正好也解决了这点。

---

## 运行

```bash
# 用真 Python(PATH 里的是 Store 占位版,不能用)
"C:/Users/11651/AppData/Local/Programs/Python/Python313/python.exe" player.py --device 27
```

依赖:`numpy scipy sounddevice PySide6 audiotsm pycryptodome`
(`pip install numpy scipy sounddevice PySide6 audiotsm pycryptodome`)

### 热键
| 键 | 功能 |
|---|---|
| 空格 | 播放/暂停 |
| ← → | 快退/快进 5 秒 |
| ↑ ↓ | 升/降调(实时秒切) |
| R | 伴奏 ⇄ 原唱引导声 |
| B | 背景切换(透明 / 洋红抠像 / 半透黑) |
| 鼠标拖动 | 移动窗口 |
| Esc | 退出 |

排查声卡输出设备:`python audio_test.py --list`,再 `python audio_test.py <索引>` 放测试音。

---

## 合规提示

从腾讯客户端提取版权伴奏/歌词属绕过内容保护,仅供作者自用直播,合规边界自行把握。密钥与解密仅用于
本机已有数据,勿分发。
