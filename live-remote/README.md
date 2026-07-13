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
- 面板外的点击穿透到下方K歌;悬浮窗拖到投屏歌词裁剪框以外即可不入镜。
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
