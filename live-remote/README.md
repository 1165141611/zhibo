# 直播遥控系统

一台安卓手机(悬浮窗控制台,同时跑全民K歌)通过局域网遥控电脑,实现:
- **声卡音轨切换**(聊天/湿唱/干唱/喇叭/闭麦/静音)→ MIDI 控制 Studio One
- **背景音乐**(QQ音乐)→ 播放/暂停/上一首/下一首 + 单独音量

```
[安卓悬浮窗 App] --WiFi/WebSocket--> [电脑后台服务] --MIDI--> Studio One
                                                  \--媒体键/pycaw--> QQ音乐
```

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

---

## 二、安卓悬浮窗 App(android-app) — 待搭建

- 前台服务 + 可拖动悬浮窗(`TYPE_APPLICATION_OVERLAY`),叠在全民K歌上方。
- 面板外的点击穿透到下方K歌。
- 连的就是上面的 `ws://192.168.x.x:8765/ws`,协议见下。

### WebSocket 协议
```jsonc
// App → 电脑
{"cmd":"scene","id":5}              // 切场景
{"cmd":"bgm","action":"next"}       // next / prev / playpause
{"cmd":"bgm_vol","value":40}        // 音量 0-100

// 电脑 → App
{"type":"state","scene":5,"bgm_vol":40,"studio_connected":true}
```

---

## 三、K歌播放器托管 + 自动曲库(阶段一,已接入)

pc-service 现在也是**电脑端 K歌中枢**(总体方案见根 [`../KARAOKE_SYSTEM.md`](../KARAOKE_SYSTEM.md)):

- **托管 K歌播放器**:`main()` 用子进程拉起 `../../karaoke-player/player.py --device 27 --hidden --paused --no-smtc`(隐藏、暂停、关SMTC)。**显隐走进程管道 IPC**:服务器往 player 的 stdin 写 `toggle`,player 自己用 Qt `show()/hide()`(**必须 Qt 自己显隐才会正确重绘**,外部 win32 `ShowWindow` 只让空壳可见不会触发 Qt 重绘);player 经 stdout 上报 `VIS:0/1`,服务器据此同步 `STATE["player_visible"]`+刷托盘+推手机。托盘"显示/隐藏 K歌歌词"文本按 `karaoke_win.is_visible()`(win32 实时读,可靠)。`karaoke_win.py` 只用于**读**可见性(`is_visible`)。托盘或 WS `{"cmd":"player_toggle"}` 触发。播放器内 ESC/升降调等快捷键照常;**服务模式下 ESC 与关窗(任务栏X)都只隐藏,进程/音频不退**,由 pc-service/托盘控制生命周期。
- **自动曲库导入器**:`library.py` 后台线程监听 `D:\WeSingCache\WeSingDL\Res`(WeSing 缓存 LRU 只留几首),把四文件齐全且写完的歌拷进 `D:\KaraokeLibrary\<mid>\` + 解 QRC 写 `meta.json`,维护 `library.json`。启动 backfill 全量补齐,之后持续监听。**每首入库成功弹系统通知**(托盘气泡 `Shell_NotifyIcon`,跨线程安全):`《歌名 - 歌手》已入库,曲库现有 N 首;`--headless` 无托盘时跳过。
- **托盘**:pystray 菜单显示 `曲库: N 首`、`监听: 运行中`、`显示/隐藏 K歌歌词`。**"退出"会先弹 Windows 确认框(是/否)**,防误点——退出不可逆(服务、子进程、监听全收尾)。
- 配置见 `config.py`(`PLAYER_DEVICE`/`WESING_RES_DIR`/`KARAOKE_LIBRARY_DIR` 等)。
- **投屏(scrcpy)已移除**:改为本机 K歌歌词窗口(绿幕抠图进直播伴侣),不再自动无线投屏。

### K歌 API(手机点歌/控制 · 阶段②-2,已接入)
pc-service 把播放器 IPC 接进 WebSocket,并加曲库列表/点歌队列(供手机 App)。

- **`GET /library`** → `{count, songs:[{mid,title,artist}]}`(曲库列表,供点歌页;曲库变化时 WS 会推状态,手机重拉)。
- **`GET /song/{mid}/karaoke`** → `{mid, lines:[{start,end,chars:[{text,start,dur}]}], notes:[{start,dur,pitch}]}`
  (某首歌的**逐字歌词**(QRC 解析,绝对 ms)+ **音高线**(`.note`,pitch 归一化 0..1),供**演唱页卡拉OK渲染**;
  手机在正唱歌切换时拉一次。解析见 `karaoke_data.py`,复用 `tripledes`(不引 numpy),按 mid 缓存。404=无此歌 QRC)。
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
  由手机端"演唱↔BGM 联动"自动恢复。手动 `kqueue_next` 仍立即开唱。
- **跨重启持久缓存**:声卡场景 + 通道静音记录 + 演唱音量存 `pc-service/state_cache.json`(gitignore),
  变更即原子写盘,服务重启时恢复(静音记录只恢复不发 MIDI;`k_vol` 在拉起播放器后下发一次)。
  App 连接首帧以后端 `k_vol` 为准反向同步手机媒体音量。
