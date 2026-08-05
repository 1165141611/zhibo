# 直播遥控系统

一台安卓手机(悬浮窗控制台,同时跑全民K歌)通过局域网遥控电脑,实现:
- **声卡音轨切换**(聊天/湿唱/干唱/喇叭/闭麦/静音)→ MIDI 控制 Studio One
- **背景音乐**(QQ音乐)→ 播放/暂停/上一首/下一首 + 单独音量

```
[安卓悬浮窗 App] --WiFi/WebSocket--> [电脑后台服务] --MIDI--> Studio One
                                                  \--SMTC(winrt)/pycaw--> QQ音乐
```

> QQ音乐 的播放/暂停/切歌走 **SMTC 会话有方向控制**(winrt 子进程按 AUMID 锁定 QQ音乐 自己的
> 会话),不再模拟无方向的全局媒体键——后者会被路由到抢占系统当前会话的 App(WeSing/浏览器/
> 直播伴侣),曾是"手机控 BGM 时好时坏、正在播的歌不同步"的根因。详见 `DEV_LOG.md` 二·补。

---

## 一、电脑后台服务(pc-service)

### 1. 安装依赖
```powershell
cd pc-service
pip install -r requirements.txt
```

### 2. 装 loopMIDI 建虚拟 MIDI 口
1. 下载安装 **loopMIDI**(Tobias Erichsen,免费)。
2. 打开后点左下 `+`,新建一个端口,默认名 `loopMIDI Port`。
   - 名字要和 `config.py` 里的 `MIDI_PORT_NAME` 对上(部分匹配即可)。

### 3. Studio One 里映射 MIDI(一次性)
1. `Studio One → 选项/设置 → 外部设备 → 添加 → 新建键盘`,MIDI 输入选 `loopMIDI Port`。
2. 对每个场景做 **MIDI Learn**:右键场景/宏对应的快捷键 → “指定 MIDI 控制” → 在手机/测试网页点一下该场景按钮,让它学习到发出的音符。
   - 音符对应关系见 `config.py` 的 `SCENE_NOTES`(默认 60~65)。

### 4. 运行
```powershell
python server.py
```
启动后终端会打印:
```
手机浏览器测试网页:  http://192.168.x.x:8765
App WebSocket 地址:  ws://192.168.x.x:8765/ws
```
托盘也会常驻一个图标。

### 5. 先用测试网页验证(不用等 App)
手机连**同一个 WiFi**,浏览器打开上面那个 `http://192.168.x.x:8765`:
- 点声卡场景 → 看 Studio One 是否切换
- 点 QQ音乐 播放/切歌、拖音量条 → 看 QQ音乐 是否响应

> QQ音乐 音量调节要求 QQ音乐 正在播放(有音频会话)才能被 pycaw 找到。
>
> **稳定性(2026-07-14)**:server.py 启动即**全局禁用 comtypes 的 GC 自动 Release**
> (`_compointer_base.__del__` → no-op)。QQ音乐 暂停/切歌后 pycaw 的会话/设备 COM 指针会悬空,
> GC 触发 `Release()` 曾造成反复闪退(`_ctypes.pyd c0000005`)与堆静默损坏(`ucrtbase c0000409`),
> 开曲库管理窗(建大量 tk 控件触发 GC)/点歌开唱(BGM 渐变刷会话)最易命中。现在所有 COM 包装
> 故意泄漏不释放(单个几十字节,常驻服务可接受)。取证与细节见 `DEV_LOG.md` 2026-07-14 条目。

---

## 二、安卓悬浮窗 App(android-app) — 待搭建

- 前台服务 + 可拖动悬浮窗(`TYPE_APPLICATION_OVERLAY`),叠在全民K歌上方。
- 面板外的点击穿透到下方K歌。
- 连的就是上面的 `ws://192.168.x.x:8765/ws`,协议见下。

### WebSocket 协议
```jsonc
// App → 电脑
{"cmd":"scene","id":5}              // 切场景
{"cmd":"bgm","action":"next"}       // next / prev / playpause / play / pause
                                    // play、pause 有方向且幂等(已在目标状态则不动),供手机
                                    // "演唱↔BGM 联动"用——playpause 无方向,状态一过期就会打反
{"cmd":"bgm_vol","value":40}        // 音量 0-100

// 电脑 → App
{"type":"state","scene":5,"bgm_vol":40,"studio_connected":true}
```

---

## 三、K歌播放器托管 + 自动曲库(阶段一,已接入)

pc-service 现在也是**电脑端 K歌中枢**(总体方案见根 [`../KARAOKE_SYSTEM.md`](../KARAOKE_SYSTEM.md)):

- **托管 K歌播放器**:`main()` 用子进程拉起 `../../karaoke-player/player.py --device 27 --hidden --paused --no-smtc`(隐藏、暂停、关SMTC)。**显隐走进程管道 IPC**:服务器往 player 的 stdin 写 `toggle`,player 自己用 Qt `show()/hide()`(**必须 Qt 自己显隐才会正确重绘**,外部 win32 `ShowWindow` 只让空壳可见不会触发 Qt 重绘);player 经 stdout 上报 `VIS:0/1`,服务器据此同步 `STATE["player_visible"]`+刷托盘+推手机。托盘"显示/隐藏 K歌歌词"文本按 `karaoke_win.is_visible()`(win32 实时读,可靠)。`karaoke_win.py` 只用于**读**可见性(`is_visible`)。托盘或 WS `{"cmd":"player_toggle"}` 触发。播放器内 ESC/升降调等快捷键照常;**服务模式下 ESC 与关窗(任务栏X)都只隐藏,进程/音频不退**,由 pc-service/托盘控制生命周期。
- **曲库导入:手动扫描窗口(2026-07-26 改版,原后台自动轮询已移除)**。托盘「扫描导入歌曲」→ 专用扫描窗口,现为 **`ttk.Notebook` 双页签**:
  - **「K歌(带音准)」页签** = 全民K歌**缓存扫描**(控件移进本页签)。**来源二选一**(2026-07-26):顶部「扫描来源」单选 `电脑缓存 / 手机全民K歌`,**一次只扫一端**(避免两端数据互相干扰);选手机才启用设备下拉/扫码连接。打开默认电脑模式、不自动扫描,点『重新扫描』才扫。**检索框**(2026-07-26):按歌名/歌手过滤扫描结果(200ms 防抖,隐藏不匹配行、**保留勾选/改名不重建**);**结果按缓存时间(`mtime`)倒序**(最近缓存的在最上;候选带 `mtime`,扫完 `_resort_render` 排序)。
  - **「QQ(无音准)」页签(2026-07-26 新增)** = **QQ音乐 登录态 API 在线搜索**导入(`qqmusic_import.py`)。补"特殊版只在 QQ音乐 有"的歌。顶栏:登录状态 + 登录类型(QQ/微信/QQ音乐App)+ 登录·退出 + 搜索框;结果表每行 `[☐ 默认不勾选 | 歌名 Entry | 歌手 Entry | 时长 | QQ | ▶试听]`。**已入库的歌也列出但整行置灰、标「已入库」、禁勾选/编辑/试听**(2026-07-26,`in_library` 标记;`search` 不再跳过已入库、只标记)。**扫码登录一次**(二维码内嵌显示,`login.get_qrcode`+轮询,凭据存 `config.QQ_CRED_PATH`,复用/过期重扫);**▶试听**=worker 里 `prepare(preview=True)` **只下原唱片段**到暂存 → 复用 `_preview` 子进程走系统默认输出播放(不进直播);**确认入库时才** `prepare()`:下 `SongFileType.FLAC` 无损原唱(明文)+ `lyric.get_lyric(qrc=True)` 逐字歌词,ffmpeg 转 PCM,**再用 Demucs 从原唱人声分离出伴奏**(2026-07-28 起弃用 QQ 的 `SpecialSongFileType.ACCOM`——它对 Live/特殊版常返回另一版原唱、仍带人声不可靠;未装 demucs 才降级两轨都用原唱占位),写四件套(**减 `.note`**,QQ音乐 无音高→无音准线)→ `library.import_candidate` 入库。**关键:有会员权限时非加密音质直接返回明文、无需 ekey/QMCv2 解密**(踩坑详见 DEV_LOG 第十八节)。下同的 PC/手机双端扫描说明:
  - **双端来源**:①PC 版 WeSing 缓存 `D:\WeSingCache\WeSingDL\Res`(`library.scan_pc()`);②**手机版全民K歌**(`mobile_import.py`:adb 读 `Android/data/.../files/{qrc,note,obbligato}`)。手机侧 **song↔tkm 靠 mtime 就近配对**(每条 tkm 归属 mtime 最近的 qrc 所属歌;只缓存了歌词没下伴奏的歌自动跳过,不误抢邻曲 tkm)。
  - **手机扫描"列表先出、转码后置"(2026-07-26 优化)**:原来 `scan_phone` 在扫描时就对每首新歌拉几MB tkm + `mobile_convert.py` 子进程解密转码(ffmpeg 解整首 + 写~100MB PCM),几秒一首,"只想看列表也要等半天"。现**列表阶段只拉小小的 qrc 解出歌名/歌手**(每首~0.1s,增量显示;实测手机上千缓存里 110 首候选 4.5s 出全);**音频解密转码推迟到真正入库/试听**(`convert_phone_song`,~2.2s/首,只做勾选的那几首;试听转过的入库直接复用暂存不重转)。新歌候选带 `needs_convert` + `phone`{serial,qname,oke,tkms} 文件引用供后置转换。
  - **扫描窗口**:顶部 adb 设备下拉(`list_devices`,默认第一台)+ **「📶 扫码连接」按钮**(弹无线 ADB 配对二维码,见下);**打开不自动扫描**(2026-07-26 改),点『重新扫描』或切换设备才开扫,慢扫描(adb 拉取 + ffmpeg 转换,可能 ~30s)期间转 `ttk.Progressbar` loading 动画 + 阶段文字(「已发现 N 首」)。**边扫边显示,扫出一首渲一首**(`scan_phone` 每转好一首经 `on_candidate` 回调即入 `results`,`_poll` 每 150ms 增量追加行,不等全部转完);行是**多选可编辑表格**——`[☑ | 歌名 Entry | 原唱者 Entry | 来源 PC/手机 | ▶试听]`(伴奏/原唱结构固定、自动判别即准,**不设交换**;**▶试听** = 子进程 `karaoke-player/preview_play.py` 走**系统默认输出**(自己听的通道,不进直播)播伴奏 + 纯文本歌词窗:当前行高亮、`←/→` 步退进 5s、`Esc` 退出,音量压低 `config.PREVIEW_VOLUME`;单实例,再点/换歌先杀旧);底部 全选/全不选/重新扫描/确认入库(扫完启用)/取消。
  - **无线 ADB 扫码连接**:「📶 扫码连接」弹二维码(`WIFI:T:ADB;S:<name>;P:<code>;;`),手机『开发者选项→无线调试→用二维码配对设备』扫码;主机后台经 `adb mdns services` 发现配对服务 → `adb pair` → 找 `_adb-tls-connect` → `adb connect`,成功即刷新设备下拉并选中新机、自动重扫(`mobile_import.make_pair_payload/wait_and_pair`,依赖 `qrcode`)。
  - **入库**:`library.import_candidate(cand, 歌名, 原唱, swap)` 只入勾选项——源目录由候选决定(PC=Res;手机=暂存),用户编辑的歌名/歌手覆盖(置 `named=True` 防 `_remigrate` 覆盖),`swap` 对调 `_accompany/_kongsinger.pcm`;完成后 `_on_lib_change()` 刷托盘+推手机+推歌单。
  - `library.start()` 只在启动**同步载清单 + 后台跑一次 `_remigrate`**(旧脏标题清洗),不再周期轮询。
  - **歌名清洗 + 手动改名**:歌名取自 QRC 的 `[ti:]`,但 WeSing 对 KTV/用户上传版本常填脏(带 `-歌手-ktv` 后缀,甚至整段是内部数字ID如成都的 `2422569`)。干净歌名只在 `KSongsDataInfo.dat`,但那是 AES 级强加密(解不动,见 DEV_LOG)。故:①`_clean_title` 去后缀/版本括号救回带垃圾后缀的(`鼓楼-赵雷-ktv`→`鼓楼`);②`_is_junk_title` 判纯数字/空标题为救不回,标 `needs_name=True`,入库通知提示"点此改名";③启动 `_remigrate` 用新规则重刷旧条目(幂等,**跳过 `named` 手动命名的不覆盖**)。
  - **改名入口**:**每首入库通知都可点**——点气泡即弹该歌的两栏(歌名/歌手)编辑框(`_last_import_mid` 记当前通知对应的歌,不再串到上一首);或点托盘"曲库"项打开**曲库管理窗**。保存走 `library.rename()`(更新 `library.json`+`meta.json`+内存清单,标 `named=True`,刷托盘+推手机)。
- **托盘**:pystray 菜单 = `遥控地址`、`Studio One MIDI`、`曲库: N 首 — 点击管理`、`扫描导入歌曲`、`演唱者`、`Studio One 显示`、`K歌歌词`、`退出`。**曲库/扫描导入/Studio One 显示/K歌歌词 均为勾选式开关**(2026-07-26 统一):打开=打钩、关闭=去钩,**开着再点即关窗**(`_lib_root`/`_scan_root` 记窗口 Tk 根做**单实例**,绝不重复开多个同名窗;`_toggle_library_window`/`_toggle_scan_window`,关窗在 `_win` 的 finally 清引用+刷托盘)。**`Studio One 显示` 与 App 的显隐开关双向同步**:托盘与 App 的 `studio_toggle` 共用 `_toggle_studio_visible`(show/hide+存盘+`refresh_tray` 刷勾选+`_threadsafe_broadcast` 推 App)。曲库项(**可点击**→ 曲库管理窗:tkinter 滚动帧(Canvas+右侧滚动条),**每行独立 Frame:斑马纹交替底色 + 悬停高亮 + 点击/播放选中态**(纯 tk 手动画,无 Treeview),**行首"勾选框"=加入歌单**(默认不选中;选中的歌进播放器顶端滚动字幕),**行末带"编辑"/"播放"/"删除"按钮**——编辑=改歌名歌手,播放=`k_play_mid` 立即 load+play(**有歌在播则切歌**,静默不弹窗),**删除=红色按钮,弹窗二次确认后**从歌单/队列摘除 + 删该歌全部文件+清单(`_lib_delete`→`library.delete`,不可恢复),待命名歌名标红)、`K歌歌词`(显隐勾选)、`退出`。**"退出"会先弹 Windows 确认框(是/否)**,防误点——退出不可逆(服务、子进程、监听全收尾)。
  - **曲库窗高性能化(2026-07-14)**:①**触底分页渲染**——结果集算好后每批只建 60 行 tk 控件,滚动到底(`yscrollcommand` 的 `last>0.94`)自动续批;内容不满一屏时 `last=1.0` 同样触发,自动填满首屏。不再一次性全量建几百行(那是打开/搜索卡顿根源)。②**搜索防抖 200ms**(原来每敲一键全表重建)。③**Live 筛选**:全部/只看Live/排除Live(约定:歌名含 `live`(不分大小写)即视为 Live 版)。④**排序**:最新入库(默认)/未勾选在前/已勾选在前(勾选=在歌单;组内仍按入库时间倒序;勾选/取消不即时重排,下次刷新才生效,防行跳动)。`_open_library_browser(selftest=True)` 为 headless 自检:自动滚底驱动分页、打印 `[LIBWIN-TEST] shown=x/y`、渲完自毁。
  - **托盘刷新踩坑(重要)**:pystray-win32 右键弹的是**缓存的菜单句柄,不会在打开时重新求值动态 lambda**——菜单文字只在 `update_menu()` 被调时才刷新,而 `update_menu` **只能在托盘线程调**(跨线程改 Win32 菜单会崩)。所以非托盘线程(曲库监听/播放器 reader)的变化**曾永远不刷新**(旧代码"下次打开自动刷新"的假设是错的)。修:`refresh_tray()` 非托盘线程时 `PostMessage(WM_TRAY_REFRESH)` 唤醒托盘线程去 `update_menu`;气泡点击(`NIN_BALLOONUSERCLICK`)也经包裹的 `WM_NOTIFY` handler 在托盘线程分发到改名框。
  - **改名对话框/曲库窗都用 tkinter,在独立线程跑各自 mainloop**——绝不阻塞托盘消息泵(同"退出"确认框教训:在托盘回调里开模态框会占死消息泵)。曲库窗用单 Tk 根 + 子 Toplevel 编辑(同根同线程稳);通知改名框各自独立根。
- 配置见 `config.py`(`PLAYER_DEVICE`/`WESING_RES_DIR`/`KARAOKE_LIBRARY_DIR`;手机导入 `ADB_PATH`/`MOBILE_FILES`/`MOBILE_STAGING_DIR`/`MOBILE_CONVERT_PATH`/`MOBILE_TKM_WINDOW` 等)。
- **投屏(scrcpy)已移除**:改为本机 K歌歌词窗口(绿幕抠图进直播伴侣),不再自动无线投屏。

### K歌 API(手机点歌/控制 · 阶段②-2,已接入)
pc-service 把播放器 IPC 接进 WebSocket,并加曲库列表/点歌队列(供手机 App)。

- **`GET /library`** → `{count, songs:[{mid,title,artist}]}`(曲库列表,供点歌页;曲库变化时 WS 会推状态,手机重拉)。
- **`GET /song/{mid}/karaoke`** → `{mid, lines:[{start,end,chars:[{text,start,dur}]}], notes:[{start,dur,pitch}], chorus:[[s,e]...]}`
  (某首歌的**逐字歌词**(QRC 解析,绝对 ms)+ **音高线**(`.note`,pitch 归一化 0..1),供**演唱页卡拉OK渲染**;
  手机在正唱歌切换时拉一次。解析见 `karaoke_data.py`,复用 `tripledes`(不引 numpy),按 mid 缓存。404=无此歌 QRC)。
  **`chorus`** = 副歌区间 `[[起ms,止ms],...]`,读自 `<曲库>/<mid>/meta.json` 的 `chorus` 键(**手动标注**,无则空;
  原供已移除的自动切镜状态机,现暂无消费方,保留字段)。
- **WebSocket 命令**(App→电脑,`server.py` 转成播放器 IPC / 队列操作):
  ```jsonc
  {"cmd":"kqueue_add","mid":"..."}   // 点歌入队(空闲则立即开唱)
  {"cmd":"kqueue_remove","idx":0}    // 删队列第 idx 首
  {"cmd":"kqueue_move","from":2,"to":0} // 重排(长按拖动/置顶)
  {"cmd":"kqueue_next"}              // 切下一首
  {"cmd":"kqueue_clear"}             // 清空队列
  {"cmd":"kplay"} {"cmd":"kpause"} {"cmd":"kplaypause"}
  {"cmd":"kkey","semi":2}            // 升降调(绝对半音)
  {"cmd":"kvocal","on":true}         // 原唱/伴奏
  {"cmd":"kvol","value":70}          // 伴奏音量 0-100(手机音量键百分比同步)
  {"cmd":"kseek","ms":90000}         // 定位
  {"cmd":"kshow"} {"cmd":"khide"} {"cmd":"player_toggle"}  // 歌词窗显隐
  ```
- **WS 状态推送**(电脑→App,`{"type":"state",...}` 里 K歌字段):`now`(正在唱 `{mid,title,artist}` 或 null)、
  `queue`(`[{mid,title,artist}]`)、`k_playing`/`k_pos`/`k_dur`(ms)/`k_key`/`k_vocal`/`k_vol`(0-100)/
  `k_title`/`k_artist`、`player_visible`、`lib_count`。**唱完切下一首开头暂停(不自动连播)**:`server.py` 的
  `_player_reader` 解析播放器 `STATE`,检测到当前歌播放结束(`pos≥dur-800` 且由播转停)→ `k_advance_paused()`
  只 `load` 队列下一首(归位到 0、暂停,等主播手动开唱);队列空则清空当前曲(`now=null`)。歌曲间歇 BGM
  由手机端"演唱↔BGM 联动"自动恢复(缓冲 2s,可在手机 BGM 悬浮面板关闭联动)。手动 `kqueue_next` 仍立即开唱。
  **空队列点第一首也不自动开唱(2026-07-15)**:`k_enqueue` 在 `_now_mid is None` 时只 `load`(载入开头暂停),
  不再 `k_play_next()`(load+play)——与"唱完切下首暂停"一致,首歌载入待唱期间 BGM 顶着,主播按播放键才开唱。
  配合播放器"未演唱态不显歌词/音准线"(见 karaoke-player),观众在开唱前只看到纯绿,不会提前露出待唱那首的词/音高。
- **BGM 播放/暂停可靠性(2026-07-14)**:①`_play_fade_in_impl` 修"恢复播放炸响"——QQ音乐(MediaSDK_Server)
  **暂停即销毁音频会话,外部无 API 阻止**;恢复时新会话默认 100%,且"出现在枚举器里"和"开始出声"几乎同时,
  暂停后枚举器里还**残留 Inactive 老会话**(实测),"轮询见到会话就压 0"会咬住死会话、永远慢半拍。终版双保险:
  **端点静音保险丝**——按播放**之前**先把(见过 QQ 会话的)设备端点静音(端点是设备级、可提前操作;新会话纵然
  100% 也放不出一个采样),等新会话被压 0 后恢复端点原状(`finally` 保证);**紧凑轮询**——80ms 一拍只扫
  设备级缓存 `_qq_dev_mgrs`(毫秒级,全量枚举一轮 ~0.3s)、只认 `GetState()==1`(Active)的新会话。渐强中每
  5 步补扫迟到会话,收尾兜底设一次目标值。**伴奏保护门控**:伴奏与 BGM 共用 PLAYBACK 1/2,保险丝**只在
  伴奏静默(`k_playing=False`)时上**(联动恢复时演唱必已停,静音无声通道无感);伴奏出声时(演唱中手动恢复
  BGM)退化为纯快速轮询(≤80ms 接管,有伴奏+人声垫底);等待期间伴奏若开播,**立即撤保险丝**。
  **进程归属校验(重大坑)**:`MediaSDK_Server.exe` 是腾讯**共用**媒体进程——**直播伴侣**的同名子进程在
  `PLAYBACK 3/4`/`VIRTUAL REC 3/4`(推流主麦链路!)上挂着音频会话,按进程名裸匹配会把 BGM 音量调到
  推流主麦上(暂停 BGM=直播间静音,实锤复现;"VIRTUAL REC 3/4 归零"悬案同源)。`_is_qq_pid` 要求
  `QQMUSIC_OWNER_CHECK` 里的共用进程**父链含 QQMusic.exe** 才算(pid 判定缓存 TTL 60s),并优先只在
  `QQMUSIC_DEVICE_HINT`(PLAYBACK 1/2)设备上找会话。②渐变时 `bgm_playing` **先乐观翻转并广播**
  (按钮/联动立刻看到正确方向),并加 **SMTC 覆盖抑制窗**(`_bgm_smtc_mute_until`,渐变后几秒内丢弃 winrt
  快照里的 `bgm_playing`)——否则子进程推来按键前的旧快照会把状态翻回去,后续 `playpause` 方向反打,表现为
  "手动暂停 BGM 后自动化失灵"。③新增有方向的 `{"cmd":"bgm","action":"play"/"pause"}`(幂等),联动全部改用。
- **跨重启持久缓存**:声卡场景 + 通道静音记录 + 演唱音量 + **Studio One 显隐** + **音准线显隐** + **歌词字体**
  + **顶端歌单(内容/显隐/位置)** + **礼物菜单(选中礼物+文字/显隐/位置)** + **K歌播放器窗口桌面位置** + **演唱者(主播名)**存 `pc-service/state_cache.json`
  (gitignore),变更即原子写盘,服务重启时恢复
  (静音记录只恢复不发 MIDI;`k_vol`/`pitch_visible`/`k_font`/`setlist*`/`player_x,player_y`/`performer` 在拉起
  播放器后下发一次(`vol`/`pitch`/`font`/`setlist`/`setlist_show`/`setlist_y`/`pos x y`/`performer`);`studio_visible`
  恢复时若上次是隐藏则重新 `hide()`,可见则不动免抢焦点)。播放器窗口位置随其 `STATE` 的 `win_x/win_y` 回读
  (纯 PC 侧信息,不推手机)。App 连接首帧以后端 `k_vol` 为准反向同步手机媒体音量。
- **演唱者(主播名)**:`STATE["performer"]`(默认"八门官上"),托盘"演唱者:<名>"菜单点击弹框可改
  (`_open_performer_dialog`),保存即存盘 + `_player_send("performer <名>")` 下发。播放器开头标题卡"演唱:<名>"用。
- **顶端滚动歌单**:曲库管理页勾选框把歌加入 `STATE["setlist"]`(mid 列表),`set_setlist_member` 更新+存盘+
  `_push_setlist`(mid→歌名 JSON)推给播放器顶端滚动字幕;播放器 `O`(显隐)/`Ctrl+↑↓`(位置)经 STATE 回读
  写盘。仅曲库页勾选 + 播放器键盘,无手机 UI。
- **音准线显隐遥控**:WS `{"cmd":"pitch_toggle"}` → pc-service 翻转 `STATE["pitch_visible"]`、发 `pitch 0/1`
  给播放器、存盘、广播;手机遥控页"窗口开关"区在"K歌歌词"下加了"音准线 显示/隐藏"开关(pc-service 对音准线
  是权威源,不从播放器 STATE 回读,避免启动竞态)。
- **礼物菜单(2026-08-05)**:播放器绿幕**左侧竖排"礼物→权益"引导条**(抖音礼物图标 + 自定义文字,如
  🎈点歌 / 🍰插队),引导观众打赏。链路三段:①**目录抓取** `gifts.py` 抓抖音 `webcast/gift/list?aid=1128`
  (匿名可取,`data.gifts[]` 含 id/name/diamond_count/icon)→ 缓存 `gift_cache/gifts_catalog.json` + 按需下图标
  PNG 到 `gift_cache/icons/`,离线优先。②**配置窗**:托盘「礼物菜单配置」(单实例,`_gift_root`)——左目录搜索
  勾选(点「＋加入」)、右已选可**填自定义文字 + ↑↓排序 + ✕删**;保存 `set_gift_config([{id,text}])` → 存
  `STATE["gifts"]` + `_push_gifts`(去重,同 `_push_setlist`)把 `gifts.resolve` 出的 `[{icon:绝对路径,text}]`
  经 `gifts <json>` 推播放器。③**显隐/位置**:WS `{"cmd":"gifts_toggle"}` 翻 `STATE["gifts_visible"]` 发
  `gifts_show 0/1`;播放器 `G` 键显隐、**鼠标单独拖动**礼物条(命中检测),`gifts_show/gift_x/gift_y` 经 STATE
  回读缓存;拉起播放器时统一重推。手机遥控页"窗口开关"区加"礼物菜单 显示/隐藏"、托盘加"礼物菜单显示"勾选项。
  **礼物图必须坐不透明底板**(彩色半透明 PNG 裸贴绿会留绿边,见 karaoke-player README「关键技术决策」)。
- **伴奏音量走感知曲线**:手机媒体音量%→伴奏增益用**平方曲线**(增益=（%/100)²,见 karaoke-player
  `audio_engine._gain_for`),低档位真正变小(最小档由线性 -23dB 降到 -46dB)、控制更细。
