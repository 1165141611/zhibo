# CLAUDE.md —— 给 AI agent 的项目须知

本文件是 `zhibo` 直播项目对后续 agent 的统一说明与规则。**开工前先读本文件和 [README.md](README.md),
再读你要动的子项目的 README。**

## 一、项目速览

三个子项目,详见 [README.md](README.md)。一句话:
- `live-remote/` = PC 遥控服务(Python/FastAPI),已完成。
- `LiveRemote/` = 安卓遥控 App(Kotlin),开发中。
- `karaoke-player/` = 自制K歌播放器(Python/PySide6),单曲 Demo 已通。

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
  - 伴奏/BGM 走 `PLAYBACK 1/2`(WeSing/karaoke-player 用,WASAPI 设备索引约 27)。
  - **别往 `PLAYBACK 3/4` 灌音频**(麦克风监听通道,会回授炸麦)。
- **全民K歌**:PC 版 `WeSing.exe`(`D:\WeSing\WeSing.exe`),缓存在 `D:\WeSingCache\WeSingDL\`;
  手机版包名 `com.tencent.karaoke`。
- **QQ音乐**:PC 版做直播 BGM,按进程名用 pycaw 控音量。

## 四、验证习惯

- 改了有运行时行为的代码,尽量实际跑一遍观察效果(启动服务/播放器、发指令、看输出),别只靠"看着对"。
- 音频类改动:用无声 headless 测试查数值(RMS/主频/连续性/时钟),再让作者试听。
- GUI 类改动:后台启动进程 + 读日志确认没崩,再让作者看窗口。

## 五、当前进展与下一步

- **live-remote**:PC 服务(声卡场景 + QQ音乐 + 显隐窗口)已完成。**K歌整合已全面接入并持续打磨**:自动曲库
  导入器 + 托管 K歌播放器子进程 + 点歌队列/播放控制 WS API + **托盘曲库管理页(勾选加歌单/编辑歌名/播放切歌/搜索)**;
  跨重启缓存(场景/音量/Studio显隐/音准线显隐/字体/歌单)存 `state_cache.json`。
- **LiveRemote**:安卓原生 App(Compose,演唱/队列/遥控三页签),遥控页含声卡场景 + QQ音乐 + 窗口开关
  (Studio One / K歌歌词 / 音准线显隐)。**注意:改了手机端功能需重新 `assembleDebug` + adb 装机才生效**。
- **karaoke-player**:已从单曲 Demo 演进为多曲直播字幕源。**KTV 双行错开歌词 + 压扁音准线(均由白染蓝)**、
  竖屏 3:4 窗、绿幕抠图、实时升降调、原唱/伴奏、`Q` 字体循环、`P` 音准线显隐、**`O`+`Ctrl+↑↓` 顶端滚动歌单**、
  手机音量键同步(感知曲线)。热键/IPC/缓存详见 [karaoke-player/README.md](karaoke-player/README.md)。
  **做 K歌 大功能前先读根 [KARAOKE_SYSTEM.md](KARAOKE_SYSTEM.md)。**

> 更细的历史与踩坑记录在各子项目 README 及 `live-remote/DEV_LOG.md`。
