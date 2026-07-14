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
  - `<mid>.qrc` —— 歌词,PC 版是 `[offset:0]\n` 明文头 + 裸密文;含逐字时间轴。`assets.py` 也兼容
    手机版 hex-QRC,但**Demo 现在纯用 PC 版 `.qrc`,自包含、不依赖手机**。
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
| `player.py` | 主程序:PySide6 无边框窗 + 逐字歌词/音高条/KTV引导圆点渲染 + 热键。**顶部写死当前歌配置**。 |
| `audio_engine.py` | 实时音频引擎:后台生产者线程做**连续流式 WSOLA 变调**喂队列,sd 回调只取。 |
| `assets.py` | 数据加载:QRC 歌词解码、`.note` 音高解析、加密PCM 解密加载(`load_pcm`)。 |
| `tripledes.py` / `qmc1.py` | QRC 的魔改 DES 解密(被 assets 引用)。 |
| `wesing_pcm_key.py` | 伴奏/原唱 PCM 的静态 256 字节 XOR 密钥。 |
| `audio_test.py` | 声卡输出设备排查工具(`--list` 列设备,`<index>` 放测试音)。 |
| `smtc_publisher.py` | 发布 SMTC 会话(曾想让直播伴侣歌词助手识别,**现已弃用**,见下,留着无害)。 |
| `lyrics_overlay_demo.html` | 歌词渲染的网页版(手机端 WebView 用;直播端不用浏览器,见下)。 |

## 直播画面接入:自渲染绿底 + 绿幕抠图(已定方案)
直播端**不用浏览器源**(抖音直播伴侣没有该素材类型),也**不用 SMTC 歌词助手**(它按歌名查它自己的
在线词库、只行级无逐字无音高,且未必收录现场/K歌特供版)。最终:**播放器自己渲染 → 窗口/全屏捕获 →
直播伴侣绿幕抠掉绿色**。为抠干净:纯绿 `#00FF00` 背景,所有文字/音高块**不透明 + 黑描边**(抗锯齿边缘
落在黑边上,绿幕抠完是干净的字幕描边)。前奏/间奏用 **KTV 引导圆点**(提前显示该行 + 一排圆点倒计时逐个
熄灭,归零无缝接逐字高亮)。窗口**不置顶、无 `Qt.Tool`**(否则不进任务栏/捕获列表)。`B` 键切
透明/绿幕/半透黑三种背景。手机端仍可用网页版透明歌词(WebView 前台常驻,不受浏览器节流影响)。

### 窗口/歌词排布(2026-07-14 改版,KTV 双行错开)
- **方形窗** `760×760`:字幕不强行铺满窗口,直播端绿幕抠图后**手动裁剪**取所需区域即可(多余留白无所谓)。
  改了尺寸后直播伴侣的捕获源要重新框选。
- **歌词=KTV 上下两行交替、左右错开、同尺寸**:按行号奇偶分槽——偶数行→**上行左对齐**,奇数行→**下行右
  对齐**(`_slot`);上下两行**同一字号**(都用 `font_big`,不再上大下小)。当前唱的行逐字高亮(青),另一
  槽位提前显示下一行(全底色候着),唱完切槽无缝。对齐经 `_align_x`(left/right/center)。
- **引导圆点在"即将唱那行"的顶部**、与行首对齐(`_draw_lead_dots(left_x, ...)`),前奏另在上方居中显示歌名。
- **音准线压扁**:高度 = `2×行高`(`≤两行歌词`),`bar_h=6`,放窗口上部一条扁平窄带,只表示旋律趋势。

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
- **绘制绝不能拖累音频回调(GIL)**(2026-07-13 修"显示绿幕窗时伴奏断续吱吱声,隐藏就消失"):
  sounddevice 的回调是 Python 函数,要抢 GIL;原来 `paintEvent` 每帧对每个字重建
  `QPainterPath.addText`(字形轮廓提取)+ 描边矢量填充,一帧 30ms+(实测 60fps 只跑出 30fps),
  窗口可见时回调最大被拖 122ms,而设备缓冲只有 46ms(`latency="high"`)→ 欠载出声。修法:
  ① **整行歌词预渲染成 QPixmap 缓存**(底色版+高亮版;高亮版懒构建,行先当预告只建底色),
  每帧只 blit + 按进度裁剪高亮,绘制降到 ~1.5ms/帧;缓存有界(8行)防内存涨,换歌/换DPR清空。
  ② 启动时**预热字体光栅化**(首次画字的一次性 ~0.1s 移出播放期)。
  ③ 设备缓冲提到 `latency=0.16`(实测 ~183ms)——窗口每次 show 的首帧曝光有 ~150ms 尖刺
  (Qt 半透明窗后备缓冲重建,消不掉)只能靠缓冲吃掉;同时 `current_ms()` 减去设备延迟做
  **显示时钟补偿**,否则歌词高亮会提前 ~180ms。`sys.setswitchinterval(0.002)` 减小 GIL 抖动。
  代价:切调/seek 到听见约多 0.15s,可接受。验证:无声数值测试(回调间隔/xrun/帧耗时),
  缓冲 183ms vs 最坏回调间隔 133ms,余量充足。
- **GUI 线程绝不做整段 PCM 解码等重活**(2026-07-13 修):`set_vocal` 首次切原唱要解码整段 kongsinger
  PCM,原来在 GUI 线程同步跑,连点"音源"就把窗口冻死。现放**后台线程**加载,完成后 `engine.swap_buffer`
  (引擎自带锁,跨线程安全),`_loading_vocal` 防重入。60fps 重绘加 `isVisible()` 守卫,隐藏态不排绘制。
  同理 `load_song` 换歌的重解码若日后要提速,也只能后台做——但注意 pc-service 的 `load→show→play` 指令
  靠单线程 FIFO 保序,换歌若改异步须自行处理"加载完再 play"的时序。

---

## 运行

```bash
# 用真 Python(PATH 里的是 Store 占位版,不能用)
"C:/Users/11651/AppData/Local/Programs/Python/Python313/python.exe" player.py --device 27
```

命令行开关:`--device N`(声卡输出设备索引)、`--hidden`(服务模式:先 `show()` 走完首次绘制再 `hide()`,并起线程读 **stdin 指令** `show/hide/toggle` 用 Qt 自己显隐)、`--paused`(不自动播放)、`--no-smtc`(关闭 SMTC 发布,避免与 pc-service 的 `smtc_helper` 打架)。服务模式下:**禁用手动关闭**(任务栏 X/关闭窗口不关也不隐藏,窗口作为绿幕捕获源常驻;但 **stdin EOF=托管它的 pc-service 退出/崩溃时会自退**,避免成为关不掉的孤儿隐藏进程);隐藏歌词(ESC 或托盘/stdin `hide`)时 **先把内容清成全绿(`blank` 态,同步画一帧再延迟隐藏)**,让直播伴侣捕获冻结帧=纯绿、不残留歌词;显示(`show`)恢复正常渲染。`showEvent/hideEvent` 经 **stdout 上报 `VIS:0/1`** 给 pc-service 同步状态。播放器由 pc-service 托管时用 `--device 27 --hidden --paused --no-smtc` 拉起,显隐走管道 IPC(不能用外部 win32 `ShowWindow`——Qt 透明窗不会重绘),见 [../live-remote/README.md](../live-remote/README.md)。

### 服务模式 IPC 协议(pc-service ↔ 播放器)
歌曲来源:优先 `D:\KaraokeLibrary\<mid>\`(永久曲库),回退 WeSing `Res\`(`song_dir_for`)。

**stdin 指令**(一行一条,`cmd [arg]`):
| 指令 | 作用 |
|---|---|
| `show` / `hide` / `toggle` | 显示/隐藏歌词窗(hide 先清全绿再隐) |
| `load <mid>` | 载入曲库任意歌(自动归位0/清调/复位原唱/暂停) |
| `play` / `pause` / `playpause` | 播放/暂停 |
| `seek <ms>` | 绝对定位 |
| `key <n>` / `key+` / `key-` | 升降调(绝对半音 / 相对) |
| `vocal 0|1` / `vocal_toggle` | 伴奏/原唱切换 |
| `vol <0-100>` | 伴奏输出音量档位(**感知/平方曲线**:增益=（档位/100)²,人耳近对数,低档位更快变小、控制更细;`volume_pct` 报回档位非增益;手机音量键同步;换歌不重置) |

**stdout 上报**:
- `VIS:0/1` —— 窗口可见性(showEvent/hideEvent 触发)
- `STATE {json}` —— 每 500ms:`pos/dur`(ms)、`playing`、`key`、`vocal`、`vol`(0-100)、`mid`、`title`、`artist`

对应引擎接口(`audio_engine.py`):`set_playing`、`seek_to_ms`、`load`、`set_semitones`、`swap_buffer`、`set_volume`/`volume_pct`、`is_playing`、`duration_ms`,全部线程安全。

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
