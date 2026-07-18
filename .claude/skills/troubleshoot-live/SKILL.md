---
name: troubleshoot-live
description: 直播系统(auto-director 自动切镜/运镜 + karaoke-player + pc-service + OBS)出故障时的排查修复。当用户说"没声音/听不到伴奏""自动切镜不动""自动切镜只切一下就停""画面黑边/变形""字幕不显示""机位画面错乱/切到别的机位""切镜卡住"等直播异常,用本 skill 按已知故障模式做决策树式排查修复。仅用于本 zhibo 直播项目。
---

# 直播故障排查

先跑通用探活定位现象,再按对应分支查。真 Python:`C:/Users/11651/AppData/Local/Programs/Python/Python313/python.exe`(下称 `<py>`)。pc-service ws `localhost:8765`,OBS ws `localhost:4455`。踩坑全集见 `auto-director/GUIDE.md §8`。

## 通用探活(先跑)
```python
import json, time, websocket
try:
    ws = websocket.create_connection('ws://localhost:8765/ws', timeout=4); m = json.loads(ws.recv()); ws.close()
    print('k_playing', m.get('k_playing'), 'k_pos', m.get('k_pos'), 'director_on', m.get('director_on'))
except Exception as e:
    print('pc-service 连不上', e)
```
+ 查进程(PowerShell):`Get-CimInstance Win32_Process -Filter "Name='python.exe'"` 里找 `server.py`(pc-service)/`director.py`。
+ OBS 当前场景:`obs_python … get_current_program_scene()`。

## 分支

### ▶ 播放器没声音
1. **声卡设备索引漂移**(最常见):接相机/ToDesk 虚拟音频会挤动 WASAPI 枚举,写死索引 27 会指到别的设备(曾指到 ToDesk Virtual Audio)。pc-service 已按名解析(`_resolve_player_device`,找 WASAPI 下含 `PLAYBACK 1/2`);查启动日志里 `[PLAYER] 输出设备按名解析 → [N] …` 是不是真 ROUTIST。
2. Windows `VIRTUAL REC 3/4` 录音端点音量被归零(pycaw 查)。
3. sounddevice 列输出设备,核对 `PLAYBACK 1/2 (WASAPI)` 当前真实索引。

### ▶ 自动切镜"只切一下就停"
- 症状:`director_on=True` 但 **director 进程已死**。病因:pc-service 拉起 director **没设 UTF-8**,`print("♪/→")` 在 Windows GBK 下 `UnicodeEncodeError` 崩主循环(崩在第一次 `[切]` 打印后,切镜动作已执行故"切一下就死")。
- 查:`_start_director` 的 `Popen` 是否传 `env=PYTHONIOENCODING=utf-8`(应已修)。手动 `<py> auto-director/director.py`(带 UTF-8)看真实报错。

### ▶ 自动切镜完全不动
- **歌在放吗**:采样两次看 `k_pos` 有没有走;没歌/暂停 → 不切,正常。
- **长前奏**:某些歌前奏很长(吉姆餐厅 68s),INTRO 期间不切镜属正常——看 director 是否有 `[心跳]`(手动跑才看得到)。
- director 连上 pc-service WS 了吗、进程活着吗。

### ▶ 画面黑边 / 变形
- **黑边**:残留 `boundsType=OBS_BOUNDS_SCALE_INNER` 覆盖 scaleX/Y。查 `content_<cam>` 的 boundsType;所有设 framing 变换带 `OBS_BOUNDS_NONE` 重设 cover。
- **变形(压扁)**:16:9 进 4:3 要 cover 等比铺满,别按 X/Y 分别缩放。
- 修法参考 `wire-camera` skill 第 4 步的验证/重设。

### ▶ 字幕不显示
- **图层顺序**:`KTV悬浮` 要在摄像头之上(obs-websocket `sceneItemIndex` **0=底、越大越上**)。
- **绿底 + 显示态**:播放器透明底/隐藏 → 捕获不到;需 `kshow` + 绿底。
- 色键滤镜在不在 `KTV悬浮` 源上。
- 一键恢复:用 `ktv-overlay` skill。

### ▶ 机位画面错乱(切到别的机位/画面对不上)
- iVCam `#N` 按**连接顺序**分配,开机顺序变了映射就错。列设备核对,用 `wire-camera` skill 重接。

## 修完
- 复述根因 + 改了什么;能重启的组件重启验证(pc-service / director / OBS 变换)。
- 若是新坑,提示补进 `GUIDE.md §8`。
