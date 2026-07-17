# -*- coding: utf-8 -*-
"""OBS 测试环境一键搭建(可复现·可整体删除)
=====================================
给自动切镜测试准备三个假机位场景:
  1. 生成三张机位底图 assets/cam{1,2,3}.png(网格+机位标签+FACE 标记,便于看推拉/平移/1:3前推)
  2. 连 OBS(obs-websocket),确保 cam1/cam2/cam3 三个场景存在
  3. 每个场景放一个图片源 content_cam{N}(铺满画布),供 director.py 做切镜+运镜

前置:OBS 已开、工具→WebSocket 服务器设置已启用(本脚本默认无鉴权 localhost:4455)。
用法:  python obs_setup.py
之后:  python director.py --demo   # 内置演示曲驱动;或 python director.py 接真实播放器

删除测试:删 auto-director/ 整个目录 + 在 OBS 里删掉 cam1/2/3 场景即可,播放器/ pc-service 无痕。
"""
import os
import obsws_python as obs

OBS_HOST, OBS_PORT, OBS_PASSWORD = "localhost", 4455, ""
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

CAMS = {
    "cam1": {"bg": (150, 45, 60),  "label": "CAM 1", "sub": "MAIN / MID",   "face": (0.56, 0.30)},
    "cam2": {"bg": (40, 90, 170),  "label": "CAM 2", "sub": "RIGHT / CLOSE", "face": (0.42, 0.34)},
    "cam3": {"bg": (40, 140, 90),  "label": "CAM 3", "sub": "HIGH / WIDE",  "face": (0.50, 0.52)},
}
W, H = 1440, 1080


# ── 1) 生成机位底图 ───────────────────────────────────────
def gen_images():
    from PIL import Image, ImageDraw, ImageFont
    os.makedirs(ASSETS, exist_ok=True)

    def font(sz):
        for p in [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf"]:
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                continue
        return ImageFont.load_default()

    def lighten(c, f=0.25):
        return tuple(min(255, int(v + (255 - v) * f)) for v in c)

    for name, cfg in CAMS.items():
        img = Image.new("RGB", (W, H), cfg["bg"])
        d = ImageDraw.Draw(img)
        grid = lighten(cfg["bg"], 0.22)
        for x in range(0, W + 1, 120):
            d.line([(x, 0), (x, H)], fill=grid, width=2)
        for y in range(0, H + 1, 120):
            d.line([(0, y), (W, y)], fill=grid, width=2)
        tl = lighten(cfg["bg"], 0.45)
        for fx in (1 / 3, 2 / 3):
            d.line([(int(fx * W), 0), (int(fx * W), H)], fill=tl, width=1)
        for fy in (1 / 3, 2 / 3):
            d.line([(0, int(fy * H)), (W, int(fy * H))], fill=tl, width=1)
        d.rectangle([4, 4, W - 5, H - 5], outline=(255, 255, 255), width=6)
        d.text((40, 30), cfg["label"], font=font(120), fill=(255, 255, 255), stroke_width=6, stroke_fill=(0, 0, 0))
        d.text((46, 170), cfg["sub"], font=font(46), fill=(235, 235, 235), stroke_width=4, stroke_fill=(0, 0, 0))
        fx, fy = cfg["face"]
        cx, cy = int(fx * W), int(fy * H)
        d.ellipse([cx - 78, cy - 78, cx + 78, cy + 78], outline=(255, 235, 120), width=8)
        d.ellipse([cx - 12, cy - 12, cx + 12, cy + 12], fill=(255, 235, 120))
        d.line([(cx - 100, cy), (cx + 100, cy)], fill=(255, 235, 120), width=2)
        d.line([(cx, cy - 100), (cx, cy + 100)], fill=(255, 235, 120), width=2)
        d.text((cx + 90, cy - 20), "FACE", font=font(40), fill=(255, 235, 120), stroke_width=3, stroke_fill=(0, 0, 0))
        img.save(os.path.join(ASSETS, name + ".png"))
        print("  wrote", name + ".png")


# ── 2)+3) 建场景 + 放图片源 ───────────────────────────────
def setup_obs():
    cl = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5)
    existing = {s["sceneName"] for s in cl.get_scene_list().scenes}
    for cam in CAMS:
        if cam not in existing:
            cl.create_scene(cam)
            print(f"  建场景 {cam}")
        # 清掉可能残留的占位源
        for old in (f"ph_{cam}_bg", f"ph_{cam}_txt", f"content_{cam}"):
            try:
                cl.remove_input(old)
            except Exception:
                pass
        cl.create_input(cam, f"content_{cam}", "image_source",
                        {"file": os.path.join(ASSETS, cam + ".png").replace("\\", "/")}, True)
        iid = cl.get_scene_item_id(cam, f"content_{cam}").scene_item_id
        print(f"  {cam}: content_{cam} itemId={iid}")


if __name__ == "__main__":
    print("生成机位底图…")
    gen_images()
    print("配置 OBS 场景…")
    setup_obs()
    print("完成。现在可运行:  python director.py --demo")
