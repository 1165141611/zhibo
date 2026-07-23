# CLAUDE.md —— 给 AI agent 的项目须知

本文件是 `zhibo` 直播项目对后续 agent 的统一说明与规则。**开工前先读本文件和 [README.md](README.md),
再读你要动的子项目的 README。**

## 一、项目速览

三个子项目,详见 [README.md](README.md)。一句话:
- `live-remote/` = PC 遥控服务(Python/FastAPI),已完成。
- `LiveRemote/` = 安卓遥控 App(Kotlin),开发中。
- `karaoke-player/` = 自制K歌播放器(Python/PySide6),单曲 Demo 已通。

> 注:早先的 `auto-director/`(多机位自动切镜/运镜,拟接 OBS)已于 2026-07-23 移除——回归直播伴侣直接推流、
> 不再用 OBS/多机位。开播后叠加评论/礼物悬浮窗 + QQ音乐时,无线主摄卡成 PPT、音画不同步,遂弃用整条 OBS 链路。

**整合大方案与路线图见 [KARAOKE_SYSTEM.md](KARAOKE_SYSTEM.md)** —— K歌播放器 + pc-service 中枢 + 手机全屏
点歌台的目标架构、已定决策、分阶段路线。**做 K歌 相关大功能前先读它**,别被单次微调的上下文带偏。

## 二、协作规则(重要)

1. **改代码 → 同步更新文档**。改了任何子项目的代码,必须在同一次改动里更新对应文档,保持一致:
   - `live-remote/` → 更新 `live-remote/README.md` 和/或 `live-remote/DEV_LOG.md`。
   - `karaoke-player/` → 更新 `karaoke-player/README.md`(尤其"代码结构""关键技术决策""TODO")。
   - `LiveRemote/` → 更新 `LiveRemote/README.md`。
   - 影响到整体结构/新增子项目/跨项目约定 → 更新根 `README.md` 和本 `CLAUDE.md`。
   - 影响到 K歌 整合系统的架构/决策/路线 → 更新 `KARAOKE_SYSTEM.md`。
   文档过期比没有文档更坑,别只改代码不改文档。
2. **不提交可再生的大文件/临时产物**:内存 dump(`.pkl`)、试听/调试音频(`.wav`)、日志(`.log`)、
   `__pycache__`、变调临时文件等。根目录有全局兜底 `.gitignore`(系统/IDE/Python 通用垃圾),
   各子项目另配自己的 `.gitignore`,新增此类产物请加进对应文件。
3. **密钥/私有数据**:`karaoke-player/wesing_pcm_key.py` 等解密密钥仅供本机自用,勿分发、勿外传。
4. **破坏性操作先确认**:删文件、清目录、重置前先看清楚内容;不要删你没创建、且与描述不符的东西。
5. **文档与注释用中文**(与作者、现有文档一致)。

## 三、共享环境(本机事实)

- **Python**:用 `C:\Users\11651\AppData\Local\Programs\Python\Python313\python.exe`。
  PATH 里的 `python` 是 Microsoft Store 占位版,**不能用**。
- **adb**:`D:\scrcpy-win64-v3.3.1\adb.exe`(随 scrcpy 附带)。安卓手机常连在 `192.168.1.6:5555`。
- **声卡**:ROUTIST R2(虚拟路由多通道)+ Studio One。
  - 伴奏/BGM 走 `PLAYBACK 1/2`(WeSing/karaoke-player 用)。**设备索引不稳**(接相机/ToDesk 虚拟音频会
    挤动 WASAPI 枚举顺序,曾把写死的索引 27 顶成 ToDesk Virtual Audio 致播放器音乐听不到);pc-service
    已改**按名解析** `_resolve_player_device()`(WASAPI 下名字含 `PLAYBACK 1/2`),`config.PLAYER_DEVICE=27` 仅回退。
  - **别往 `PLAYBACK 3/4` 灌音频**(麦克风监听通道,会回授炸麦)。
  - 推流采集链路:音乐→`PLAYBACK 1/2`→Studio One「他人听」总线→ASIO `PLAYBACK 3/4`→
    `VIRTUAL REC 3/4`→直播伴侣主麦克风。**直播间没声先查 Windows 里 `VIRTUAL REC 3/4`
    录音端点音量**(2026-07 曾被归零,全链路静音;已破案:是 pc-service 按进程名裸匹配
    `MediaSDK_Server.exe` 调 BGM 音量误伤——该进程是腾讯共用组件,**直播伴侣**的同名子进程
    在 `PLAYBACK 3/4`/`VIRTUAL REC 3/4` 挂着麦克风链路会话。现已按父进程链归属校验 + 设备
    白名单修复。**教训:腾讯系音频会话按进程名匹配必须校验父链归属**)。
- **全民K歌**:PC 版 `WeSing.exe`(`D:\WeSing\WeSing.exe`),缓存在 `D:\WeSingCache\WeSingDL\`;
  手机版包名 `com.tencent.karaoke`。
- **QQ音乐**:PC 版做直播 BGM。**音量**按进程名(带父链归属校验)用 pycaw 控;**播放/暂停/切歌**
  走 SMTC 会话有方向控制(winrt 子进程按 AUMID 锁定 QQ音乐 会话,见 `config.QQMUSIC_SMTC_HINT`),
  不再用无方向的全局媒体键(会被抢占系统当前会话的 App 截走)。

## 四、验证习惯

- 改了有运行时行为的代码,尽量实际跑一遍观察效果(启动服务/播放器、发指令、看输出),别只靠"看着对"。
- 音频类改动:用无声 headless 测试查数值(RMS/主频/连续性/时钟),再让作者试听。
- GUI 类改动:后台启动进程 + 读日志确认没崩,再让作者看窗口。

## 五、当前进展与下一步

- **live-remote**:PC 服务(声卡场景 + QQ音乐 + 显隐窗口)已完成。**QQ音乐 传输控制已重构为
  SMTC 会话有方向控制**(2026-07-15):winrt 子进程按 AUMID(`config.QQMUSIC_SMTC_HINT`)锁定 QQ音乐
  自己的会话,播放/暂停/切歌用 `try_play/try_pause` 而非无方向全局媒体键(治"手机控 BGM 时好时坏、
  正在播的歌不同步"——媒体键会被抢占系统当前会话的 WeSing/浏览器/直播伴侣截走);SMTC 快照每秒
  无条件重发防丢帧漂移;新增 `bgm_vol` 周期回读把 PC 上手动改的音量反向同步到手机。详见
  `live-remote/DEV_LOG.md` 二·补。**K歌整合已全面接入并持续打磨**:自动曲库
  导入器 + 托管 K歌播放器子进程 + 点歌队列/播放控制 WS API + **托盘曲库管理页(勾选加歌单/编辑歌名/播放切歌/
  搜索防抖/Live筛选/勾选排序/触底分页渲染)**;
  跨重启缓存(场景/音量/Studio显隐/音准线显隐/字体/歌单)存 `state_cache.json`。
  **点歌逻辑打磨(2026-07-15)**:空队列点第一首也不自动开唱(`k_enqueue` 空闲时只 `load` 载入开头暂停),
  与"唱完切下首暂停"一致,首歌待唱期间 BGM 顶着、主播手动按播放开唱。
- **LiveRemote**:安卓原生 App(Compose,演唱/队列/遥控三页签),遥控页含声卡场景 + 窗口开关
  (Studio One / K歌歌词 / 音准线显隐);**QQ音乐 已从遥控页单列控制区移除,改由常驻悬浮球(现含遥控页,
  三页签通吃)统一承载**;点歌抽屉支持搜索 + 触底分页;**演唱页音准块高亮对齐 PC(2026-07-15):
  白底=未唱、`clipRect` 裁青=已唱,"唱过染色没唱是白"**;演唱↔BGM 联动(开唱暂停 QQ音乐、
  停唱缓冲 2s 恢复,BGM 悬浮面板有联动总开关,走服务端有方向的 `bgm play/pause` 幂等指令)。
  **按钮按压手感**统一在 `Common.kt` 的 `noRippleClick`:弹性缩放(按下 0.96/松手回弹 spring)+ 淡高亮 +
  轻触感三层叠加,缩放的 `graphicsLayer` 前置到链最外层包住整枚按钮(含底色)。
  **注意:改了手机端功能需重新 `assembleDebug` + adb 装机才生效**。
- **karaoke-player**:已从单曲 Demo 演进为多曲直播字幕源。**KTV 双行错开歌词(经典 KTV 双色描边:未唱白底黑边/
  已唱蓝底白边+黑 keyline)+ 压扁音准线**、
  竖屏 3:4 窗、绿幕抠图、实时升降调、原唱/伴奏、`Q` 字体循环、`P` 音准线显隐、**`O`+`Ctrl+↑↓` 顶端滚动歌单**、
  手机音量键同步(感知曲线)。热键/IPC/缓存详见 [karaoke-player/README.md](karaoke-player/README.md)。
  **2026-07-15 四项**:①歌名/歌手显示采用入库保存值(`meta.json` 清洗+改名后),非 QRC 原文;
  ②未演唱态(载入待唱、从未开唱)绿幕只出纯绿、不画歌词/音准线,开唱即恢复(`_ever_played` 标志);
  ③窗口桌面位置记忆(拖动记住,显隐/服务重启后恢复到关闭时位置;`_saved_pos` + STATE `win_x/win_y` +
  pc-service `state_cache.json`,拉起时 `pos x y` 下发);④歌词+圆点换**经典 KTV 双色描边**(未唱白底黑边/
  已唱蓝底白边,外圈黑 keyline 保绿幕边界;`_outlined` 分层描边 `OW_BLACK=6/OW_WHITE=4`),圆点倒计时改**逐个消失
  + 开唱前空一拍**(slot=lead/(n+1));顶端滚动歌单也统一成未唱歌词款(白底黑描边,名间隔减半);⑤音准线
  加粗描边看齐歌词、**起唱竖线改音高游标**(白亮点+光晕,y 随音符上下滑动、无音符落底);⑥**开头标题卡**
  (开唱前几秒居中显 歌名/原唱/演唱:主播名,渐隐后出歌词+音准线;演唱者=托盘可改的 `performer`,缓存+IPC 下发)。
  **做 K歌 大功能前先读根 [KARAOKE_SYSTEM.md](KARAOKE_SYSTEM.md)。**

> 更细的历史与踩坑记录在各子项目 README 及 `live-remote/DEV_LOG.md`。
