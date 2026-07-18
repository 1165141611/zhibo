# -*- coding: utf-8 -*-
"""KTV 歌词叠层一键接入:给 `KTV悬浮`(karaoke-player 绿幕歌词窗口捕获)加绿幕色键,
并同步到 cam1/cam2/cam3 三个场景、**置于摄像头之上**(director 只动摄像头层 content_<cam>,不碰本叠层)。

前置:OBS 里已有一个 `窗口捕获` 源名为 `KTV悬浮`,抓的是 karaoke-player 窗口(标题含 KaraokePlayer)。
用法:python ktv_overlay.py

要点/踩坑:
- 色键滤镜挂在**源**上(不是场景项)→ 一次添加,三个场景全生效。
- **图层顺序**:obs-websocket 里 `sceneItemIndex` **0=底层、越大越上层**(和直觉/部分文档相反,以实测为准)。
  叠层必须在摄像头(content_<cam>, index 0)之上,故设为最大索引。原来手加时在 index 0(底)被摄像头盖住、没显示。
- 播放器窗口须**处于显示态**(pc-service `kshow`)且渲染**绿底**(纯绿 #00FF00),窗口捕获才有内容可抠。
- 叠层用的是竖屏 3:4 播放器窗口摆在 4:3 横屏画布上(顶部歌单/底部歌词),布局可在 OBS 里手动调 KTV悬浮 的位置/缩放。
"""
import obsws_python as obs

NAME = "KTV悬浮"
SCENES = ["cam1", "cam2", "cam3"]
CW, CH = 1440, 1080          # 画布
# karaoke-player 窗口捕获源尺寸(720×960 @175% DPI = 1260×1680)。换 DPI/窗口尺寸要重量这几个值。
SW, SH = 1260, 1680
# 窗口内各元素竖直位置(实测,源像素):歌单 606~658 / 歌词+音准 1382~1565 / 底部状态栏 1603~1668。
SETLIST_TOP = 606            # 歌单顶(作上锚点)
LYRICS_BOTTOM = 1565         # 歌词底(作下锚点);状态栏 1603+ 被裁掉
GAP = 45                     # 画布上下留白(歌单顶间隙 == 歌词底间隙,对称)
cl = obs.ReqClient(host="localhost", port=4455, password="", timeout=5)

# 1) 绿幕色键(挂在源上,三场景通用)
if NAME not in {i["inputName"] for i in cl.get_input_list().inputs}:
    raise SystemExit(f"OBS 里没有名为 {NAME} 的源;请先在 OBS 加『窗口捕获』抓 karaoke-player 窗口并命名 {NAME}")
if "绿幕抠图" not in [f["filterName"] for f in cl.get_source_filter_list(NAME).filters]:
    cl.create_source_filter(NAME, "绿幕抠图", "chroma_key_filter_v2",
                            {"key_color_type": "green", "similarity": 400, "smoothness": 80, "spill": 100})
    print("已加绿幕色键滤镜")
else:
    print("绿幕色键已存在")

# 2) KTV 布局:裁到 [歌单顶, 歌词底](切掉底部状态栏+多余绿边),等比缩放并上下留等量间隙 GAP、水平居中
cropTop, cropBottom = SETLIST_TOP, SH - LYRICS_BOTTOM
vis_h = SH - cropTop - cropBottom
sc_ = (CH - 2 * GAP) / vis_h            # 内容高度 = 画布高 - 上下各 GAP
tr = {"cropTop": cropTop, "cropBottom": cropBottom, "cropLeft": 0, "cropRight": 0,
      "scaleX": sc_, "scaleY": sc_, "positionX": (CW - SW * sc_) / 2, "positionY": float(GAP),
      "rotation": 0.0, "alignment": 5}

# 3) 同步到三场景 + 置顶(叠层盖在摄像头之上)
for sc in SCENES:
    items = [it["sourceName"] for it in cl.get_scene_item_list(sc).scene_items]
    if NAME not in items:
        cl.create_scene_item(sc, NAME, True)
    iid = cl.get_scene_item_id(sc, NAME).scene_item_id
    cl.set_scene_item_transform(sc, iid, dict(tr))
    n = len(cl.get_scene_item_list(sc).scene_items)
    cl.set_scene_item_index(sc, iid, n - 1)   # 顶层
    print(f"{sc}: KTV悬浮 已同步布局 + 置顶")
print(f"完成(cropTop={CROP_TOP} cropBottom={CROP_BOTTOM} scale={sc_:.3f})。"
      "确保 karaoke-player 显示(kshow)且绿底,即可看到歌单在顶/歌词在底浮于三机位画面上。")
