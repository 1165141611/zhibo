# auto-director —— 自动切镜 / 运镜（原型）

> 📖 **完整系统指南见 [GUIDE.md](GUIDE.md)**：原理与数据流、参考视频分析、**移动机位后如何重新校准**、
> 参数调节速查、运行手册、调试踩坑记录。本 README 是文件级速览,细节看 GUIDE。

多机位 + 数据驱动自动切镜的**方案原型**。需求背景见根目录讨论文档
《直播多机位与自动切镜方案》：用「多个静止机位 + 偶尔脚本化运镜」替代单机位数字放大跟随，
做出接近赵雷 Live 的多角度 KTV 冲击感。

> 现阶段是**纯模拟**：用假机位底图（SVG）代替真实摄像头，验证状态机 + 切镜/运镜的"感觉"，
> 先不接 OBS / 真实画面。逻辑跑通后再对接。

## 文件

- `config_schema.py` + `director_config.json` —— **切镜/运镜参数的配置系统**。schema 是单一事实源（默认/类型/范围/中文标签/
  分组 + 校验规则）；JSON 是可编辑数据（首次运行生成、gitignore、**改存即热重载生效**）。参数不再散在源码里，
  越界钳位 + 非法枚举丢弃 + 跨字段纠正，坏配置不搞崩 director。分组/校验/热重载/未来 UI 详见 [GUIDE §7](GUIDE.md)。
- `director.py` —— **OBS 自动切镜 + 运镜驱动（独立进程·可整体删除）**。纯消费 pc-service 只读接口
  （WS 实时进度 + `/song/{mid}/karaoke`）+ 驱动 OBS（obs-websocket v5）。**不碰绿幕播放器、不碰 pc-service 核心**，
  对播放器零性能开销。状态机+护栏与 `director-sim.html` 一致（已单测逐帧对齐）。
  - **切镜（cut）**：`SetCurrentProgramScene` 切场景。
  - **编排（镜头调色板，2026-07-16 重构）**：每次切镜 = 随机抽一个「镜头」= **机位×景别×运镜×停留时长**。
    - 依据：分析参考片（赵雷《Over》Live 312s）——**~10s 一切、90% 静镜**，丰富感来自**景别/角度/节奏的变化**，
      而非多运镜或多切。我们只有 3 机位、无乐队可切，故用**数字景别派生多镜头**（`full/loose/high/close/thirds_l/thirds_r`）
      + **随机运镜**（`static/push/pull/pan/drift`）+ **变化停留**（主歌5-9s/副歌3-6s/间奏6-11s，18% 概率多停留），
      并**用满 3 机位**（`STATE_CAMS` 加权、不连切同机、尽量不连同景别）、**每 ~45s 强制广角呼吸**、**段落切换即切**、
      **前奏/间奏也持续切+运镜**（不再一次定住）。调色板/权重全在 `SHOT_PALETTE`/`STATE_CAMS`/`HOLD_RANGE`，好调。
    - **日常动态特写（closeup，1/2 机位）**：主歌/副歌高频抽中（`SHOT_PALETTE` 里 `("close","closeup")`/`("xclose","closeup")`）。
      **不再用固定 z（旧 close 1.3/xclose 1.75，人一远头占比就上不去，看着不像特写）**，而是**按 YuNet 实测脸框动态放大到
      「人头占画面高 ~1/2」**（xclose ~0.62），并复用 hero 三件套：**锁脸慢跟随 + 拟人轻晃 + 轻推/拉**（整个停留内缓动）。
      只在 `CLOSEUP_CAMS`（cam1/cam2）生效，cam3 抽中退静态（远机高倍放大糊）。参数 `CLOSEUP_*`。
    - **默认主镜纯跟随（follow）**：`PAUSE`（唱完回主机 cam1 待机）用 `["loose","follow"]` —— 正常景别 + **只锁脸慢跟随、
      不推拉不横移**（`still=True`）。待机 `not playing` 时钟冻结，故 z 稳定不动、`cx/cy` 低通跟随不依赖时钟每帧照跟，
      人一动画面轻轻跟上。跟随强度同 closeup 的 `CLOSEUP_FOLLOW_ALPHA`。
  - **手动主镜模式（App 关闭自动切镜，`manual_tick`）**：director 现在**常驻**，「自动切镜运镜」开关 = 切模式而非启停进程。
    关闭 = 锁 cam1 + **人脸跟随（同待机 follow 内核）** + 按 `cam_zoom` 放大（`z=cam_zoom/100`，夹 `manual.z_max`，
    `manual.zoom_lowpass` 平滑滑块）。pc-service 不再 kill 进程 / 不再自己变换 cam1（单写者不打架）。参数见配置 `manual` 组。
    - **横移镜头（trackpan，1/2 机位）**：人像从画面一侧**拟人横扫到另一侧**（方向随机 L↔R），有**正常景别**与
      **放大到头占½**两档；与 closeup 同一套锁脸慢跟随 + 拟人轻晃，全程不露黑边（cover 横向余量）。主歌/副歌各占 ~24%/34%。
      端点 `CLOSEUP_PAN_AX`。取代旧的单向 `trackLR`（后者仍保留给间奏/前奏的广角横移）。
    - **招牌长运镜（hero，主机 cam1 专属 ~18s）**：**每首歌保证触发一次**（`HERO_GUARANTEE`：载入即规划一个目标时刻，
      落在演唱区间前半或后半随机、避开开口与歌尾；到点若逢间奏则**顺延到下一句**，绝不卡间奏）；此外主歌/副歌切到主机时
      还按 `HERO_PROB`/`HERO_COOLDOWN` 有额外机会（**每首歌最多 `HERO_MAX_PER_SONG`＝2 次、每段块最多一次**，再多显腻）。
      两阶段长镜——① **慢推 15s**：从最远（z=1.0 铺满不黑边）`smoothstep`
      缓推到「**人头占画面高 ≈3/4**」超特写、头靠上，
      全程叠**两正弦左右轻晃**（仿真人手持）；② **速拉 3s**：`outCubic` 后撤到「**头占 <1/2**」中景收尾。头占比经
      `_z_for_head` 由 YuNet 实测脸框高（`CAM_FACE_H`）换算成 z（16:9→4:3 高度受限：`输出头高比=头源高比×z`）。
      参数见脚本顶 `HERO_*`（时长/头占比/摇晃/冷却/概率/z 上下限，`HERO_ENABLE=False` 可关）。
      **关注点 `cx/cy` 慢速低通跟随**（起镜对准真人头，之后按 `HERO_FOLLOW_ALPHA` 低通跟随——既不逐帧硬跟致抖、
      也不锁死致人一动就对不准，只平滑跟上直播中的轻微移动）；脸框高先钳 `HERO_FACEH_CLAMP` 防误检甩飞 z，
      `z_end ≤ z_near×HERO_PULL_RATIO` 保证后拉明显。
      单独循环观察：**`python hero_test.py --no-track --gap 4`**（不走状态机、不需放歌；占位图上关跟踪看纯几何）。
  - **实时头部跟踪（YuNet）**：后台线程用 `cv2.FaceDetectorYN`（模型 `models/yunet.onnx`）定时抓每台画面检测头部，
    EMA 平滑后写 `CAM_FACE_LIVE`（眼中点）；特写/三分每次切镜读它**锁定最新头位并跟人移动**，不与 OBS 插件冲突。
    `close/xclose/thirds` 的 `ay≈0.4` 给足头顶留白（眼睛落上三分，解决"头偏下")。`TRACK_FACE=False` 可关，退回静态 `CAM_FACE`。
    注意：坐标须转 Python `float`（YuNet 返回 numpy float32，直接进 OBS 的 JSON 会 `not serializable`）。
  - **运镜（move）**：`SetSceneItemTransform` 30Hz 插值（**不需 Move 插件**）。变换目标 = 每场景里名为
    `content_<cam>` 的源（脚本顶 `CONTENT_NAME`）。framing 数学同模拟器（源上 `cx,cy` 放大 `z` 落到输出 `ax,ay`）。
    可 `ENABLE_MOVES=False` 只切不动。
    - **防黑边**：`set_framing` 把位置夹到"仍盖满画布"的区间（`posX∈[-(z-1)·CW, 0]`），任何运镜都不露黑边；
      1:3 前推要把脸（`cx≈0.56`）推到左三分（`ax=0.36`）且不露边，需放大 `z≥(1-ax)/(1-cx)≈1.5`（故 thirds `z=1.5`）。
  - 依赖见 `requirements.txt`（`websocket-client` + `obsws-python`，与其它子项目无关）。
  - `python director.py --dry-run` —— 不连 OBS，只打印切镜决策（装 OBS 前先验数据链）。
  - `python director.py --demo` —— 内置演示曲循环驱动 OBS，不需 pc-service（先看效果用这个）。
  - `python director.py` —— 接真实播放器（改脚本顶 `OBS_PASSWORD`、`SCENES`、`PC_SERVICE`；需 pc-service+播放器在放歌）。
  - 删除测试：删 `auto-director/` 整个目录 + 在 OBS 里删掉 cam1/2/3 场景，回滚 pc-service 的 CORS/chorus
    两处只读加法即可，播放器无痕。
- `hero_test.py` —— **单独循环测试主机招牌长运镜(hero)**。只对 cam1 反复跑「慢推15s到头占3/4超特写(带手持轻晃)→速拉3s回头占<1/2中景」,
  不走状态机、不切别机位、不需 pc-service/放歌,纯看这一个运镜手感。复用 director 的 `start_hero/tick_hero` + `ObsDriver`。
  `--no-track`(占位图观察用静态人脸位)/`--gap N`(两遍间隔)/`--once`(只一遍)。改手感改 director.py 顶 `HERO_*` 再重跑。
  ⚠️ **跑它前必须先关正式 director**(安卓遥控页「自动切镜运镜」开关 off,或 WS 发 `director {on:false}`)——
  否则 pc-service 托管的 director 进程和本脚本**两路 30Hz 同时改 cam1 变换会狂闪抖动**;测完再打开开关恢复。
- `wire_camera.py` —— **把某机位场景换成真实摄像头**(UVC/虚拟摄像头),保持源名 `content_<cam>` 故 director 无需改。
  `python wire_camera.py`（列设备）/ `cam1 "#1"` / `cam2 "#2"` / `cam3 "#3"`（三台手机全走 iVCam,按连接顺序编号）。
  **存在就改设备、不存在才新建**(避开删除异步竞态)、**等比铺满 4:3 画布**（16:9 居中裁切不变形）、精确名优先。
  ⚠️ iVCam `#N` 后缀按连接顺序分配 → 每次**固定顺序连手机(1→2→3号)**,否则映射错位。
- `obs_setup.py` —— **OBS 测试环境一键搭建**：生成三张机位底图（`assets/cam{1,2,3}.png`，网格+机位标签+FACE
  标记，便于看运镜）+ 在 OBS 里建 cam1/2/3 三场景、各放一个 `content_<cam>` 图片源。`python obs_setup.py` 跑一次即可。
  （`assets/` 可再生、已 gitignore。）
- `director-sim.html` —— 单文件导播台模拟器，双击即开（无需服务器）。三种数据源模式：
  - **演示曲**：内置逐字时间轴 + 副歌标注，即开即看理想行为。
  - **载入音乐**：选任意音频 → Web Audio 实时能量/起音分析 → 数据驱动切镜
    （**无歌词时的代理**：能量高=副歌、低能量持续=间奏、上升沿=切点吸附。精度不如真值，仅演示用）。
  - **接真实播放器**：连 pc-service（默认 `localhost:8765`）→ 订阅 WS 实时进度、按当前 `mid`
    拉 `/song/{mid}/karaoke` 的**真值逐字+音高** → 驱动假机位。这是最终架构的验证形态：
    真播放器放真歌，自动切镜跟着真实逐字/节奏走，只差真实摄像头和 OBS。播放由真实播放器控制
    （模拟器的播放键在此模式禁用）。**副歌**需在 `meta.json` 标注 `chorus` 后才会触发（见下）。
  - 右侧导播台：三机位监视器（红框=当前直播画面）、指令流（实际下发的协议）、护栏实时可调。

## 状态机（对照方案文档 §4）

`PAUSE / INTRO / VERSE / CHORUS / INTERLUDE / OUTRO`，判定输入：
逐字时间轴 `words[]`、手动副歌区间 `chorusRanges[]`、伴奏播放态、（进阶）音准/能量。

**六条护栏**（`director-sim.html` 内 `planCut`）：
①切点吸附字/起音边界　②最短镜头（主歌≥6s/副歌≥4s）　③不连切同机 + 加随机
④切换视角差≥30°　⑤锁主机兜底键　⑥运动只当标点（开场推/尾奏拉/副歌进入前推）。

## 指令协议（状态机 → OBS）

归一化坐标，直接映射 OBS：`cut`→`SetCurrentProgramScene`；`move`→Move 插件 / 定时
`SetSceneItemTransform`。

```jsonc
// 切镜
{ "t": 12.34, "cmd": "cut", "camera": "cam2", "transition": "cut|fade",
  "state": "VERSE", "reason": "auto|locked" }

// 运镜（framing = { z 放大, cx/cy 源上关注点, ax/ay 落到输出的锚点 }）
{ "t": 3.0, "cmd": "move", "camera": "cam3", "kind": "push|pull|pan|thirds|breath",
  "framing": { "from": {"z":1.0,"cx":0.5,"cy":0.5,"ax":0.5,"ay":0.5},
               "to":   {"z":1.28,"cx":0.56,"cy":0.30,"ax":0.34,"ay":0.42} },
  "dur": 4.0, "ease": "inOutCubic", "state": "INTRO" }
```

- `framing` 数学：源上 `(cx,cy)` 放大 `z` 后落到输出 `(ax,ay)`。
  默认 `(0.5,0.5)→(0.5,0.5)` 即满画面正中；**1/3 前推** = 把人脸 `(cx,cy)` 落到 `ax=0.34`。

## 机位约定（对照方案文档 §5）

- `cam1` 主·正前偏左·平视中景（大本营，主歌主力，角度 -20°）
- `cam2` 右·近景特写（与主机分居左右，角度差最大，副歌/情绪句，+40°）
- `cam3` 高远·广角大全景（呼吸镜头，开场推/尾奏拉起点，间奏，0°）

## 对接现有系统（pc-service）

数据接口**已现成**，模拟器「接真实播放器」模式直接消费，pc-service 侧只做了两处小改：

- **实时播放态**：`ws://<pc>:8765/ws` 广播 `{"type":"state", ...}`，含 `k_pos/k_dur`(ms)、
  `k_playing`、`k_mid`、`k_title`（手机 App 用的同一条广播）。
- **逐字 + 音高 + 副歌**：`GET http://<pc>:8765/song/{mid}/karaoke` →
  `{lines:[{start,end,chars:[{text,start,dur}]}], notes:[{start,dur,pitch}], chorus:[[s,e]...]}`。
  `chorus` 读自 `<曲库>/<mid>/meta.json` 的 `chorus` 键（**手动标注**，`[[起ms,止ms],...]`），无则空。
- pc-service 改动：`karaoke_data.song_karaoke` 加 `chorus` 字段（读 meta.json）；`server.py` 加
  CORS（放开只读接口跨源，供独立打开的模拟器 fetch）。播放器**未改**。

## 待办 / 下一步

- [x] 模拟器接真实播放器（WS 实时进度 + `/song/{mid}/karaoke` 逐字/音高）。
- [x] 对接 OBS：`director.py` 用 obs-websocket 做切镜 + 30Hz 变换插值运镜（不需 Move 插件），
  假机位占位图上验通（切镜+推/拉/平移/1:3前推全跑通）。
- [x] 接真实播放器实测：`python director.py` 已实测——pc-service 放真歌（赵雷·吉姆餐厅），director 按真实
  播放进度自动切镜+运镜，护栏（6s/4s 最短镜头）、长前奏（该曲前奏 68s，全程 INTRO 等待正确）、seek 都正常。
  注意：①副歌未标注 `chorus` 时不触发 1:3 前推（那段按主歌切）；②WS 状态每 500ms 一帧，director 用
  `now_t()` 插值出精确时钟；③长前奏/间奏期间只有 `[心跳]` 日志（无切镜属正常，别误判卡死）。
- [ ] `chorusRanges` 标注 UI：在曲库管理页给每首歌标副歌区间，写入 `meta.json` 的 `chorus` 键
  （数据契约已通，缺录入界面；先可手动编辑 meta.json 验证）。
- [ ] 换真实摄像头：把每个场景的 `content_<cam>` 图片源换成真机位「摄像头」源（Camo/DroidCam；director 无需改）。
- [x] KTV 字幕接入 OBS：`KTV悬浮`（karaoke-player 绿幕窗口捕获）+ 绿幕色键 + 同步到三场景并置顶，`ktv_overlay.py` 一键接入（见 GUIDE §3.5）。
  剩：竖屏 3:4 播放器窗在 4:3 横屏画布的布局微调（顶歌单/底歌词位置,OBS 里手动调 KTV悬浮 项）。
- [ ] 抖音合规：OBS **不推流**，改「启动虚拟摄像机」→ 直播伴侣选 OBS Virtual Camera 官方推流。
- [ ] 多机延迟对齐；三机白平衡/曝光锁死。
