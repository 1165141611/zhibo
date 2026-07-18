---
name: ktv-overlay
description: 直播 KTV 歌词字幕叠层的接入/恢复/布局调整。当用户说"字幕不显示了""重建场景后恢复字幕""调 KTV 字幕布局""歌单/歌词的位置/间隙""字幕绿幕抠图""KTV悬浮"等,用本 skill 给 karaoke-player 绿幕歌词窗(OBS 里的 KTV悬浮 窗口捕获)加绿幕色键、同步到三机位场景并置顶、按 KTV 款排版(歌单在顶/歌词在底/裁掉底部状态栏/上下等间隙)。仅用于本 zhibo 直播项目。
---

# KTV 字幕叠层接入 / 布局

工具是 `auto-director/ktv_overlay.py`。它给 `KTV悬浮`(karaoke-player 绿幕歌词窗口的窗口捕获)加绿幕色键 + 同步到三场景并置顶 + KTV 布局。

## 环境
- **真 Python**:`C:/Users/11651/AppData/Local/Programs/Python/Python313/python.exe`(下称 `<py>`)。
- OBS websocket `localhost:4455`;pc-service ws `localhost:8765`。
- OBS 里要有一个**窗口捕获**源名为 `KTV悬浮`,抓 karaoke-player 窗口(标题含 `KaraokePlayer`)。没有就先在 OBS 手动加。
- **前置(重要)**:karaoke-player 窗口须**显示态 + 绿底**才有内容可抠 —— pc-service `kshow` 显示 + 放一首歌(有歌词渲染)。透明底/隐藏则捕获不到,字幕不显示。

## 步骤
1. **一键接入**:`<py> auto-director/ktv_overlay.py`
   - 做:加绿幕色键(挂在**源**上→三场景通用)+ 同步 `KTV悬浮` 到 cam1/2/3 + **置顶**(盖在摄像头之上)+ KTV 布局(裁掉底部状态栏、歌单在顶/歌词在底、上下等间隙、水平居中)。
2. **显示播放器 + 放歌**(验证前提):
   ```python
   import json, websocket
   def cmd(c):
       ws = websocket.create_connection('ws://localhost:8765/ws', timeout=4); ws.recv()
       ws.send(json.dumps(c)); ws.close()
   cmd({"cmd": "kshow"}); cmd({"cmd": "kplay"}); cmd({"cmd": "kseek", "ms": 85000})  # seek 到主歌有歌词处
   ```
3. **验证**(必做):截三场景图,确认**歌词浮现在画面上、绿抠干净、歌单在顶/歌词在底、上下间隙对称、无底部状态栏**。
   若某场景没有 `KTV悬浮` 或不在顶层→重跑第 1 步。
4. **布局微调**:改 `ktv_overlay.py` 顶部常量后重跑:
   - `GAP`(上下留白 px,现 45;调大=字更小、留白更多)
   - `SETLIST_TOP=606` / `LYRICS_BOTTOM=1565`(源像素锚点,决定裁到哪)
   - `SW,SH=1260,1680`(源尺寸 = 720×960 @175%DPI)——**换 DPI/窗口尺寸必须重量**这些坐标(截原始源找各元素 y 位置)。

## 踩坑(详见 GUIDE §3.5 / §8)
- **图层顺序**:obs-websocket `sceneItemIndex` **0=底层、越大越上层**(反直觉/与部分文档相反);`KTV悬浮` 必须比摄像头 index 大才盖在上面。
- **绿底前置**:播放器不是绿底(透明/半透黑)或隐藏 → 捕获不到,字幕不显示。
- **裁剪坐标随 DPI/窗口尺寸变**:现值只对 1260×1680。
