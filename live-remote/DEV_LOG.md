# 直播遥控系统 · 开发日志

> 一台安卓手机(悬浮窗控制台,同时跑全民K歌)通过局域网遥控电脑,控制 Studio One 声卡场景 + QQ音乐 背景音乐。人可以离开电脑直播。

---

## 一、总体架构

```
 [安卓手机:悬浮窗控制台(叠在全民K歌上)]
        │  WebSocket(局域网 WiFi,JSON)
        ▼
 ┌─────────────────────────────────────────────┐
 │  电脑后台服务 pc-service (Python, 常驻托盘)    │
 │  ├─ Mackie MIDI → loopMIDI → Studio One 声卡场景 │
 │  ├─ 媒体键 + pycaw → QQ音乐 背景音乐            │
 │  ├─ win32 → 显示/隐藏 Studio One 窗口          │
 │  └─ SMTC(winrt) → 读 BGM 进度/歌名(只读)      │
 └─────────────────────────────────────────────┘
```

投屏说明:全民K歌 在手机上,scrcpy 只裁"歌词两行"投到电脑做绿幕抠图;悬浮控制台拖到裁剪框外,不进直播间。

---

## 二、电脑后台服务(pc-service)

### 文件
- `server.py` —— 主程序:FastAPI + WebSocket + 托盘 + 全部控制逻辑
- `config.py` —— 所有可调参数(端口、场景、CC/音符、渐变时长、QQ音乐进程名)
- `winmm_midi.py` —— 纯 ctypes 的 Windows MIDI 收发(免编译,不用 python-rtmidi)
- `smtc.py` —— winrt 读系统媒体会话(BGM 进度/歌名)
- `studio_win.py` —— win32 显示/隐藏 Studio One 窗口
- `static/index.html` —— 手机测试网页(App 出来前用浏览器就能全功能测)
- `run_server.bat` —— 双击启动(写死了 Python 路径)
- `requirements.txt`

### 运行
- Python:`C:\Users\11651\AppData\Local\Programs\Python\Python313\python.exe`(系统 PATH 里的 python 是 Store 占位版,不能用,故 bat 写死路径)
- 依赖:`pip install -r requirements.txt`
- 启动:双击 `run_server.bat`,托盘常驻,控制台打印手机访问地址 `http://<电脑IP>:8765`

### 每次直播启动顺序(重要)
1. loopMIDI(建议设开机自启,否则虚拟端口不存在)
2. Studio One(打开工程)
3. 双击 `run_server.bat`
4. 手机开控制台

---

## 三、WebSocket 协议(手机 ↔ 电脑)

```jsonc
// 手机 → 电脑
{"cmd":"scene","id":1}              // 声卡场景 1-5(聊天/湿唱/干唱/喇叭/闭麦)
{"cmd":"reset_scene"}               // 声卡"归位"(记录重置为全不静音)
{"cmd":"bgm","action":"next"}       // next / prev / playpause(playpause 带渐强渐弱)
{"cmd":"bgm_vol","value":40}        // BGM 音量 0-100
{"cmd":"studio_toggle"}             // 显示/隐藏 Studio One 窗口

// 电脑 → 手机(状态广播)
{"type":"state","scene":1,"bgm_vol":40,"studio_connected":true,
 "bgm_title":"...","bgm_artist":"...","bgm_pos":88.5,"bgm_dur":233.4,
 "bgm_playing":true,"studio_visible":true}
```

---

## 四、声卡场景(Studio One)—— 关键,踩坑最多

### 结论方案:Mackie Control + 纯记录 + 归位
- Studio One 里加一个 **Mackie Control** 外部设备,**接收自 = loopMIDI Port**。
- Mackie 协议:每条通道的静音按钮 = 固定音符(通道1=16 … 通道8=23)。
  我们的 4 条人声通道 = 混音台第 3~6 条 = **音符 18/19/20/21**(聊天/湿/干/喇叭)。
- 发"音符按下(vel127)+松开(vel0)"= **切换**该通道静音(Mackie 是开关式)。
- 场景逻辑在 `config.py` 的 `SCENES`:active 那条不静音、其余静音;闭麦=全静音。
- 服务端 `_mute_state` **记录**各通道状态,**只翻转需要改变的通道**(逻辑已验证正确)。

### 同步纪律(因为 Mackie 是开关式、且 Studio One 不回传状态)
- 约定:服务启动/归位时记录 = **全不静音**。
- 直播前把 4 条通道的 M **全部关掉**一次 → 实际与记录对齐。
- 中途若乱(重启服务、手动点过 M):关掉 4 条 M → 点手机 **"⟳ 归位"**。

### 踩过但走不通的路(别再试)
- **Control Link 焦点/全局映射**:静音只能全局映射,通用键盘/控制界面设备的 CC 不被当控制器识别,左上角"手"区域收不到硬件 → 绑不上。
- **回读状态(feedback)**:给 Studio One 的 Mackie 设 `发送到 = MackieFB`(第二条 loopMIDI 口),想读回静音 LED 状态。实测 **Studio One 完全不回传**(Mackie 需控制器先握手,虚拟口做不到)→ 放弃回读,改"纯记录+归位"。
  - `MackieFB` 端口和 `发送到` 配置留着无害,不用管;`server.py` 里 `open_feedback/learn_states` 是死代码,保留备用。

---

## 五、背景音乐(QQ音乐)—— 也踩过坑

- **播放/暂停/上下一首**:系统媒体键(全局,不抢焦点)。QQ音乐 响应系统媒体会话。
- **音量**:pycaw 控制 QQ音乐 的**音频会话音量**(不是 QQ音乐 界面滑块,但这是实际能听到的音量)。
  - **坑**:QQ音乐 新版声音走 `QQMusic.exe`,但可能出现在**声卡(ROUTIST R2)的多条虚拟路由**上,且**不在默认设备**。所以要**跨所有活跃设备**找出全部 `QQMusic.exe` 会话,**一起同步调**(见 `_resolve_qq_sessions`)。`config.QQMUSIC_PROCS` 是候选进程名。
- **渐强渐弱**:playpause 时用 pycaw 把会话音量在 `FADE_SECONDS`(默认1秒)内平滑升/降。渐变进行中再点会被忽略。播放状态由 `STATE["bgm_playing"]` 内部跟踪(启动假定在播)。
- **进度条 + 歌名**:歌名/位置/时长来自 winrt(SMTC),但 winrt 放在**独立子进程 `smtc_helper.py`** 里跑,每秒打印一行 JSON 到 stdout,主进程后台线程读取更新状态。QQ音乐 不支持外部 seek,所以进度条只读、无快进退。

### 崩溃根因备忘(重要,别踩回去)
- 症状:服务启动后闪退 / 手机连上时崩(ExitCode -1073741819 / faulthandler 抓到 RPC_E_WRONG_THREAD 0x8001010e + access violation)。
- 根因:**winrt(COM MTA)和 pycaw(comtypes STA)在同一进程/线程把 COM 公寓搞乱**,GC 释放 pycaw 对象时段错误崩溃。
- **最终方案(现行)**:
  - winrt 放到**独立子进程** `smtc_helper.py`(和主进程 pycaw 的 COM 彻底隔离)→ 不崩。
  - 主进程只剩 pycaw + pystray(都是 STA,同种模式,兼容)→ **托盘恢复**。
  - **pythonw 无窗口启动**(`run_server.bat` 用 `start "" pythonw server.py`),只有托盘图标,右键退出。pythonw 下 stdout 为 None,已重定向到 `server.log`(否则 uvicorn 日志报错、后台线程起不来)。
  - **切记:winrt 绝不能在主进程 import/运行,必须留在子进程。**

---

## 六、显示/隐藏 Studio One
- `studio_win.py`:进程 `Studio One.exe`,主窗口类名 `CCLWindowClass`。
- ShowWindow(SW_HIDE / SW_SHOW),只藏 GUI,音频引擎不受影响。

---

## 七、待办:安卓悬浮窗 App

**方案:原生 Kotlin"悬浮壳 + WebView"**(Android Studio)
- 一个前台服务弹**可拖动悬浮窗**(TYPE_APPLICATION_OVERLAY),里面装 WebView 加载 `http://<电脑IP>:8765`,**复用现成网页界面**,不重写 UI。
- 权限:仅需"显示在其他应用上层"(SYSTEM_ALERT_WINDOW)+ 前台服务;**不需要无障碍**(不碰 K歌 界面)。
- 自己用**不需要正式签名**(调试签名即可安装)。
- 悬浮窗拖到 scrcpy 歌词裁剪框外,不入镜。

---

## 七·补、安卓 App 悬浮体验优化(2026-07-12)

三项优化(`LiveRemote/`):
1. **自动进入悬浮**:`MainActivity.onCreate` 里若已存 IP 且已授权,后台线程用 `HttpURLConnection`(超时 1.2s)探测后端;可达则自动 `startForegroundService` + `moveTaskToBack`,免去手点"启动"。用 `autoStarted` 防重入。
2. **悬浮球态**:`OverlayService` 根容器改为 `FrameLayout`,同时放「面板」和「56dp 圆形悬浮球」(`GradientDrawable` OVAL),按可见性切换,窗口 `WRAP_CONTENT` 自适应大小。
   - 通用 `dragListener(onTap)`:用 `scaledTouchSlop` 区分拖动/轻点;球轻点=展开,拖动=移位。面板顶部灰条只拖动。
   - "—"收起成球,球点一下展开,"×"关闭。
3. **横条紧凑 UI**:`static/index.html` 改为**宽横条工具栏**(flex 竖排多行:场景行 / Studio+归位+播放控制行 / 音量行 / 曲目+进度行),按钮 padding/字号/圆角、间距全面缩小。
   - **无内部滚动**:网页 `ResizeObserver` 把 `document.body.scrollHeight` 经 `Android.reportHeight()`(`@JavascriptInterface`)回报,`OverlayService` 据此(×density)精确设定 WebView 高度,窗口贴合内容。
   - WebView 宽度 = 屏宽 − dp(12)(近满屏横条),窗口 `WRAP_CONTENT` 自适应;`overScrollMode=NEVER`。
   - **去掉悬浮态右上角「×」关闭按钮**(避免误触);关闭改由 App 内「停止悬浮控制台」或通知栏。「—」仍收起为悬浮球。

编译验证:`gradlew :app:compileDebugKotlin` BUILD SUCCESSFUL。

---

## 七·补二、进入悬浮 = 自动无线投屏 scrcpy(2026-07-12)

目标:进入悬浮态时,PC 自动经**无线 adb** 连手机并拉起 **scrcpy**(只投屏、不带声音);进入悬浮前在 App 端加**闸门**判断手机是否开了【无线调试】。方式=**安卓11无线调试**(端口动态,靠 adb mDNS 发现),scrcpy 在 PATH。

**PC 端** `pc-service/scrcpy_win.py`(新)+ `server.py` + `config.py`:
- `ensure_scrcpy(phone_ip)`:①scrcpy 已跑则直接返回;②`adb start-server`;③`adb devices` 找 `ip:port` 形态的已连无线设备;④没有就 `adb mdns services` 找 `_adb-tls-connect` 端口(优先匹配手机 IP)→ `adb connect` → 再查;⑤`scrcpy -s <serial> --no-audio ...`。带 8s 节流,防网页重连狂刷 adb。
- WS 新增指令 `ensure_scrcpy`:`phone_ip = ws.client.host`(手机局域网 IP),阻塞调用丢 `asyncio.to_thread`。STATE 加 `scrcpy_ok/scrcpy_msg` 推回网页。
- `config.py`:`ADB_PATH/SCRCPY_PATH="adb"/"scrcpy"`(PATH)、`SCRCPY_ARGS=["--no-audio","--window-title=手机投屏"]`。
- **一次性前置**:手机开【无线调试】+ 电脑 `adb pair 手机IP:配对端口`(配对码见手机页)。配对码需现场输入,无法无人值守,故不自动化。
- 实测:`adb mdns services` 能发现 `192.168.1.6:43819`(手机 USB 连着时无线调试也在广播)→ 发现逻辑通。未真跑 scrcpy 以免弹窗。

**网页** `static/index.html`:WS `onopen` 即 `send({cmd:'ensure_scrcpy'})`;状态栏加第二个圆点(投屏),`scrcpy_ok` 亮绿/红,`title` 显示 `scrcpy_msg`,**点一下重试**。

**App 闸门(初版·已改)** `MainActivity.enterOverlayWithGate()`:进悬浮前读 `adb_wifi_enabled`。
> 改进(见下"闸门改为问 PC"):5555 方案不需要开"无线调试",读该开关会误拦,故改为问 PC 能否 adb 连上手机。

编译验证:`gradlew :app:compileDebugKotlin` BUILD SUCCESSFUL;`scrcpy_win/server/config` 语法 OK;`scrcpy_win` 实机自检通过。

**双方案 + 闸门改为问 PC(2026-07-12,承上)**
- 实测发现:旧后台服务没重启,`ensure_scrcpy` 从没被处理(server.log 无 SCRCPY 行、横幅是旧版)——**改代码后必须重启服务**。
- `config.py` 加 `SCRCPY_FIXED_PORT=5555` + `SCRCPY_USE_MDNS=True`;`scrcpy_win` 抽出 `_connect_device()`:**先试固定 5555、失败再走 mDNS 动态**,两条路都兜。
- **已帮用户实机打通 5555**:手机在 WiFi(无线调试开着、已配对),`adb connect 手机:动态口` → `adb -s ... tcpip 5555` → `adb connect 手机:5555`=device;`ensure_scrcpy('192.168.1.6')` 全链路成功、scrcpy 实际拉起。(注:手机重启后 5555 失效,逻辑会自动 fallback mDNS。)
- **闸门改为问 PC**:`scrcpy_win.can_reach()`(连不启动 scrcpy)+ `server.py` 加 `GET /scrcpy/check`(用 `request.client.host` 当手机 IP,**注册在 StaticFiles 挂载前**)。`MainActivity.enterOverlayWithGate()` 改为后台 GET `/scrcpy/check`:`reachable=false` 才弹排查窗(仍可"仍然进入");查不到(旧服务/网络)不拦、放行。去掉 `adb_wifi_enabled` 相关逻辑。

再验证:Kotlin BUILD SUCCESSFUL;`server` 可 import,路由顺序 `/ws,/scrcpy/check,<static>` 正确;`can_reach` 实机返回 True。

**归位按钮修复 + WebView 不刷新的根因(2026-07-12,承上)**
- 现象:归位点了没反应,其他键正常。根因:只有归位用 `window.confirm()`,而 WebView 悬浮窗是 `FLAG_NOT_FOCUSABLE`,原生弹窗弹不出、`confirm()` 直接返回 false → `send` 不执行。
- **更深的坑**:WebView 只在悬浮窗**首次创建**时 `loadUrl` 一次,重启服务/WS 重连都**不重载 HTML**——所以改了网页一直看的是内存里的旧页面("还是没用"的真正原因)。
- 修法:
  - `index.html` 归位改成**长按 5 秒**(`touchstart/mousedown` 起计时 + CSS 进度条填充,松开/离开取消,到点发 `reset_scene`),不依赖任何原生弹窗。
  - `OverlayService`:`settings.cacheMode = LOAD_NO_CACHE`;`onStartCommand` 里 `root!=null` 时 `webView.loadUrl(url)` 重载——**再点"启动"即可刷新网页**,不必停止再开。
- 已 `assembleDebug` 出包并**经无线 adb(-t)装到手机**(192.168.1.6:5555)= Success。APK: `app/build/intermediates/apk/debug/app-debug.apk`。
- 注意:`/scrcpy/check` 与 scrcpy 自动拉起仍需**重启 PC 服务**才生效;旧服务下网关 404 → App 放行不拦(降级安全)。

**失焦自动收起为悬浮球(2026-07-12,承上)** `OverlayService`:
- 窗口 flags 加 `FLAG_WATCH_OUTSIDE_TOUCH`(配合原有 `FLAG_NOT_FOCUSABLE`),即可收到窗口外触摸的 `ACTION_OUTSIDE`。
- 在 `root(FrameLayout)` 上设 `OnTouchListener`:`ACTION_OUTSIDE && !isBall` → `showBall(true)` 收起;返回 false 不消费,底层 K歌 界面照常收到该触摸。
- 已 assembleDebug + 无线 adb 装机 = Success。

---

## 八、环境速查
- Python: `C:\Users\11651\AppData\Local\Programs\Python\Python313\python.exe`
- 声卡: Midiplus USB Audio / ROUTIST R2(多虚拟路由 PLAYBACK 1-6 等)
- loopMIDI 端口: `loopMIDI Port`(主) + `MackieFB`(回读,现未用)
- Studio One 工程: "我的直播声卡效果",通道 1音乐/2电脑/3聊天/4唱歌(湿)/5唱歌(干)/6喇叭/9手机
- 服务端口: 8765

---

## 九、阶段一整合:K歌播放器托管 + 自动曲库 + 移除投屏(2026-07-13)

pc-service 升级为电脑端 K歌中枢(总体方案见根 `KARAOKE_SYSTEM.md`)。

- **移除 scrcpy 自动投屏**:删 `scrcpy_win.py`,清掉 `server.py`(`import`、`STATE.scrcpy_*`、`_handle_cmd` 的 `ensure_scrcpy` 分支+`phone_ip`、`/scrcpy/check` 端点)、`config.py`(整段 scrcpy 配置)、`static/index.html`(scrcpy 圆点/`ensureScrcpy`/state 处理)。直播歌词改走本机 K歌播放器绿幕窗(见 karaoke-player)。
  - 坑:删 `ensure_scrcpy` 分支后,`_handle_cmd` 里紧跟的 `elif cmd=="scene"` 必须改成 `if`,否则语法错。
- **托管 K歌播放器(子进程 + 管道 IPC 显隐)**:仿 `start_smtc_reader` 用 `subprocess.Popen` 拉起 `karaoke-player/player.py --device 27 --hidden --paused --no-smtc`(全局 `_player_proc`,`do_quit` terminate)。**显隐踩过坑**:一开始想用 `karaoke_win.py`(仿 studio_win)按窗口标题 win32 `ShowWindow` 控制,但 **Qt 透明窗被外部 `SW_SHOW` 只显空壳、Qt 不重绘(截图是深灰/透视穿透,不是绿幕)**——PrintWindow 才看得到它其实在画。正解=**管道 IPC**:server 往 player 的 stdin 写 `toggle`,player 用 Qt `show()/hide()`(自己显隐才会重绘,`win.raise_()+activateWindow()` 取焦点),player 经 stdout 报 `VIS:0/1`,server 的 `_player_reader` 线程据此同步 `STATE["player_visible"]`+刷托盘+推手机。`karaoke_win.py` 降级为只**读**可见性(`is_visible`,win32 `IsWindowVisible` 跨进程读是准的)。player.py 加 `--hidden`(先 `win.show();win.hide()` 走完首次绘制初始化再隐)/`--paused`/`--no-smtc`,`showEvent/hideEvent` 上报 VIS,**服务模式:禁用手动关闭**(closeEvent 直接 `e.ignore()`,任务栏 X 不关不隐藏,窗口作绿幕捕获源常驻);**隐藏歌词先清成全绿再隐**(`blank` 态:paintEvent 只画绿背景;`hide_lyrics` 先 `repaint()` 画全绿、`QTimer.singleShot(80, hide)` 延迟隐藏,让直播伴侣捕获冻结帧=纯绿不残留歌词);`show_lyrics` 恢复正常渲染。托盘项固定文案"K歌歌词" + **勾选表示显隐**,`checked=lambda: STATE["player_visible"]` + WS `player_toggle`。
  - **托盘勾选卡住坑**:pystray 右键用预建 hmenu、`checked` 在 `update_menu()` 建菜单时才求值(后台线程调 `update_menu` 已验证能正确改勾选位)。一开始 `checked` 用 `karaoke_win.is_visible()` 现读,隐藏时 `hideEvent` 上报 VIS:0 的瞬间原生 `IsWindowVisible` 可能还没翻 False → 重建菜单读到"仍可见"→卡在勾选。改用**权威的 `STATE["player_visible"]`**(player 经 VIS 显式上报 True/False,`_player_reader` 先设 STATE 再 `refresh_tray`),无竞态。
- **自动曲库导入器 `library.py`**:后台线程监听 `WESING_RES_DIR`,四文件齐全且连续两轮 `(size,mtime)` 签名一致(=写完)才拷进 `KARAOKE_LIBRARY_DIR\<mid>\` + 解 QRC 写 `meta.json` + `library.json`。QRC 解码借用 karaoke-player 的 `tripledes`(`sys.path.append`,不引 numpy)。启动 backfill 全量补齐。
- **托盘动态菜单**:pystray 菜单项文本用 `lambda i: ...`(打开菜单即重求值),显示 `曲库: N 首`/`监听: 运行中`/`显示隐藏K歌`;`refresh_tray()=icon.update_menu()`。`STATE` 加 `player_visible`/`lib_count`/`watcher_running`。
- **验证**:import server OK;backfill 4 首→`D:\KaraokeLibrary` 各含4文件+meta(歌名正确);player 子进程隐藏启动、HWND show/hide 生效、`--no-smtc` 下无媒体会话;`server.py` 起后 HTTP 8765 正常、`/scrcpy/check` 404、KaraokePlayer 窗口在、`library.json` 4 键。

---

## 十、托盘"退出"加确认弹窗(2026-07-13)

托盘退出不可逆(停服务 + terminate 两个子进程 + `os._exit`),加 Windows 原生确认框防误点。

- **坑**:一开始直接在 `do_quit` 里 `ctypes.windll.user32.MessageBoxW`,弹窗能出但**按钮点了没反应、整个托盘卡死**。根因:pystray 托盘菜单回调跑在它自己的 Win32 消息泵线程里,同步弹模态框会**占住那条消息泵**,连弹窗自己的按钮消息都分发不了。
- **修法**:弹窗放到**独立 daemon 线程**(`do_quit` 里 `threading.Thread(target=_confirm).start()`),MessageBox 在自己线程有独立消息循环、按钮正常;选"是"才在该线程执行 `_really_quit`(收尾子进程→`os._exit`),选"否"直接返回、托盘照常挂着。`MB_TOPMOST` 保证压最前;整段 try/except,非 Windows/无 GUI 弹窗失败时回退旧行为直接退出。
- 代码 `server.py` `run_tray.do_quit`/`_really_quit`;文档同步 `README.md` 托盘条目。

---

## 十一、阶段②-2:K歌点歌/队列/控制 WS API(2026-07-13)

pc-service 把播放器 IPC 接进 WebSocket + 曲库列表 + 点歌队列,供手机 App 对接(路线图第②步服务器侧,至此第②步整体完成)。

- **曲库列表**:`GET /library` → `{count, songs:[{mid,title,artist}]}`(读 `library.manifest()`,按歌名排序),注册在 StaticFiles mount 之前。
- **点歌队列**(`server.py` 全局 `_queue`/`_now_mid`):`k_enqueue(mid)`(append,空闲即 `k_play_next`)、`k_play_next()`(pop 队首 → 给 player 发 `load/show/play`;空则停末尾)、`k_remove(idx)`、`k_clear()`;每次变更调 `_sync_queue_state()` 把 `STATE["now"]`(`{mid,**song_meta}` 或 null)、`STATE["queue"]`(带 meta 列表)刷好并推手机。
- **控制指令**(`_handle_cmd` 新增,全转成播放器 IPC):`kqueue_add/remove/next/clear`、`kplay/kpause/kplaypause`、`kkey`(绝对半音)、`kvocal`(原唱/伴奏)、`kseek`、`kshow/khide`。
- **STATE 解析 + 唱完自动下一首**:`_player_reader` 除 `VIS:` 外,解析 player 每 500ms 的 `STATE {json}` → 更新 `k_playing/k_pos/k_dur/k_key/k_vocal/k_mid/k_title/k_artist` 并推手机;检测 `prev_playing and not playing and dur>0 and pos>=dur-800`(=当前歌自然放到尾)→ 调 `k_play_next()` 自动切下一首。
  - **验证坑**:单次快照(切歌后 3.5s 抓一帧)一度看到"服务端 now 已切、但 player 还在放旧歌"像 bug;改**轮询**才看清是正常的**加载过渡**:t+2 旧歌到尾(now 切新歌、队列清空、发 load+play)→ 中间 ~1.5s 在加载新歌 62MB PCM → t+3.5 新歌 `k_mid` 更新、`playing=True`。不是 bug,是换歌加载延迟(可后续预加载优化)。
- **STATE 字段**:`server.STATE` 加 `k_playing/k_pos/k_dur/k_key/k_vocal/k_mid/k_title/k_artist/now/queue`,随 WS `state` 推。
- **验证**:`/library` 真实 HTTP 返回 5 首(含监听器新收的《理想》);入队自动开唱、控制指令、唱完自动下一首全通。
- **补**(2026-07-13,为手机 App):加 `kqueue_move{from,to}` 队列重排指令(`k_move`),供手机长按拖动/置顶。
- **补**(2026-07-13,演唱页卡拉OK数据):`karaoke_data.py` + `GET /song/{mid}/karaoke` → 某首歌的 QRC 逐字
  时间轴 + `.note` 音高线(归一化 0..1)。复用 karaoke-player 的 `tripledes`(不引 numpy),按 mid 缓存;
  实测郭源潮 30 行 / 390 音符、吉姆餐厅 34 行 / 450 音符,404 正常。手机演唱页在切歌时拉一次喂 `KaraokeStage`。
- **补**(2026-07-13,`--headless`):`server.py main()` 加 `--headless`:无托盘,服务跑后台线程、主线程
  `threading.Event().wait()` 阻塞(供无界面/自动化测试)。**坑**:一开始 headless 让 uvicorn 跑主线程 →
  与 pycaw/COM 冲突**段错误**;改回「服务后台线程 + 主线程仅阻塞」即稳。
- **切歌闪退 + 孤儿播放器 修复**(2026-07-13):
  - **服务端切歌闪退**根因:切歌→播放器 show→上报 `VIS:1`→**读取线程**调 `refresh_tray()`→pystray
    `update_menu()` **跨线程改 Win32 菜单**→原生崩溃。修:`refresh_tray` 记录托盘线程 id,**只在托盘线程
    调 update_menu**,其它线程跳过(菜单项是动态 lambda,下次打开自动刷新,不影响正确性)。
  - `_player_send` 加锁串行(读取线程自动下一首 vs 事件循环手动切歌并发写管道);自动下一首加
    `st.mid==_now_mid` 守卫,防与手动切歌撞车跳一首。
  - **孤儿播放器**(服务端崩了,歌词子进程关不掉):`player.py` 的 stdin 读取循环遇 EOF(=父进程 pc-service
    退出/崩溃)→ 发 `__quit__` 让 Qt 优雅退 + `os._exit(0)` 兜底。**实测**:只杀 server 进程,player 4s 内自退。
- **点歌开唱瞬间服务端闪退 修复**(2026-07-13,pycaw/COM):
  - **根因**:点歌开唱→App「演唱↔BGM 联动」发 `bgm playpause`→服务端音量渐变**从异步线程连发 20~40 次
    `set_qq_volume`**;旧代码里**每次调用会话缓存为空就重枚举全部 35 个音频设备(ROUTIST 多虚拟路由)**——
    高频 COM 设备枚举原生崩溃(概率性,QQ音乐无活跃会话时每次都空转重枚举,最易触发)。另有暂停后会话指针
    悬空、继续调 `SetMasterVolume` 崩的风险。**实测**:40 次快速 set 稳定段错误(exit 139)。
  - **修复**:①所有 pycaw/COM 操作丢到**一条专用 MTA 工作线程**串行执行(`_pycaw_exec`),异步/托盘线程不再
    直接碰 COM。②会话解析**限频**(`_QQ_RESOLVE_INTERVAL=1.5s`),缓存为空也不狂枚举;单次调用**只用缓存、
    失败即清缓存**,不再一次调用内反复枚举。③**整段渐变在 worker 线程一次跑完**(解析一次会话、用缓存指针
    逐步调、线程内 sleep),而非从异步线程连发几十次。④暂停/切歌后**清掉悬空会话指针**,严禁复用。
  - **实测**:原崩溃的 40 次快速 set 连跑 3 遍全过;10 次间隔重枚举、演唱↔BGM 完整渐变 3 轮、整机 headless
    启动 HTTP 200 均正常。
- **端到端真机联调通过**(2026-07-13):手机 App(装 `-t`)↔ pc-service ↔ 播放器全链路实测——WS 连接/状态同步、
  `/library` 点歌入队自动开唱、进度本地插值、**演唱页卡拉OK渲染(逐字歌词+KTV圆点+音准块+播放头active高亮)**、
  遥控页声卡/BGM/窗口开关、悬浮 BGM 球全部正常。**测试用 `adb reverse tcp:8765` 隧道**(手机 127.0.0.1:8765
  → PC),因 PC 侧 Windows 防火墙默认未放行 8765 入站、且有 QuickQ VPN 适配器;**真机 LAN 直连需在 PC 放行
  8765 入站**(安全设置,由作者自行加规则)。
- **歌词同步卡顿 / 按钮失灵 / 连续操作卡死 —— 三层性能优化**(2026-07-13):
  症状:手机歌词一顿一顿几乎不动、点按钮没反馈、连续多次操作后播放器直接卡死。**三层各有一个会叠加的坑:**
  1. **手机端 20fps 全树重组风暴(卡顿主因)**:老版 `MainActivity` 每 50ms `st.copy(posMs=displayPos)` 重建
     全局状态并传给**所有页面**,整棵 Compose 树每秒重组 20 次,`buildAnnotatedString` 反复分配 → UI 线程被
     吃满。**改**:新增 `rememberPlayhead`(帧驱动、锚点+真实帧时间差插值),返回 `State<Int>`,**只在绘制
     作用域(Canvas onDraw)/`derivedStateOf` 里读取**——每帧只失效绘制层,不再触发整树重组;音准线用 Canvas
     绘制作用域读播放头,歌词用 `derivedStateOf` 仅在"当前行/已唱字数/圆点数"变化时重组;进度条改
     `SmoothProgressBar`(帧驱动只重绘)。
  2. **pc-service 事件循环被 pycaw 阻塞(按钮失灵+歌词冻结)**:`bgm_vol`/`ping`/连接时读音量在事件循环里
     `set_qq_volume/get_qq_volume` → `_pycaw_call(...).result(timeout=8)` **同步等最多 8 秒**;单线程 pycaw 执行器
     被渐变占用时,整个事件循环卡死→所有 WS 指令不处理、状态不广播。**改**:`schedule_qq_volume`(乐观更新
     STATE + 合并高频拖动、丢 pycaw 线程,绝不 `.result()` 阻塞循环)、`schedule_qq_volume_read`(异步读+回推)。
     另把 `_player_send` 改为提交到**专用单线程 IO 执行器**(`_player_io_exec`,FIFO 保序),事件循环不再因
     阻塞管道写卡住。
  3. **播放器换原唱在 GUI 线程同步解码 PCM(连续操作卡死)**:`set_vocal` 里 `load_pcm` 整段解码原唱在 GUI
     线程跑,反复点"音源"多次解码排队冻窗。**改**:原唱首次加载放**后台线程**,`engine.swap_buffer`(自带锁)
     跨线程安全;加 `_loading_vocal` 防重入。另 60fps 重绘加 `isVisible()` 守卫,隐藏(服务默认态)不排绘制。
  详见根 [KARAOKE_SYSTEM.md](../KARAOKE_SYSTEM.md) 第三节"时钟/同步"决策与各子项目 README。
- **点歌无反应 / 播放器不弹出 —— 子进程 stdout 编码(GBK)崩掉读取线程(根因)**(2026-07-13):
  症状:手机点歌电脑毫无反应、播放器不弹窗;查出与上面的性能优化**无关**,是一条更底层的编码 bug。
  - **根因**:Windows 下被托管的子进程(`player.py` / `smtc_helper.py`)`sys.stdout` 默认 **GBK(cp936)**。
    播放器每 500ms 上报的 `STATE {…中文歌名…}`、启动打印 `载入: …`,以及 SMTC 的中文 BGM 名,都以 GBK 字节
    写出;而 pc-service 用 `encoding="utf-8"` 读 → **第一行中文就 `UnicodeDecodeError` 崩掉读取线程**(静默
    死亡)。之后没人再排空子进程 stdout → **管道缓冲写满 → 播放器 `stdout.flush()` 阻塞 GUI 线程 → 对
    `show/load/play` 完全无响应**(点歌只进 `_queue`、`_now_mid` 卡死、窗口不弹)。SMTC 同病 → 读取线程崩、
    helper 变孤儿(实测现场堆了 **10 个** smtc_helper 孤儿进程)。
  - **修复(两端强制 UTF-8 + 解码兜底)**:①`player.py` 启动即 `sys.stdout/stdin.reconfigure(encoding="utf-8",
    errors="replace")`;②`start_player`/`start_smtc_reader` 的 `Popen` 传 `env=PYTHONIOENCODING=utf-8` +
    读取侧 `errors="replace"`(双保险,任何杂字节也绝不崩读取循环)。
  - **实测验证**:独立喂 stdin 探针——`show→VIS:1`、`play→playing:true 且 pos 递进`、`pause/hide` 全响应、
    STATE 合法 UTF-8;清孤儿重启 server 后 WS 端到端——点歌自动弹窗+开唱(`now`/`k_playing`/`k_pos` 正确)、
    seek/升降调/暂停/隐藏全通,且 smtc helper 只剩 1 个不再繁殖。
  - **教训**:凡 Windows 下 `Popen` 读子进程含中文的 stdout,**父子两端都要显式 UTF-8**,别信默认编码。
- **手机音量键 → 伴奏音量**(2026-07-13,新功能,四层打通):手机媒体音量百分比 = 伴奏音量。
  ①`audio_engine` 加 `_vol` 增益(回调内 `buf*vol`,float 赋值原子免锁,换歌不重置)+ `set_volume/volume_pct`;
  ②播放器 IPC 加 `vol <0-100>` 指令、STATE 上报加 `vol`;③pc-service 加 `kvol` 指令(乐观更新 STATE["k_vol"]
  →`_player_send`)、`_player_reader` 解析回传;④App:`MainActivity.onKeyDown` 拦截音量键(仅已连接且有当前曲,
  否则不拦)→ 调手机媒体音量(弹系统音量条)→ `syncKaraokeVolFromPhone()` 发 `kvol`{百分比};开唱换歌也同步
  一次;`k_vol` 走乐观抑制窗防闪动。**实测**:`kvol 30/85` 播放器均回报正确,999 夹取到 100。
- **pycaw 悬空指针 GC 崩溃(access violation 连环)修复**(2026-07-13):测 kvol 期间 server 崩死,
  `server.log` 连环 `comtypes __del__ → Release() → access violation`。**根因**:QQ暂停/切歌后缓存的
  `ISimpleAudioVolume` 指针悬空,`_clear_qq_cache` 只是丢列表 → 引用归零 → **GC 触发 comtypes 对悬空指针
  调 Release() = 对已释放内存写 → 原生崩**。**修复**:①**指针坟场** `_qq_graveyard`——丢弃的会话指针永久持有
  引用、绝不让 GC 释放(每个几十字节,常驻服务漏这点无所谓,稳定性优先);②`_qq_running()` 门卫——QQ音乐
  进程不存在时 `_resolve_qq_sessions` 直接返回空,不再白枚举 35 个音频设备。**顺带**:smtc_helper 加 5s 心跳写
  (原来只在快照变化时写 stdout,没歌在播时永远发现不了管道已断 → 父进程崩后成孤儿,上次现场堆了 10 个即此)。
  **实测**:kvol 全链路 + 20 次快速重连风暴(上次崩溃场景)+ 风暴后再操作,全过,日志零异常。
- **切歌改为静默(不再自动弹窗置顶)**(2026-07-13):原 `k_play_next()` 切歌发 `load→show→play`,
  播放器收到 `show` 会 `show()+raise_()` 抢到最前,打断作者正在操作的窗口。按作者要求去掉 `show`,
  现在只发 `load→play`:切歌不改变歌词窗显隐,窗口显示与否完全由托盘勾选 / 遥控 `kshow/khide/player_toggle`
  手动控制(隐藏时照常播放出声)。
- **场景/音量跨重启持久缓存**(2026-07-13,按作者要求):新增 `pc-service/state_cache.json`
  (已进 `.gitignore`)——`_save_persist()` 在场景切换/归位/kvol/播放器音量上报变化时原子整写
  (临时文件 + `os.replace`,坏文件启动时静默忽略);`main()` 起步先 `_restore_persist()` 恢复
  `scene`、`_mute_state` 静音记录、`k_vol`。关键:静音记录**原样恢复、不发 MIDI**(Studio One 在服务
  重启期间状态不变,记录=现实即可继续准确切换);`k_vol` 在 `start_player()` 拉起播放器后下发一次
  `vol`(播放器默认 100)。离线冒烟:存/取往返 + 坏文件容错通过。
- **App 连接时音量方向反转:先"后端 → 手机"**(2026-07-13,配套上条):原来只有"手机 → 后端"
  (音量键/换歌 push),App 刚连上时手机音量是多少伴奏就被改成多少。现在 `RemoteViewModel` 在
  `onConn` 置 `volPullPending`,连接后第一帧 state 把 `k_vol` 静默设为手机媒体音量
  (`setStreamVolume`,不弹音量条),并跳过该帧的换歌 push(手机音量档位粗,round-trip 会把后端
  精确值盖成近似值);此后音量键/换歌恢复"手机 → 后端"。断线重连同样生效。
- **唱完不再自动连播:切下一首开头暂停,BGM 顶上**(2026-07-13,按作者要求):`_player_reader` 检测到
  自然唱完(`pos≥dur-800` 且由播转停)后不再调 `k_play_next()`(load+play 连播),改调新增的
  `k_advance_paused()`:队列有歌 → 只发 `load`(播放器 load 自带归位到 0、清调、切回伴奏、暂停),
  等主播手动按播放开唱;队空 → 清空当前曲(`now=null`)。歌曲间歇的 QQ音乐 BGM 恢复**不在服务端做**,
  由手机端既有"演唱↔BGM 联动"完成:App 看到演唱停止即自动恢复它暂停过的 BGM,主播开唱下一首时又渐弱
  暂停——服务端再做会与联动双重切换打架。手动 `kqueue_next`(切下一首)保持原样立即开唱。
- **入库成功弹系统通知**(2026-07-13,按作者要求):`library.py` 的 `start()` 加第三个注入回调
  `on_import(mid, meta, 库存数)`,`_scan_once` 每首入库成功后调用(回调包 try/except,通知失败绝不影响
  入库循环);`server.py` 新增 `_on_lib_import` → `_tray_icon.notify("《歌名 - 歌手》已入库,曲库现有 N 首")`。
  pystray 的 `notify` 底层是 `Shell_NotifyIcon(NIF_INFO)`,**没有 `update_menu` 那种线程亲和限制**,可从
  library 监听线程直接调(独立脚本实测跨线程弹泡正常、退出干净);歌名空(QRC 缺 [ti:])回退显示 mid;
  `--headless` 无托盘时静默跳过。