# LiveRemote —— 安卓悬浮窗遥控 App

直播遥控系统的手机端。一个可拖动的悬浮窗,叠在全民K歌上方,加载 PC 后台服务的网页当控制台,
用来切声卡场景、控 QQ音乐 背景音乐。人可以离开电脑。

> 属于 `zhibo` 直播项目子项目。总览见 [../README.md](../README.md);跨项目规则见 [../CLAUDE.md](../CLAUDE.md)。
> 配套 PC 端(WebSocket 服务 + 控制台网页)在同级 [`../live-remote/`](../live-remote/)。

## 方案

**原生 Kotlin 悬浮壳 + WebView**:不自绘界面,直接用 WebView 加载 PC 服务的现成网页
(`http://<电脑IP>:8765`),复用同一套控制台 UI。这样界面改动只需改 PC 端网页,App 基本不用动。

- 只需 `SYSTEM_ALERT_WINDOW`(悬浮窗)权限,不需要无障碍。
- 自用,不需正式签名。

## 代码结构

| 文件 | 作用 |
|---|---|
| `app/src/main/java/.../MainActivity.kt` | 入口:填电脑 IP、申请悬浮窗权限、scrcpy 就绪门槛检查、启动/停止悬浮服务、记住上次 IP 自动启动。 |
| `app/src/main/java/.../OverlayService.kt` | 前台服务(`specialUse`):用 `WindowManager` 建悬浮窗,内嵌 `WebView` 加载 `http://<ip>:8765`,可拖动。 |
| `app/src/main/AndroidManifest.xml` | 权限:INTERNET、SYSTEM_ALERT_WINDOW、FOREGROUND_SERVICE(_SPECIAL_USE)。 |

- `applicationId = com.example.liveremote`,minSdk 26,targetSdk 36。
- Gradle 用了国内阿里云镜像(见 `settings.gradle.kts`),避免连 Google/Maven 官方超时。

## 构建/运行

1. Android Studio 打开本目录,Gradle 同步(首次较慢,走镜像)。
2. 手机开开发者模式 + USB/无线调试,`Run` 装到手机(常连 `192.168.1.6:5555`)。
3. 先启动 PC 端服务(见 `../live-remote/README.md`),记下它打印的 `http://<电脑IP>:8765`。
4. App 里填这个 IP → 授予悬浮窗权限 → 悬浮窗出现,即 PC 控制台网页。

> 手机和电脑要在**同一 WiFi**。悬浮窗拖到投屏歌词裁剪框以外即可不入镜。

## 现状 / TODO

- 已能:填 IP、悬浮窗、加载 PC 网页、拖动、记忆 IP 自动启动、scrcpy 就绪门槛。
- 待完善:见提交历史与 `../live-remote/DEV_LOG.md` 中协议部分;界面迭代主要在 PC 端网页做。
