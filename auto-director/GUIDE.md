# 自动切镜 + 伴奏驱动运镜 · 系统指南

> 本文是 `auto-director` 的**完整系统说明**:原理、运行、参考视频依据、**移动机位后如何重新校准**、
> 参数速查、调试踩坑。文件级速览见 [README.md](README.md);需求背景见根目录《直播多机位与自动切镜方案》
> 与 [../KARAOKE_SYSTEM.md](../KARAOKE_SYSTEM.md)。
>
> **常用运维已封装成 skill**(`.claude/skills/`,直接触发不必翻本文):
> `wire-camera`(换/调机位后重接+校准画布)、`ktv-overlay`(KTV 字幕接入/布局)、`troubleshoot-live`(直播故障排查)。
> 本文是这些 skill 背后的原理/参数依据。

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
- `director.py` —— 核心:状态机 + 镜头编排 + 运镜 + YuNet 头部跟踪,驱动 OBS。参数不再散在文件顶部,统一走配置系统(见 §7)。
- `config_schema.py` —— **配置的单一事实源**:所有切镜/运镜参数的默认值/类型/范围/中文标签/分组 + 校验规则(见 §7)。
- `director_config.json` —— **可编辑的配置数据**(首次运行由 schema 默认生成、gitignore、支持热重载)。
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
  `trackLR` 主体从右滑到左 / **`closeup` 动态特写**(见下)。比例按段落:主歌少动、副歌多动、间奏缓动。
- **动态特写 `closeup`(1/2 机位·主歌副歌高频)**:普通 `close/xclose` 是**固定** z(1.3/1.75),人一离远头占比就上不去、不像特写;
  `closeup` 改为**按 YuNet 实测脸框(`CAM_FACE_H`)动态放大到"头占~1/2"**(`CLOSEUP_HEAD=0.5`,xclose `CLOSEUP_XHEAD=0.62`),
  并复用 hero 三件套:**锁脸慢跟随(`CLOSEUP_FOLLOW_ALPHA`)+ 拟人轻晃(`CLOSEUP_SWAY_AMP`,比 hero 更轻)+ 轻推或拉
  (`CLOSEUP_PUSH`,整个停留内缓动到目标)**。只在 `CLOSEUP_CAMS`(cam1/cam2)生效,cam3 抽中退静态(远机高倍糊)。
  `start_closeup`/`tick_closeup` 与 hero 同源,只是幅度小、时长=普通停留、不设两阶段。
- **横移 `trackpan`(1/2 机位·双向)**:人像从画面一侧**拟人横扫到另一侧**(输出锚点 `ax` 在 `CLOSEUP_PAN_AX=(0.28,0.72)`
  两端间缓动,方向每次随机),复用 closeup 的锁脸慢跟随 + 拟人轻晃。两档:`close/xclose`=**放大横移(头占~½)**、
  `loose/full`=**正常景别横移**(z 保底 `CLOSEUP_PAN_ZMIN` 留横向余量)。16:9 源进 4:3 画布横向本就有富余,**全程不露黑边**
  (实测 216 帧 0 触边)。与 closeup 同一入口 `start_closeup(...,pan=True)`。取代旧单向 `trackLR`(后者留给间奏/前奏广角横移)。
- **默认主镜纯跟随 `follow`(唱完回主机待机用)**:`PAUSE` 调色板即 `[["loose","follow",1]]` —— 回到主机 `cam1` 后
  **正常景别(loose)+ 只锁脸慢跟随、不推拉不横移**(`start_closeup(...,still=True)`:z 起=止、ax 居中)。刻意"纯跟随"
  是因为待机时 `not playing`、`now_t()` 冻结,推拉/晃动会卡在起点;而 `cx/cy` 的低通跟随不依赖时钟,每帧照跟,
  所以待机主镜**稳定不动 z、人一动画面轻轻跟上**。跟随强度同款用 `CLOSEUP_FOLLOW_ALPHA`。
- **停留时长**:`HOLD_RANGE` 每段随机抽,`LINGER_PROB=0.18` 概率"多停留一会儿"(节奏不均匀)。
- **段落切换即切**(`force_cut`);**前奏/间奏也持续切+运镜**(不再一次定住)。

**招牌长运镜(hero,主机专属 ~18s)**:两阶段长镜(`start_hero`/`tick_hero`,参数化生成 framing、接管普通插值)。
**触发**分两路:①**保证触发**(`HERO_GUARANTEE`,`plan_hero`)——每首歌载入即规划一个目标时刻(演唱区间内 `[开口+HERO_EDGE_HEAD,
歌尾−(hero时长+HERO_EDGE_TAIL)]` 随机,前半后半都可能),主循环到点且正在 `VERSE/CHORUS` 就强制切主机跑一次;**到点若逢
`INTERLUDE` 则条件不满足、顺延到下一句**(不卡间奏);②**额外机会**——切到主机·主歌/副歌·冷却 `HERO_COOLDOWN` 过时,再按
`HERO_PROB` 概率来(且未达每首上限 `HERO_MAX_PER_SONG`＝2、当前段块没用过——**每段最多一次、每首最多两次**)。
`seg_idx` 每次状态切换递增标识段块,`hero_seg` 记住上次触发的段块;`do_cut` 里统一起镜并计数:
- **阶段1 慢推(15s)**:z 从 `1.0`(最远、铺满画布不黑边)`smoothstep` 缓推到"**人头占画面高 ≈ `HERO_HEAD_NEAR`(3/4)**"
  的超特写,眼中点由 `ay=0.50` 升到 `0.36`(头靠上留白);全程叠加**两正弦左右轻晃**(`HERO_SWAY_AMP`,仿真人手持,
  两端 `sway_env` 渐入渐出)。
- **阶段2 速拉(3s)**:`outCubic` 后撤到"**头占 < 1/2**"(`HERO_HEAD_END=0.45`)的中景收尾,摇晃渐隐。
- **关注点慢速跟随**:`cx/cy`(对准哪)起镜读一次真人头位,之后每帧向实时头位**低通跟随**(`HERO_FOLLOW_ALPHA≈0.03`,
  时间常数≈1s)。两头都不取:**不逐帧硬跟**——放大 2~3× 下逐帧跟跳动的跟踪值会把 `posX=ax·CW−cx·sws` 放大成剧烈抖动
  (实测硬跟 cx 每帧跳 0.06 = 横移几百像素);**也不完全锁死**——那样直播中人一移动就对不准、走出框。低通滤掉 YuNet
  0.5s 阶跃与检测噪声(逐帧跳变实测 <0.001),只平滑跟上人的缓慢移动。`cx/cy` 钳到 `[0.30,0.70]`。
- **头占比 → z 换算**(`_z_for_head`):假设 16:9 源等比铺满 4:3 画布(高度受限,`base·sh=CH`)⇒ `输出头高比 = 头源高比 × z`,
  故 `z = 目标比 / (HERO_HEAD_FACTOR × 脸框高)`;脸框高由 YuNet 实测写入 `CAM_FACE_H`,**先钳到 `HERO_FACEH_CLAMP`**
  (防误检的极小框——占位图上曾测到 0.09——把 z 甩到上限);`z_near` 夹在 `[HERO_Z_NEAR_MIN, HERO_Z_MAX]`,
  `z_end ≤ z_near × HERO_PULL_RATIO`(保证阶段2 后拉幅度肉眼明显,不被检测误差挤没)。
  换镜/`reset_director` 会清 `RT["hero"]`,段落突变的强制切会打断它。
- **观察/调参**:`hero_test.py` 单独循环跑这一个镜头(`python hero_test.py --no-track --gap 4`),不走状态机、不需放歌。
  占位图上 YuNet 检测不可靠 → 观察手感用 `--no-track`(静态人脸位);接真机位有清晰大脸后再开跟踪。

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

## 6. 参数调节速查(想改效果 → 改哪个)

> **参数已统一进配置系统(见 §7)**:改 `director_config.json` 存盘即**热重载生效**,不用动源码、不用重启。
> 下表沿用大家熟悉的旧常量名,与 JSON 组的对应:`HOLD_RANGE→hold_range`、`SHOT_PALETTE→palette`、
> `STATE_CAMS→state_cams`、`CAM_PRESETS→cameras.list.<cam>.presets`、`HERO_*→hero.*`、`CLOSEUP_*→closeup.*`、
> `其余散量→rhythm/tracking/connection`。`config_schema.py` 是默认值/范围/中文标签的事实源。

| 你想要 | 改什么(JSON 键 · 所在组) |
|---|---|
| 切镜更勤 / 更慢 | `hold_range` 各段 [min,max] 秒;整体调小=更勤 |
| 节奏更不均匀 / 更规律 | `LINGER_PROB`(多停留概率) |
| 运镜更多 / 更少 | `SHOT_PALETTE` 里各段 `static` 与运镜条目的权重比 |
| 某台机位用更多 / 更少 | `STATE_CAMS` 各段机位权重 |
| 广角呼吸更频繁 / 更少 | `WIDE_BREATHER`(秒) |
| 某台可做的景别(特写/三分/超特写) | `CAM_PRESETS`(每台允许的 preset 集合) |
| 日常特写头占多大 | `CLOSEUP_HEAD`(现0.5≈头占半屏)、`CLOSEUP_XHEAD`(现0.62);z 上限 `CLOSEUP_Z_MAX` |
| 日常特写推拉/晃动幅度 | `CLOSEUP_PUSH`(推拉 z 幅度)、`CLOSEUP_SWAY_AMP`(拟人晃) |
| 日常特写用哪些机位/多频繁 | `CLOSEUP_CAMS`(现 cam1/cam2);频率=`SHOT_PALETTE` VERSE/CHORUS 里 `closeup` 项权重 |
| 横移幅度/方向 | `CLOSEUP_PAN_AX`(左右端点,方向每次随机);正常横移最小 z `CLOSEUP_PAN_ZMIN` |
| 横移出现频率 | `SHOT_PALETTE` VERSE/CHORUS 里 `trackpan` 项权重(现 close/loose 两档) |
| 推/拉幅度更大更狠 | `apply_shot` 里 push 的 `target.z-0.3`、pull 的 `target.z+0.32` |
| 超特写更紧 / 更松 | `framing_for` 的 `xclose` z(现 1.75;更清需相机上 1080p) |
| 特写头顶留白多少 | `framing_for` 里 close/xclose/thirds 的 `ay`(越小头越靠上) |
| 右→左移镜速度/幅度 | `apply_shot` 里 `trackLR` 的 `dur` 与 `ax` 起止(0.63→0.37) |
| 每首歌保证来一次 hero / 关掉保证 | `HERO_GUARANTEE`(True=必触发一次);触发窗口避头/避尾 `HERO_EDGE_HEAD`/`HERO_EDGE_TAIL` |
| 保证之外的额外 hero 频率 | `HERO_PROB`(概率)、`HERO_COOLDOWN`(最小间隔秒) |
| 一首歌最多几次 hero | `HERO_MAX_PER_SONG`(现 2:保证 1 + 额外 1);每段块最多一次由 `seg_idx`/`hero_seg` 保证 |
| hero 跟人移动的快慢/稳抖 | `HERO_FOLLOW_ALPHA`(大=跟得快但易抖,小=稳但跟得慢;0=锁死,→1=逐帧硬跟) |
| hero 慢推/速拉时长 | `HERO_PUSH_DUR`(现 15s)、`HERO_PULL_DUR`(现 3s) |
| hero 推到多近 / 拉回多松 | `HERO_HEAD_NEAR`(头占比,现 0.75)、`HERO_HEAD_END`(现 0.45) |
| hero 摇晃更明显/更稳 | `HERO_SWAY_AMP`(幅度)、`HERO_SWAY_PERIOD`(周期) |
| hero 后拉幅度(阶段2 缩多少) | `HERO_PULL_RATIO`(收尾 z ≤ 近景 z ×此值,越小拉得越开) |
| hero 头占比算不准(推太狠/不够) | 校 `HERO_HEAD_FACTOR`(头/脸框比)或 `CAM_FACE_H` 回退值;`z` 上下限 `HERO_Z_MAX`/`HERO_Z_NEAR_MIN`;误检钳位 `HERO_FACEH_CLAMP` |
| hero 抖动 / 关注点乱跳 | 关注点已起镜锁定;仍偏可调起点钳区间或先 `--no-track` 看纯几何 |
| 单独循环看 hero 效果 | `python hero_test.py --no-track --gap 4`(`--once` 只跑一遍) |
| 关掉招牌长推 | `HERO_ENABLE=False` |
| 副歌触发 1:3 前推 | 给歌 `meta.json` 标 `chorus` 区间(见下)+ 提副歌切换频率 |
| 头部跟踪灵敏度 | `TRACK_SMOOTH`(越大越跟手)、`TRACK_INTERVAL`(检测周期) |
| 关掉自动跟踪 | `TRACK_FACE=False`(退回静态 `CAM_FACE`) |
| 只切镜不运镜 | `enable_moves`(rhythm) |
| 关闭自动切镜后主镜跟随快慢 | `manual.follow_alpha`(同 closeup) |
| 手动放大跟滑块的平滑/上限 | `manual.zoom_lowpass`(越小越顺)、`manual.z_max`(cam_zoom→z 护栏) |
| 手动主镜头顶留白 | `manual.ay` |

**副歌标注**:给曲库 `<mid>/meta.json` 加 `"chorus": [[起ms,止ms], ...]`,pc-service 的 `/song/{mid}/karaoke`
会带出来,director 据此进 CHORUS 段(触发前推/更紧景别/切得更勤)。未标注则那段按主歌切。

---

## 7. 配置系统(集中管理·校验·热重载)

所有切镜/运镜参数不再散在源码里,统一走三件套:

- **`config_schema.py`(单一事实源)**:每个参数的 `default / type / min / max / unit / group / label(中文) / desc`
  + 跨字段规则。既用于校验,也供未来 UI 自动渲染控件。
- **`director_config.json`(数据·gitignore)**:首次运行由 schema 默认生成(== 迁移前原值,行为不变);
  自己或未来 UI 编辑这一份。
- **`director.py` 的 `load_config/_apply_cfg/maybe_reload_config`**:启动读 JSON→校验→灌进全局常量;
  主循环每 ~1s 按 mtime 热重载。`import director` 时先用默认值填满全局(无文件 I/O),故工具/测试也能直接用。

**分组(11 组)**：`rhythm`(节奏/切点/呼吸)、`cameras`(场景名/角度/人脸位/可用景别/主机/特写机位)、
`state_cams`(每状态机位权重)、`hold_range`(每状态停留区间)、`palette`(每状态镜头调色板)、
`framing`(每景别 z/cx/cy/ax/ay,cx/cy 用 `"face"` 表示锁实时人脸)、`moves`(push/pull/pan/drift/trackLR 的幅度/时长/缓动)、
`closeup`(动态特写+横移)、`hero`(招牌长镜)、`manual`(关闭自动切镜后的手动主镜跟随+放大平滑)、
`tracking`(YuNet)、`connection`(pc-service/OBS)。

**校验/约束(坏配置绝不搞崩 director)**：类型强制转换、标量按 min/max **钳位**、非法枚举(未知景别/运镜/机位/状态)
**丢弃并告警**、负权重清 0、区间 lo>hi 交换、**跨字段**纠正(如 `hero.head_end≥head_near`、`closeup.z_min≥z_max` 自动下调)。
坏 JSON / 缺字段 → 用默认、只打印 `[CFG] ⚠` 告警。

**热重载 vs 需重启**：`rhythm/state_cams/hold_range/palette/framing/moves/closeup/hero` + `tracking` 的检测周期/平滑/截图尺寸
= **改 JSON 即时生效**(下一个镜头就用新值);`connection`(OBS/pc-service 地址)、`tracking.track_face/model_path/det_*`
(连接与检测器已建立)= **改后需重启 director**(schema 里标 `live=False`)。

**为什么是 mtime 轮询而非 IPC**:pc-service 用 `Popen(..., stdin=DEVNULL)` fire-and-forget 拉起 director,
且 director 是反向连去 pc-service 的 WS 客户端(只拉只读态),没有从 pc-service 推入运行中 director 的通道。
让 director 读自己的 JSON、谁写都行,不引入新 IPC,也不破坏 director "可整体删除"的独立性。

**后期可视化**:任何编辑器只要把 JSON **原子写回**(temp + `os.replace`),director 就会热重载。两条现成落点:
pc-service 的 tkinter 托盘窗加"运镜参数"面板;或手机遥控页加 `cmd:"director_config"` 让 pc-service 代写 JSON。
schema 自带 type/range/label/group,控件可自动生成。

---

## 8. 运行手册(怎么把整套跑起来)

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

### 由 App/pc-service 开关 —— director 常驻 · 开关=模式切换(2026-07-18 改)
安卓遥控页「自动切镜运镜」开关 → WS `director {on}`。**director 由 pc-service 托管且常驻(不再随开关启停进程)**,
开关只切它的**模式**(`director_on`/`cam_zoom` 随 STATE 广播下发,director 作为 WS 客户端消费):
- **开 = 自动编排模式**:状态机 + 调色板 + hero/closeup/trackpan 自动切镜运镜(全程 director 独占 cam1)。
- **关 = 手动主镜模式**(`manual_tick`):锁 cam1 + **人脸跟随(同待机 follow 内核)** + 按 `cam_zoom` 放大
  (`z = cam_zoom/100`,夹到 `manual.z_max`;`manual.zoom_lowpass` 平滑滑块)。**唱完待机 / 关闭自动切镜 效果一致,都跟脸。**

pc-service 侧(`server.py`):`set_director` 现在**总是 `_start_director()`(幂等常驻)+ `_obs_cut_main()` 基线切场景 + 广播**,
不再 `_stop_director`/`_obs_zoom_main`(cam1 的变换/放大/跟随全交给常驻 director,**单写者、不打架**);`cam_zoom` 处理只存值 + 广播
(director 应用)。`_stop_director` 仅留 atexit 防孤儿。App 的放大滑块仍只在关闭自动切镜时可用(`enabled=!directorOn`),**App 无需改**。
> 限制:director 常驻后若自身崩溃,pc-service 无 watchdog 自动拉起,重开开关即触发 `_start_director()` 恢复。

---

## 9. 调试踩坑记录(值得记住的点)

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
16. **两个进程同抢一个机位变换 = 狂闪抖动**:单独跑 `hero_test.py`(或任何直连 OBS 的调试脚本)时,若 pc-service
    托管的正式 `director.py` 还开着,**两路 30Hz 都在 `set_scene_item_transform(cam1)`**,画面在两套 framing 间
    每帧横跳 → 剧烈闪动。排查时先 `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` 看命令行,
    确认只有一个进程在控 cam1。**跑 hero_test 前先关正式 director**(遥控页开关 off,或 WS `director {on:false}`——
    它还会顺带回主机),测完再开。**教训:OBS 变换是共享可变状态,同一时刻只能有一个控制者。**
17. **hero 长运镜别逐帧跟跟踪值**:放大 2~3× 时 `posX=ax·CW−cx·sws`,`cx` 每帧跳 0.06 就是画面横移几百像素;
    `tick_hero` 若每帧读 `CAM_FACE_LIVE`(YuNet 0.5s 阶跃 + 占位图误检游走)→ 抖到不能看。已改**起镜对准 + 慢速低通跟随
    `cx/cy`**(`HERO_FOLLOW_ALPHA`):滤掉阶跃/噪声不抖,又能跟上直播中人的轻微移动——**完全锁死不行**(人一动就对不准/出框),
    **逐帧硬跟也不行**(抖),低通跟随是唯一解。同理脸框高误检(占位图上测到 0.09)会把 z 换算甩到上限致阶段2 拉不回,
    已加 `HERO_FACEH_CLAMP` 钳位 + `HERO_PULL_RATIO` 保底后拉。

---

## 9. 待办 / 可选进阶

- [ ] **镜头内持续跟随**:现在每次切镜锁定头位(切之间跟随);可加"特写保持期间实时跟着头微调",长特写里走动也居中。
- [ ] **相机上 1080p**:Camo/iVCam 现 720p,超特写偏软;两台提到 1080p 更锐。
- [ ] **副歌标注 UI**:在曲库管理页给歌标 `chorus` 区间(写 meta.json),现可手动编辑验证。
- [ ] **反向左→右移镜 / 更多运镜类型**按需加。
- [ ] 多机延迟对齐(给快的相机加渲染延迟滤镜)、三机白平衡/曝光锁死。

---

*本指南随系统演进更新。改了 director 参数/逻辑,记得同步本文与 [README.md](README.md)。*
