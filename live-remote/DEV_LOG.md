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
 │  ├─ SMTC(winrt 子进程)→ 有方向控 QQ音乐 播放/切歌 │
 │  │   + 读歌名/进度/状态(锁 QQ音乐 会话,双向) │
 │  ├─ pycaw → QQ音乐 单独音量(含渐强渐弱 + 回读)  │
 │  └─ win32 → 显示/隐藏 Studio One 窗口          │
 └─────────────────────────────────────────────┘
```

投屏说明:全民K歌 在手机上,scrcpy 只裁"歌词两行"投到电脑做绿幕抠图;悬浮控制台拖到裁剪框外,不进直播间。

---

## 二、电脑后台服务(pc-service)

### 文件
- `server.py` —— 主程序:FastAPI + WebSocket + 托盘 + 全部控制逻辑
- `config.py` —— 所有可调参数(端口、场景、CC/音符、渐变时长、QQ音乐进程名)
- `winmm_midi.py` —— 纯 ctypes 的 Windows MIDI 收发(免编译,不用 python-rtmidi)
- `smtc.py` —— winrt:按 AUMID 锁定 QQ音乐 会话,读歌名/进度/状态 + 有方向控播放/切歌
- `smtc_helper.py` —— 跑 `smtc` 的独立子进程(与主进程 pycaw COM 隔离);stdout 报快照、stdin 收控制指令
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
   - **踩坑(2026-07)**:重启/快速启动(休眠恢复)后可能出现 loopMIDI 在跑但端口列表为空、
     Studio One Mackie 报"没找到端口"。配置(注册表 `HKCU\Software\Tobias Erichsen\loopMIDI\Ports`)
     和 teVirtualMIDI 驱动其实都正常,**把 loopMIDI 进程退出重开一次即可自动重建两个端口**,
     然后重启 pc-service(它启动时才打开 MIDI 口),Studio One 一般会自动找回端口。
2. Studio One(打开工程)
3. 双击 `run_server.bat`
4. 手机开控制台

---

## 二·补、QQ音乐 传输控制重构(2026-07-15)—— 治"手机控 BGM 时好时坏 + 正在播的歌不同步"

**根因**:老实现走两条脆弱的开环链路——
1. 状态显示:`smtc.py` 用 `get_current_session()` 读"**系统当前**媒体会话"。直播时 WeSing /
   浏览器 / 直播伴侣 随时会抢走 current session → 手机上 BGM 歌名/状态串成别的 App,或对方无
   歌名致快照 None → STATE 冻结在旧值(=**正在播的歌没同步**)。
2. 播放控制:手机点播放/暂停 → 服务端模拟**全局媒体键** `VK_MEDIA_PLAY_PAUSE`。媒体键**无方向**,
   被 Windows 路由到抢了会话的那个 App → QQ音乐 纹丝不动(=**控制失效**)。且暂停后 SMTC 快照
   不再变化,老 `smtc_helper` "只在变化时才发" → 那帧 `bgm_playing=false` 被永久丢掉 → 状态漂移
   → 幂等 `play` 以为在播不恢复、手动 `playpause` 走错分支方向反打,越点越乱。

**修法**(4 项):
1. `smtc.py` 改用 `get_sessions()` 遍历所有会话,按 `source_app_user_model_id` 含
   `config.QQMUSIC_SMTC_HINT`(默认 `"qqmusic"`)**精确锁定 QQ音乐 自己的会话**;锁不到才退回
   current session 保底。显示与控制从此都对准 QQ音乐,不再被抢会话干扰。
2. **有方向控制**:播放/暂停/上下首改用会话的 `try_play/try_pause/try_skip_*_async`,替换全局
   媒体键。彻底消除"打到别的 App"和"方向反打"。winrt 调用仍全在子进程(与主进程 pycaw COM 隔离)。
3. `smtc_helper.py` 加 **stdin 指令通道**(父进程 `_smtc_send` 下发 play/pause/next/prev),并
   **每约 1s 无条件重发一帧快照**(父进程侧自去重广播),杜绝"丢一帧就永久漂移"。启动时把所有
   会话 AUMID 打进 `server.log`(`#SMTC 媒体会话 AUMID: [...]`),便于确认/调整 hint。
4. **`bgm_vol` 周期回读**:新增 `_start_bgm_vol_poller()`(间隔 `config.BGM_VOL_POLL_INTERVAL`,
   默认 4s,只在在播且不渐变时读,走 pycaw 专线程),把 PC 上**手动改的** BGM 音量反向同步到手机。
   顺手清掉了 main() 里那条早已不存在的 `_bgm_poller` 过期注释。

**实测中又揪出并修掉两个 bug**(2026-07-15 headless 实跑 + WS 客户端验证):
- **SMTC 读取线程被 `UnboundLocalError: time` 秒杀**:`start_smtc_reader` 的 `worker()` 里子进程退出
  重启处写了 `import time`,使 `time` 变**函数局部名**,上面 `time.monotonic()`(抑制窗判断)一执行
  就 UnboundLocalError → 读取线程当场死 → BGM 歌名/进度/状态**根本不上报**(极可能就是"正在播的歌
  不同步"的一大主因)。删掉函数内 `import time`,用模块级导入即可。**教训:函数里任何位置 import 了
  模块级已有的名字,该名字全函数变局部,之前的引用全 UnboundLocalError。**
- **QQ音乐 SMTC `playback_status` 不可靠**:实测它**在播时常年卡在 2(Changing)甚至 1(Opened)**,
  只偶尔正确报 4(Playing)。照字面 `status==4` 判断 → "正在播的歌"被判成暂停,`bgm_playing=False`。
  修法(`smtc.py`):确定态(4=在播;0/3/5=Closed/Stopped/Paused=没播)照信;**含糊态(1/2)看 position
  是否推进**——进度在走=在播,冻结=没播。QQ音乐 的 position 一直可靠,是比 status 更硬的信号。
  实测:修前 play 后 4.2s 掉回 False 卡死;修后 play 后稳定 True 不掉。

**未动**:pycaw 音量渐强渐弱那套(端点静音保险丝 / active 会话轮询 / 指针坟场)保持原样——
它治的是"QQ 暂停即销毁会话、恢复时新会话默认 100% 炸响",与控制方向无关,不能一起删。

**若 BGM 仍锁不到 QQ音乐**:看 `server.log` 里 `#SMTC 媒体会话 AUMID` 那行,把 QQ音乐 对应
那串的稳定片段填进 `config.py` 的 `QQMUSIC_SMTC_HINT`,重启 pc-service 即可。

## 二·补²、播放/暂停失灵 + 音量自动回 100% 的彻底排查(2026-07-16)

**用户报症**:手机连续点几次"把暂停的 QQ音乐 点成播放" → 电脑 QQ音乐 没反应 → 过几秒手机上
播放键自动回到暂停、音量条自动弹回 100%。逐层实测(隔离 winrt 控制 / WS 走服务端 / pycaw 探针)
挖出**四个叠加的坑**:

1. **`bgm_vol` 周期轮询触发 25s 级设备枚举,把 pycaw 单线程整段占死**。本机 ROUTIST 有 35 个
   虚拟音频设备,一次全量 `_resolve_qq_sessions`(`GetAllDevices`+逐设备 Activate)实测能到 **25s**;
   我上一轮加的音量反向轮询每 4s 就引发一次 → 播放/暂停的渐变全排在它后面等好几秒~几十秒,
   表现就是"点了没反应,过一会儿才动或干脆被 SMTC 抑制窗过期翻回旧态"。
   **修**:轮询改 `_get_qq_volume_impl(resolve=False)` **只读已有缓存指针、绝不枚举**;启动时后台
   预热一次缓存;持久会话让缓存长期有效。
2. **`_fading` 期间后续指令被直接 `return` 丢弃** → "连点几次没反应"。**修**:改 `_bgm_apply` **合并
   最新期望**(`_bgm_desired`),当前渐变收尾后自动续做到最新意图,连点/反向点都收敛到最后一下。
3. **QQ音乐 暂停不销毁会话,但恢复播放时会把自己的会话音量"一次性重置回 100%"**(实测:暂停时
   把会话设 0 或 20,恢复后 ~1~1.5s 都变 100;而恢复**后**再设就稳)。老代码"轮询到会话就压 0 再
   渐强"压得太早——渐强都跑完了 QQ 才重置到 100,于是**最终卡 100、用户音量丢失 + 炸响**。
   **修**:恢复时先静音设备端点当保险丝(仅伴奏静默时,别掐共用 PLAYBACK 1/2 的伴奏)→ 有方向
   play → **等 QQ 那次重置真的发生(会话音量跳到 ≥90)**→ 压 0 接管 → 解静音 → 渐强到 full →
   补设两拍兜底。
4. 附带修掉自己上一轮 `_play_fade_in_impl` 因"QQ 暂停即销毁会话"的**过时假设**写出的
   ~6s 死等新会话轮询(实测会话根本不销毁、指针跨暂停仍有效,`test_ptr` 验证)。

**结果**(WS 实测):播放秒恢复、暂停正常、**用户设的音量(如 45)跨暂停/播放稳稳保持不再回 100**、
端点不会卡静音、连点合并到最后一下、无 25s 卡顿。
**代价**:恢复播放有 ~1.5s 静音窗(端点静音等 QQ 重置那一下)再淡入——BGM 场景可接受。
**教训**:①QQ音乐 系会话音量是"QQ 自己会在恢复时重置的",任何"设了就不管"的写法都会被它打回;
必须等它重置后再接管。②本机 35 设备,任何自动/周期性的全量枚举都是雷,只能读缓存。

**补(2026-07-16 同日)——切歌(next/prev)也会触发同一个"重置回100%"**:实测 directed next 后
会话音量从 40 立刻变 100 并保持(和恢复播放同源,QQ 换歌时重置自己的会话音量)。老代码切歌只发
transport、完全不管音量 → 新歌 100% 炸响、手机音量条弹回 100(用户报的就是这个)。
**修**:next/prev 也走"静音端点保险丝(伴奏静默时)→ 有方向切歌 → 等 QQ 重置发生 → 压回用户音量
→ 解静音 → 补设兜底"(`_bgm_switch`/`_bgm_switch_impl`);连点切歌用 `_bgm_switch_again` 收尾补一次
音量接管。实测:设 38 后连点 next×3,**实际会话音量稳稳 38、不回 100**。

**再补——自动切歌(一首放完 QQ 自动下一首)同样会重置到 100**。它不是手机点的 next,不走
`_bgm_switch`,所以要**被动检测 + 补接管**:
- `smtc.py`/`smtc_helper.py`:子进程改成每 ~0.35s 探一次快照,**歌名/播放态一变立即发**(进度类仍每
  1s 无条件重发一帧),让父进程尽快知道换歌了,缩短炸响窗。
- `server.py` SMTC 读取线程:检测到**歌名变 / 进度大幅回退**且"在播 + 不在渐变"(=非手机点的
  next/prev)→ 判定自动切歌 → `_reassert_bgm_vol()` 在 pycaw 线程把会话音量反复压回 `STATE.bgm_vol`
  ~1.8s(盖过 QQ 的一次性重置)。
- **反向轮询忽略 ≥98 的读数**:QQ 换歌/恢复重置到 100 不是"PC 端手动改",若被反向同步会把
  `STATE.bgm_vol` 冲成 100、连累接管拿错目标;想要满音量走手机滑条前向设即可。
自动切歌新歌已在播、无法预先静音,只能尽快压回;配合 0.35s 快检测 + 每 0.3s 补设,实测 0.15s 采样
都基本抓不到 100 的瞬时(炸响窗 < 0.3s)。实测:设 40 后模拟自动切歌(外部 directed next 绕过
`_bgm_switch`),**会话音量全程 40、不回 100**。

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

> ⚠️ **传输控制(播放/暂停/上下首)已于 2026-07-15 重构为 SMTC 会话有方向控制,见上「二·补」。
> 本节的"系统媒体键"方案已废弃**(媒体键无方向、会被抢占系统当前会话的 App 截走)。音量(pycaw)一节仍有效。

- **播放/暂停/上下一首**:~~系统媒体键(全局,不抢焦点)~~(已废弃,见二·补:改 SMTC 有方向控制)。QQ音乐 响应系统媒体会话。
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

- **曲库列表**:`GET /library` → `{count, songs:[{mid,title,artist,plays}]}`(读 `library.manifest()`),**默认按点歌次数 `plays` 倒序、同次数按歌名升序**,注册在 StaticFiles mount 之前。
- **点歌队列**(`server.py` 全局 `_queue`/`_now_mid`):`k_enqueue(mid)`(append,空闲即 `k_play_next`)、`k_play_next()`(pop 队首 → 给 player 发 `load/show/play`;空则停末尾)、`k_remove(idx)`、`k_clear()`;每次变更调 `_sync_queue_state()` 把 `STATE["now"]`(`{mid,**song_meta}` 或 null)、`STATE["queue"]`(带 meta 列表)刷好并推手机。
- **控制指令**(`_handle_cmd` 新增,全转成播放器 IPC):`kqueue_add/remove/next/clear`、`kplay/kpause/kplaypause`、`kkey`(绝对半音)、`kvocal`(原唱/伴奏)、`kseek`、`kshow/khide`。
- **STATE 解析 + 唱完自动下一首**:`_player_reader` 除 `VIS:` 外,解析 player 每 500ms 的 `STATE {json}` → 更新 `k_playing/k_pos/k_dur/k_key/k_vocal/k_mid/k_title/k_artist` 并推手机;检测 `prev_playing and not playing and dur>0 and pos>=dur-800`(=当前歌自然放到尾)→ 调 `k_play_next()` 自动切下一首。
  - **验证坑**:单次快照(切歌后 3.5s 抓一帧)一度看到"服务端 now 已切、但 player 还在放旧歌"像 bug;改**轮询**才看清是正常的**加载过渡**:t+2 旧歌到尾(now 切新歌、队列清空、发 load+play)→ 中间 ~1.5s 在加载新歌 62MB PCM → t+3.5 新歌 `k_mid` 更新、`playing=True`。不是 bug,是换歌加载延迟(可后续预加载优化)。
- **STATE 字段**:`server.STATE` 加 `k_playing/k_pos/k_dur/k_key/k_vocal/k_mid/k_title/k_artist/now/queue`,随 WS `state` 推。
- **验证**:`/library` 真实 HTTP 返回 5 首(含监听器新收的《理想》);入队自动开唱、控制指令、唱完自动下一首全通。
- **补**(2026-07-13,为手机 App):加 `kqueue_move{from,to}` 队列重排指令(`k_move`),供手机长按拖动/置顶。
- **补**(2026-07-16,点歌次数):每次点歌(`k_enqueue` → `library.bump_play(mid)`)给该曲 `plays` +1,
  存进 `library.json`(只存清单,不写 meta.json——启动迁移会按 QRC 重写 meta.json 冲掉它)。`/library`
  改按 `plays` 倒序返回;手机 App(`Song.plays` + `RemoteViewModel.refreshLibrary` 按 `plays` 倒序、
  同次数按歌名)默认点歌列表常点的浮最前。**改了手机端,已 `assembleDebug` + adb 装机**。
- **补**(2026-07-16,每曲默认调式):曲库管理页每行加 **`−  <调>  +`** 控件(`_bump_key`),点即
  `library.set_key(mid, n)` 存进 `library.json`(夹 [-6,6],0=原调;**不触发全量刷新**,就地更新标签不丢
  滚动位置);正在唱这首则实时 `_player_send("key n")` 边调边听。载歌统一走 `_player_load(mid)`=
  `load`(player 会清调到 0)+ 若默认调非 0 补下发 `key n`,**手机点到这首直接是调好的调**。
  **手机端不用改**:App 的 `key` 本就跟随服务端推的 `k_key`(载入后 player 上报 `key=n` → server 推
  → App 显示),实测点歌 鼓楼(默认 +3)载入后 `k_key=3` 自动生效,无需重装 App。
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
- **歌名不准 + 托盘计数不刷新 —— 两个 bug 一起修**(2026-07-14,承上条,作者反馈):作者新入库成都/程艾影/鼓楼,
  成都显示成 `2422569`、鼓楼显示成 `鼓楼-赵雷-ktv`,且托盘曲库数没变、疑似"没入库"。
  - **排查一:歌名根因不在解密,在源数据**。歌名取自 QRC `[ti:]`。dump 成都 QRC:`[ti:2422569][ar:0]`——
    WeSing 对这首 KTV 版伴奏在歌词文件里就存了内部数字ID,**继续解密同一文件没用,源就是脏的**。鼓楼是
    `[ti:鼓楼-赵雷-ktv]`(带歌手+ktv 后缀,可清洗)。**试过在线反查(QQ音乐搜歌词)全废**:返回翻唱/电台
    节目,连 [ti:] 本来对的《吉姆餐厅》都排第6、《郭源潮》搜不到——放弃联网。
  - **排查二:干净歌名在本地但 AES 加密**。作者点明应走本地解密。WeSing `User Data\KSongsDataInfo.dat`
    (最近唱过的歌,加了三首后才更新)确有干净名,但**熵 7.904、16字节完美对齐却 133 块全不重复(非ECB,
    是CBC/CTR)**;多策略(PCM静态XOR / 单字节异或全扫 / 3DES / 位反转)零命中;KSongs 系列 DLL **无 TEA
    delta**,AES S-box 只在登录/OpenSSL,DLL 是 stripped C++ 搜不到 key。= 真 AES + 运行时密钥,要解只能
    逆向/扒内存——即 karaoke-player README 标记的"弯路"。**方案放弃,转手动改名**(作者预授权"不行就手动")。
  - **修法(歌名)**:`library._clean_title`(去 `-歌手-ktv`/版本括号,英文连字符标题不误伤)+ `_is_junk_title`
    (纯数字/空=救不回)→ `_qrc_meta` 返回加 `needs_name`。救回鼓楼(→`鼓楼`)、成都标 needs_name。纯数字歌手
    (`0`)也清成空。`rename(mid,title,artist)` 更新 `library.json`+`meta.json`+内存清单、标 `named=True`;
    `pending_rename()` 列待命名。启动 `_remigrate` 用新规则重刷旧条目(幂等,**跳过 named 不覆盖手动订正**)。
    实测:8 首重迁移——鼓楼修好、成都标 needs_name、其余不变;`rename(成都,赵雷)` 落盘正确。
  - **修法(手动改名 UI)**:入库若 `needs_name`,通知文案改"点此改名"并记 `_last_needs_name_mid`;
    **点气泡通知**→ 弹 tkinter 两栏(歌名/歌手)编辑框(**独立线程跑 mainloop,绝不阻塞托盘消息泵**,同
    do_quit 教训)→ 存 `library.rename`。托盘另加"✎ 待命名(N)"项(`visible` 回调,仅有待命名时显示)兜底。
  - **修法(托盘计数不刷新,独立真 bug)**:根因——**pystray-win32 右键弹的是缓存菜单句柄,不会在打开时重新
    求值动态 lambda**,菜单文字只在 `update_menu()` 被调时刷新;而 `update_menu` 只能在托盘线程调(跨线程改
    Win32 菜单会崩),非托盘线程(曲库监听/播放器reader)的 `refresh_tray()` **旧代码直接跳过**→ 计数/勾选
    变化后永不刷新("下次打开自动刷新"的注释是错的)。修:注册自定义窗口消息 `WM_TRAY_REFRESH=WM_USER+20`,
    `refresh_tray` 非托盘线程时 `PostMessageW` 唤醒托盘线程,经 pystray 的 `_dispatcher` 分发到 `update_menu`
    (托盘线程执行=安全);气泡点击也经包裹的 `WM_NOTIFY(WM_USER+11)` handler 同法分发到改名框。
    **实测**:真 pystray 图标,非消息泵线程 PostMessage → 菜单文字从"5"刷新到"9"、气泡点击回调触发、干净退出。
- **改名交互完善 + 曲库管理窗**(2026-07-14,作者反馈继续):
  - **通知点击串歌 bug**:原来只有 `needs_name` 的歌才记 mid、才可点,非待命名的点了没反应;且改完一首后,
    下一首(无 needs_name)通知点击打开的仍是上一首。根因:balloon 目标是 `_last_needs_name_mid`,只在
    needs_name 时更新。修:改为 `_last_import_mid`,**每首入库都更新**(=当前气泡对应的歌;Windows 只显示
    最新气泡,故它就是可见气泡的歌),点气泡永远编辑"这条通知的歌",所有歌都能点改。
  - **托盘精简**:移除"监听: 运行中"行(`watcher_running` 仍留 STATE 无害)。
  - **曲库项可点击 → 曲库管理窗**(`_open_library_browser`):tkinter `ttk.Treeview`,按 `added` **倒序**列
    全部歌,顶部搜索框 `StringVar.trace_add` 实时过滤(歌名/歌手),双击行或"编辑选中"→ 子 `Toplevel`
    `grab_set` 模态改名(保存后 `refresh()` 重列),待命名歌行标红。**单 Tk 根 + 子 Toplevel 同根同线程**
    (多 Tk 根跨线程易崩,故编辑用 Toplevel 不另开根);通知改名框仍各自独立根(独立线程),两者共用
    `_build_edit_form(container, mid, on_saved)`。
  - **实测**:真实曲库(13首)冒烟——Treeview 倒序正确(最新在顶)、搜"赵雷"按歌手过滤、搜"鼓楼"过滤到1首、
    编辑 Toplevel 预填正确、构建+搜索+编辑+销毁无异常;server 重启零错误、`/library` 13 首歌名全干净。
- **Studio One 显隐持久 + 伴奏音量感知曲线**(2026-07-14,作者反馈):
  - **Studio One 显隐跨重启持久**:`_save_persist` 加 `studio_visible`,`studio_toggle` 命令改后 `_save_persist()`,
    `_restore_persist` 恢复 `STATE["studio_visible"]`;**上次隐藏则重新 `studio_win.hide()`,可见则不动**
    (避免启动抢焦点)。与声卡场景/静音/k_vol 同存 `state_cache.json`。实测:save 写入、False 恢复调一次 hide、
    True 恢复不动窗口,往返全过。
  - **伴奏音量感知(平方)曲线**:作者反馈"手机音量调到最小档依然较大"。根因:`audio_engine` 原来
    **线性**映射(增益=%/100),但人耳近对数,最小档(15档制约7%)线性=-23dB 仍听得清。改 `_gain_for(pct)`
    返回 `(pct/100)²`:最小档→0.5%≈**-46dB**(真正小声)、50%→25%、100%→原样,低端衰减更快、控制更细。
    重构 `_vol`→`_vol_pct`(档位,`volume_pct` 报它,STATE/手机同步用档位不受影响)+ `_gain`(回调用,平方后)。
    实测:各档位 dB 曲线对照(线性 vs 平方)正确、`volume_pct` 仍报档位、150 夹到 100;重启后 `k_vol=14`
    (作者本就调很低)现增益 0.0196(-34dB)明显更轻。
- **曲库管理窗改版:每行编辑/播放按钮 + 右侧滚动条 + 播放即切歌**(2026-07-14,作者反馈):
  - Treeview → **Canvas + 内嵌 Frame + `ttk.Scrollbar`(右侧)**:为了每行放真 `tk.Button`(Treeview 单元格放不了
    控件)。`inner` 用 grid,列0(歌名)`columnconfigure(weight=1)` 可伸缩,列1歌手/列2时间/列3编辑/列4播放
    固定;`canvas.create_window` + `<Configure>` 同步 scrollregion 与内层宽度;`bind_all("<MouseWheel>")` 滚轮
    (本解释器独享)。`refresh()` 销毁重建行,搜索/改名后重列。
  - **播放按钮 → `k_play_mid(mid)`**:立即 `load`+`play`,**有歌在播则=切歌**(静默,不发 show,同 k_play_next
    风格);不入队,`_queue` 保持不变(本首自然唱完仍按队列续)。
  - **移除底部"编辑选中"按钮**(编辑移到每行);编辑仍走子 `Toplevel` + `_build_edit_form`(同根同线程)。
  - 实测:真实曲库(93首)冒烟——滚动条 mapped、行数正确、点第一行"播放"触发 k_play_mid、"编辑"开预填框、
    搜"鼓楼"过滤到1首;整屏截图确认布局(歌名|歌手|时间|编辑|播放 对齐,右侧滚动条)正常;server 重启零错误。
  - **补:行分隔/选中样式**(作者反馈"看不出行边界、点中无选中态"):纯 tk 无 Treeview 的行样式,手动画——
    每行独立 `Frame`(斑马纹交替底色 #fff/#f4f5f7)+ 悬停高亮(#eaf1fb)+ 点击/播放选中态(#c8e0f8)。
    每行绑 `<Enter>/<Leave>/<Button-1>` 到行框+各格 Label,`paint(hover)` 按"选中>悬停>底色"重着色;
    `sel["mid"]` 跨搜索保留。列用行内 grid(列0 `weight=1 minsize=170` 伸缩)保证跨行对齐。截图确认三态清晰。
- **音准线显隐遥控 + 缓存(四层打通)**(2026-07-14,作者要求):
  - **播放器** `player.py`:加 `self.show_pitch`(默认 True),`paintEvent` 用它守卫 `_draw_pitch`;stdin 加
    `pitch 0|1` 命令;STATE 上报加 `pitch`。渲染实测:`show_pitch=False` 时音准带消失、歌词不受影响。
  - **pc-service** `server.py`:`STATE["pitch_visible"]`;WS `pitch_toggle` → 翻转 + `_player_send("pitch …")`
    + `_save_persist` +(`_handle_cmd` 末尾统一)广播;`_save_persist/_restore_persist` 加 `pitch_visible`;
    `start_player` 拉起后随 `vol` 一起下发 `pitch`(重启继承)。**`_player_reader` 回读 `st["pitch"]`**(同
    key/vocal/vol 的回读模式,有变即 `_save_persist`)——这样播放器 `P` 键切换也同步到手机(见下条快捷键)。
  - **手机 App**:`AppState.pitchVisible`、`parseState` 读 `pitch_visible`、`RemoteViewModel.togglePitch()`
    发 `pitch_toggle`、`RemoteScreen` "窗口开关"区在"K歌歌词"下加 `WindowRow("音准线 显示/隐藏", …)`。
  - **验证**:WS 端到端(`websockets` 客户端)——初值 True → `pitch_toggle` 广播翻 False + `state_cache.json`
    落 False → 再 toggle 切回 True,全 OK;Kotlin `:app:compileDebugKotlin` + `assembleDebug` BUILD SUCCESSFUL
    (APK 10.1MB)。**手机当前未连(USB/无线/mDNS 均无设备),APK 已出待装。**
- **播放器右下角快捷键提示 + `P` 键音准线 + 确认手机演唱页音准线独立**(2026-07-14,承上):
  - `player.py` 加 `P` 键 → `show_pitch` 翻转(经 STATE 回读同步手机);`paintEvent` 加 `_draw_hotkeys`——
    右下角画 `←→ 步退/进   ↑↓ 升降调   R 原唱/伴奏   P 音准线`,与左下 `_draw_status` **同款**
    (font_status 11pt + 白字黑描边 ow=3),静态文案建一次 pixmap 缓存(DPR 变时清)。`win.grab()` 裁底条
    截图确认:左下播放信息、右下快捷键提示两条对齐同款。
  - **确认手机演唱页音准线不受电脑端影响**:`SingScreen` 用 `KaraokeStage(...)` 画音准线,只依赖 `lyrics.notes`,
    **完全不引用 `pitchVisible`**(该字段只用于遥控页开关)。故手机演唱页音准线**始终显示**,与电脑端 `pitch_visible`
    无关——无需改代码,查证即结论。
- **音准块描边淡化**(2026-07-14,作者反馈"边线又粗又黑、手机屏缩小后像黑线条"):`_draw_pitch` 的音准块
  描边由 `黑(0,0,0)/2px` 改为 `深蓝灰(45,55,70)/1px`——仍非绿够暗(绿幕抠图的边缘缓冲照样干净),但细一半、
  颜色柔和,缩到手机屏不再是刺眼粗黑线。只动音准块;歌词(ow=5)/圆点的黑描边不变(文字要粗描边才清晰)。
  放大 3× 截图确认:音准块变成很淡的细边。
- **音准块加粗 20% + 歌词黑边改 1px**(2026-07-14,作者要求):`_draw_pitch` 的 `bar_h` 6→7.2(≈+20%);
  逐字歌词 base/hi(`_word_entry`/`_draw_word_line`)与前奏歌名(`_draw_plain_line`)的黑描边 5/4px→`1px`
  (base 与 hi 必须同宽,否则高亮盖不齐)。状态栏/快捷键(3px)、圆点(2px)不动。截图确认歌词清爽、音准略粗。
- **音准块染色改为"同歌词"由白染蓝**(2026-07-14,作者反馈"颜色反了"):原来 active 块青底、已唱部分填白
  (=已唱变白),反了。改为:每块**底白**(245,245,245)+描边,**播放头左侧(已唱)裁出染蓝**(80,220,255,
  同歌词高亮),右侧留白;跨播放头那块正好左蓝右白,随乐由白染蓝。删掉旧的 active/pr 分支。
- **歌词字体 Q 键循环 + 缓存**(2026-07-14,作者选定 7 款):`player.py` 加 `FONTS` 列表[(名,族,是否加粗)]
  =微软雅黑/黑体/思源黑Black/思源黑Medium/思源宋/思源宋Black/楷体(bold 位与选字示例一致:名含 Black/Medium
  的不叠粗);`_apply_font(idx)` 换 font_big/small、重算 `_line_h`、清文字缓存(旧字体 pixmap 作废),`__init__`
  改为 `font_idx=0` + `_apply_font(0)`;`Q` 键循环 + `_flash_status` 左下角提示当前字体名(2s);IPC `font <idx>`;
  STATE 上报加 `font`。**pc-service**:`STATE["k_font"]`;`_save_persist/_restore_persist` 加 `k_font`;`start_player`
  随 vol/pitch 下发 `font`;`_player_reader` 回读 `st["font"]`(Q 键切换即缓存,同 pitch)。**验证**:7 款逐个
  `_apply_font` 渲染截图全 OK(青高亮+白染);WS state 含 `k_font`;持久化往返 save/restore k_font=6 通过。
  仅键盘快捷键,未加手机 UI(作者只要 Q 键)。
- **歌单勾选 + 顶端滚动字幕 + 布局下移 + O/Ctrl↑↓**(2026-07-14,作者四点需求):
  1. **曲库管理页加勾选框列**:每行 col0 加 `tk.Checkbutton`(默认按 `STATE["setlist"]`),`command` 调
     `set_setlist_member(mid, on)`(更新 STATE + `_save_persist` + `_push_setlist` 推播放器 + 广播)。**坑**:
     `BooleanVar` 必须被 `command` 的 `var=var` 默认参数引用住,否则被 GC → 勾选态丢(渲染测试脚本没引用就白框)。
  2. **播放器顶端横向循环滚动歌单**(`_draw_setlist`):只歌名、空格分隔、无序号无歌手;`_make_line_pixmap` 建
     一张 pixmap(尾部补空格接头无缝),`time.monotonic()*speed % period` 平铺两份滚。内容经 IPC `setlist <json>`
     从 pc-service(据勾选 `_push_setlist`)推来。**主题下移**:`_layout()` 把音准带+两行歌词底部锚定到状态栏上方。
  3. **`Ctrl+↑↓` 移歌单**(`_move_setlist`):`keyPressEvent` 里 Up/Down 判 `ControlModifier` 分流(无修饰=升降调);
     上界 0(不越窗顶)、下界 `pitch_top-歌单高`(不覆盖音轨,`_layout()` 算)。
  4. **`O` 键歌单显隐** + 右下角快捷提示**改两行**(叠在状态栏上方,右对齐不与左下播放信息横向撞)含
     `O 歌单`/`Ctrl+↑↓ 移歌单`。**缓存**:`STATE` 加 `setlist`/`setlist_visible`/`setlist_y`,save/restore + start
     下发 + `_player_reader` 回读 `setlist_show`/`setlist_y`(同 pitch)。
  **验证**:渲染截图——顶部滚动歌单(6首名)、中部留白、底部音轨+双行歌词、右下两行快捷键;Ctrl+↓到底=音轨顶-歌单高、
  Ctrl+↑到顶=0;O 隐藏;曲库窗勾选框列渲染 OK;离线集成——勾选→STATE+推正确歌名+落盘、取消→移除、三字段持久化往返全过。
- **竖屏 3:4 窗 + 歌单间距收窄 + 快捷提示底对齐**(2026-07-14,作者微调):①窗 `760×760`→`720×960`(3:4 竖屏,配
  竖屏直播);②歌单分隔 `8 普通空格`→`两个全角空格`(≈两字间距);③`_draw_hotkeys` 底行 `base` 由 `h-26-fh` 改
  `h-26`,与左下播放信息(`_draw_status`,也 h-26)**同底对齐**(右对齐,长度下二者不横向撞)。截图确认三点 OK。
- **修:歌单启动不显示(竞态)**(2026-07-14,作者反馈"不显示滚动歌单"):`library.start()` 原来只 spawn 后台
  worker **异步**载 `_MANIFEST`,而 `main()` 紧接着 `start_player()`→`_push_setlist()` 取歌名,此刻 manifest 可能
  还没载完 → `song_meta` 全 None → 推空歌单 → 播放器顶端无字幕。**修**:`_MANIFEST = _load_manifest()` 挪到
  `start()` **同步**先载完再返回(小 json,快),`_remigrate`(读 QRC 慢)仍留 worker 后台;并给 `_on_lib_change`
  加 `_push_setlist()`(迁移修正歌名/新歌入库后重推)。**验证**:`start()` 返回后 `song_meta` 立即取到 45 首名;
  重启后经 WS 显示托管播放器,顶端滚动歌单正常出现。
- **手机遥控页加"滚动歌单 显示/隐藏"开关**(2026-07-14,作者要求,放"K歌歌词"上方):`server.py` 加 WS
  `setlist_toggle`(翻转 `STATE["setlist_visible"]` + `_player_send("setlist_show …")` + 存盘;`_broadcast` 发整个
  STATE 故 `setlist_visible` 已在广播里)。App:`AppState.setlistVisible`、`parseState` 读 `setlist_visible`、
  `RemoteViewModel.toggleSetlist()`、`RemoteScreen` "窗口开关"区顺序 Studio One → **滚动歌单** → K歌歌词 → 音准线。
  **验证**:WS 端到端 `setlist_toggle` 翻转+落盘+切回全过;`assembleDebug` BUILD SUCCESSFUL。**手机当前离线
  (adb 192.168.1.6:5555 offline),APK 已出待装。**
- **歌词双行改"按乐句分槽"(方案C)**(2026-07-14,作者反馈"带圆点首句落下排很奇怪"):原来严格奇偶全局分槽,
  一段的首句(奇数行)会落到下排、其后句在上排,阅读顺序倒;且引导圆点画在下排句左端上方→漂到右上、像挂在
  上排句尾。**改**:`_compute_slots()` 预计算——大空档(≥`PHRASE_GAP`=4s)后的乐句首行**回到上排**,句内左右
  交替。保证:①一段首句在上排(顺读);②句内两行永远异槽(不重叠);③起唱不跳位(active 与引导态同一槽表);
  ④远句(下一行是新乐句首)不提前显示,免重叠。引导圆点因此总落在"即将唱句=乐句首=上排"的句首上方。
  **验证**:槽位表打印正确(line0上/句内交替)、前奏引导截图确认圆点+首句在左上、次句在右下。
- **曲库管理窗高性能化 + 手机点歌抽屉触底分页**(2026-07-14,作者要求):
  - **PC(`_open_library_browser`)**:①**触底分页渲染**——筛选/排序先在数据层算好整个结果集,tk 控件每批只建
    60 行;`yscrollcommand` 包一层(喂 `vsb.set` 同时做触底检测,`last>0.94` 即 `after_idle` 续批,`queued` 防
    重入);内容不满一屏时 `last=1.0` 同样触发 → 自动填满首屏。不再一次性全量建几百行(打开/每键搜索都全表重建
    是老版卡顿根源)。②**搜索防抖 200ms**(`after_cancel`+`after`)。③**Live 筛选**下拉:全部/只看Live/排除Live
    (歌名含 `live` 不分大小写即视为 Live)。④**排序**下拉:最新入库(默认)/未勾选在前/已勾选在前(勾选=在
    `STATE["setlist"]`;组内按入库时间倒序;勾选/取消不即时重排防行跳动)。计数改"已显示 x / 共 y 首"。
    加 `selftest=True` headless 自检参数:自动滚底驱动分页、打印进度、渲完自毁。
    **验证**:500 首假歌(1/5 带 Live)自检——首屏自动填到 120,触底每次 +60 直到 500/500,无报错。
  - **App(`PickerSheet`)**:`LazyColumn` 只喂过滤结果前 `visible` 条(每批 `PICKER_PAGE=60`),`derivedStateOf`
    监听最后可见项进倒数 6 条 → 追加;`shown.size` 变化让 effect 重跑,仍在底部就继续追加(不卡在"差几条
    不加载")。搜索词变化 `remember(query)` 重置回首批;末尾"上滑加载更多 · x/y"提示行。手机端按需求只做
    搜索 + 触底分页(Live 筛选/勾选排序仅 PC 曲库管理窗)。**验证**:`assembleDebug` BUILD SUCCESSFUL + adb 装机。
- **服务端反复闪退(开曲库管理窗 / 点歌开唱)根因定位 + 修复**(2026-07-14,作者报"容易闪退"):
  - **取证**:Windows 事件日志 4 次同签名崩溃 `python.exe / _ctypes.pyd+0x8535 / c0000005`(7/13 22:02、
    7/14 00:04、7/14 20:19:55、20:20:23)+ 1 次 `ucrtbase c0000409`(failfast,堆已被改坏的典型);按崩溃
    进程启动时间(FILETIME)与存活进程对时,崩的都是 **server.py 主进程**(20:20:06 启动的那次只活了 17 秒);
    播放器/手机 App 完全不用 ctypes/comtypes,排除。`server.log` 同期实录两条
    `comtypes __del__ → Release() → access violation writing`(其一写往 DLL 映射区地址)。
  - **根因**:7/13 的"指针坟场"只保住了**缓存的** `ISimpleAudioVolume`;每次 `_resolve_qq_sessions` 仍会
    临时创建几十个 COM 包装(GetAllDevices 的 35 个设备、SessionManager、枚举器、每会话 ctl/ctl2),QQ音乐
    暂停/切歌后这些指针悬空,**GC 在任意线程触发都会去 `__del__→Release()`**:被 ctypes SEH 兜住就是
    server.log 里的 "Exception ignored"(**"写"类 AV 落在可写页上会静默改坏堆**,后来在随机位置
    c0000409);兜不住就直接 c0000005 闪退。所以**开曲库管理窗**(一次建几十上百个 tk 控件,触发分代 GC,
    毒 `__del__` 恰好在 tk 线程执行)和**点歌开唱**(BGM 联动渐变频繁解析/作废会话)最易命中。
  - **修复**:server.py 导入 pycaw 后全局把 `comtypes._post_coinit.unknwn._compointer_base.__del__`
    替换为 no-op——**所有 COM 包装都不再由 GC Release**(坟场哲学推广到全部;故意泄漏,单个几十字节,
    常驻服务可接受)。comtypes 升级找不到该私有类时仅打日志不拦启动。
  - **验证**:独立进程 import server(补丁生效)后,真实环境(QQ音乐 5 会话)连续 5 轮
    `_resolve_qq_sessions` + `get_qq_volume` + `gc.collect()`,零 AV、零 "Exception ignored"、进程存活。
    **需重启托盘服务才生效。**
- **演唱↔BGM 联动整修:恢复炸响 / 手动暂停打断自动化 / 联动开关 / 2s 衔接缓冲**(2026-07-14,作者报四问题):
  - **① 恢复播放炸响(100%→跳回正常)根因**:QQ音乐 暂停会**销毁音频会话**,恢复后新会话 1~2s 才建好且
    **默认音量 100%**;`_play_fade_in_impl` 老写法按下播放后只 `sleep(0.2)` 解析一次,解析不到又被
    `_QQ_RESOLVE_INTERVAL=1.5s` 限频拦住,而整段渐强只有 1s → **一次音量都没设上**,新会话以 100% 出声,
    直到之后有人碰音量才跳回。**修**:按下播放后最多轮询 3s(每 0.25s 清缓存+解禁限频重解析,pycaw 专线程内
    串行安全),会话一出现**立刻压 0** 再渐强;渐强中途 set 失败即清缓存下步重解析;收尾兜底设一次目标值。
  - **② "手动暂停 BGM 后自动化失灵"根因**:媒体键 playpause **无方向**,而 `bgm_playing` 有两处会过期——
    渐变函数乐观改完后,smtc_helper 子进程可能又推来一帧**按键前**的旧快照把它翻回去;手机端联动旧代码又拿
    本地 `bgmPlaying` 决定发不发、`!s.bgmPlaying` 不满足时直接**弃恢复并清账**。方向一反,播放/暂停从此错位。
    **修(服务端)**:渐变开始就乐观翻转 `bgm_playing` 并广播;加 **SMTC 覆盖抑制窗** `_bgm_smtc_mute_until`
    (渐变后 3~7s 内丢弃快照里的 `bgm_playing`,窗外恢复 SMTC 权威);新增**有方向且幂等**的
    `{"cmd":"bgm","action":"play"/"pause"}`(已在目标状态则不动)。**修(App)**:联动全部改发有方向指令,
    恢复不再依赖可能过期的 `bgmPlaying`;手动 `bgmToggle` 只取消当次待办恢复+清"是我们暂停的"记账,
    下个演唱周期照常联动。
  - **③ BGM 悬浮面板加"演唱联动"开关**(`BgmFab.kt` 面板底部 Switch):`RemoteViewModel.bgmAutoFollow`
    (StateFlow,SharedPreferences `bgm_auto_follow` 持久,默认开);关闭即取消待办恢复+清记账,联动完全不介入
    (`prevSinging` 仍跟踪,重开不误触发)。
  - **④ 衔接留 2s**:停唱后不再立即恢复——`interlockBgm` 起 2s 延迟协程(`bgmResumeJob`)再发 `bgm play`;
    缓冲期内又开唱则取消恢复、BGM 保持暂停且记账保留(下次停唱照常恢复);手动操作也取消待办。
  - **验证**:headless mock 测试(模拟"新会话 1.2s 后才出现、默认 100%")——渐强首笔即压 0%(会话出现后
    0.05s 内)、单调升到目标 60%、零 100% 出声窗口;渐弱 57%→0% 后才按暂停;抑制窗内快照 `bgm_playing`
    被丢弃。`assembleDebug` BUILD SUCCESSFUL + adb 装机成功。**服务端需重启托盘服务才生效。**
- **恢复播放炸响二次返工:端点静音保险丝(终版)**(2026-07-14,作者报"起播瞬间还是炸,问能否不销毁仅暂停"):
  - **研究结论**:暂停/恢复时销毁并重建音频会话是 QQ音乐(MediaSDK_Server)内部行为,SMTC/媒体键层面
    **没有任何外部 API 能"仅暂停不销毁"**;且新会话"出现在枚举器"与"开始出声"几乎同时——凡是"轮询见到
    会话再压 0"的方案都天然慢半拍。上一版还有个隐藏坑:**暂停后枚举器里残留 Inactive 老会话**(真实环境
    实测:暂停态下枚举仍返回 1 个 QQ 会话、`GetState()==0`),旧轮询"见到会话就 break"会立刻咬住死会话
    压 0,真正的新会话后来 100% 起来根本没被碰到。
  - **终版方案(从两头堵死)**:
    1. **端点静音保险丝**:`_mute_qq_endpoints()` 在按播放**之前**把(见过 QQ 会话的)设备**端点**静音
       ——端点音量接口(`IAudioEndpointVolume`)是**设备级**的,不随会话销毁失效,可以提前操作;新会话
       纵然默认 100%,一个采样也放不出来。等新会话压 0 后 `_restore_qq_endpoints()` 恢复原 mute 状态
       (`finally` 保证,绝不把设备留在静音态)。端点静音会连带同设备其它声音(如伴奏),但联动恢复时演唱
       已停(2s 缓冲),静音窗仅"等新会话"那零点几秒,无感。
    2. **紧凑轮询只认 Active**:新增设备级缓存 `_qq_dev_mgrs`/`_qq_dev_eps`(全量解析时顺带刷新;
       SessionManager 也是设备级、跨会话销毁存活),`_resolve_qq_fast(active_only=True)` 只扫这几个设备
       (实测 **1ms**,全量枚举 ~0.28s)、只收 `GetState()==1` 的会话——Inactive/Expired 残留不算数。
       80ms 一拍轮询,出现即压 0;渐强中每 5 步补扫迟到会话(多路由设备上会话建得晚的补进缓存)。
  - **验证**:①headless 时序仿真(Inactive 残留 + 新会话 0.9s 后 100% 出现),硬性不变量"端点未静音时
    不得存在未接管(>95%)的 Active 会话"全程未触发——零炸响窗口;压 0、端点恢复、渐强到 60% 时序全对;
    老 Inactive 会话未被抬音量。②真实环境只读探针:全量解析 1 会话/1 设备/1 端点接口 OK(0.28s),
    快速路径 1ms,暂停态 `active_only` 正确返回 0(证实残留会话陷阱存在)。**需重启托盘服务生效。**
  - **伴奏保护门控(作者追问"端点静音会不会连伴奏一起关")**:伴奏与 BGM 共用 PLAYBACK 1/2——保险丝
    若在伴奏出声时上,确会把伴奏一起掐掉(联动流程不会:恢复 BGM 时演唱已停 2s、伴奏静默,静音无声通道
    无感;但**演唱中手动恢复 BGM** 会命中)。修:保险丝**只在 `STATE["k_playing"]=False` 时上**;伴奏
    在放时退化为纯快速轮询(≤80ms 接管,且有伴奏+人声垫底,瞬时小信号被盖住);等待期间 `k_playing`
    翻 true(用户突然开唱)→ **当拍立即撤保险丝**再继续轮询。**验证**:仿真两用例——A:`k_playing=True`
    全程零端点操作、会话出现后同拍被压 0、终值 60%;B:保险丝已上、0.4s 伴奏开播 → 0.403s 即撤
    (新会话 0.965s 才出现),不再复上,终值 60%。
- **重大误伤实锤:"暂停 BGM 通道就静音" = BGM 控制一直在掐直播伴侣的推流主麦**(2026-07-14,作者报
  "不管手机还是电脑点暂停 bgm,通道就静音"):
  - **排查取证(对运行中服务实测)**:①全端点扫描——`PLAYBACK 3/4`(监听)和 `VIRTUAL REC 3/4`(推流
    主麦采集)上各挂着一个 `MediaSDK_Server.exe` 的 **Active 会话,音量恰=BGM 设定值 60%**;②进程父链
    ——本机唯一的 MediaSDK_Server(pid 26088)**父进程是直播伴侣.exe**,与 QQ音乐 无关(QQMusic.exe
    独立挂在 explorer 下);③WS 动态复现——发 `bgm pause`,两个 MediaSDK 会话被渐弱到 **0%**,其中
    `VIRTUAL REC 3/4` 上**会话音量=端点音量**,端点直接归 0 → **直播间主麦静音**,BGM 暂停多久静音
    多久;发 `bgm play` 又回 60%。
  - **根因**:`config.QQMUSIC_PROCS` 含 `MediaSDK_Server.exe`(旧注释称"QQ音乐新版音频进程"),但它是
    **腾讯共用媒体组件,直播伴侣也起同名子进程**挂麦克风链路会话;按进程名裸匹配 → BGM 音量渐变/滑条
    全程操控推流主麦。**"VIRTUAL REC 3/4 音量莫名归零"悬案(2026-07,见 CLAUDE.md)同源破案。**
    QQ音乐 真正的渲染会话是 QQMusic.exe 自己,在 PLAYBACK 1/2 上。
  - **修复**:①`_is_qq_pid(pid)` **归属校验**——`QQMUSIC_OWNER_CHECK` 里的共用进程(MediaSDK_Server)
    必须**父进程链(向上4级)含 QQMusic.exe** 才算 QQ音乐;结果按 pid 缓存(TTL 60s,快速轮询高频调用);
    `_resolve_qq_sessions`/`_resolve_qq_fast`/`_qq_running` 全部改用。②**设备白名单**
    `QQMUSIC_DEVICE_HINT="PLAYBACK 1/2"`——优先只在 BGM 设备上找会话,一无所获才退全设备(归属校验
    仍兜底);设备缓存(保险丝会静音的端点)因此只含 PLAYBACK 1/2,推流链路设备永不被碰。③渐强等待循环
    补"中途全量重扫一次"(快速路径空转 >1s 时),应对新会话落在未缓存设备。
  - **验证**:真实进程 `_is_qq_pid`——QQMusic(23912)=True、直播伴侣的 MediaSDK(26088)=False;真实
    解析只命中 PLAYBACK 1/2 上 1 个 QQMusic 会话(设备/端点缓存各 1);三个时序仿真用例重跑全过。
    **现场已恢复**:两个 MediaSDK 会话 + VIRTUAL REC 3/4 端点已设回 100%,QQMusic 会话设回 60%。

## 十二、自动切镜开关改为"director 常驻·模式切换"(2026-07-18)
「自动切镜运镜」开关以前 = **启停 director 子进程**:关 = `terminate()` + `_obs_cut_main` + `_obs_zoom_main`(居中静态放大)。
问题:人脸跟踪/跟随全在 director 进程,进程一停就**没跟随**——关闭后的手动主镜是死板的居中放大,和"唱完待机跟脸"不一致。
- **改法(server.py)**:`set_director(on)` 现在**总是 `_start_director()`(幂等常驻)+ `_obs_cut_main()` 基线切场景 + 广播**,
  **不再 `_stop_director`/`_obs_zoom_main`**;开关只改 `STATE["director_on"]` 并随 `{type:state,...}` 广播下发。
  `cam_zoom` 处理只 clamp+存值+(必要时幂等 `_start_director`),**不再自己 `_obs_zoom_main`**——cam1 的变换/放大/跟随全交给常驻 director。
  `_stop_director` 仅留 atexit 防孤儿。
- **director 侧**:消费广播的 `director_on`/`cam_zoom`,新增 `manual_tick`——关闭自动切镜 = 锁 cam1 + 人脸跟随(同待机 follow 内核)
  + `z=cam_zoom/100`(夹上限、低通平滑滑块)。开=自动编排照旧。**单写者**(只有 director 写 cam1 变换),不打架。
- **App 无需改**(开关发 `director{on}`、滑块发 `cam_zoom{value 100~250}`、滑块 `enabled=!directorOn` 语义全吻合)。
  细节与参数(`manual` 配置组)见 `auto-director/GUIDE.md` §7 / "由 App/pc-service 开关"。**限制**:director 常驻后崩溃无 watchdog,重开开关即恢复。
    **需重启托盘服务生效。**

## 十三、移除自动切镜/运镜 + OBS 整条链路(2026-07-23)
**背景**:本地测试 OBS 尚可,但**真开播**后叠加评论/礼物透明悬浮窗 + QQ音乐,画面整体卡顿、音画不同步,
**无线主摄卡成 PPT**。根因是开播即启动 x264 编码 + 推流,叠加透明窗 DWM/GPU 合成 + WiFi 上行推流与无线主摄
收流互相争抢带宽、CPU 满载饿死摄像头解码线程。权衡后**放弃 OBS + 多机位 + 自动切镜/运镜,回归直播伴侣直接推流**。
- **删除**:整个 `auto-director/` 子项目(director 状态机/护栏、`obs_setup.py`、`wire_camera.py`、模拟器、GUIDE)。
- **pc-service**:去掉 `config.py` 的 `DIRECTOR_PATH/OBS_HOST/OBS_PORT/OBS_PASSWORD/MAIN_CAM_SCENE/MAIN_CAM_SOURCE`;
  `server.py` 去掉 STATE 的 `director_on`/`cam_zoom`、`director`/`cam_zoom` 两条 WS 指令、整段 OBS/director 托管
  (`_obs/_obs_cut_main/_obs_zoom_main/_start_director/_stop_director/set_director` + atexit + `obsws_python` 导入),
  以及仅供模拟器跨源的 `CORSMiddleware`。**依赖 `obsws-python` 随子项目删除不再需要。**
- **安卓 App**:遥控页移除「自动切镜运镜」开关 + 主镜放大滑块(`Models/RemoteViewModel/RemoteScreen/MainActivity`
  的 `directorOn`/`camZoom` 全链),已 `assembleDebug` 重打包 + adb 装机。
- **歌词不受影响**:K歌歌词本就走 `karaoke-player 绿幕窗 → 直播伴侣窗口捕获 + 绿幕抠图`,不经 OBS。
  回直播伴侣后在其中把播放器绿幕窗加成「窗口捕获」素材、设绿幕色键即可(原 `ktv-overlay` skill 的 OBS 版已随之删除)。
- **`chorus` 字段**:原供自动切镜状态机,现暂无消费方,`meta.json`/`karaoke_data` 仍保留字段不动。
    **需重启托盘服务生效。**

## 十四、BGM 手机控制"按了又弹回/反复点/唱着自动响"根治(2026-07-23)
**症状**(直播现场):手机点播放伴奏→按钮显示播放,~2s 后无反应又弹回暂停,反复点好几次才真播;暂停也常要点
多次;唱着唱着 BGM 自己响起来。**根因三层叠加**:
1. **服务端 SMTC 抑制窗设得太晚**(主因)。`_pycaw_exec` 是 `max_workers=1` **单线程**,渐变/音量泵/回读/
   `_reassert_bgm_vol`(自动切歌占 **1.8s**)全串在这一条线程。而防"winrt 快照冲掉乐观 `bgm_playing`"的抑制窗
   `_bgm_smtc_mute_until` 原本在 `_bgm_fade_impl`(executor 线程)里才设——线程被占时渐变排队延迟,这段延迟内
   winrt 每 0.35~1s 推来的**真实** `bgm_playing`(此刻 QQ 还没起播=False)就把刚乐观翻转的 STATE 冲回去并广播。
   **修**:抑制窗改到 `_bgm_apply`(事件循环线程)**同步设好**,早于把渐变丢进 executor;`max(...)` 不缩短、
   渐变真开跑时 `_bgm_fade_impl` 再按实际起点续设。
2. **App 端 `bgmPlaying` 无乐观锁**。`playing`(K歌)有 `playLockUntil`,`bgmPlaying` 却直接采信每次回推 →
   任何过渡期的错误广播立即翻按钮。**修**:加 `bgmPlayingLockUntil`(`BGM_LOCK_MS=2500`),发指令后短暂以本地为准。
3. **手动按钮走无方向 `playpause` + 联动恢复擦肩**。`playpause` 方向依赖可能已被冲错的状态→再点就反着来;
   联动"停唱 2s 后恢复"的协程与"重新开唱"擦肩时 cancel 拦不住已发出的 `play`→BGM 盖过演唱。**修**:
   `bgmToggle` 改发**有方向** `play`/`pause`(方向由本地意图定);联动开唱**无条件**发幂等 `pause`(盖过在途恢复)、
   恢复前**二次确认没重新开唱**再发 `play`。
- **改动**:`server.py` `_bgm_apply`(同步设窗);`RemoteViewModel.kt` `bgmToggle`/`interlockBgm`/回推 reconcile +
  新增 `bgmPlayingLockUntil`。已重启 pc-service + `assembleDebug` 装机。
- **压测验证**(`scratchpad/bgm_*.py`,QQ音乐在跑真链路):急躁连点最终态跟手、间隔发 0 flap、**混乱场景**
  (连续拖音量 + 穿插切歌占 pycaw 线程 + play/pause)最终态跟手、**0 长时间错误态**;尤其"play 落在 next 切歌
  (占线程)期间"状态稳稳保持不翻——正是同步抑制窗生效点。
    **需重启托盘服务生效。**

## 十五、点歌入队致"正在唱的歌卡顿一下"根治(2026-07-23)
**症状**:手机点新歌入队时,**正在演唱的歌**卡顿一下。**根因(跨进程 GIL 饿死)**:入队 `k_enqueue`→
`library.bump_play`(点歌次数+1)→ `_on_lib_change` 回调 → **`_push_setlist()` 无条件重推顶端滚动歌单**给播放器。
但入队并没改歌单内容(只是 `plays` 变了触发了曲库回调),播放器 `set_setlist` 却每次都 `_setlist_pix=None`
**强制重建滚动 pixmap**(QFontMetrics/QPainter 字体渲染,GUI 线程重活)。播放器是单进程:GUI 线程重建时
**持有 GIL** → 饿死 WSOLA 音频**生产者线程** → 音频队列排空 → 回调 `outdata[take:]=0` 输出静音(blocksize
缓冲仅 ~160ms,GUI 停顿超过就断音)→ **正在播的歌卡顿**。
- **修(双保险)**:
  ① **播放器 `audio_engine`/`player.py`**:`set_setlist(titles)` **内容不变直接 return**,不重置 `_setlist_pix`
     → 不触发重建。这是根治点(挡住一切冗余重推)。
  ② **服务端 `server.py`**:`_push_setlist()` 加 `_last_setlist_pushed` 去重缓存,内容没变不发 IPC;
     `start_player` 里改 `_push_setlist(force=True)` 强推,确保播放器(重)拉起时必收到完整歌单。
- **验证**:`scratchpad/enqueue_smoke.py` 走 enqueue→bump_play→_on_lib_change→_push_setlist 全路径(不触发真实
  播放、不进直播链路):连续入队 3 首队列正确增长、`k_playing=False` 无音频、清空还原、pc-service+播放器均存活不崩。
    **需重启托盘服务生效(播放器由服务托管,重启服务即换新播放器代码)。**

## 十六、SMTC winrt 子进程线程爆炸致"全系统 UI 卡顿"根治(2026-07-23)
**症状**:电脑有时整体很卡——桌面右键菜单要等一会才出、切换应用卡、各种 UI 变慢,但**CPU/内存看着都充足**;
**停掉 pc-service 就恢复**。**排查**:用 `GetGuiResources` 逐进程测 GDI/USER/句柄/线程(`scratchpad/gdi_probe.ps1`):
先排除 `refresh_tray`——狂发 300 次入队触发 `update_menu`,pc-service GDI/USER/句柄纹丝不动(147/59/846),托盘菜单不漏。
真凶在 **winrt/smtc 子进程:线程数 420~472 剧烈 churn**(反复创建/销毁)、句柄 ~1250。**根因**:`smtc_helper.py`
**每 0.35s 一次 `asyncio.run()`**(新建/销毁事件循环)+ **每帧 `MediaManager.request_async()`**(重新枚举整个 SMTC
基础设施),winrt 异步完成回调在 Windows 线程池起线程,30 分钟跑了 ~5000 次 → 线程池膨胀到数百线程反复 churn →
**拖垮内核线程管理器/调度器 → 全桌面 UI 卡**(线程创建是内核开销+上下文切换,用户态 CPU% 却不高)。停 pc-service
=带走子进程=thrash 停=恢复,与症状完全吻合。
- **修**:①`smtc_helper.py` 改用**单个常驻事件循环**(`loop=new_event_loop()` + `loop.run_until_complete`,
  **绝不每帧 asyncio.run**);②**缓存 MediaManager 复用**(`smtc.get_manager()` 请求一次,`snapshot(mgr)`/
  `control(mgr,action)` 收缓存 mgr,不再每帧 request_async;失败置 None 下轮重取)。
- **验证(修复后 90s 趋势)**:winrt 线程 **450→10~14 稳定**(↓97%)、句柄 ~1250→~200 稳定、USER 32~65→2~5;
  且 SMTC 功能完好:`bgm_title/bgm_playing` 正确、`bgm_pos` 实时推进。**这条是整机卡顿的真凶,前述都只是局部。**
- 附:全局定时器分辨率被压到 1ms,是播放器音频(PortAudio 低延迟)正常需求,非本问题,保持不动。
    **需重启托盘服务生效。**
## 十七、手机版全民K歌接入 + 曲库导入改「扫描窗口」(2026-07-26)

**背景**:PC 版 WeSing 对用户自传/部分歌**下不了伴奏**;手机版能下。破解手机资源并接入,补齐曲库来源。

**手机资源(`/sdcard/Android/data/com.tencent.karaoke/files/`,adb 可读、模拟器免 root)**:
- `qrc/<songmid>_original.qrc` 逐字歌词、`note/<songmid>.oke` 音高——与 PC 版**同一条 hex→3DES(QRC_KEY)→zlib**
  解密链(`assets._qrc_decrypt` 直接吃;`assets.load_notes` 已能识别 `.oke`/hex 自动解密)。
- `obbligato/<filemid>.tkm` 伴奏/原唱——**QQ音乐 QMCv1 静态密钥加密的 M4A**。破解:`mask128[i]=KEY256[(i²+27)&0xff]`,
  keystream=`mask128*256`(前 32768B)再按 0x7FFF 环绕(`startblk=firstblk+firstblk[1:-1]`=65534B、
  `commonblk=firstblk[:-1]`=32767B 循环),明文=密文 XOR keystream → 标准 M4A,ffmpeg(imageio-ffmpeg)解码。
  **KEY256 与 PC 伴奏 PCM 的 XOR 静态密钥(`wesing_pcm_key.PCM_XOR_KEY`)是同一张 256 字节表**,只是用法不同
  (PC=直接 256 周期 XOR;手机=二次索引 mask128)。即 unlock-music/libtakiyasha 支持的 `.tkm`,查开源即得,不必自研。
  > 密文分析时"周期128、非简单XOR"的假象正是 `(i²+27)%256` 二次索引所致;头 `c3 4a d6 ca 90 67 f7 52`=mask128 头,
  > 与 m4a `ftyp` crib 反推逐字节吻合。曾一度想上 frida 动态 dump,查开源后发现是已知格式,直接静态解。

**新增/改动**:
- `karaoke-player/mobile_convert.py`(子进程 CLI):手机三件套 → PC 四件套。**伴奏/原唱自动判**——按 `.note` 音符
  时间轴算两条 tkm 的"中置声道能量(音符段/间奏段)比",**比值小的=伴奏**(消了中置人声;实测伴奏 0.49 vs 原唱 1.0)。
- `live-remote/pc-service/mobile_import.py`:`list_devices`(adb devices)、`scan_phone`(adb `stat` 列 mtime →
  **song↔tkm mtime 就近配对**:每条 tkm 归属 mtime 最近的 qrc 所属歌;只缓存歌词没下伴奏的歌自动跳过,**不误抢邻曲 tkm**
  → adb pull → 调 mobile_convert 子进程 → 候选)。
- `library.py` **去后台轮询**:`scan_pc()` 列 PC 新歌候选、`import_candidate(cand,title,artist,swap)` 只入勾选项
  (源目录由候选定、用户编辑歌名/歌手覆盖并置 `named=True`、`swap` 对调伴奏/原唱)。`start()` 只载清单 + 跑一次 `_remigrate`。
- `server.py` **托盘「扫描导入歌曲」→ `_open_scan_window()`**(自带线程 + 自建 Tk 根,仿 `_open_library_browser`):
  adb 设备下拉(默认第一台)+ 连接状态;打开即双端扫描,worker 线程跑、`root.after` 轮询刷 `ttk.Progressbar` loading;
  **边扫边显示**——`scan_phone(on_candidate=...)` 每转好一首回调即入 `st["results"]`,`_poll`/`_render_new` 每 150ms
  只追加新行(`range(len(rows), len(results))`),不等全部转完;行是**多选可编辑表格**(☑ + 歌名/原唱 Entry + 来源 +
  手机源「伴奏⇄原唱」交换按钮);确认(扫完启用)只入勾选、`_on_lib_change` 刷。
- `config.py` 加 `ADB_PATH`/`MOBILE_FILES`/`MOBILE_STAGING_DIR`/`MOBILE_CONVERT_PATH`/`MOBILE_TKM_WINDOW`。
- **每行「▶ 试听」预览**(2026-07-26 追加):`server._preview(cand)` 子进程拉起
  `karaoke-player/preview_play.py <src_root> <mid>`——sounddevice **系统默认输出**(自己听的通道,不进直播链路)
  播伴奏(音量 `config.PREVIEW_VOLUME=0.4` 压低)+ Tk 纯文本歌词窗(当前行高亮、`←/→` 步退进 5s、`Esc` 退出);
  单实例(再点/换歌先 terminate 旧的)。因音频已在扫描时转成四件套,试听只是播已存在文件、不重转。
  **伴奏/原唱自动判别可靠(note 中置能量比),已去掉交换按钮**(结构固定,确认一次即准;万一判错用试听即可发现)。
- **"列表顶部滚轮上滚露白"根治**(Canvas 自定义滚动 bug):当**内容比视口短**(行少,常见)时,滚轮上滚
  `canvas.yview_scroll(-1,"units")` 会把短内容**往下推**、顶部露出一截空白,且 `yview` 报 `(0,1)` 不回夹
  (实测 `canvasy(0)` 被推到 -150)。内容比视口高时正常夹在 0。**修**:滚轮处理器加判——
  `inner.winfo_height() <= canvas.winfo_height()` 就 `yview_moveto(0)` 锁顶不滚,超出才滚。
  **扫描窗 + 曲库管理窗同一模式,两处都修**。
- **顺带小改进**:空标题(`needs_name`,KTV/自传版 `[ti:]` 清洗后为空)歌名框填灰红占位「（待命名,点此输入)」
  (`_TITLE_PH`,聚焦即清;`_row_title` 导入时把没动过的占位视为空,仍走 needs_name 可后续改名);
  `_qrc_meta` 把字面量歌手 `None`/`null` 也清成空。
- **无线 ADB 扫码连接**(扫描窗口「📶 扫码连接」):`mobile_import.make_pair_payload()` 生成
  `WIFI:T:ADB;S:studio-<hex>;P:<6位码>;;`,`server._open_pair_dialog` 用 `qrcode` 渲染二维码;手机『无线调试→
  用二维码配对设备』扫码后广播 `_adb-tls-pairing._tcp`,`wait_and_pair()` 轮询 `adb mdns services` 发现→`adb pair`
  →找 `_adb-tls-connect`→`adb connect`,成功刷新设备下拉选中新机并重扫。依赖 `qrcode`(pillow 已有)。

**验证**:`mobile_convert` 转「不由自主」→ `assets.Song` 断言 29 行/228 音符/伴奏 292.1s、accompany 中置能量比 0.494(=伴奏);
`scan_pc`+`import_candidate`(改名/去重/swap)隔离测通;`mobile_import.scan_phone` 真机(emulator-5554)跑通,家乡(只缓存歌词)
正确跳过;扫描窗口构建/渲染/poll 无异常。**改动需重启 pc-service 生效。**

## 十八、QQ音乐 导入(补"特殊版只在 QQ音乐 有"的歌;2026-07-26)

> ⚠️ **最终方案(2026-07-28,见本节末"弃用 QQ ACCOM stem"条):伴奏一律 Demucs 从原唱分离,不再取 ACCOM。**
> 本节前半段(下方 07-26 记录)描述的"取 `SpecialSongFileType.ACCOM` 当伴奏"已废弃——ACCOM 对 Live/特殊版
> 常返回另一版原唱、仍带人声。阅读时以节末 07-28 决策为准。

**目标**:个别特殊版/DJ版/KTV版只在 QQ音乐 有,要能扒**逐字歌词 + 伴奏(或原唱后续自分离)**进曲库。

**调研过程(踩坑记录,免得后人重走)**:
1. **歌词——离线可解**。PC 版歌词缓存在 `D:\QQMusicCache\QQMusicLyricNew\*_qm.qrc`,是二进制(非手机版 hex)。
   本地封装比手机多一层:**`qmc1_decrypt(整文件 XOR 128字节 PRIVKEY) → 跳过前 11 字节 → buggy-3DES(同 QRC_KEY
   `!@#)(*$%123ZXC!@!@#)(NHL`) → zlib`**。算法出自 chenmozhijin/**LDDC**(`core/decryptor/qmc1.py`+`__init__.py`),
   而项目 `karaoke-player/tripledes.py` 正是同作者(`cmzj@cmzj.org`)的 buggy DES(sbox4 里著名的 `10,10` 重复 bug)。
   实测解通多首(TFBOYS/赵雷KTV版等),逐字时间轴与 `assets.load_lyrics` 完全兼容。
2. **音频——绕开了 QMCv2/ekey 这条深坑**。缓存音频 `downloadproxyNew\tp2p\...\*.mgg` 是 **QMCv2**(试 QMCv1 静态密钥解不出
   OggS),且 **ekey 不本地持久化**(文件尾无 STag/musicex footer;`qmlist64.db` 被 QQ音乐 自己加密打不开;16.6 版无
   freenote 那个 `MMKVStreamEncryptId` 明文 vault)。一度走"登录态 API 取 ekey(`music.vkey.GetEVkey`)+ `ybpyqmc` 解密",
   但拿到的是 V1 型 ekey(704 base64→528 字节、开头 ASCII `gyV21gG4`),ybpyqmc 0.1.0 解出乱码。**转机**:改试**非加密音质**
   发现——**本账号有会员权限时 QQ音乐 直接返回明文文件,根本不需要 ekey/解密**:
   - `SpecialSongFileType.ACCOM` → **明文 OggS 伴奏 stem**(真·卡拉OK伴奏,269.7s 整首、与原唱采样数完全一致对齐)
   - `SongFileType.FLAC`(退 MP3_320/128)→ 明文原唱;`SongFileType.*` 走 `GetVkey` 返回 `ID3`/`fLaC`/`ftyp` 明文
   - `lyric.get_lyric(mid, qrc=True)` → 库内部已解密的**明文 QRC XML**(逐字),直接可用

**实现**:
- **登录态 API**:`qqmusic-api-python`(0.7.0,活跃维护;`Client().song/search/login/lyric`,异步)。扫码登录
  (`login.get_qrcode(QRLoginType.QQ/WX/MOBILE)`+`check_qrcode` 轮询)存 `KaraokeLibrary/qq_cred.json`,复用/过期重扫。
- **`pc-service/qqmusic_import.py`**:`login_qr()`/`search()`/`prepare()`。搜索轻量(仅元数据、去重排除库里已有);
  **确认入库时才** `prepare()`:取 ACCOM 伴奏 url + 原唱 url(按 `config.QQ_ORIGINAL_QUALITY` 优先级)+ 歌词明文,
  niquests 下载 → imageio-ffmpeg 转 44.1k/16bit 立体声 PCM → 与四件套一致的 XOR 加密写盘。有 ACCOM:伴奏=ACCOM、
  原唱=FLAC(等长对齐);无 ACCOM:两轨都用原唱(主播再自分离)。**QQ音乐 无音高数据 → 写空 `.note`**
  (`load_notes` 返回空→播放器不显音准线);歌词写明文 QRC XML。产出交 `library.import_candidate` 入库,下游零改动。
- **明文 QRC 分支**:`karaoke-player/assets._qrc_decrypt` 和 `pc-service/library._qrc_meta` 各加一支——识别
  `<?xml`/`<QrcInfos`/`LyricContent=` 开头即当已解密明文直接返回(不再走 hex/PC头 解密)。
- **扫描窗改双页签**(`_open_scan_window`):`ttk.Notebook`——「K歌(带音准)」=原 PC+手机缓存扫描(控件全 reparent 进
  tab 帧);「QQ(无音准)」=登录状态/登录类型下拉/登录·退出/搜索框 + 结果表(勾选/改歌名歌手/时长/QQ标)+ 确认入库。
  各自 worker 线程 + `root.after` 轮询刷 UI(仿现有 `_do_scan`/`_poll`);二维码用 `tk.PhotoImage(data=base64)` 内嵌显示
  (失败退回 `os.startfile` 打开临时 png);两页签滚动区改 `<Enter>/<Leave>` 时才 `bind_all` 滚轮,避免互抢。

**验证**:登录(musicid=1165141611 扫码成功)→ 搜"周杰伦 晴天" 3 候选 → `prepare` 下 ACCOM+FLAC → 四件套
(伴奏/原唱各 47.5MB 等长、空 .note、9.8KB 明文歌词)→ `assets.Song` 加载出 **63 行逐字歌词/0 音符/269.7s/原唱伴奏等长对齐**;
`server._open_scan_window()` 冒烟:import + 双页签构建 + 6s 轮询无异常。**加密 QMCv2 路(ybpyqmc)本账号用不上,留作
无明文音质时的备用,当前未接。改动需重启 pc-service 生效。**

**首轮联调修正(2026-07-26)**:①**打开扫描窗不再自动扫描**(K歌页签去掉 open 时的 `_start_scan()`,改状态提示;
点『重新扫描』/切设备才扫)。②**QQ 结果加 ▶试听**:`_qpreview()` worker 里 `prepare(cand, preview=True)`——
**只下伴奏一轨**(优先 ACCOM 退原唱、两轨共用,省流量)写暂存,再复用 `_preview` 子进程播放;之后勾选入库会再下全量覆盖。
③**列表默认全不选**(K歌 `_add_row`/QQ `_qadd_row` 的 `chk` 改 `value=False`)。④**修 QQ「确认入库」点了没反应**——
根因是 `qconfirm_btn` 建了、`_qconfirm` 写了,但**漏了 `qconfirm_btn.config(command=_qconfirm)`** 没接上,补上即好。
⑤**同名不同版分不清**(搜"船长"出两个"船长 赵雷"):候选标题改用 **`title`**(含"(Live)/(DJ阿树版)"版本后缀)兜底 `name`
(`name`/`subtitle` 都不带后缀,后缀只在 `title`)。

**下载慢的真因 + 提速(2026-07-26 补)**:试听/入库慢**不是代理**——实测 `getproxies()` 空、代码里 `Session.trust_env=False`
关代理后速度**一模一样**(238 vs 239 KB/s),换 QQ音乐 UA / 去 `redirect=1` / 直连重定向目标也都 ~220KB/s。是 **QQ CDN 对
vkey 授权流按连接限速**(单连接~210KB/s≈5倍实时码率,"够流畅播、防批量下")。**关键**:限速**按连接**——实测 3 连接并行
达 634KB/s(线性 3 倍)。据此把 `_download` 改**并行分块**(`_DL_CONNS=5`,Range 0-0 探总长→分块并发→拼接;拿不到长度/
量<512KB 退单连接)。再叠两招:试听只取开头 `_PREVIEW_MAX_BYTES=0.9MB`;**原唱默认音质降 FLAC→MP3_320**
(`config.QQ_ORIGINAL_QUALITY`,~55MB→~10MB;原唱只作切换参考、MP3_320 够;想无损把 FLAC 提前)。伴奏仍 ACCOM ogg(~23MB)。
**实测:试听 11s→3.7s、入库(晴天)79s→36s**(伴奏 24s+原唱 10s,均 269.7s 等长)。伴奏 24s 是 23MB@~1MB/s 的地板。

**进度条 + 人声分离伴奏(2026-07-26 再补)**:
- **下载进度条**:`_download` 加 `on_progress(done,total)`(并行各块增量经锁汇总);`prepare` 加 `pct_cb(label,pct)`
  串到 QQ 页签——`qst["dl_pct"]`(None=转圈 / 0-100=百分比),`_qpoll` 据此把 `qprog` 在 indeterminate↔determinate 间切,
  状态文字同显 "下载伴奏 45%"。登录/搜索/写盘等无百分比阶段仍转圈。
- **无 ACCOM 的歌用 Demucs 分离出伴奏**:`SpecialSongFileType.ACCOM` 只有部分歌有(特殊版/Live/DJ 版常没有),没有时
  原方案两轨都放原唱=听不到伴奏。现 `prepare` 无 ACCOM 时,若 `separation_available()`(装了 demucs)→
  `separate_accompaniment(原唱pcm)`:**子进程**跑 `python -m demucs --two-stems vocals -n htdemucs`(torch 不常驻直播服务,
  有 CUDA 自动用 GPU),取 `no_vocals.wav` 当伴奏。没装 demucs 则降级两轨都用原唱(不报错)。安装见 requirements.txt
  可选段(CUDA torch + demucs)。原唱音质入库默认 MP3_320(见上),分离基于它。
  **实测(2026-07-26)**:装 `torch 2.6.0+cu124 / demucs 4.1.0`(Python313),`separate_accompaniment` 端到端跑通
  (56s 片段 CPU 50s、中置人声能量降到原唱 66%、输出等长)。**本机 NVIDIA 驱动过旧**(`cuda.is_available()=False`,报
  "driver too old, found version 11060"=只到 CUDA 11.6,cu124 需 12.4),故**当前跑 CPU**(整首约 3-4 分钟);
  **要 GPU(一首 15-30s)需更新显卡驱动**到支持 CUDA 12.x,之后 demucs 自动用 GPU、无需改代码。分离阶段 UI 走转圈。
- **K歌页签扫描来源二选一(2026-07-26)**:原 `_do_scan` 一次扫 PC+手机两端,数据会互相掺。改顶部「扫描来源」单选
  `电脑缓存 / 手机全民K歌`,`_do_scan(gen, serial, mode)` **只扫选中那端**;`_on_mode()` 切换时启用/禁用设备行(手机模式才启用
  设备下拉+扫码连接)+ 刷提示;换设备只在手机模式自动重扫;扫码配对成功自动切手机模式。默认电脑模式、打开不自动扫描。
- **曲库管理加「删除」(2026-07-26)**:每行末尾红色「删除」按钮(col7,窗宽 640→720)。点击 `messagebox.askyesno`
  **二次确认**(不可恢复)→ `_lib_delete(mid)`:先 `set_setlist_member(mid,False)` 出歌单、从 `_queue` 摘除并 `_sync_queue_state`,
  再 `library.delete(mid)`(删 `KaraokeLibrary/<mid>/` 整个四件套+meta + 清 `_MANIFEST`/library.json + `_on_lib_change` 刷托盘/推库)。
  正在唱的歌已载进播放器内存,删文件不影响当前这遍。`library.delete` 用假 mid 隔离测过(目录/清单清掉、真库数不变)。
- **曲库窗打开崩溃闪退根治(2026-07-26)**:Windows 事件日志实锤——`python.exe` 崩在 **`tcl86t.dll` 异常码
  `0x80000003`(Tcl_Panic)**,今天崩多次。根因是**多线程 tkinter**:曲库/扫描/改名窗各在自己后台线程跑独立 Tk 根,
  而 Python 循环 GC 会在**任意线程**触发,一旦在非 Tk 线程 finalize 窗口里的 tk 对象(曲库窗建 100+ `BooleanVar`、
  QQ 页签的 `PhotoImage`)就从错误线程调 Tcl → 硬崩溃(故单开曲库窗不崩、一进多线程服务里必崩,日志里那串
  "Variable.__del__ main thread is not in main loop" 只是被守卫忽略的表征)。**修法(threaded-tkinter 通用)**:
  新增 `_tk_gc_enter/exit`(引用计数)+ `_tk_window_thread(fn)` 包住所有 Tk 窗口线程——**任一窗口开着就 `gc.disable()`**、
  全关了才 `gc.enable()`;窗口线程退出时先在**本 Tk 线程**上 `gc.collect()` 就地回收本窗 tk 循环(其 __del__ 在自己线程
  有 _tkinter 守卫、不崩),不留给后台线程回收。实测:开窗后 `gc.isenabled()=False`、反复开关无异常。
- **人声分离加进度条(2026-07-26)**:`separate_accompaniment` 改 `Popen` 实时读 demucs stderr,正则解析 tqdm 的
  `NN%`(`read1` 流式、按 `\r/\n` 切段)→ `pct_cb('分离伴奏', pct)`,串进 `prepare` 的 `pct_cb` → QQ 页签同一条进度条
  (`qst["dl_pct"]`)。实测分离 0→100% 逐档上报(模型已缓存后 56s 片段 CPU 仅 20s)。
- **托盘窗口改勾选式单实例开关 + Studio One 显隐进托盘(2026-07-26)**:曲库/扫描导入托盘项原是"每点开一个新窗"
  (能重复开、无勾选)。改成像「K歌歌词」的**勾选式开关**:模块级 `_lib_root`/`_scan_root` 记窗口 Tk 根(`_win` 建根时
  set+`refresh_tray`、finally 清+`refresh_tray`);`_toggle_library_window`/`_toggle_scan_window`——未开→开、已开→
  `root.after(0, root.destroy)` 关窗(单实例,不重复开);`checked=lambda i: _lib_root is not None`。新增托盘 **`Studio One 显示`**
  勾选项(`checked=studio_visible`)。**Studio One 显隐托盘↔App 双向同步**:抽出 `_toggle_studio_visible`(show/hide+存盘+
  `refresh_tray`+`_threadsafe_broadcast`),托盘项与 App 的 `studio_toggle` 命令共用它——App 切时刷托盘勾选、托盘切时推 App。
  隔离测:曲库/扫描 toggle 开设关清、Studio toggle True↔False 不崩、pystray 动态文本+勾选项可构建。
- **扫描列表显示已入库(置灰禁选)(2026-07-26)**:原三处扫描(`library.scan_pc`/`mobile_import.scan_phone`/
  `qqmusic_import.search`)都**跳过**已入库的歌。改成**不跳过、只标 `in_library=True`**返回(手机侧已入库的**不再 adb 拉取/
  ffmpeg 转换**,直接用 `library.manifest()` 里的标题列出,省时;PC/QQ 侧同理用库里标题)。UI(`_add_row`/`_qadd_row`)对
  `in_library` 行:勾选框 `state=disabled`、歌名/歌手 Entry `state=disabled`+`disabledforeground` 置灰、来源列改标「已入库」、
  去掉试听按钮;全选/全不选与确认入库的 `picked` 都 `not r.get("in_library")` 跳过。状态栏显示"共 N 首(灰色=已入库,M 首新歌)"。
  实测 `scan_pc` 对 WeSing 缓存返回 3 已入库+2 新歌(原只返 2 新歌),tk 的 `disabled/disabledforeground` 选项合法、窗口渲染无异常。
- **手机扫描"列表先出、转码后置"(2026-07-26)**:原 `scan_phone` 扫描时就对每首新歌 `_pull` 几MB tkm + `mobile_convert.py`
  解密转码(tkm QMCv1→m4a→ffmpeg 解整首→写~100MB PCM,几秒/首),导致"只加载列表也要等很久"。**歌名只在小 qrc 里**,不必
  先转音频。改:`scan_phone` **列表阶段只 `_pull` qrc + `library._qrc_meta` 解出歌名/歌手**(~0.1s/首,增量回调显示),候选带
  `needs_convert=True` + `phone`{serial,qname,oke,tkms};**重活拆到新函数 `convert_phone_song(cand)`**,在**确认入库 `_confirm._work`**
  与**试听 `_preview_kge`** 里才调(转过则 `accompany.pcm` 存在即复用,试听后入库不重转)。UI 进度用 `st["op_msg"]`(_poll 空闲态显示)。
  **实测(真机 192.168.1.2):上千 qrc 缓存里 110 首候选(92 已入库+18 新歌)4.5s 全出**(原需逐首转码几十秒~几分钟);
  单首全新转换 2.2s、四件套伴奏 291.7s 原唱等长;二次调用复用 0.00s。与 QQ 页签"搜索即时、入库才下载"同一套延迟思路。
- **K歌页签加检索框 + 缓存时间倒序(2026-07-26)**:候选统一加 `mtime`(`scan_pc`=缓存目录 `getmtime`;`scan_phone`=qrc 的
  stat `%Y`)。扫描时仍增量显示(扫描顺序),**扫完 `_resort_render` 按 `mtime` 倒序重排重渲**(最近缓存在最上)。顶部新增「检索」
  框(`search_var` + `trace_add` + 200ms 防抖 `root.after`):`_apply_filter` 按歌名/歌手子串**隐藏/重排可见行(pack_forget→依 rows
  顺序重 pack)**,不重建行→**勾选/改名不丢**;`_set_all` 只对可见行生效。行 dict 存 `rf` 供显隐。实测 scan_pc/scan_phone 候选均带
  mtime、倒序正确、窗口构建无异常。(QQ 页签本就是在线搜索、另有搜索框,不受影响。)
- **曲库窗与扫描窗同时开→后开的被干扰 根治(2026-07-26)**:现象——两窗同开时,**后开**那个的下拉/单选失灵(扫描后开:点
  手机模式没反应、adb 下拉一直禁用;曲库后开:筛选/排序/搜索下拉空白)。**根因(实证)**:两窗各在自己线程建独立 `tk.Tk()` 根,
  而 **`tk.StringVar()`/`tk.BooleanVar()`/`tk.PhotoImage()` 不传 `master` 时绑到全局 `tkinter._default_root`——即"第一个打开的
  窗口"的 Tcl 解释器**(第2个 `Tk()` 不会改 `_default_root`)。于是后开窗口里的变量全绑到先开窗口的解释器 → 后开窗口的
  Radiobutton 改不动自己的 `scan_mode`(点手机模式后 var 仍 'pc' → `_on_mode` keep 禁用)、Combobox 的 `textvariable` 跨解释器
  → 显示空白。实证:`tk.StringVar()` 绑 `r1.tk`、`tk.StringVar(master=r2)` 才绑 `r2.tk`;旧写法点单选 var 卡 'pc',master=r2 后
  正常变 'phone'。**修复**:给两窗全部 **12 处 `tk.StringVar/BooleanVar/PhotoImage` + 配对二维码的 `ImageTk.PhotoImage` 共 13 处**
  创建点显式传 `master=<本窗根/Toplevel>`(绑各自窗口解释器,不依赖全局默认根)。双窗口(曲库先/扫描后)共存冒烟无异常。
  **教训:多 Tk 根(多窗口各自线程)时,所有 tk 变量/图片(含 PIL ImageTk)必须显式 `master=`。** 顺带:K歌页签检索框缩短
  (width=16)移到「重新扫描」左侧同一行,不再独占一行。
- **`_save_persist` 加"未恢复不写盘"守卫(2026-07-26)**:事故——开发期某测试 `import server` 后在**默认空 STATE** 上调到
  `_save_persist`(经 `_toggle_studio_visible`/`set_setlist_member`),把用户真实 `state_cache.json` 覆盖成默认值(**setlist 被清空**,
  致播放器顶端滚动歌单不显示)。根因:`_save_persist` 无条件写全局缓存路径,任何 import 本模块的进程都可能误伤。**修复**:加
  `_persist_ready` 标志,`_restore_persist` 里置 True,`_save_persist` 未 ready 直接 return——生产 `main()` 必先 `_restore_persist`
  故不受影响,而测试/早期误调不再写盘。隔离测(临时路径):未 ready 不写、ready 后正常写。歌单推送链本身完好(`song_meta` 解析
  歌名、`set_setlist_member` 加歌均正常);被清空的 setlist mid 无备份不可恢复,需在曲库管理重新勾选(master= 修复后勾选已正常)。
- **剥掉 QQ 歌词开头的信息行 → 统一有开头标题卡(2026-07-28)**:现象——QQ音乐 导入的歌**开头不出居中大字标题卡**,且
  **歌名/原唱被当歌词唱出来**。根因:`lyric.get_lyric(qrc=True)` 返回的明文 QRC,其 `LyricContent` 开头有几条**带时间戳的
  信息行**(QQ 约定):`[0,dur]歌名 (Live版) - 歌手`(总在第一条)+ `[t,dur]词:xxx` `曲:xxx` `编曲:xxx` 等署名。这些行
  `[数字,数字]…` 会被 `assets.load_lyrics` 当真歌词解析出来 → ①当字幕唱出;②首句起点被顶到 0ms,而 `_compute_title_dur`
  据首句起点定时长(`min(TITLE_MAX, first-600)`,`<600ms` 判 0=不显)→ 标题卡被判为 0 不出现。全民K歌歌无此信息行
  (首句十几秒起、纯歌词),故只 QQ 源有此问题。**修复**(`qqmusic_import._strip_qq_meta`):只在 QRC **开头连续区**剥掉
  信息行——歌名行(第一条时间行,靠 ' - ' 或含歌手名识别)+ 署名行(`_QQ_CREDIT_RE`:词/曲/编曲/制作/混音/…),命中
  第一条真歌词即停(绝不误删中段恰含 ' - '/署名词的歌词);`[ti:]/[ar:]/[offset:]` 等非时间行原样保留。`prepare()` 写盘前
  过一遍;历史入库的用 `clean_library_lyrics()`(判据 `.note` 空 + 明文 QRC,全民K歌不碰)修——`python qqmusic_import.py
  --clean-lyrics`。**实测**:修库中 2 首(赵雷《小行迹/船长》Live)→ 首句从 0ms 归位到 38.9s/32.1s(`title_dur=5500` 满显)、
  歌词首行变真歌词;重跑 0 改动(幂等);逐字时间轴/头部 `[ti:]` 均完好。**教训:QQ QRC 头部的带时间戳信息行必须剥,否则
  既污染字幕又害标题卡不显。**
- **补:署名行不止在开头,尾奏也一大串(2026-07-28)**:回看发现两首歌**唱完后**又刷出一整块制作人员署名
  (`音乐总监/指挥:陈伟伦` `吉他:磊子/董长跃/Kris` `贝斯/鼓/打击乐/键盘/小号/长笛/和声/管弦乐/编曲/混音/音乐工程:…`)。
  原 `_strip_qq_meta` 只剥"开头连续区"(命中第一条真歌词即停),尾奏这堆漏了。**改判据**:弃用开头专用的 `_QQ_CREDIT_RE`
  词表,换成**通用 `_looks_like_credit(text)`**——判"角色:人名"式带冒号短句(冒号左 1-15 字职务名/无句读,冒号右 ≤24 字
  人名可 `/、,&` 分隔,整行无句末标点),**任意位置命中即删**;歌名行仍只对第一条时间行判。真歌词几乎不含冒号 + 结构约束
  兜底,基本不误删。**实测**:小行迹 39→27 行、船长 72→59 行,末尾归位到真歌词末句、全曲再无残留冒号署名行;幂等。
  **教训:QQ 卡拉OK版把词/曲/乐手/混音等署名成块塞在前奏**和**尾奏**,两头都要剥,判据按"带冒号署名短句"通用识别而非穷举职务词表。
- **弃用 QQ ACCOM stem,改一律 Demucs 分离伴奏(2026-07-28)**:**症状**——QQ音乐 导入的《小行迹 (Live)》伴奏轨其实
  也是原唱(与原唱是两个不同版本、都带人声)。**根因**:`SpecialSongFileType.ACCOM` 号称"卡拉OK伴奏 stem",但对
  **Live/特殊版常返回另一版原唱**(有人声、非伴奏);QQ音乐 本就不带真伴奏,ACCOM 不可信。实测该曲两轨中置(人声)/两侧
  能量比 2.60 / 2.45——**伴奏轨人声比原唱还高**,坐实是原唱。**决策**(用户拍板):**不再取 ACCOM**,QQ 导入统一
  **保留高质量原唱(`config.QQ_ORIGINAL_QUALITY` 改 FLAC 无损优先)+ Demucs 从原唱人声分离出伴奏**。改 `qqmusic_import`:
  `_fetch_urls_and_lyric` 去掉 ACCOM 取址;`prepare` 入库路径固定"下原唱→`separate_accompaniment`(未装 demucs 才降级两轨都
  用原唱占位)";试听只下原唱片段。**修库**:重搜得 media_mid/song_type→`prepare` 重下 FLAC + 分离→**只覆盖库里两个 PCM**
  (`.qrc`/`.note`/`meta.json`/`library.json` 不动,避开运行中 pc-service 抢清单)。**代价**:每首入库多花整首 Demucs 时间
  (本机无 CUDA 走 CPU,约 3-4 分钟/首),换来真伴奏;有真·官方伴奏的歌(如个别正常版)也一并走分离,牺牲那点质量换一致可靠。
  改动需重启 pc-service 对下次新导入生效(库文件已就地修好,播放器下次载歌即用)。

## 十九、托盘子窗口"开窗要等好几秒/卡顿"根治 —— 常驻单 Tk 根 + 延迟构建(2026-07-28)

**症状**:从托盘打开「曲库管理」「扫描导入歌曲」等子窗口时明显发慢、偶尔要等好几秒才出图。

**根因(三处叠加)**:
1. **每开一个窗都新建 `tk.Tk()`**:各窗口原本各在自己的后台线程 `tk.Tk()` 建一个全新 Tcl/Tk 解释器
   再跑独立 `mainloop()`。实测冷建 `tk.Tk()+ttk` ~169ms/次(首个窗还要一次性付 Tcl/Tk DLL 加载 + 系统
   字体枚举);窗口一关即销毁,故**每次打开都重付**这份成本。多 `tk.Tk()` 根跨解释器还脆(到处 `master=root`)。
2. **扫描窗在 Tk 线程上同步 `import qqmusic_import`**:连带 numpy/niquests/qqmusic_api 一串冷导入(空闲实测
   合计 ~0.7s+,实运行 GIL 负载下更久),且 QQ 页签本体也当场全搭完才首次绘制 → **首开扫描窗卡好几秒的主因**。
3. **曲库窗显示前同步渲首批 60 行**(每行约 6 控件 = 360+ 控件),压在首帧前建 → 首帧再卡几百 ms。

**修法**:
- **A. 启动预热 QQ 导入**:`main()` 起后台线程 `_prewarm_qqmusic()` 先 `import qqmusic_import`,填进 sys.modules;
  日后 QQ 页签构建时的 import 变瞬时命中。
- **B. QQ 页签延迟构建**:扫描窗只即时建「K歌」页签;「QQ」页签的控件 + `_qpoll` 轮询挪进 `_build_qq_tab_impl()`,
  由 `<<NotebookTabChanged>>` 在**用户第一次切到 QQ 页签**时才建(不用就不建、也不 import)。
- **C. 曲库窗先出壳再灌行**:初次 `refresh()` 改 `root.after_idle(refresh)`,让窗壳 + 工具栏先出图,60 行放到 idle 再渲。
- **D. 常驻单 Tk 根 + 窗口做 Toplevel**(架构层):新增 `_ui_thread_main()`——一个守护线程建**一个隐藏 `tk.Tk()` 根**
  (`withdraw()`)并跑**唯一的 `mainloop()`;所有窗口(曲库/扫描/改名/演唱者)改成它的 `Toplevel`,开窗只建 Toplevel
  (几十 ms),再开近乎瞬时。**跨线程只用队列**:托盘/后台线程把"建/毁窗口"可调用丢进 `_ui_queue`,由 UI 线程自己的
  周期 `after` 定时器 `_drain` 抽取执行——**绝不从别的线程直接碰 Tcl**(那正是 `Tcl_Panic` 崩溃的来源)。

**GC 崩溃防护沿用**(见 `_tk_gc_enter/exit` 老注释:循环 GC 在别的线程 finalize tk 对象 → `Tcl_Panic` 硬崩):
改由 `_tk_win_close_guard(root, on_closed)` 接管——窗一开 `_tk_gc_enter()` 禁 GC(引用计数,多窗安全),窗**真正销毁**
(只认 root 自身 `<Destroy>`,过滤子控件事件)时在**本 UI 线程** `gc.collect()` 清本窗遗留 tk 循环、再 `_tk_gc_exit()` 恢复,
并跑 `on_closed`(清 `_lib_root/_scan_root` 勾选态 + 刷托盘)。**隔离复现验证**:并发开两窗→gc depth 1→2(禁用)、
逐一关窗→2→1→0(归零重启用),其间别的线程 `gc.collect()` 无 `Tcl_Panic`,进程正常结束。

**保留**:`_open_library_browser(selftest=True)` 的 headless 自检仍走独立 `tk.Tk()`+自带 `mainloop`(不依赖常驻 UI 线程)。
托盘勾选态仍用 `_lib_root/_scan_root is not None` 判断(现指向 Toplevel,关窗由 `<Destroy>` 善后置 None)。

**教训**:长驻托盘服务里反复弹的 Tk 窗口,别每次 `tk.Tk()` 重建解释器——常驻单根 + Toplevel 复用最省;跨线程操作 Tk 一律
走"UI 线程自有定时器抽队列",不跨线程碰 Tcl;窗口里的重导入(numpy/网络库)预热到后台线程,别压在 Tk 线程首帧前。

## 二十、礼物菜单:绿幕左侧竖排"礼物→权益"引导条(2026-08-05)

**目标**:直播绿幕上加一列礼物引导条(抖音礼物图标 + 自定义文字,如 🎈点歌 / 🍰插队),引导观众打赏。
静态展示(非响应真实礼物事件);主播能自选礼物、填每个礼物对应的权益文字、鼠标拖动摆放。

**链路三段**:
1. **目录抓取** `pc-service/gifts.py`:GET 抖音 `webcast/gift/list?aid=1128`(**匿名可取,不需 room_id/cookie**;
   `data.gifts[]` 含 `id`/`name`/`diamond_count`/`icon.url_list`)→ 缓存 `gift_cache/gifts_catalog.json`(1373 项)
   + 图标 PNG 按 id **用到才下**到 `gift_cache/icons/`。离线优先,`fetch_catalog(refresh=True)` 才重抓,联网失败回退缓存。
2. **配置窗**:托盘「礼物菜单配置」(单实例 `_gift_root`,常驻根 Toplevel)——左目录搜索勾选(点「＋加入」,只渲染
   过滤后前 120 条,靠搜索缩小,不为 1373 项做全量分页/批量下图);右已选 = 缩略图(只对已选下载)+ 名 + **自定义
   文字输入 + ↑↓ 排序 + ✕ 删**。保存 `set_gift_config([{id,text}])` → 存 `STATE["gifts"]` + `_push_gifts`。
3. **推送/显隐/位置**:`_push_gifts`(去重,同 `_push_setlist` 的 GIL 守卫)把 `gifts.resolve` 出的
   `[{icon:图标绝对路径, text}]` 经 IPC `gifts <json>` 推播放器;播放器预合成每个礼物成一张 pixmap 竖排 blit。
   显隐 WS `{"cmd":"gifts_toggle"}` → `gifts_show 0/1`;播放器 `G` 键 + **鼠标单独拖动**礼物条(命中检测:按在条上
   拖它、否则拖整窗);`gifts_show/gift_x/gift_y` 经播放器 STATE 回读缓存,拉起播放器时统一重推。

**绿幕坑(核心)**:抖音礼物图是**彩色半透明 PNG**,直接贴纯绿会被抠像留绿边(软边与绿混合)、且图内绿/青色块会
被抠穿。修法同歌词/音准线:每个礼物坐一张**不透明深色圆角底板 + 最外一圈黑 keyline**(抗锯齿边缘落黑上,绿幕干净
抠;文字坐不透明底板故白字无需描边;emoji 走 `drawText` 才出彩色,非字形路径)。卡片**预合成 QPixmap 缓存一次**
(内容/字体/DPR 变才重建),`paintEvent` 只 blit——图标缩放 + drawText 是 GUI 线程重活,绝不能每帧做(同 setlist 教训)。

**跨端**:手机遥控页"窗口开关"区加"礼物菜单 显示/隐藏"(`gifts_toggle`,`giftsVisible` 随 `state` 广播);
托盘"礼物菜单显示"勾选项。跨重启缓存 `STATE["gifts"]/gifts_visible/gift_x/gift_y` 进 `state_cache.json`。

**教训**:任何要叠到绿幕上的彩色/半透明图,一律先垫不透明底板 + 黑 keyline,别指望抠像能干净处理软边;
外部图标资源按 id 落盘懒下载,别为一个下拉列表批量拉全量图。

### 二十·补:去黑底改剪影描边 + 尺寸调节(2026-08-06)

作者反馈黑色底板太重。**去掉不透明底板,改"剪影描边"**:礼物图/文字直接贴绿仍会留绿边,故不能裸贴——
`_build_gift_pix` 改为:①图标+白字画到透明"内容层"(白字 `drawText`,emoji 出彩色);②取内容层 alpha
填黑得"黑剪影"(`CompositionMode_SourceIn`);③黑剪影在 8 个方向各偏移 `r` 画一遍 = 一圈黑轮廓,再盖回内容层。
等价歌词/音准线的黑 keyline(抗锯齿边缘落黑上、绿幕干净抠),但描的是**任意位图剪影**而非字形路径,故 emoji
(彩色)也能统一描边。视觉从"黑卡片"变"贴纸式描边浮层",轻得多。

**尺寸调节**:新增 `gift_scale`(0.4~2.0,越界夹取)缩放图标/文字/间距/描边全部量。配置窗底部加 **`tk.Scale`
滑块 40~200%**:拖动 `command` live 推 `set_gift_scale(save=False)`(播放器实时预览)、`<ButtonRelease-1>` 松手
`save=True` 存盘。IPC `gift_scale <f>`、STATE 回读、`start_player` 重推、跨重启缓存,与位置/显隐一套。

**微调(2026-08-06 当日,据作者反馈)**:①**描边太粗** → 描边宽 `GIFT_OUTLINE` 由 3 降到 **2**,对齐歌单
(歌单是居中笔画 `OW_BLACK(6)×font_small/font_big=4`、外露约 2px;剪影外扩 r 全部外露,故 r=2 与之相当)。
②**整体偏大** → base 尺寸下调:图标 56→**48**、文字 18→**16**、间距/竖距略减。③**最小还是偏大** → 尺寸下限
`GIFT_SCALE_MIN` 由 0.6 降到 **0.4**(滑块 40%),player/server 三处夹取同步改。

**再调(同日,作者仍嫌描边太黑太粗)**:描边**细 + 淡**——`GIFT_OUTLINE` 2→**1.5**(剪影外扩 r 变细),
新增 `GIFT_OUTLINE_ALPHA=0.6`:改**先把 8 向偏移黑剪影画到一张"环层"、再整层一次性 `setOpacity(0.6)` 画上**
(而非直接 8 次叠画——那样偏移重叠处会更黑),得均匀的淡描边。**权衡**:淡成半透明后描边在纯绿上是暗绿,
抠像可能留一丝淡边(纯黑不透明最干净);若真留边,把 ALPHA 调回 1.0 + 描边色改深灰(green-safe 又不刺眼)。
**三调(同日)**:仍嫌粗 → `GIFT_OUTLINE` 1.5→**1.0**(1px);**行距缩小一半** `GIFT_GAP` 9→**4**。

**四调(同日,改成可调项)**:作者要"描边粗细/间距/描边颜色都放配置窗里方便现场调"。于是把这三者(+ 已有尺寸)
全变成播放器**实例属性** `gift_outline/gift_gap/gift_color`(+`gift_scale`),配置窗底部「样式」面板用**两行网格吃满
横向**:行1 菜单尺寸整行滑块;行2 描边粗细(0~3px)/ 菜单间距(0~24px)/ 描边颜色(`colorchooser` 取色器,色块预览)
三组并排。滑块拖动 live 推、松手/选定即存(`set_gift_outline/set_gift_gap/set_gift_color`)。IPC `gift_outline/gift_gap/
gift_color`、STATE 回读、`start_player` 重推、跨重启缓存,全套照 `gift_scale` 那条路。**描边色改回不透明**(去掉上一版
的半透明 alpha):opaque 才绿幕干净不留暗绿边,**"淡化"改由选浅灰实现**(默认深灰 `#333`)——既满足审美又 green-safe。

**教训**:绿幕上叠彩色/半透明内容,不一定要垫不透明底板——"取剪影 alpha 填黑、多方向偏移描边"能给**任意位图
(含彩色 emoji)**加干净黑 keyline,比矩形底板轻。这套剪影描边可复用到日后别的绿幕浮层元素。

## 二十一、绿幕样式控制窗:礼物/歌单/歌词统一样式 + 歌单改鼠标拖动(2026-08-06)

作者要把样式控制集中管理、并给歌单/歌词也加样式项。做了:

**新托盘窗「绿幕样式控制」**(`_open_style_window`,单实例 `_style_root`):三分区(礼物菜单 / 歌单 / 歌词),
每区滑块 + 取色器。礼物样式从「礼物菜单配置」窗**移过来**(那窗只留选礼物+文字+排序)。统一走 `set_style(key,v,save)`:
`_GP_CMD` 查播放器 IPC 命令名(**字体大小的命令名是 `*_font`**,其余同名)、`_GP_RANGE` 夹取;滑块拖动 `save=False`
live 预览、松手/选色 `save=True` 存盘。`_STYLE_KEYS`(歌单/歌词 8 项)在 STATE 回读 / `_save/_restore_persist` /
`start_player` 重推里统一遍历,和礼物样式一样跨重启缓存。

**歌单样式**(播放器实例属性 `setlist_pt/outline/color/margin`):字体大小 / 描边宽 / 描边色 / **左右边距**。
**歌词样式**(`lyric_pt/outline/color/margin`):同上。`_apply_font` 改用 `lyric_pt`/`setlist_pt` 建 font_big/small;
`_word_entry` 的 base/hi 描边改用 `lyric_color`/`lyric_outline`(不再写死黑/OW_BLACK);`_setlist_entry` 用
`setlist_color/outline`;`_draw_lyrics` 的 margin 用 `lyric_margin`。**描边颜色 = 那圈黑 keyline 的颜色**(默认黑,
绿幕最干净;歌词 KTV 蓝白填充不受此控),淡化同礼物建议选深灰。

**歌单竖直位置改鼠标拖动**:`mousePressEvent` 命中检测加一档——按在歌单**居中带**(`_setlist_bbox`)→ `_setlist_drag`
**仅竖直**拖(横向固定居中),上不越顶、下不压歌词(`_setlist_max_y`=音准带顶 − 歌单高);原 `Ctrl+↑↓` 保留。
命中优先级:礼物条 → 歌单带 → 拖整窗。**歌单/歌词都改成"水平居中带"**:`_setlist_band`/`_draw_lyrics` 按 margin
左右留白、居中(margin=离窗口两侧距离=居中带宽度)。歌词位置**固定底部**,不做拖动。

**边界处理**:描边宽做成可调后,`_make_line_pixmap` 的 `PAD=6` 仍够(描边居中于路径、外露 ow/2;歌词描边上限 10→
5px<6、歌单 8→4px<6,不裁边)。字体大小变 → `_apply_font` 重建 + 清缓存(_line_h 变,`_layout` 自适应)。
描边宽/色变 → 清 `_word_cache`/`_setlist_pix` 重建;边距变 → 只影响绘制位置,不重建 pixmap。

**教训**:歌词渲染是全项目最吃 GIL/最多缓存的地方,加可调项时严格分清"要重建 pixmap 的(字体/描边宽/描边色)"
和"只改绘制位置的(边距/竖直位置)"——后者绝不清缓存,免无谓重建抢音频回调 GIL(同 setlist 冗余重推那条教训)。
