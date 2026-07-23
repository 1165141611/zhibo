---
name: troubleshoot-live
description: 直播系统(karaoke-player K歌播放器 + pc-service + 直播伴侣)出故障时的排查修复。当用户说"没声音/听不到伴奏""字幕不显示""歌词窗捕获不到/绿幕没抠干净""播放器没拉起"等直播异常,用本 skill 按已知故障模式做决策树式排查修复。仅用于本 zhibo 直播项目。(注:多机位自动切镜/运镜 + OBS 整条链路已于 2026-07-23 移除,回归直播伴侣直接推流,相关分支不再收录。)
---

# 直播故障排查

先跑通用探活定位现象,再按对应分支查。真 Python:`C:/Users/11651/AppData/Local/Programs/Python/Python313/python.exe`(下称 `<py>`)。pc-service ws `localhost:8765`。踩坑历史见 `live-remote/DEV_LOG.md`。

## 通用探活(先跑)
```python
import json, time, websocket
try:
    ws = websocket.create_connection('ws://localhost:8765/ws', timeout=4); m = json.loads(ws.recv()); ws.close()
    print('k_playing', m.get('k_playing'), 'k_pos', m.get('k_pos'), 'player_visible', m.get('player_visible'))
except Exception as e:
    print('pc-service 连不上', e)
```
+ 查进程(PowerShell):`Get-CimInstance Win32_Process -Filter "Name='python.exe'"` 里找 `server.py`(pc-service)/`player.py`(K歌播放器)。

## 分支

### ▶ 播放器没声音
1. **声卡设备索引漂移**(最常见):接相机/ToDesk 虚拟音频会挤动 WASAPI 枚举,写死索引 27 会指到别的设备(曾指到 ToDesk Virtual Audio)。pc-service 已按名解析(`_resolve_player_device`,找 WASAPI 下含 `PLAYBACK 1/2`);查启动日志里 `[PLAYER] 输出设备按名解析 → [N] …` 是不是真 ROUTIST。
2. Windows `VIRTUAL REC 3/4` 录音端点音量被归零(pycaw 查)——推流采集链路的关口,曾被误伤归零致全链静音。
3. sounddevice 列输出设备,核对 `PLAYBACK 1/2 (WASAPI)` 当前真实索引。
4. 采集链路:音乐→`PLAYBACK 1/2`→Studio One「他人听」总线→ASIO `PLAYBACK 3/4`→`VIRTUAL REC 3/4`→直播伴侣主麦克风。逐段核对,别往 `PLAYBACK 3/4` 直接灌音频(麦克风监听,会回授炸麦)。

### ▶ 字幕不显示(直播伴侣窗口捕获 + 绿幕)
- **播放器在放吗**:采样两次看 `k_pos` 有没有走;没歌/暂停 → 词不动,正常。未开唱态(载入待唱、从未开唱)只出纯绿背景不画词,属正常。
- **窗口捕获对不对**:直播伴侣里「窗口捕获」素材要绑到 K歌播放器窗(标题 `KaraokePlayer`);播放器被隐藏(`khide`)或最小化 → 捕获不到,先 `kshow` 让窗口出来。
- **绿幕色键**:直播伴侣里给该窗口捕获素材加「色度键/绿幕」,抠掉播放器绿底;没加或键色不对 → 满屏绿或抠不干净。
- **绿底 + 显示态**:播放器需处于显示态且渲染绿底(非透明底),捕获才有内容。

### ▶ 播放器没拉起 / 崩了
- pc-service 托管播放器为子进程;查进程列表有没有 `player.py`。
- 手动带 UTF-8 跑看真实报错:`set PYTHONUTF8=1 && <py> karaoke-player/player.py --device 27`(Windows GBK 下 `print` 含 `♪/→/中文` 会 `UnicodeEncodeError` 崩,pc-service 拉起时已强制 `PYTHONIOENCODING=utf-8`)。

## 修完
- 复述根因 + 改了什么;能重启的组件重启验证(pc-service / 播放器)。
- 若是新坑,提示补进 `live-remote/DEV_LOG.md`。
