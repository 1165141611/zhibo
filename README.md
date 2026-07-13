# zhibo —— 直播工具集

作者自用的一套直播(唱歌)辅助系统。核心场景:一个人直播唱歌,用全民K歌出伴奏/评分,用 Studio One +
声卡处理人声,人有时要离开电脑。围绕这个场景有两套独立又相关的系统:

```
┌─────────────────────────────────────────────────────────────┐
│  直播遥控系统                                                  │
│  [安卓悬浮窗 App: LiveRemote] --WiFi/WebSocket--> [PC服务: live-remote] │
│                                                  ├─ MIDI → Studio One 声卡场景 │
│                                                  └─ 媒体键/pycaw → QQ音乐 BGM   │
├─────────────────────────────────────────────────────────────┤
│  自制K歌播放器                                                 │
│  [karaoke-player] 从PC版WeSing扒歌词/音高/伴奏 → 自渲染 → 直播伴侣 │
└─────────────────────────────────────────────────────────────┘
```

## 子项目

| 目录 | 是什么 | 技术栈 | 文档 |
|---|---|---|---|
| [`live-remote/`](live-remote/) | PC 后台遥控服务(声卡场景 + BGM 控制) | Python / FastAPI + WebSocket | [README](live-remote/README.md) · [DEV_LOG](live-remote/DEV_LOG.md) |
| [`LiveRemote/`](LiveRemote/) | 安卓悬浮窗遥控 App(叠在 K歌 上) | Kotlin / Android Studio | [README](LiveRemote/README.md) |
| [`karaoke-player/`](karaoke-player/) | 自制干净歌词/音高/伴奏播放器 | Python / PySide6 + sounddevice | [README](karaoke-player/README.md) |

> 命名坑:`live-remote`(小写,PC 服务)和 `LiveRemote`(驼峰,安卓 App)是两个不同项目,别搞混。

## 两套系统的关系

- **直播遥控系统**:让你直播时能离开电脑,用手机切声卡场景、控背景音乐。PC 端已完成,安卓 App 开发中。
- **自制K歌播放器**:解决直播间歌词显示问题(原来投屏抠图效果差)。独立项目,单曲 Demo 已通,曲库/多曲/接入待做。

各子项目详情、运行方式、技术决策见各自 README。跨项目的环境与协作规则见 [`CLAUDE.md`](CLAUDE.md)。
