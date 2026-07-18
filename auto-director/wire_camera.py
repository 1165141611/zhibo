# -*- coding: utf-8 -*-
"""把某个机位场景(cam1/2/3)的源换成真实摄像头(UVC/虚拟摄像头),名字仍为 content_<cam>。
director.py 靠 content_<cam> 这个名字做切镜/运镜,故换真相机只需保持同名。

用法:
  python wire_camera.py cam1 Camo          # iPhone(Camo)接 cam1
  python wire_camera.py cam2 iVCam         # 安卓(iVCam)接 cam2
  python wire_camera.py cam3 OsmoAction    # 大疆 接 cam3
参数2 = 设备名的一段(模糊匹配,大小写不敏感)。不带参数则只列出可用设备。
"""
import sys
import time
import obsws_python as obs

OBS_HOST, OBS_PORT, OBS_PASSWORD = "localhost", 4455, ""


def list_devices(cl):
    # 优先复用已有的 dshow 输入读设备列表(避免临时探针残留导致下次 601)
    existing = next((i.get("inputName") for i in cl.get_input_list().inputs
                     if i.get("inputKind") == "dshow_input"), None)
    if existing:
        items = cl.get_input_properties_list_property_items(existing, "video_device_id").property_items
        return [(it.get("itemName"), it.get("itemValue")) for it in items]
    probe = "_wire_probe"
    try:
        cl.remove_input(probe)
    except Exception:
        pass
    cl.create_input("cam1", probe, "dshow_input", {}, False)
    items = cl.get_input_properties_list_property_items(probe, "video_device_id").property_items
    try:
        cl.remove_input(probe)
    except Exception:
        pass
    return [(it.get("itemName"), it.get("itemValue")) for it in items]


def cover_framing(cl, scene, iid, CW, CH, z=1.0):
    tr = cl.get_scene_item_transform(scene, iid).scene_item_transform
    sw, sh = tr["sourceWidth"], tr["sourceHeight"]
    if not sw or not sh:
        return None
    sc = max(CW / sw, CH / sh) * z
    sws, shs = sw * sc, sh * sc
    posX = min(0.0, max(CW - sws, 0.5 * CW - 0.5 * sws))
    posY = min(0.0, max(CH - shs, 0.5 * CH - 0.5 * shs))
    cl.set_scene_item_transform(scene, iid, {
        "boundsType": "OBS_BOUNDS_NONE",   # 清残留边界框(否则会覆盖 scale 导致黑边)
        "scaleX": sc, "scaleY": sc, "positionX": posX, "positionY": posY,
        "cropLeft": 0, "cropRight": 0, "cropTop": 0, "cropBottom": 0,
        "rotation": 0.0, "alignment": 5})
    return sw, sh


def main():
    cl = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5)
    gv = cl.get_video_settings()
    CW, CH = gv.base_width, gv.base_height
    devices = list_devices(cl)

    if len(sys.argv) < 3:
        print("可用视频设备:")
        for name, _ in devices:
            print("  -", name)
        print("\n用法: python wire_camera.py <cam1|cam2|cam3> <设备名片段>")
        return

    cam, needle = sys.argv[1], sys.argv[2].lower()
    scene = cam
    # 匹配:精确名优先(解决 iVCam 多机同名歧义:"e2eSoft iVCam" vs "e2eSoft iVCam #2"),
    # 否则模糊子串;子串多命中时取最短名(=无 #N 后缀的基础设备)。
    exact = [(n, v) for n, v in devices if (n or "").lower() == needle]
    subs = [(n, v) for n, v in devices if needle in (n or "").lower()]
    cand = exact or subs
    if not cand:
        print(f"没找到含 '{sys.argv[2]}' 的设备。可用:")
        for n, _ in devices:
            print("  -", n)
        return
    if not exact and len(cand) > 1:
        cand = sorted(cand, key=lambda x: len(x[0] or ""))
        print(f"'{sys.argv[2]}' 命中多个 {[n for n, _ in cand]},取最短名(基础设备)。要精确请用完整名或 '#2'/'#3'。")
    name, dev_id = cand[0]
    print(f"匹配设备: {name}")

    src = f"content_{cam}"
    inputs = {i["inputName"] for i in cl.get_input_list().inputs}
    if src in inputs:
        # 已存在 → 直接改设备(避开 remove/create 异步竞态);并确保它在本场景里
        cl.set_input_settings(src, {"video_device_id": dev_id, "active": True}, True)
        items = [it["sourceName"] for it in cl.get_scene_item_list(scene).scene_items]
        if src not in items:
            cl.create_scene_item(scene, src)
    else:
        cl.create_input(scene, src, "dshow_input", {"video_device_id": dev_id, "active": True}, True)
    time.sleep(1.8)                   # 等设备起来出分辨率
    iid = cl.get_scene_item_id(scene, src).scene_item_id
    wh = cover_framing(cl, scene, iid, CW, CH)
    cl.set_current_program_scene(scene)
    print(f"{cam} ← {name}  源分辨率 {wh[0] if wh else '?'}x{wh[1] if wh else '?'}  已铺满 {CW}x{CH} 画布,已切到该场景")


if __name__ == "__main__":
    main()
