# LiveRemote —— 直播K歌手机端(原生 Kotlin / Jetpack Compose)

直播K歌系统的**手机歌房遥控台**。局域网连 PC 中枢(pc-service),给**主播本人**看卡拉OK逐字字幕+音准线、
点歌管队列、切声卡场景、控 QQ音乐 背景音乐。人可以离开电脑。

> 属于 `zhibo` 直播项目子项目。总览见 [../README.md](../README.md);跨项目规则见 [../CLAUDE.md](../CLAUDE.md)。
> 整合大方案见根 [`../KARAOKE_SYSTEM.md`](../KARAOKE_SYSTEM.md);配套 PC 端在 [`../live-remote/`](../live-remote/)。

## 方案(2026-07-13 定稿,据高保真原型)

**原生 Kotlin + Jetpack Compose 全屏 App**,底部 TabBar **三页签**:

1. **演唱**(默认):卡拉OK逐字字幕 + 音准线(Compose Canvas)+ 控制条(声卡快切/步进步退/升降调/音源)。
2. **队列**(含点歌):演唱中卡片(切下一首)+ 点歌按钮(→ 底部选歌抽屉)+ 长按拖动重排的等待队列。
3. **遥控**:声卡场景(**只留「归位」**,场景切换在演唱页)+ **自动切镜运镜**(开关 + 主镜头放大滑块,
   开自动时滑块禁用;关闭时切回主机、可数字放大远近)+ 窗口开关(Studio One / 滚动歌单 / **悬浮绿幕**(=K歌播放器窗,2026-08-06 由"K歌歌词"改名)/ 音准线 / 礼物菜单)。
   QQ音乐 背景音乐**不再单列控制区**,改由常驻悬浮球统一承载(见下),避免同页两处重复。
- 全局:可拖动的**悬浮 QQ音乐迷你控制台**(现三页签**含遥控页**均常驻)、连接状态点/重连横幅、设置页(填电脑 IP)。

WebSocket 客户端连 pc-service(`ws://<电脑IP>:8765/ws`),曲库走 `GET /library`。协议见
[`../live-remote/README.md`](../live-remote/README.md#k歌-api手机点歌控制--阶段-2已接入)。

**设计规范(开发基准):** [`UI_SPEC.md`](UI_SPEC.md)(配色/字体/圆角/组件/交互/字段映射)。
原型与说明:`../UI design/App prototype development plan/`。最初 brief:[`UI_DESIGN.md`](UI_DESIGN.md)(历史留存)。

> **⚠️ 方案已从"悬浮壳 + WebView"转为全屏原生 Compose**。旧的 `OverlayService`(悬浮 WebView 叠在
> 手机版 WeSing 上)对应的是"手机跑 K歌"的老路线;新架构下 K歌伴奏/歌词由 PC karaoke-player 出,
> 手机只当主播的控制+看词屏,故改全屏原生。scrcpy 就绪门槛等旧逻辑一并移除。

## 代码结构

| 路径 | 作用 |
|---|---|
| `ui/theme/` | 设计系统:`Color.kt`(青#45D6FF 等 token)、`Type.kt`、`Theme.kt`。 |
| `net/RemoteClient.kt` | OkHttp WebSocket 客户端:连 `/ws`、解析 `state` JSON、自动重连;HTTP `GET /library`。 |
| `model/Models.kt` | 数据模型:`AppState`/`Song`/`QueueItem` 等。 |
| `RemoteViewModel.kt` | 持连接与状态,暴露 `StateFlow<AppState>` + 指令函数;演唱↔BGM 联动。 |
| `MainActivity.kt` | Compose 入口:三页签脚手架、连接横幅、设置页、悬浮 BGM、Toast。 |
| `ui/screens/` | `SingScreen` / `QueueScreen` / `RemoteScreen` + `PickerSheet`。 |
| `ui/components/` | `KaraokeStage`(音准线+逐字歌词 Canvas)、`Playhead`(帧驱动本地插值+`SmoothProgressBar`)、`BgmFab`、通用卡片/胶囊/按钮。 |

> **性能红线(2026-07-13,卡顿排查后立)**:进度/歌词的本地插值用 `rememberPlayhead` 返回的 `State<Int>`,
> **只能在绘制作用域(`Canvas` onDraw)或 `derivedStateOf` 里读 `.value`**。切勿把插值后的进度写回全局
> `AppState`/每帧重建 `st` 传给页面——那会让整棵树 20fps+ 重组,直接卡死 UI 线程(老版就是这么卡的)。
> 详见根 [`../KARAOKE_SYSTEM.md`](../KARAOKE_SYSTEM.md) "性能红线"。
>
> **播放头同步策略(2026-07-13 二次修卡顿)**:服务端每 ~500ms 回推的 `k_pos` 带传输延迟+抖动,
> **禁止每拍硬重锚**(老版 `LaunchedEffect(posMs)` 每次回推向后跳几十 ms,歌词高亮一卡一卡)。
> `rememberPlayhead` 现为:**关键节点(开播/暂停/seek/换歌,偏差 >350ms)才硬校正**;平时把偏差
> 折算成 ±10% 内的速率微调(clock slewing),约 1s 内悄悄追平——播放头单调平滑,又不与电脑端漂移。

> **UI/交互约定(2026-07-13)**:
> - **音准线几何一律用 dp**(`KaraokeStage` 里 `DP_PER_SEC=60`/`HEAD_RATIO=0.30`/`NOTE_H_DP=10`,DrawScope 内
>   `.dp.toPx()`)。别用裸设备像素——高 DPI 手机上会又小又挤成一堆散点。音高来自 `GET /song/{mid}/karaoke`
>   的归一化 `pitch∈[0,1]`,渲染再压进 `[0.12,0.88]` 留上下白边(仿 PC ±2 半音)。
> - **音准块高亮对齐 PC 播放器(2026-07-15)**:每块**底色全白(`C.LyricWhite`)=未唱**,落在播放头左侧的部分
>   `clipRect` 裁出来铺**青色(`C.Accent`)=已唱**,正在唱的块青色填到播放头处——即"唱过染色、没唱是白",
>   与电脑绿幕播放器 `_draw_pitch` 同法。旧版"未唱蓝底 `NoteIdle` / 仅当前块白填"已废弃。
> - **按压手感统一在 `noRippleClick`**(`Common.kt`),全 App 按钮通吃,三层叠加做"实体键"效果:
>   ①**弹性缩放**——按下弹到 0.96、松手回弹(spring),是"按下去"的主体现;②`drawWithContent` 叠淡高亮;
>   ③按下瞬间一次极轻触感(`TextHandleMove`)。关键实现:缩放的 `graphicsLayer` **前置**到整条链最外层
>   (`Modifier.graphicsLayer{…}.then(this)`),这样它包住调用方的 `clip/background/内容`,是整枚按钮
>   (含底色描边)一起缩,而非只缩里面的图标/文字——绕开了"graphicsLayer 依赖链中位置、易只缩内容"的坑。
>   graphicsLayer 只改绘制不改测量,前置不打乱布局。
> - **长按确认**用 `HoldToConfirmButton`(如遥控页"归位",按住 2s 进度填满即触发,中途松手取消);
>   触发瞬间中等震动(`LocalHapticFeedback` LongPress)+ 进度条立即清空。归位后场景全回未选中:
>   服务端 `reset_scene` 把 `scene=null`,`reduce()` 收到 null 置 0(缺字段才保留旧值),`resetScene()` 另做乐观清零。
> - **乐观更新防闪动**:`RemoteViewModel` 对 调/音源/进度/播放/伴奏音量 设了 `*LockUntil` 抑制窗(1.2s),
>   期间忽略服务端对该字段的回推,等播放器把新值上报再放行;配合演唱页 `AnimatedContent` 让调号平滑过渡显示 `+1`。
> - **音量键 → 伴奏音量**:唱歌时(已连接且有当前曲)`MainActivity.onKeyDown` 拦截音量键——先正常调手机
>   媒体音量(弹系统音量条),再把百分比经 `kvol` 同步给播放器(伴奏音量 = 手机媒体音量百分比);开唱换歌
>   时也会同步一次。空闲/未连接时音量键保持系统默认行为。
>   **例外——连接首帧方向反转(2026-07-13)**:刚连上/重连后第一帧 state,以后端 `k_vol`(服务端有跨重启
>   持久缓存)为准,静默把手机媒体音量设过去,并跳过该帧的换歌 push;之后恢复"手机 → 后端"。
> - **点歌抽屉下拉收起(2026-07-13)**:`PickerSheet` 头部(把手+标题行)可下拉——`Animatable` 存位移,
>   面板用 **lambda 版 `offset{}`**、蒙层淡出用 `graphicsLayer{}`(高频值只在布局/绘制阶段读,拖动零重组,
>   符合性能红线);`draggable` 自带松手速度,下拉超 1/4 高度或快甩(>1500px/s)即收起,否则弹回。
>   手势不挂列表区,避免与 `LazyColumn` 滚动嵌套冲突。
> - **点歌列表按点歌次数倒序(2026-07-16)**:`Song` 加 `plays` 字段(pc-service `/library` 返回,
>   每次点歌 +1 存 `library.json`);`RemoteViewModel.refreshLibrary` 由"按歌名排序"改为**按 `plays`
>   倒序、同次数按歌名升序**——常点的歌浮到点歌抽屉最前。服务端也已按 `plays` 倒序返回,前后一致。
> - **点歌抽屉触底分页(2026-07-14)**:`PickerSheet` 的 `LazyColumn` 只喂过滤结果的前 `visible` 条
>   (每批 `PICKER_PAGE=60`),`derivedStateOf` 监听"最后可见项进入倒数 6 条"→ 追加一批;追加后若仍在
>   底部附近,`shown.size` 变化会让 effect 重跑继续追加,直到离开底部或全部显示(不会卡在"差几条不加载")。
>   搜索词一变(`remember(query)`)自动重置回首批;末尾有"上滑加载更多 · x/y"提示行。大曲库首开面板/
>   清空搜索不再整表 diff+测量掉帧。
> - **悬浮球位置持久化(2026-07-13)**:`BgmFab` 把球位置按**归一化比例**存 SharedPreferences
>   (`cfg` 的 `fab_rx/fab_ry`),拖动结束写盘;启动时在 remember 初始化块**同步读回**(prefs 早被
>   ViewModel 加载,内存命中)→ 首帧渲染前位置已就绪,启动即上次位置、不跳动。展开面板跟随球位置弹出。
>   球底左右**两枚对称状态点**:右下=BGM 播放状态(绿=播放中/灰=未播放),左下=**演唱联动**状态
>   (绿=已开启/灰=关闭),不展开面板一眼可辨两态。
> - **演唱↔BGM 联动 v2(2026-07-14)**:`RemoteViewModel.interlockBgm`——开唱自动暂停 QQ音乐,停唱
>   **延迟 2s**(`bgmResumeJob` 协程,期间又开唱/手动操作即取消)再自动恢复(仅恢复联动暂停过的)。
>   全程发**有方向且幂等**的 `{"cmd":"bgm","action":"pause"/"play"}`(服务端已在目标状态则不动)——
>   不再用无方向 `playpause`,本地 `bgmPlaying` 过期也打不反方向(旧版"手动暂停 BGM 后自动化失灵"即此)。
>   **联动总开关**在 BGM 悬浮面板底部("演唱联动" Switch),存 `cfg` 的 `bgm_auto_follow`(默认开);
>   关闭即取消待办恢复+清记账,完全不介入。手动 `bgmToggle` 只取消当次待办,下个演唱周期照常联动。
> - **返回键(2026-07-13)**:`App` 里统一 `BackHandler`——设置页/点歌抽屉开着时第一优先关浮层;
>   否则两次返回才退出(第一次弹"再按一次退出程序",2s 内再按 `finish()`),防误触退到桌面。
> - **演唱页无歌态(2026-07-13)**:`hasSong=false`(队列唱完/空闲)时**不整页替换**——布局保持,声卡快切
>   与悬浮 BGM 照常可用;演唱相关控件(播放/步进/升降调/音源/进度)置灰禁用(`alpha 0.4` +
>   `noRippleClick(enabled=false)`),歌词舞台位置换成 `PickGuide`("去点歌"引导)。空闲时忽略播放器残留的
>   `k_pos/k_dur`(它还载着上一首),进度显示 0:00。配合 PC 端"唱完切下一首开头暂停"(见 live-remote README)。

- `applicationId = com.example.liveremote`,minSdk 26,targetSdk 36,compileSdk 36.1,AGP 9.2.1(内置 Kotlin)。
- Gradle 走国内阿里云镜像(见 `settings.gradle.kts`)。

## 构建/运行

1. Android Studio 打开本目录,Gradle 同步(首次较慢,走镜像 + 下 Compose 依赖)。
2. 命令行:`./gradlew assembleDebug`;装机 `./gradlew installDebug`(手机常连 `192.168.1.6:5555`)。
3. 先启动 PC 端服务(见 `../live-remote/README.md`),记下 `http://<电脑IP>:8765`。
4. App 设置页填电脑 IP → 连接。手机和电脑须**同一 WiFi**。

## 现状 / TODO

- **已落地(2026-07-13)**:据原型用 Compose 实现三页签 + WS 客户端,`assembleDebug` 通过出 APK。
  三页签 + 队列长按拖拽 + 点歌抽屉 + 遥控 + 悬浮 QQ音乐 + 演唱↔BGM 联动 + 设置全实现;
  **演唱页卡拉OK数据已打通**(切歌时拉 `GET /song/{mid}/karaoke` → `KaraokeStage` 渲染逐字高亮 + 音准块 + KTV 圆点)。
- **命令行构建**:先 `$env:JAVA_HOME='D:\Android Studio\jbr'`(JDK21),再 `./gradlew.bat :app:assembleDebug`。
- **待联调**:装机连真实 pc-service 跑一遍(WS 状态/进度插值/切歌卡拉OK/拖拽重排/悬浮球);逐字与音准的
  视觉细节按主播反馈微调。详见 [`../KARAOKE_SYSTEM.md`](../KARAOKE_SYSTEM.md) 路线图第③④步。
