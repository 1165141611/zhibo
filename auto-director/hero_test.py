# -*- coding: utf-8 -*-
"""
hero 招牌长运镜 · 单独循环测试(便于肉眼观察调参)
====================================================
只对主机 cam1 反复跑那段两阶段长推(慢推15s到"头占3/4"超特写带手持轻晃 → 速拉3s回"头占<1/2"中景),
循环播放。**不走状态机、不切别的机位、不需 pc-service/放歌**,纯看这一个运镜的手感。

复用 director.py 的 hero 逻辑(start_hero/tick_hero/_z_for_head/CAM_FACE_H)与 OBS 驱动(ObsDriver)、
头部跟踪(start_tracker)。改手感直接改 director.py 顶部的 HERO_* 常量,再重跑本脚本即可。

前置:
  - OBS 已开,装了 obs-websocket(director.py 顶 OBS_HOST/PORT/PASSWORD 配好)。
  - 已 `python obs_setup.py` 建好 cam1 场景 + content_cam1 源(想看真机位先 `python wire_camera.py cam1 "#1"`)。

用法:
  python hero_test.py              # 连 OBS,循环跑 hero,每次之间在"最远起点"静置 GAP 秒
  python hero_test.py --gap 3      # 两次长镜间隔 3s
  python hero_test.py --once       # 只跑一次
  python hero_test.py --no-track   # 关头部跟踪(用 director.py 里 CAM_FACE_H 的静态回退值)
"""
import time
import argparse

import director as D


def _park_far(obs, cam):
    """把画面落到 hero 起点(z=1.0 最远、铺满不黑边、眼中点居中),供间隔期静置观察起始构图。"""
    fx, fy = D.CAM_FACE_LIVE.get(cam, D.CAM_FACE.get(cam, (0.5, 0.5)))
    D.RT["framing"] = {"z": 1.0, "cx": fx, "cy": fy, "ax": 0.5, "ay": D.HERO_AY_FAR}
    obs.set_framing(cam, D.RT["framing"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=4.0, help="两次长镜之间在最远起点静置的秒数")
    ap.add_argument("--once", action="store_true", help="只跑一次,不循环")
    ap.add_argument("--no-track", action="store_true", help="关闭 YuNet 头部跟踪(用静态回退脸位/脸框高)")
    args = ap.parse_args()

    D.load_config(initial=True)          # 读 director_config.json(与正式 director 同一份参数)
    if not D.ENABLE_MOVES:
        print("[!] 配置里 rhythm.enable_moves=false,请先打开再测运镜。")
        return

    obs = D.ObsDriver(dry=False)
    if not getattr(obs, "moves_ok", False):
        print("[!] 运镜未就绪:检查 OBS 是否开着、cam1 场景里是否有名为 content_cam1 的源(跑 obs_setup.py)。")
        return

    if not args.no_track and D.TRACK_FACE:
        D.start_tracker()          # 头部实时跟踪锁脸/测脸框高;失败会自动退回静态值
        time.sleep(1.0)            # 给它一个检测周期先拿到一帧,首镜就能锁到人

    cam = D.HERO_CAM
    obs.cut(cam)                   # 切到主机场景

    # 假时钟:playing=True 让 director.now_t() 按墙钟走(hero 的 t0/tau 都基于它)
    D.RT["playing"] = True
    D.RT["base_pos"] = 0.0
    D.RT["base_wall"] = time.monotonic()

    total = D.HERO_PUSH_DUR + D.HERO_PULL_DUR
    dt = 1.0 / D.TICK_HZ
    n = 0
    print(f"[hero测试] 循环跑 {cam}:慢推{D.HERO_PUSH_DUR:.0f}s→速拉{D.HERO_PULL_DUR:.0f}s"
          f"(共{total:.0f}s)+ 间隔{args.gap:.0f}s。Ctrl+C 退出。")
    _park_far(obs, cam)
    time.sleep(min(2.0, args.gap))

    try:
        while True:
            n += 1
            D.RT["last_hero_t"] = -999.0      # 绕过冷却,想跑就跑
            D.start_hero(cam)
            t0 = D.now_t()
            while True:                       # 跑完这一遍长镜
                time.sleep(dt)
                t = D.now_t()
                took = D.tick_hero(t)
                if D.RT["fdirty"]:
                    D.RT["fdirty"] = False
                    obs.set_framing(cam, D.RT["framing"])
                if not took and (t - t0) > total:
                    break
            print(f"[第{n}次] 完成一遍")
            if args.once:
                break
            _park_far(obs, cam)               # 回到最远起点静置,便于看下一遍的起始构图
            time.sleep(args.gap)
    except KeyboardInterrupt:
        print("\n退出。")


if __name__ == "__main__":
    main()
