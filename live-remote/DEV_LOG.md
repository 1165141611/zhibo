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
