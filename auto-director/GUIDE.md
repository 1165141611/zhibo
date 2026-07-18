# 自动切镜 + 伴奏驱动运镜 · 系统指南

> 本文是 `auto-director` 的**完整系统说明**:原理、运行、参考视频依据、**移动机位后如何重新校准**、
> 参数速查、调试踩坑。文件级速览见 [README.md](README.md);需求背景见根目录《直播多机位与自动切镜方案》
> 与 [../KARAOKE_SYSTEM.md](../KARAOKE_SYSTEM.md)。

---

## 1. 这套系统是什么

用**多机位 + 数据驱动的自动切镜/运镜**替代单机位数字放大跟随,做接近赵雷 Live 的多角度 KTV 感。
真实三机位现状:

| 机位 | 设备(1号/2号/3号手机,全走 iVCam) | OBS 里的设备名 | 角色 |
|---|---|---|---|
| **cam1 主** | 1号手机(**先连**) | `e2eSoft iVCam #1` | 正前偏左·中景,大本营;可到贴脸超特写 |
| **cam2 侧辅** | 2号手机(**后连**) | `e2eSoft iVCam #2` | 侧面·近景/情绪(低角度仰拍) |
| **cam3 侧高** | 3号手机(**最后连**) | `e2eSoft iVCam #3` | 高远·广角"呼吸",可前推/后撤 |

> ⚠️ **iVCam 后缀 `#N` 按连接顺序分配,不是设备固定属性**。接 3 台时依次是 `#1`/`#2`/`#3`(接 1~2 台时首台可能无后缀)。
> 所以每次开机务必**固定顺序连:1号→2号→3号**,否则 `#N` 会错位、机位映射乱掉。跟声卡索引漂移是同类坑(见 §8)。
> 实测 iVCam 分辨率 1080p~1440p(比 UVC 的 720p 清晰)。

### 数据流(合规版)

```
karaoke-player(绿幕字幕/精确时钟) --STATE 每500ms--> pc-service(中枢)
        │                                              │ ws://:8765/ws  +  /song/{mid}/karaoke
        │(真实歌曲进度/逐字/音高)                        ▼
        │                                    auto-director/director.py
        │                                    (状态机+编排+YuNet头部跟踪)
        │                                              │ obs-websocket:4455
        ▼                                              ▼
   三台真实相机 ──USB/WiFi──> OBS(合成:切场景+运镜变换) ──虚拟摄像机──> 直播伴侣 ──> 抖音
```

- **抖音不允许 OBS 直接 RTMP 推流**(未备案封号)。所以 OBS **不推流**,用「启动虚拟摄像机」输出一路合成画面,
  直播伴侣把它当一个「摄像头」源,官方推流(合规)。直播伴侣里只有这一个源,**不用切场景**——所有切镜/运镜都在 OBS 里做。
- director **只读**消费 pc-service(WS 进度 + HTTP 逐字/音高),**不碰播放器、不碰 pc-service 核心**,对播放器零开销。

---

## 2. 文件与 OBS 约定

**auto-director/ 目录:**
- `director.py` —— 核心:状态机 + 镜头编排 + 运镜 + YuNet 头部跟踪,驱动 OBS。配置全在文件顶部。
- `wire_camera.py` —— 把某机位场景的源换成真实摄像头(保持源名 `content_<cam>`)。
- `obs_setup.py` —— 一键搭建占位测试环境(生成机位底图 + 建 cam1/2/3 场景)。
- `ktv_overlay.py` —— **KTV 歌词叠层接入**:给 `KTV悬浮`(karaoke-player 绿幕窗口捕获)加绿幕色键 + 同步到三场景并置顶(见 §3.5)。
- `director-sim.html` —— 纯网页导播台模拟器(假机位 SVG,不接 OBS,验证逻辑用)。
- `models/yunet.onnx` —— YuNet 人脸检测模型(OpenCV 官方,已 gitignore,可再下)。
- `requirements.txt` —— `websocket-client + obsws-python + opencv-python-headless + numpy`。

**OBS 里必须的约定(director 靠这个工作):**
- 三个**场景**命名 `cam1` / `cam2` / `cam3`。
- 每个场景里放**一个源**,命名 `content_cam1` / `content_cam2` / `content_cam3`(占位图或真实摄像头都行)。
- 画布(设置→视频)= **4:3 横屏 `1440×1080`**(直播用 4:3)。
- 工具→WebSocket 服务器设置:启用,端口 4455,本地测试可关身份验证(director 顶部 `OBS_PASSWORD=""`)。

---

## 3. 编排逻辑(切镜怎么切、运镜怎么运)

### 3.1 歌曲状态机(消费真实逐字轴)
`current_state(t)` 按真实播放进度判定段落:
- **PAUSE** 没在播 → 回主机静止
- **INTRO** 第一个字之前(某些歌前奏很长,如吉姆餐厅 68s)
- **OUTRO** 最后一个字之后
- **CHORUS** 落在手动标注的 `chorus` 区间(未标注则不触发)
- **INTERLUDE** 不在任何字内、且前后字间隔 > `INTERLUDE_GAP`(4s)——排除长音字中间
- **VERSE** 其余

### 3.2 镜头调色板:每次切镜 = 机位 × 景别 × 运镜 × 停留时长
参考片"丰富靠变化非多切"(见 §4)。我们 3 机位无乐队可切,故用**数字景别派生多镜头 + 随机运镜 + 变化节奏**:

- **机位**(`STATE_CAMS` 加权、不连切同机、每 `WIDE_BREATHER=45s` 强制广角呼吸)
- **景别 preset**:`full` 满景 / `loose` 松景(z1.12) / `high` 略仰 / `close` 特写(z1.3) /
  `xclose` 贴脸超特写(z1.75,仅 cam1) / `thirds_l·thirds_r` 左右三分(z1.5)。
  每台可用景别由 `CAM_PRESETS` 限制:**cam3 大疆远,只做 full/loose/high/close**(不做 thirds/xclose,高倍放大糊)。
- **运镜 move**:`static` 静止 / `push` 缓推 / `pull` 缓拉后撤 / `pan` 平移 / `drift` 极慢漂移 /
  `trackLR` 主体从右滑到左。比例按段落:主歌少动、副歌多动、间奏缓动。
- **停留时长**:`HOLD_RANGE` 每段随机抽,`LINGER_PROB=0.18` 概率"多停留一会儿"(节奏不均匀)。
- **段落切换即切**(`force_cut`);**前奏/间奏也持续切+运镜**(不再一次定住)。

### 3.3 头部实时跟踪(YuNet)——特写精准锁人
后台线程用 `cv2.FaceDetectorYN`(`models/yunet.onnx`)每 `TRACK_INTERVAL=0.5s` 抓三台画面检测头部,
EMA(`TRACK_SMOOTH`)平滑后写 `CAM_FACE_LIVE`(两眼中点)。特写/三分**每次切镜读它锁定最新头位并跟人移动**。
`close/xclose/thirds` 的 `ay≈0.4` 给足头顶留白(眼睛落上三分)。`TRACK_FACE=False` 可关,退回静态 `CAM_FACE`。
> 相比 OBS 人物跟踪插件:插件会和我们的运镜抢同一个变换,自研跟踪只提供"对准哪"的坐标,所有设计运镜全保留、不冲突。

### 3.4 两条底层保障
- **防变形(cover 缩放)**:真相机多 16:9,画布 4:3;按 X/Y 分别缩放会压扁,故**等比铺满+裁多余边**(`set_framing`)。
- **防黑边(位置钳制)**:放大/平移后画面位置夹到"仍盖满画布"的区间,任何运镜都不露黑边;
  1:3 前推要把脸推到三分且不露边,需 `z≥(1-ax)/(1-cx)≈1.5`(故 thirds z=1.5)。

### 3.5 KTV 歌词叠层(`KTV悬浮`)
每个场景里除了摄像头源 `content_<cam>`,还有一层 **`KTV悬浮`** = **karaoke-player 绿幕歌词窗口的窗口捕获**
(顶部滚动歌单 + 底部逐字歌词/音准)。约定:
- **一个源、三场景共用**:`KTV悬浮` 加进 cam1/cam2/cam3 三个场景(同一输入),**置于摄像头之上**(顶层),
  这样不管 director 切到哪台,歌词始终浮在画面上。
- **绿幕色键**:滤镜「绿幕抠图」(`chroma_key_filter_v2`, green)挂在**源**上 → 三场景全生效,抠掉纯绿 `#00FF00` 只留字。
- **director 不碰它**:director 只变换 `content_<cam>`;`KTV悬浮` 固定不动,所以摄像头推拉/切换时歌词稳定不跟着缩放(正确)。
- **前置**:karaoke-player 窗口须**显示态**(pc-service `kshow`)且**绿底**,窗口捕获才有内容;隐藏/透明底则捕获不到。
- **图层顺序坑**:obs-websocket 的 `sceneItemIndex` **0=底层、越大越上层**;叠层必须比摄像头 index 大才盖在上面。
- **一键接入/恢复**:`python ktv_overlay.py`(重建场景后跑一次即可)。
- **布局(KTV 款)**:`ktv_overlay.py` 裁成"歌单在顶、歌词在底、状态栏切掉、上下等间隙、水平居中":
  源 1260×1680(720×960 @175%DPI),元素位置——歌单 y606~658 / 歌词+音准 y1382~1565 / 底部状态栏 y1603~1668。
  以 `SETLIST_TOP=606`(上锚)、`LYRICS_BOTTOM=1565`(下锚)裁剪(状态栏 1603+ 切掉),缩放后**上下各留 `GAP=45px`**
  (歌单顶间隙 == 歌词底间隙,对称)、水平居中。换 DPI/窗口尺寸要重量这几个坐标;微调改 `SETLIST_TOP/LYRICS_BOTTOM/GAP` 重跑。

---

## 4. 参考视频分析(赵雷《Over》Live)——编排的依据

用 opencv 拆帧 + 帧差测切点/运动量,量化结果(312s,31 个镜头):
- **约 10 秒一切**(中位 8.8s),比直觉慢;**90% 是静止镜头**,运镜只 10%。
- **镜头时长跨度大**:2.8s ~ 18s,长短随情绪。

**关键认知**:它"生动不呆板"**不靠多运镜,靠景别/角度/节奏的巨大变化**——主唱正面/左侧脸/右侧脸/仰拍大特写/中景多种镜头 +
大量切乐手 + 细节插入 + 每 40~60s 一个大全景"呼吸"。运镜只在**情绪顶点**用。

**对我们的指导(3 机位、无乐队可切):**
- 用**数字景别**把 3 台派生成十几种镜头(满/松/特写/超特写/左右三分/略仰),替代它的物理多角度。
- **节奏**:主歌中速(5-9s)、副歌快(3-6s)、间奏长(6-11s),偶尔多停留;整体略比参考快一点(用户偏好更生动)。
- **运镜当标点 + 少量日常**:开场推/尾奏拉/间奏平移/副歌前推是标点;日常 static 为主,穿插 drift/push 提生动。
- **技巧移植**:广角呼吸(cam3 每 ~45s)、切点吸附乐句(主副歌切在下一个字起)、贴脸情绪超特写、右→左移镜、大疆推拉。

---

## 5. 【重点】下次移动机位后,怎么重新校准

**只要保持 OBS 里三个场景各有一个 `content_<cam>` 源,director 代码不用改。** 按需做以下:

### A. 换了设备 / 设备名变了 → 用 wire_camera.py 重接
**先固定顺序连手机**:iPhone 先连 iVCam(→`e2eSoft iVCam`),安卓后连(→`e2eSoft iVCam #2`)。然后:
```bash
cd auto-director
python wire_camera.py            # 先列出当前所有摄像头设备名(核对 #1/#2/#3 对没对)
python wire_camera.py cam1 "#1"  # 1号手机 → cam1
python wire_camera.py cam2 "#2"  # 2号手机 → cam2
python wire_camera.py cam3 "#3"  # 3号手机 → cam3(若用大疆则 python wire_camera.py cam3 OsmoAction)
```
**存在就改设备、不存在才新建**(避开 obs-websocket 删除异步的竞态),自动**等比铺满 4:3 画布**(16:9 不变形)、切到该场景。
参数 2 = 设备名片段:精确名优先,子串多命中取最短名;用 `"#1"`/`"#2"`/`"#3"` 精确指定第 N 台。

### B. 只是挪动了机位/改了构图 → 头部跟踪会自动适应
`CAM_FACE_LIVE` 由 YuNet 每 0.5s 实时更新,**摆好机位、人站到唱歌位,特写会自动跟着新头位**,一般不用手动改。
- 想更新**静态回退值**(跟踪未就绪时用):改 director.py 顶部 `CAM_FACE`(眼中点归一化坐标)。
- 想快速核对当前检测:临时截图跑一次 YuNet(见 §8 的思路)看眼中点坐标。

### C. 机位远近变了,影响可用景别 → 调 CAM_PRESETS
- 若某台**离人更远了**(像 cam3),就别让它做 thirds/xclose(放大糊)——从它的 `CAM_PRESETS` 集合里去掉。
- 若某台**离人更近了**,可以给它加 close/thirds/xclose。

### D. 声卡/相机插拔后播放器没声 → 已自动按名解析(见 §8),一般无需处理
若仍异常:pc-service 的 `PLAYER_DEVICE_NAME`(默认 `PLAYBACK 1/2`)+ 回退 `PLAYER_DEVICE`。

### E. 校准后重启 director
```bash
python director.py        # 接真实播放器(需 pc-service + 播放器在放歌)
```

---

## 6. 参数调节速查(想改效果 → 改哪个,均在 director.py 顶部)

| 你想要 | 改什么 |
|---|---|
| 切镜更勤 / 更慢 | `HOLD_RANGE` 各段 (min,max) 秒;整体调小=更勤 |
| 节奏更不均匀 / 更规律 | `LINGER_PROB`(多停留概率) |
| 运镜更多 / 更少 | `SHOT_PALETTE` 里各段 `static` 与运镜条目的权重比 |
| 某台机位用更多 / 更少 | `STATE_CAMS` 各段机位权重 |
| 广角呼吸更频繁 / 更少 | `WIDE_BREATHER`(秒) |
| 某台可做的景别(特写/三分/超特写) | `CAM_PRESETS`(每台允许的 preset 集合) |
| 推/拉幅度更大更狠 | `apply_shot` 里 push 的 `target.z-0.3`、pull 的 `target.z+0.32` |
| 超特写更紧 / 更松 | `framing_for` 的 `xclose` z(现 1.75;更清需相机上 1080p) |
| 特写头顶留白多少 | `framing_for` 里 close/xclose/thirds 的 `ay`(越小头越靠上) |
| 右→左移镜速度/幅度 | `apply_shot` 里 `trackLR` 的 `dur` 与 `ax` 起止(0.63→0.37) |
| 副歌触发 1:3 前推 | 给歌 `meta.json` 标 `chorus` 区间(见下)+ 提副歌切换频率 |
| 头部跟踪灵敏度 | `TRACK_SMOOTH`(越大越跟手)、`TRACK_INTERVAL`(检测周期) |
| 关掉自动跟踪 | `TRACK_FACE=False`(退回静态 `CAM_FACE`) |
| 只切镜不运镜 | `ENABLE_MOVES=False` |

**副歌标注**:给曲库 `<mid>/meta.json` 加 `"chorus": [[起ms,止ms], ...]`,pc-service 的 `/song/{mid}/karaoke`
会带出来,director 据此进 CHORUS 段(触发前推/更紧景别/切得更勤)。未标注则那段按主歌切。

---

## 7. 运行手册(怎么把整套跑起来)

```bash
# 1) 启动 pc-service(它会按名在正确声卡上拉起播放器)
cd live-remote/pc-service && python server.py

# 2) OBS 打开,确认三场景 cam1/2/3 各有 content_<cam> 源、WebSocket 已启用

# 3) 启动 director(真实模式)
cd auto-director && python director.py
#    python director.py --demo       # 无 pc-service,用内置演示曲驱动看效果
#    python director.py --dry-run    # 不连 OBS,只打印切镜决策

# 4) 点歌播放(手机/托盘),或用 WS 指令;OBS 里三机位就跟着歌自动切镜运镜
# 5) 真开播:OBS「启动虚拟摄像机」→ 直播伴侣加「摄像头」源选 OBS Virtual Camera → 官方推流
```
调试期保持某首歌循环:`scratchpad/keep_playing.py` 思路(快到结尾就 seek 回主歌、暂停就续播)。

### 由 App/pc-service 开关(2026-07-18)
现在 **director 由 pc-service 托管**,不必手动跑:安卓遥控页「自动切镜运镜」开关 → WS `director {on}` →
开=pc-service `Popen(python director.py)`;关=终止进程 + **立即切主机 cam1** + 应用主镜放大档位。
`cam_zoom {value:100~250}`(仅关闭时)= pc-service 直接 obs-websocket 居中数字放大 cam1(cover 不变形、防黑边)。
pc-service 侧见 `_start_director/_stop_director/set_director/_obs_cut_main/_obs_zoom_main`(server.py)、
`config.DIRECTOR_PATH/OBS_*/MAIN_CAM_*`。开着自动切镜时 App 的放大滑块禁用(director 独占 OBS,两者互斥不打架)。

---

## 8. 调试踩坑记录(值得记住的点)

1. **声卡设备索引会漂移**:播放器原按写死索引 `--device 27` 输出;接相机/ToDesk 虚拟音频会新增音频端点、
   挤动 WASAPI 枚举顺序,27 变成了 ToDesk Virtual Audio → **音乐灌进去听不到**。已修:pc-service
   `_resolve_player_device()` **按名解析**(WASAPI 下含 `PLAYBACK 1/2`),写死索引只当回退。**教训:别写死设备索引。**
2. **numpy float32 不能进 OBS 的 JSON**:YuNet 返回 float32,直接传 `set_scene_item_transform` 会
   `not JSON serializable`。所有坐标 `float()` 转 Python 浮点。
3. **长前奏的"假卡死"误诊**:吉姆餐厅前奏 68s,INTRO 期间不切镜,日志只在状态变化时打印 → 看起来"卡住"其实在正常等待。
   加了低频 `[心跳]` 日志区分"在工作 vs 崩了"。**别只看日志静止就判卡死。**
4. **16:9 相机进 4:3 画布会压扁**:必须**等比铺满+裁切(cover)**,不能按 X/Y 分别缩放。
5. **运镜露黑边**:放大/平移后要把位置**钳制**到覆盖区间;1:3 前推需 `z≥1.5` 才能既到三分又不露边。
6. **WS 线程别做阻塞网络请求**:换歌拉逐字轴(HTTP 最长 5s)必须放**独立线程**,否则卡住主循环、时钟漂。
7. **obs-websocket 30Hz 变换没问题**(压测 150 次/5s 无卡),运镜可放心 30Hz 插值,不需 Move 插件。
8. **抖音不许 OBS 推流**:改虚拟摄像机 → 直播伴侣官方推。
9. **DroidCam 需谷歌服务**(安卓),换成 **iVCam**(`e2eSoft iVCam`)。**iVCam 多机同名、后缀 `#2`/`#3` 按连接
   顺序分配**(不是设备固定属性)——务必固定顺序连(iPhone→安卓→第三台),否则映射错位。`wire_camera.py` 已改
   **精确名优先**、子串多命中取最短名,用 `"#2"`/`"#3"` 精确指定第 N 台。跟"声卡索引漂移"同类:**别依赖不稳定的枚举名/序**。
10. **头部跟踪 > OBS 插件**:插件与我们的变换抢控制;自研 YuNet 只给坐标,运镜全保留。
11. **cv2 5.0 无 CascadeClassifier**,但有 **FaceDetectorYN(YuNet)**,用它。
12. **占位图调运镜手感无意义**:必须接真机位才能判断,数字景别叠加物理角度差才是真正的多镜头。
13. **obs-websocket 删除是异步的**:`RemoveInput` 后立刻 `CreateInput` 同名会 601「已存在」,随后异步删除才生效→源丢失。
    `wire_camera.py` 已改**"存在就 SetInputSettings 改设备、不存在才 CreateInput"**,彻底避开删/建竞态。
14. **残留边界框(bounds)覆盖 scale**:某些源带 `boundsType=OBS_BOUNDS_SCALE_INNER`(OBS 自动适配留下的),
    它会**无视你设的 scaleX/Y**、按边界框内切摆放 → 16:9 进 4:3 出现黑边。所有设 framing 变换处必须显式带
    `boundsType=OBS_BOUNDS_NONE`(wire_camera 的 cover_framing、director 的 set_framing、pc-service 的 _obs_zoom_main 均已加)。
15. **pc-service 托管 director 必须设 UTF-8 环境**:`_start_director` 的 `Popen` 若不传 `PYTHONIOENCODING=utf-8`,
    Windows 默认 GBK,director `print("♪/→/中文")` 会 `UnicodeEncodeError` 崩主循环——**恰在第一次 `[切]` 打印,
    表现为"开自动切镜只切一下就不动"**(切镜动作在 print 前已执行,进程随即死;stderr 走 DEVNULL 看不到崩因)。
    与 `start_player` 同一个坑。托管任何有中文/符号输出的子进程都要设 UTF-8。

---

## 9. 待办 / 可选进阶

- [ ] **镜头内持续跟随**:现在每次切镜锁定头位(切之间跟随);可加"特写保持期间实时跟着头微调",长特写里走动也居中。
- [ ] **相机上 1080p**:Camo/iVCam 现 720p,超特写偏软;两台提到 1080p 更锐。
- [ ] **副歌标注 UI**:在曲库管理页给歌标 `chorus` 区间(写 meta.json),现可手动编辑验证。
- [ ] **反向左→右移镜 / 更多运镜类型**按需加。
- [ ] 多机延迟对齐(给快的相机加渲染延迟滤镜)、三机白平衡/曝光锁死。

---

*本指南随系统演进更新。改了 director 参数/逻辑,记得同步本文与 [README.md](README.md)。*
