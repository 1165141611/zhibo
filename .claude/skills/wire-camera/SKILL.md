---
name: wire-camera
description: 直播多机位换/挪/调整相机后,重接 OBS 并校准画布。当用户说"换了机位""挪了相机""cam3 改成大疆了""某机位画面不对/黑边/变形""重新接一下摄像头""把摄像头N改成…"等,用本 skill 把 OBS 场景的 content_<cam> 源重新绑到正确设备、等比铺满 4:3 画布(无黑边/不变形)并截图验证。仅用于本 zhibo 直播项目(auto-director + OBS)。
---

# 机位重接 / 校准

给 auto-director 的 OBS 三机位(cam1/cam2/cam3)重接真实相机并铺满画布。工具是 `auto-director/wire_camera.py`。

## 环境
- **真 Python**:`C:/Users/11651/AppData/Local/Programs/Python/Python313/python.exe`(PATH 的 `python` 是 Store 占位版,不能用)。下称 `<py>`。
- OBS 已开、工具→WebSocket 服务器设置已启用(默认 `localhost:4455` 无鉴权)。
- 约定:三场景命名 `cam1/cam2/cam3`,各含一个源 `content_<cam>`(director 靠这个名字工作)。

## 步骤
1. **固定顺序连设备**(关键坑):iVCam 每台手机都叫 `e2eSoft iVCam`,后缀 `#1/#2/#3` **按连接顺序**分配,不是设备固定属性。务必按 **1号→2号→3号** 顺序连;大疆走 UVC,名为 `OsmoAction6`。
2. **列设备核对**:`<py> auto-director/wire_camera.py`(无参=列出所有摄像头设备名),确认 `#1/#2/#3` 或 `OsmoAction6` 对应哪个机位。
3. **重接**:`<py> auto-director/wire_camera.py <cam1|cam2|cam3> "<设备名片段>"`
   - 例:`cam1 "#1"` / `cam2 "#2"` / `cam3 "#3"`(全手机);或 `cam3 OsmoAction`(大疆)。
   - 精确名优先,用 `"#1"/"#2"/"#3"` 精确指定第 N 台。脚本"存在就改设备、不存在才建",并等比铺满 4:3(cover)、清残留边界框、切到该场景。
4. **验证铺满/无黑边/无变形**(必做,别省):截该场景图查是否横向铺满:
   ```python
   import base64, numpy as np, cv2, obsws_python as obs
   cl = obs.ReqClient(host='localhost', port=4455, password='', timeout=5)
   scn = 'cam3'
   cl.set_current_program_scene(scn)
   r = cl.get_source_screenshot(scn, 'jpg', 1440, 1080, -1)
   img = cv2.imdecode(np.frombuffer(base64.b64decode(r.image_data.split(',',1)[1]), np.uint8), cv2.IMREAD_COLOR)
   xs = [x for x in range(img.shape[1]) if img[:, x].max() > 25]
   print('左黑 %d  右黑 %d (应都为0)' % (min(xs), img.shape[1]-1-max(xs)))
   ```
   - **有黑边** → 多半是残留 `bounds`(见踩坑)。用 `set_scene_item_transform` 显式带 `boundsType:'OBS_BOUNDS_NONE'` + cover 变换重设即可。
   - 相机**刚接分辨率读不准**也会短暂黑边,等 1~2s 重跑第 4 步 / 重设 cover。
5. **KTV 字幕层**:若重建过场景,确认 `KTV悬浮` 仍在该场景**顶层**(否则被摄像头盖住)——用 `ktv-overlay` skill 恢复。
6. **头部跟踪自动适应**:CAM_FACE 由 director 的 YuNet 实时更新;摆好机位、人站到唱歌位即自动锁脸,一般不用手动改坐标。
7. **重启 director**:若自动切镜在跑,重启生效(手机 App「自动切镜运镜」开关关再开;或 pc-service `{"cmd":"director","on":...}`)。

## 踩坑(详见 auto-director/GUIDE.md §5 / §8)
- iVCam `#N` 按连接顺序 → **固定顺序连**,否则机位映射错乱。
- 残留 `boundsType=OBS_BOUNDS_SCALE_INNER` 会**覆盖 scaleX/Y** 致黑边 → 所有 framing 变换必须带 `OBS_BOUNDS_NONE`。
- 16:9 相机进 4:3 画布要 **cover 等比铺满 + 裁边**,不能按 X/Y 分别缩放(会压扁变形)。
