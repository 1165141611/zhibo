# -*- coding: utf-8 -*-
"""
自动切镜测试驱动(独立进程·可整体删除)
=====================================
职责:纯消费 pc-service 的只读接口 + 驱动 OBS 切场景。**不碰绿幕播放器,不碰 pc-service 核心**。

数据链:
  karaoke-player(不动) --STATE--> pc-service --ws://:8765/ws--> 本脚本 --obs-websocket--> OBS

  - 实时播放态:订阅 pc-service WS 的 {"type":"state", k_pos/k_dur/k_playing/k_mid ...}
  - 逐字+音高+副歌:HTTP GET /song/{mid}/karaoke → {lines, notes, chorus}
  - 切镜:OBS SetCurrentProgramScene(把当前直播场景切成目标机位)

状态机 + 六护栏与 director-sim.html 完全一致(那边已逐首验证过),这里是 Python 移植。
第一阶段只做「切镜(cut)」;平滑运镜(move)留了 TODO 钩子,确认切镜 OK 后再补(见 README)。

依赖(装在本目录,与播放器无关):  pip install -r requirements.txt
运行:
  python director.py --dry-run        # 不连 OBS,只打印切镜决策(装 OBS 前先验数据链)
  python director.py                  # 连 OBS 真切场景

删除测试:删掉 director.py + requirements.txt,回滚 pc-service 的 CORS/chorus 两处只读加法即可。
"""
import os
import sys
import json
import math
import time
import random
import argparse
import threading
import urllib.parse
import urllib.request

# ── 配置系统(集中管理·可校验·可热重载)──────────────────────
# 所有切镜/运镜参数集中在 config_schema.py(单一事实源)+ director_config.json(可编辑数据)。
# 启动 load_config() 读 JSON → schema 校验/钳位/跨字段规则 → _apply_cfg 灌进本模块的全局常量;
# 主循环按 mtime 热重载,改 JSON 即时生效(连接/跟踪线程/模型类参数改后需重启,标 live=False)。
# 各常量的默认值/范围/中文标签见 config_schema.py。后期可视化 UI 直接读该 schema 自动渲染控件。
import config_schema as cs

_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_DIR, "director_config.json")
_cfg_mtime = 0.0


def _write_json(path, data):
    """原子写 JSON(temp + os.replace),避免读到半截文件。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _apply_cfg(cfg, first=False):
    """把校验后的配置灌进本模块全局常量(函数体按名读全局,故重载后即时生效)。"""
    g = globals()
    r = cfg["rhythm"]
    g.update(TICK_HZ=r["tick_hz"], INTERLUDE_GAP=r["interlude_gap"], SNAP_WINDOW=r["snap_window"],
             SNAP_GRACE=r["snap_grace"], WIDE_BREATHER=r["wide_breather"], WIDE_BOOST=r["wide_boost"],
             LINGER_PROB=r["linger_prob"], LINGER_ADD=(r["linger_add_lo"], r["linger_add_hi"]),
             ENABLE_MOVES=r["enable_moves"])
    cl = cfg["closeup"]
    g.update(CLOSEUP_HEAD=cl["head"], CLOSEUP_XHEAD=cl["xhead"], CLOSEUP_Z_MIN=cl["z_min"],
             CLOSEUP_Z_MAX=cl["z_max"], CLOSEUP_PUSH=cl["push"], CLOSEUP_SWAY_AMP=cl["sway_amp"],
             CLOSEUP_SWAY_PERIOD=cl["sway_period"], CLOSEUP_FOLLOW_ALPHA=cl["follow_alpha"],
             CLOSEUP_AY=cl["ay"], CLOSEUP_PAN_AX=(cl["pan_ax_lo"], cl["pan_ax_hi"]), CLOSEUP_PAN_ZMIN=cl["pan_zmin"])
    h = cfg["hero"]
    g.update(HERO_ENABLE=h["enable"], HERO_PUSH_DUR=h["push_dur"], HERO_PULL_DUR=h["pull_dur"],
             HERO_HEAD_NEAR=h["head_near"], HERO_HEAD_END=h["head_end"], HERO_AY_FAR=h["ay_far"],
             HERO_AY_NEAR=h["ay_near"], HERO_SWAY_AMP=h["sway_amp"], HERO_SWAY_PERIOD=h["sway_period"],
             HERO_HEAD_FACTOR=h["head_factor"], HERO_Z_MAX=h["z_max"], HERO_Z_NEAR_MIN=h["z_near_min"],
             HERO_PULL_RATIO=h["pull_ratio"], HERO_FACEH_CLAMP=(h["faceh_clamp_lo"], h["faceh_clamp_hi"]),
             HERO_FOLLOW_ALPHA=h["follow_alpha"], HERO_COOLDOWN=h["cooldown"], HERO_PROB=h["prob"],
             HERO_GUARANTEE=h["guarantee"], HERO_EDGE_HEAD=h["edge_head"], HERO_EDGE_TAIL=h["edge_tail"],
             HERO_MAX_PER_SONG=h["max_per_song"])
    mn = cfg["manual"]
    g.update(MANUAL_FOLLOW_ALPHA=mn["follow_alpha"], MANUAL_AY=mn["ay"],
             MANUAL_ZOOM_LOWPASS=mn["zoom_lowpass"], MANUAL_Z_MAX=mn["z_max"])
    cam = cfg["cameras"]
    g["CONTENT_NAME"] = cam["content_name"]
    g["HERO_CAM"] = cam["hero_cam"]
    g["CLOSEUP_CAMS"] = set(cam["closeup_cams"])
    scenes, angle, face, faceh, presets = {}, {}, {}, {}, {}
    for c, rec in cam["list"].items():
        scenes[c] = rec["scene"]; angle[c] = rec["angle"]; face[c] = tuple(rec["face"])
        faceh[c] = rec["face_h"]; presets[c] = set(rec["presets"])
    g.update(SCENES=scenes, CAM_ANGLE=angle, CAM_FACE=face, CAM_PRESETS=presets)
    g["STATE_CAMS"] = cfg["state_cams"]
    g["HOLD_RANGE"] = {s: tuple(v) for s, v in cfg["hold_range"].items()}
    g["SHOT_PALETTE"] = {s: [tuple(x) for x in lst] for s, lst in cfg["palette"].items()}
    g["FRAMING"] = cfg["framing"]
    g["MOVES"] = cfg["moves"]
    tr = cfg["tracking"]
    mp = tr["model_path"]
    g.update(TRACK_FACE=tr["track_face"], TRACK_INTERVAL=tr["interval"], TRACK_SMOOTH=tr["smooth"],
             FACE_MODEL=os.path.normpath(mp if os.path.isabs(mp) else os.path.join(_DIR, mp)),
             DET_SCORE=tr["det_score"], DET_NMS=tr["det_nms"], DET_TOPK=tr["det_topk"],
             SHOT_W=tr["shot_w"], SHOT_H=tr["shot_h"])
    cn = cfg["connection"]
    g.update(PC_SERVICE=cn["pc_service"], OBS_HOST=cn["obs_host"], OBS_PORT=cn["obs_port"], OBS_PASSWORD=cn["obs_password"])
    # 运行时可变态:首次从配置初始化;热重载不覆盖 tracker 实时写入的 CAM_FACE_LIVE / CAM_FACE_H
    if first or "CAM_FACE_LIVE" not in g:
        g["CAM_FACE_LIVE"] = dict(face)
        g["CAM_FACE_H"] = dict(faceh)


def load_config(initial=False, verbose=True):
    """读 director_config.json → 校验 → 应用。缺失则落一份默认供编辑;
    热重载(initial=False)时若文件正被编辑成坏 JSON,则保留当前配置不动。返回是否应用成功。"""
    global _cfg_mtime
    raw, parse_failed = None, False
    missing = not os.path.exists(CONFIG_PATH)
    if not missing:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            parse_failed = True
            if verbose:
                print(f"[CFG] 解析 {os.path.basename(CONFIG_PATH)} 失败: {e}")
    try:
        _cfg_mtime = os.path.getmtime(CONFIG_PATH) if not missing else 0.0
    except OSError:
        _cfg_mtime = 0.0
    if parse_failed and not initial:
        print("[CFG] 保留当前配置(编辑中?)")
        return False
    cfg, warns = cs.validate(raw)          # raw=None → 纯默认
    if missing:
        try:
            _write_json(CONFIG_PATH, cs.default_config())
            _cfg_mtime = os.path.getmtime(CONFIG_PATH)
            if verbose:
                print(f"[CFG] 已生成默认配置 {os.path.basename(CONFIG_PATH)}(可编辑,热重载生效)")
        except Exception as e:
            print(f"[CFG] 写默认配置失败: {e}")
    _apply_cfg(cfg, first=initial)
    if verbose:
        tag = "初始加载" if initial else "热重载"
        print(f"[CFG] {tag}完成" + (f",{len(warns)} 条告警:" if warns else ""))
        for w in warns[:12]:
            print(f"[CFG] ⚠ {w}")
    return True


def maybe_reload_config():
    """主循环调用:配置文件 mtime 变了就热重载。"""
    try:
        m = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return
    if m != _cfg_mtime:
        load_config(initial=False)


# 导入即用默认值填充全局常量(无文件 I/O),保证 `import director` 的工具/测试拿到齐全的常量;
# main() 再 load_config(initial=True) 从 JSON 覆盖(并按需生成默认文件)。
_apply_cfg(cs.default_config(), first=True)


def framing_for(preset, cam):
    """景别 preset → framing(读 FRAMING 配置表)。cx/cy 为 "face" 时用该机位实时头部位(眼中点),
    否则用数值;full/loose/high 用画面中心。ay 给足头顶留白(眼睛落上三分)。"""
    fx, fy = CAM_FACE_LIVE.get(cam, CAM_FACE.get(cam, (0.5, 0.5)))
    spec = FRAMING.get(preset) or FRAMING.get("full") or {"z": 1.0, "cx": 0.5, "cy": 0.5, "ax": 0.5, "ay": 0.5}
    return {
        "z": spec["z"],
        "cx": fx if spec["cx"] == "face" else spec["cx"],
        "cy": fy if spec["cy"] == "face" else spec["cy"],
        "ax": spec["ax"], "ay": spec["ay"],
    }


def _wchoice(weight_map, exclude=None):
    items = [(k, w) for k, w in weight_map.items() if k != exclude and w > 0]
    if not items:
        items = [(k, w) for k, w in weight_map.items() if w > 0]
    r = random.uniform(0, sum(w for _, w in items))
    acc = 0
    for k, w in items:
        acc += w
        if r <= acc:
            return k
    return items[-1][0]


def pick_cam(state, t):
    weights = dict(STATE_CAMS.get(state, {"cam1": 1}))
    if t - RT["last_wide_t"] > WIDE_BREATHER and "cam3" in weights:   # 太久没广角 → 强抬 cam3
        weights["cam3"] = weights.get("cam3", 1) + WIDE_BOOST
    return _wchoice(weights, exclude=RT["active"])


def pick_shot(state, cam):
    """加权抽 (景别 preset, 运镜 move),按该机位可用景别过滤,尽量不与上一镜同景别(避免机械重复)。"""
    allowed = CAM_PRESETS.get(cam, {"full", "loose", "high", "close", "thirds_l", "thirds_r"})
    pal = [(p, m, w) for (p, m, w) in SHOT_PALETTE.get(state, [("loose", "static", 1)]) if p in allowed]
    if not pal:
        pal = [("loose", "static", 1)]

    def draw():
        r = random.uniform(0, sum(w for _, _, w in pal))
        acc = 0
        for p, m, w in pal:
            acc += w
            if r <= acc:
                return p, m
        return pal[-1][0], pal[-1][1]

    p, m = draw()
    if p == RT.get("last_preset") and len(pal) > 1:
        p, m = draw()
    return p, m


def hold_for(state):
    lo, hi = HOLD_RANGE.get(state, (5, 9))
    h = random.uniform(lo, hi)
    if random.random() < LINGER_PROB:
        h += random.uniform(*LINGER_ADD)
    return h

# ── 共享运行时状态 ────────────────────────────────────────
RT = {
    "playing": False, "base_pos": 0.0, "base_wall": 0.0,  # 用 base_pos+已过时间 插值出精确 t
    "dur": 0.0, "mid": "", "title": "",
    "song": None,          # {words:[{text,tStart,tEnd}], chorus:[{tStart,tEnd}], end}
    "active": "cam1", "last_cut_t": -99.0, "last_state": None, "onset_cd": 0.0,
    "locked": False, "connected": False,
    # 编排
    "hold": 6.0, "last_wide_t": -999.0, "last_preset": None, "force_cut": False,
    # 运镜
    "framing": {"z": 1.0, "cx": 0.5, "cy": 0.5, "ax": 0.5, "ay": 0.5},
    "mv_from": None, "mv_to": None, "mv_start": 0.0, "mv_dur": 0.0, "mv_ease": "inOutCubic",
    "mv_kind": None, "fdirty": False,
    # 招牌长运镜(hero):活动时置为参数 dict,由 tick_hero 参数化生成 framing(接管普通插值)
    "hero": None, "last_hero_t": -999.0,
    "hero_target_t": -1.0, "hero_done": False, "force_hero": False,  # 每首歌"保证触发一次"的规划时刻/是否已触发/本次强制标志
    "hero_count": 0, "hero_seg": -1, "seg_idx": 0,  # 本歌已触发次数 / 上次触发所在段块号 / 当前段块号(用于"每段最多一次")
    "closeup": None,       # 日常动态特写:活动时置参数 dict,由 tick_closeup 接管 framing
    # 手动主镜模式(App 关闭自动切镜):由 pc-service 广播的 director_on/cam_zoom 驱动;manual_* 为跟随/放大的运行态
    "director_on": True, "cam_zoom": 100,
    "manual_active": False, "manual_cx": 0.5, "manual_cy": 0.5, "manual_z": 1.0,
}
_lock = threading.Lock()


def now_t():
    """插值当前播放时刻(秒):上次 WS 快照位置 + 之后经过的墙钟(播放中才走)。"""
    if RT["playing"]:
        return RT["base_pos"] + (time.monotonic() - RT["base_wall"])
    return RT["base_pos"]


# ── 拉取当前歌的逐字/音高/副歌(HTTP,stdlib)────────────────
def fetch_timeline(mid):
    url = f"http://{PC_SERVICE}/song/{urllib.parse.quote(mid)}/karaoke"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            k = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[TL] 取 {mid} 失败: {e}")
        return None
    words = []
    for ln in k.get("lines", []):
        for c in ln.get("chars", []):
            words.append({"text": c["text"], "tStart": c["start"] / 1000, "tEnd": (c["start"] + c["dur"]) / 1000})
    words.sort(key=lambda w: w["tStart"])
    chorus = [{"tStart": a / 1000, "tEnd": b / 1000} for a, b in k.get("chorus", [])]
    end = RT["dur"] or (words[-1]["tEnd"] + 3 if words else 0)
    print(f"[TL] {mid}: {len(words)} 字, {len(chorus)} 段副歌{'(未标注)' if not chorus else ''}")
    return {"words": words, "chorus": chorus, "end": end}


# ── 状态机(与 director-sim.html 一致)──────────────────────
def current_state(t):
    if not RT["playing"]:
        return "PAUSE"
    song = RT["song"]
    if not song or not song["words"]:
        return "PAUSE"
    w = song["words"]
    if t < w[0]["tStart"]:
        return "INTRO"
    if t > w[-1]["tEnd"]:
        return "OUTRO" if t < song["end"] else "PAUSE"
    for c in song["chorus"]:
        if c["tStart"] <= t <= c["tEnd"]:
            return "CHORUS"
    # 间奏:t 不在任何字内,且前一字结束到后一字开始间隔 > GAP(排除长音字中间,否则误判)
    in_word = False
    prev_end, next_start = -9.0, 1e9
    for x in w:
        if x["tStart"] <= t <= x["tEnd"]:
            in_word = True
            break
        if x["tEnd"] <= t:
            prev_end = max(prev_end, x["tEnd"])
        if x["tStart"] >= t:
            next_start = min(next_start, x["tStart"])
    if (not in_word) and prev_end >= 0 and (next_start - prev_end) > INTERLUDE_GAP and prev_end < t < next_start:
        return "INTERLUDE"
    return "VERSE"


def near_cut_point(t):
    """切点吸附:有字的起始落在 [t-SNAP_WINDOW, t] 内(护栏1)。"""
    for x in RT["song"]["words"]:
        if t - SNAP_WINDOW <= x["tStart"] <= t:
            return True
    return False


def plan_cut(t, s, do_cut):
    if RT["locked"]:
        if RT["active"] != "cam1":
            do_cut("cam1", "locked", s)
        return
    if s == "PAUSE":                              # 暂停/没点歌:回主机待机,保持人脸跟随(loose follow)
        if RT["active"] != "cam1" or RT["mv_kind"] != "follow":   # 不在主机 或 主机上没跑跟随镜 → 起一个 follow
            do_cut("cam1", "pause", s)
        return
    if not RT["force_cut"]:                        # 非"状态刚变"的强制切:按本镜停留时长
        overdue = t - RT["last_cut_t"]
        if overdue < RT["hold"]:                   # 还没停够 → 不切(时长随机,见 hold_for)
            return
        # 到点后:主歌/副歌尽量吸附字边界(乐句感);器乐长段等不到字、或超时太久就直接切
        if s in ("VERSE", "CHORUS") and not near_cut_point(t) and overdue < RT["hold"] + SNAP_GRACE:
            return
    RT["force_cut"] = False
    do_cut(pick_cam(s, t), "auto", s)


def plan_hero():
    """为当前歌规划一个"保证触发 hero"的目标时刻:落在演唱区间内(前半或后半随机),避开刚开口(HERO_EDGE_HEAD)
    与歌尾(留够 hero 时长 + HERO_EDGE_TAIL 缓冲,别被 OUTRO 打断)。到点时若正逢间奏,主循环会自动顺延到下一句。"""
    RT["hero_done"] = False
    RT["hero_target_t"] = -1.0
    if not HERO_GUARANTEE:
        return
    song = RT["song"]
    if not song or not song["words"]:
        return
    vs = song["words"][0]["tStart"]          # 第一个字(演唱开始)
    ve = song["words"][-1]["tEnd"]           # 最后一个字(演唱结束)
    need = HERO_PUSH_DUR + HERO_PULL_DUR + HERO_EDGE_TAIL
    lo, hi = vs + HERO_EDGE_HEAD, ve - need
    RT["hero_target_t"] = random.uniform(lo, hi) if hi > lo else max(vs, (vs + ve - need) / 2)
    print(f"[HERO] 本歌规划触发时刻 ≈ {RT['hero_target_t']:.0f}s (演唱 {vs:.0f}~{ve:.0f}s)")


def reset_director():
    RT["active"] = "cam1"
    RT["last_cut_t"] = -99.0
    RT["last_state"] = None
    RT["onset_cd"] = 0.0
    RT["hold"] = 6.0
    RT["last_wide_t"] = -999.0
    RT["last_preset"] = None
    RT["force_cut"] = True
    RT["hero"] = None
    RT["closeup"] = None
    RT["last_hero_t"] = -999.0
    RT["hero_count"] = 0
    RT["hero_seg"] = -1
    RT["seg_idx"] = 0
    plan_hero()


# ── 运镜:缓动 + framing 插值(与 director-sim.html 一致)────
_EASE = {
    "inOutCubic": lambda x: 4 * x * x * x if x < 0.5 else 1 - ((-2 * x + 2) ** 3) / 2,
    "outCubic": lambda x: 1 - (1 - x) ** 3,
    "linear": lambda x: x,
}


def start_move(kind, to, dur, ease="inOutCubic"):
    RT["mv_from"] = dict(RT["framing"])
    base = {"z": 1.0, "cx": 0.5, "cy": 0.5, "ax": 0.5, "ay": 0.5}
    base.update(to)
    RT["mv_to"] = base
    RT["mv_start"] = now_t()
    RT["mv_dur"] = dur
    RT["mv_ease"] = ease
    RT["mv_kind"] = kind
    RT["fdirty"] = True
    print(f"[MOVE] {now_t():6.1f}s  {kind:6s} on {RT['active']} → z{base['z']:.2f}  ({dur:.1f}s)")


def tick_move(t):
    if RT["mv_to"] is None:
        return
    p = min(1.0, (t - RT["mv_start"]) / RT["mv_dur"]) if RT["mv_dur"] > 0 else 1.0
    e = _EASE.get(RT["mv_ease"], _EASE["linear"])(p)
    f = {k: RT["mv_from"][k] + (RT["mv_to"][k] - RT["mv_from"][k]) * e
         for k in ("z", "cx", "cy", "ax", "ay")}
    RT["framing"] = f
    RT["fdirty"] = True
    if p >= 1.0:
        RT["mv_to"] = None
        RT["mv_kind"] = None


def apply_shot(preset, move, cam):
    """把抽中的镜头(景别+运镜)应用出去:静止=直接落位;运镜=设起点+启动插值;closeup=启动动态特写(锁脸+推拉+晃)。"""
    target = framing_for(preset, cam)
    if move in ("closeup", "trackpan", "follow"):  # 锁脸镜:closeup=原地特写, trackpan=横移, follow=默认主镜纯跟随
        if ENABLE_MOVES and cam in CLOSEUP_CAMS:
            start_closeup(cam, preset, pan=(move == "trackpan"), still=(move == "follow"))
            return
        move = "static"                 # 该机位/未启用运镜 → 退成静态普通景别
    if not ENABLE_MOVES or move == "static":
        RT["framing"] = dict(target)
        RT["mv_to"] = None
        RT["mv_kind"] = None
        RT["fdirty"] = True
        return
    mv = MOVES
    if move in ("descend", "ascend"):      # cam3 广角组合运镜:z + cy 同步(前推下降 / 后退上升)+ 偶尔横移
        p = mv[move]
        cx0 = cx1 = 0.5
        if random.random() < p["pan_prob"]:            # 随机是否横移 + 随机方向(环绕感)
            d = p["pan_amp"] / 2.0
            cx0, cx1 = (0.5 - d, 0.5 + d) if random.random() < 0.5 else (0.5 + d, 0.5 - d)
        start = {"z": p["z_from"], "cx": cx0, "cy": p["cy_from"], "ax": 0.5, "ay": 0.5}
        goal = {"z": p["z_to"], "cx": cx1, "cy": p["cy_to"], "ax": 0.5, "ay": 0.5}
        RT["framing"] = start
        start_move(move, goal, random.uniform(*p["dur"]), p["ease"])
        return
    if target["z"] < mv["min_move_z"]:     # 运镜要放大留边距,否则被防黑边钳制看不出动
        target = dict(target)
        target["z"] = mv["min_move_z"]
    if move == "trackLR":                  # 主体从右滑到左(锚点 ax_start→ax_end,z 留移动余量)
        p = mv["trackLR"]
        target = dict(target)
        target["z"] = max(target["z"], p["z_min"])
        target["ax"] = p["ax_end"]
        start = dict(target)
        start["ax"] = p["ax_start"]
        dur, ease = random.uniform(*p["dur"]), p["ease"]
        RT["framing"] = start
        start_move(move, target, dur, ease)
        return
    start = dict(target)
    if move == "push":                     # 缓推:起点更松更远 → 落到目标
        p = mv["push"]
        start["z"] = max(1.0, target["z"] + p["z_start_delta"])
        dur, ease = random.uniform(*p["dur"]), p["ease"]
    elif move == "pull":                   # 缓拉/后撤:起点更紧 → 拉开
        p = mv["pull"]
        start["z"] = target["z"] + p["z_start_delta"]
        dur, ease = random.uniform(*p["dur"]), p["ease"]
    elif move == "pan":                    # 平移:横向移动关注点
        p = mv["pan"]
        start["cx"] = min(p["cx_start_max"], target["cx"] + p["cx_start_delta"])
        dur, ease = random.uniform(*p["dur"]), p["ease"]
    else:                                  # drift 极慢微推,给静镜一点呼吸
        p = mv["drift"]
        start["z"] = max(1.0, target["z"] * p["z_start_factor"])
        dur, ease = random.uniform(*p["dur"]), p["ease"]
    RT["framing"] = start
    start_move(move, target, dur, ease)


def _smoothstep(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def _z_for_head(cam, head_ratio):
    """把"人头占画面高度比例"换成缩放 z(假设 16:9 源等比铺满 4:3 画布,高度受限 ⇒ 输出头高比 = 头源高比 × z)。
    脸框高先钳到 HERO_FACEH_CLAMP,防 YuNet 误检的极小框(如占位图上测到 0.09)把 z 换算甩到上限。"""
    lo, hi = HERO_FACEH_CLAMP
    head_frac = HERO_HEAD_FACTOR * min(hi, max(lo, CAM_FACE_H.get(cam, 0.16)))
    return head_ratio / head_frac


def start_hero(cam):
    """启动主机招牌长运镜:z 从 1.0(最远不黑边)慢推到"头占 3/4"超特写,再速拉回"头占 <1/2"。
    关注点 cx/cy 在此刻锁定一次、整遍不变(固定机位推镜,不逐帧跟踪 → 消除放大下的位置抖动)。"""
    fx, fy = CAM_FACE_LIVE.get(cam, CAM_FACE.get(cam, (0.5, 0.5)))
    fx = min(0.70, max(0.30, fx))         # 关注点钳到合理区间,防跟踪误检把镜头甩偏到边角
    fy = min(0.62, max(0.30, fy))
    z_near = min(HERO_Z_MAX, max(HERO_Z_NEAR_MIN, _z_for_head(cam, HERO_HEAD_NEAR)))
    z_end = min(_z_for_head(cam, HERO_HEAD_END), z_near * HERO_PULL_RATIO)   # 收尾确保 <1/2 且后拉明显
    z_end = max(1.0, min(z_end, z_near - 0.4))
    RT["hero"] = {"cam": cam, "t0": now_t(), "cx": fx, "cy": fy, "z_far": 1.0, "z_near": z_near, "z_end": z_end}
    RT["mv_to"] = None                    # 停掉普通插值,交给 tick_hero
    RT["mv_kind"] = "hero"
    RT["framing"] = {"z": 1.0, "cx": fx, "cy": fy, "ax": 0.5, "ay": HERO_AY_FAR}
    RT["fdirty"] = True
    RT["last_hero_t"] = now_t()
    print(f"[HERO] {now_t():6.1f}s  主机长推 on {cam}  z1.0→{z_near:.2f}(头{HERO_HEAD_NEAR:.0%})"
          f"→{z_end:.2f}(头<50%)  {HERO_PUSH_DUR + HERO_PULL_DUR:.0f}s")


def tick_hero(t):
    """参数化生成 hero 当前 framing。返回 True=本帧由 hero 接管(跳过普通插值);False=无 hero 或已结束。"""
    h = RT["hero"]
    if not h:
        return False
    cam = h["cam"]
    tau = t - h["t0"]
    total = HERO_PUSH_DUR + HERO_PULL_DUR
    # 关注点:向实时头位"慢速低通跟随"——既不逐帧硬跟(会把 YuNet 0.5s 阶跃放大成抖),
    # 也不完全锁死(人一动就对不准/走出框);低通滤掉噪声与阶跃,只平滑跟上直播中的轻微移动。
    lx, ly = CAM_FACE_LIVE.get(cam, (h["cx"], h["cy"]))
    lx = min(0.70, max(0.30, lx)); ly = min(0.62, max(0.30, ly))
    h["cx"] += (lx - h["cx"]) * HERO_FOLLOW_ALPHA
    h["cy"] += (ly - h["cy"]) * HERO_FOLLOW_ALPHA
    fx, fy = h["cx"], h["cy"]
    if tau <= HERO_PUSH_DUR:                        # 阶段1:慢推(smoothstep 起步/收尾都缓)
        p = tau / HERO_PUSH_DUR if HERO_PUSH_DUR > 0 else 1.0
        e = _smoothstep(p)
        z = h["z_far"] + (h["z_near"] - h["z_far"]) * e
        ay = HERO_AY_FAR + (HERO_AY_NEAR - HERO_AY_FAR) * e
        sway_env = _smoothstep(min(1.0, tau / 2.0)) * _smoothstep(min(1.0, (HERO_PUSH_DUR - tau) / 1.5))
    elif tau <= total:                              # 阶段2:速拉(outCubic 先快后缓地后撤)
        p = (tau - HERO_PUSH_DUR) / HERO_PULL_DUR if HERO_PULL_DUR > 0 else 1.0
        e = 1 - (1 - p) ** 3
        z = h["z_near"] + (h["z_end"] - h["z_near"]) * e
        ay = HERO_AY_NEAR + (HERO_AY_FAR - HERO_AY_NEAR) * e
        sway_env = (1 - p) * 0.5                     # 摇晃随后拉渐隐
    else:                                           # 结束:释放,末帧 framing 由普通系统冻结保持到下次切镜
        RT["hero"] = None
        RT["mv_kind"] = None
        return False
    # 左右轻晃:两正弦叠加(主摆 + 更快小摆)+ 极轻竖摆,像真人手持;sway_env 控制两端渐入渐出
    sway = HERO_SWAY_AMP * (math.sin(2 * math.pi * tau / HERO_SWAY_PERIOD)
                            + 0.4 * math.sin(2 * math.pi * tau / (HERO_SWAY_PERIOD * 0.37)))
    RT["framing"] = {"z": z, "cx": fx, "cy": fy,
                     "ax": 0.5 + sway * sway_env, "ay": ay + 0.35 * sway * sway_env}
    RT["fdirty"] = True
    return True


def start_closeup(cam, preset, pan=False, still=False):
    """启动锁脸镜。三态:
    - pan=True:横移(人像从画面一侧拟人扫到另一侧,方向随机;close/xclose 为放大横移,loose/full 为正常景别横移)。
    - still=True:默认主镜纯跟随(不推拉、不横移、居中),只锁脸慢跟随 —— 给"唱完回主机待机"用,稳。
    - 都不设:原地动态特写(close/xclose 放大到"头占~1/2",随机轻推/拉)。
    三态都锁脸慢跟随 + 拟人轻晃,时长=普通停留(RT['hold'])。z 计算/跟随/晃动均借鉴 hero。"""
    fx, fy = CAM_FACE_LIVE.get(cam, CAM_FACE.get(cam, (0.5, 0.5)))
    fx = min(0.70, max(0.30, fx)); fy = min(0.62, max(0.30, fy))
    if preset in ("close", "xclose"):               # 特写档:按脸框动态放大到头占目标
        head = CLOSEUP_XHEAD if preset == "xclose" else CLOSEUP_HEAD
        z_target = min(CLOSEUP_Z_MAX, max(CLOSEUP_Z_MIN, _z_for_head(cam, head)))
    else:                                           # 正常景别档:用该景别固定 z,横移时保底留余量
        z_target = framing_for(preset, cam)["z"]
    if pan:                                         # 横移:z 不推拉(起=止,留余量),ax 从一端缓动到另一端,方向随机
        z_target = max(z_target, CLOSEUP_PAN_ZMIN)
        z_start = z_target
        lo, hi = CLOSEUP_PAN_AX
        ax0, ax1 = (lo, hi) if random.random() < 0.5 else (hi, lo)
    elif still:                                     # 纯跟随:z 不推拉、居中(z 不依赖时钟,待机 not playing 也稳)
        z_start = z_target
        ax0 = ax1 = 0.5
    else:                                           # 原地:轻推或拉,ax 居中
        z_start = max(1.0, z_target - CLOSEUP_PUSH) if random.random() < 0.5 else z_target + CLOSEUP_PUSH
        ax0 = ax1 = 0.5
    RT["closeup"] = {"cam": cam, "t0": now_t(), "cx": fx, "cy": fy, "ax0": ax0, "ax1": ax1,
                     "z_start": z_start, "z_target": z_target, "dur": max(2.0, RT["hold"])}
    RT["mv_to"] = None                              # 停掉普通插值,交给 tick_closeup
    RT["mv_kind"] = "trackpan" if pan else ("follow" if still else "closeup")
    RT["framing"] = {"z": z_start, "cx": fx, "cy": fy, "ax": ax0, "ay": CLOSEUP_AY}
    RT["fdirty"] = True


def tick_closeup(t):
    """参数化生成 closeup/trackpan 当前 framing(锁脸慢跟随 + 轻推拉 或 横移 + 拟人晃)。返回 True=本帧接管。
    不自行结束——持续到下次切镜(do_cut 清 RT['closeup'])。"""
    c = RT["closeup"]
    if not c:
        return False
    cam = c["cam"]
    tau = t - c["t0"]
    lx, ly = CAM_FACE_LIVE.get(cam, (c["cx"], c["cy"]))   # 锁脸:向实时头位慢跟随(滤抖,跟轻微移动)
    lx = min(0.70, max(0.30, lx)); ly = min(0.62, max(0.30, ly))
    c["cx"] += (lx - c["cx"]) * CLOSEUP_FOLLOW_ALPHA
    c["cy"] += (ly - c["cy"]) * CLOSEUP_FOLLOW_ALPHA
    p = min(1.0, tau / c["dur"]) if c["dur"] > 0 else 1.0
    z = c["z_start"] + (c["z_target"] - c["z_start"]) * _smoothstep(p)   # 轻推拉缓动到目标后保持(横移时起=止)
    axb = c["ax0"] + (c["ax1"] - c["ax0"]) * _smoothstep(p)              # 横移:人像从一端缓动到另一端(原地时 ax0=ax1)
    sway = CLOSEUP_SWAY_AMP * (math.sin(2 * math.pi * tau / CLOSEUP_SWAY_PERIOD)
                               + 0.4 * math.sin(2 * math.pi * tau / (CLOSEUP_SWAY_PERIOD * 0.37)))
    RT["framing"] = {"z": z, "cx": c["cx"], "cy": c["cy"],
                     "ax": axb + sway, "ay": CLOSEUP_AY + 0.35 * sway}
    RT["fdirty"] = True
    return True


def manual_tick(obs):
    """手动主镜模式(App 关闭自动切镜):锁 cam1 + z=cam_zoom/100 + 人脸跟随。不跑状态机/编排。
    与待机 PAUSE 的 follow 同一跟随内核(cx/cy 低通),z 由 cam_zoom 滑块驱动(再低通更顺、夹到护栏)。"""
    cam = HERO_CAM                      # 主机(默认 cam1)
    if not RT["manual_active"]:         # 刚进 manual:切主机、清接管镜、seed 跟随起点
        RT["manual_active"] = True
        RT["hero"] = None
        RT["closeup"] = None
        RT["mv_to"] = None
        fx, fy = CAM_FACE_LIVE.get(cam, CAM_FACE.get(cam, (0.5, 0.5)))
        RT["manual_cx"] = min(0.70, max(0.30, fx))
        RT["manual_cy"] = min(0.62, max(0.30, fy))
        RT["manual_z"] = min(MANUAL_Z_MAX, max(1.0, RT["cam_zoom"] / 100.0))
        RT["active"] = cam
        obs.cut(cam)
        print(f"[手动] 关闭自动切镜 → 主机 {cam} 人脸跟随, z={RT['manual_z']:.2f}(cam_zoom {RT['cam_zoom']})")
    # 锁脸低通跟随(滤 YuNet 阶跃/噪声,跟人缓动)
    lx, ly = CAM_FACE_LIVE.get(cam, (RT["manual_cx"], RT["manual_cy"]))
    lx = min(0.70, max(0.30, lx)); ly = min(0.62, max(0.30, ly))
    RT["manual_cx"] += (lx - RT["manual_cx"]) * MANUAL_FOLLOW_ALPHA
    RT["manual_cy"] += (ly - RT["manual_cy"]) * MANUAL_FOLLOW_ALPHA
    # z 向 cam_zoom 目标低通(滑块拖动更顺)
    z_target = min(MANUAL_Z_MAX, max(1.0, RT["cam_zoom"] / 100.0))
    RT["manual_z"] += (z_target - RT["manual_z"]) * MANUAL_ZOOM_LOWPASS
    RT["framing"] = {"z": RT["manual_z"], "cx": RT["manual_cx"], "cy": RT["manual_cy"],
                     "ax": 0.5, "ay": MANUAL_AY}
    obs.set_framing(cam, RT["framing"])


def build_demo_song():
    """内置演示曲(与 director-sim.html 一致):无 pc-service 也能在 OBS 里看自动切镜。"""
    words = []
    t = [5.0]

    def push(txt, n, dur):
        for i in range(n):
            words.append({"text": txt[i % len(txt)], "tStart": t[0], "tEnd": t[0] + dur * 0.9})
            t[0] += dur

    push("轻轻的风吹过窗", 7, 0.55); t[0] += 0.6
    push("你的样子还在心上", 8, 0.5); t[0] += 4.5
    c1 = t[0]; push("就这样看着你走", 7, 0.42); push("走过我的春秋", 7, 0.42); c1e = t[0]; t[0] += 5.0
    push("风又吹过山丘", 7, 0.5); t[0] += 0.5
    push("岁月不肯回头", 7, 0.5); t[0] += 4.0
    c2 = t[0]; push("就这样看着你走", 7, 0.4); push("走过我的春秋", 7, 0.4)
    push("直到白了头", 6, 0.45); c2e = t[0]; t[0] += 3.0
    return {"words": words, "chorus": [{"tStart": c1, "tEnd": c1e}, {"tStart": c2, "tEnd": c2e}],
            "end": t[0] + 3}


# ── pc-service WS 订阅(被动只读)───────────────────────────
def start_ws():
    import websocket   # websocket-client

    def on_message(ws, raw):
        try:
            d = json.loads(raw)
        except Exception:
            return
        if d.get("type") != "state":
            return
        new_mid = d.get("k_mid", "") or ""
        need_fetch = None
        with _lock:
            RT["base_pos"] = (d.get("k_pos", 0) or 0) / 1000
            RT["base_wall"] = time.monotonic()
            RT["dur"] = (d.get("k_dur", 0) or 0) / 1000
            RT["playing"] = bool(d.get("k_playing"))
            RT["title"] = d.get("k_title", "")
            if "director_on" in d:                       # App「自动切镜运镜」开关:True=自动编排, False=手动主镜跟随
                RT["director_on"] = bool(d.get("director_on"))
            if "cam_zoom" in d:                          # 手动主镜放大档位 100~250(→ z=cam_zoom/100)
                RT["cam_zoom"] = d.get("cam_zoom") or 100
            if new_mid and new_mid != RT["mid"]:
                RT["mid"] = new_mid
                need_fetch = new_mid
        # 换歌 → 拉逐字轴放**独立线程**:fetch_timeline 阻塞最长 5s,绝不能卡在 WS 线程里
        # (否则拉取期间收不到 state 帧,base_pos/base_wall 不更新,时钟会漂/卡)。
        if need_fetch:
            def _load(mid=need_fetch):
                song = fetch_timeline(mid)
                with _lock:
                    if RT["mid"] == mid:         # 期间没再换歌才采用
                        RT["song"] = song
                        reset_director()
            threading.Thread(target=_load, daemon=True).start()

    def on_open(ws):
        RT["connected"] = True
        print(f"[WS] 已连接 pc-service ws://{PC_SERVICE}/ws")

    def on_close(ws, *a):
        RT["connected"] = False
        print("[WS] 断开,3s 后重连…")

    def on_error(ws, e):
        print(f"[WS] 错误: {e}")

    def run():
        while True:
            try:
                ws = websocket.WebSocketApp(
                    f"ws://{PC_SERVICE}/ws",
                    on_open=on_open, on_message=on_message,
                    on_close=on_close, on_error=on_error)
                ws.run_forever()
            except Exception as e:
                print(f"[WS] run_forever 异常: {e}")
            time.sleep(3)

    threading.Thread(target=run, daemon=True).start()


# ── OBS 驱动 ──────────────────────────────────────────────
class ObsDriver:
    def __init__(self, dry):
        self.dry = dry
        self.cl = None
        self.moves_ok = False
        self.CW = self.CH = 0
        self.items = {}          # cam -> (scene, itemId, Sw, Sh)
        if dry:
            print("[OBS] --dry-run:不连 OBS,只打印切镜决策")
            return
        import obsws_python as obs
        self.cl = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5)
        print(f"[OBS] 已连接 {OBS_HOST}:{OBS_PORT}")
        if ENABLE_MOVES:
            try:
                gv = self.cl.get_video_settings()
                self.CW, self.CH = gv.base_width, gv.base_height
                for cam, scene in SCENES.items():
                    name = CONTENT_NAME.format(cam=cam)
                    iid = self.cl.get_scene_item_id(scene, name).scene_item_id
                    tr = self.cl.get_scene_item_transform(scene, iid).scene_item_transform
                    self.items[cam] = (scene, iid, tr["sourceWidth"], tr["sourceHeight"])
                self.moves_ok = bool(self.items)
                print(f"[OBS] 运镜就绪: 画布 {self.CW}x{self.CH}, content 源 {list(self.items)}")
            except Exception as e:
                print(f"[OBS] 运镜初始化失败(降级为仅切镜): {e}")
                self.moves_ok = False

    def cut(self, cam):
        scene = SCENES.get(cam, cam)
        if self.dry or self.cl is None:
            return
        try:
            self.cl.set_current_program_scene(scene)
        except Exception as e:
            print(f"[OBS] 切场景 {scene} 失败: {e}")

    def set_framing(self, cam, f):
        """把归一化 framing 转成 OBS 变换应用到该机位的 content 源(源上 cx,cy 放大 z 落到输出 ax,ay)。"""
        if self.dry or self.cl is None or not self.moves_ok:
            return
        it = self.items.get(cam)
        if not it:
            return
        scene, iid, sw, sh = it
        z = max(1.0, f["z"])   # z<1 无法盖满画布,兜底为 1
        # 等比缩放"铺满"画布(cover),不按 X/Y 分别拉伸——真相机多为 16:9,画布 4:3,
        # 分别缩放会把画面压扁变形;cover = 等比放到能盖满、多出的边裁掉,任何画幅都不失真。
        base = max(self.CW / sw, self.CH / sh)   # z=1 时刚好盖满画布的等比系数
        sc = base * z
        sws, shs = sw * sc, sh * sc              # 缩放后的源尺寸(像素)
        posX = f["ax"] * self.CW - f["cx"] * sws
        posY = f["ay"] * self.CH - f["cy"] * shs
        # 防黑边:把位置夹到"仍覆盖画布"的区间(image 覆盖 [0,CW] 要求 posX∈[CW-sws, 0])
        posX = min(0.0, max(self.CW - sws, posX))
        posY = min(0.0, max(self.CH - shs, posY))
        try:
            self.cl.set_scene_item_transform(scene, iid, {
                "boundsType": "OBS_BOUNDS_NONE",   # 清残留边界框(否则覆盖 scale 致黑边/变形)
                "scaleX": float(sc), "scaleY": float(sc),
                "positionX": float(posX), "positionY": float(posY),  # 转 Python float:跟踪值是 numpy float32,JSON 不认
                "cropLeft": 0, "cropRight": 0, "cropTop": 0, "cropBottom": 0,
                "rotation": 0.0, "alignment": 5,   # 5 = 左上对齐
            })
        except Exception as e:
            print(f"[OBS] set_framing {cam} 失败: {e}")


# ── 实时头部跟踪(YuNet)────────────────────────────────────
def start_tracker():
    """后台线程:定时抓每台相机源画面,YuNet 检测头部,EMA 平滑后写 CAM_FACE_LIVE(眼中点)。
    独立 obs 连接(不与主循环抢);检测不到就保留上次。特写/三分每次切镜读它锁定最新头位。"""
    if not TRACK_FACE:
        return
    try:
        import cv2
        import base64
        import numpy as np
        import obsws_python as obs
        det = cv2.FaceDetectorYN.create(FACE_MODEL, "", (320, 320), DET_SCORE, DET_NMS, DET_TOPK)
        cl = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5)
    except Exception as e:
        print(f"[TRACK] 初始化失败,退回静态人脸位: {e}")
        return
    print("[TRACK] YuNet 头部跟踪已启动")

    def run():
        while True:
            for cam in SCENES:
                try:
                    r = cl.get_source_screenshot("content_" + cam, "jpg", SHOT_W, SHOT_H, -1)
                    img = cv2.imdecode(np.frombuffer(base64.b64decode(r.image_data.split(",", 1)[1]), np.uint8), cv2.IMREAD_COLOR)
                    H, W = img.shape[:2]
                    det.setInputSize((W, H))
                    _, faces = det.detect(img)
                    if faces is not None and len(faces):
                        f = max(faces, key=lambda f: f[2] * f[3])
                        ex = (f[4] + f[6]) / 2 / W      # 两眼中点 x
                        ey = (f[5] + f[7]) / 2 / H      # 两眼中点 y
                        px, py = CAM_FACE_LIVE.get(cam, (0.5, 0.5))
                        a = TRACK_SMOOTH
                        CAM_FACE_LIVE[cam] = (float(round(px + (ex - px) * a, 3)), float(round(py + (ey - py) * a, 3)))
                        fh = f[3] / H                 # 脸框高占画面比 → 供 hero 换算"头占 3/4"的 z
                        ph = CAM_FACE_H.get(cam, 0.16)
                        CAM_FACE_H[cam] = float(round(ph + (fh - ph) * a, 3))
                except Exception:
                    pass
            time.sleep(TRACK_INTERVAL)

    threading.Thread(target=run, daemon=True).start()


# ── 主循环 ────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不连 OBS,只打印切镜决策")
    ap.add_argument("--demo", action="store_true", help="不连 pc-service,用内置演示曲的时钟循环驱动 OBS")
    args = ap.parse_args()

    load_config(initial=True)           # 读 director_config.json(缺失则生成默认)→ 校验 → 灌进全局常量
    obs = ObsDriver(args.dry_run)
    if not args.dry_run:
        start_tracker()                 # YuNet 头部实时跟踪(特写锁人)
    if args.demo:
        RT["song"] = build_demo_song()
        RT["dur"] = RT["song"]["end"]
        RT["title"] = "演示曲"
        RT["playing"] = True
        RT["connected"] = True
        RT["base_pos"] = 0.0
        RT["base_wall"] = time.monotonic()
        reset_director()        # 规划本(演示)曲的 hero 保证触发时刻
        print(f"[DEMO] 内置演示曲({RT['dur']:.0f}s)循环驱动 OBS,不需 pc-service")
    else:
        start_ws()

    def do_cut(cam, reason, s):
        RT["active"] = cam
        RT["last_cut_t"] = now_t()
        RT["onset_cd"] = 0.5
        RT["hero"] = None                       # 换镜先清掉上一段 hero/closeup(否则 tick_ 会继续接管新机位)
        RT["closeup"] = None
        if cam == "cam3":                       # 记录广角时刻(周期性呼吸用)
            RT["last_wide_t"] = now_t()
        obs.cut(cam)
        # 主机·主歌/副歌 → 启用招牌 18s 长推。本歌"保证触发"那次(force_hero)无条件走;额外机会需:保证已完成、
        # 未达每首上限(HERO_MAX_PER_SONG)、当前段块未用过(seg_idx≠hero_seg,即"每段最多一次")、冷却过、概率中。
        if (ENABLE_MOVES and HERO_ENABLE and cam == HERO_CAM and s in ("VERSE", "CHORUS")
                and (RT["force_hero"]
                     or (RT["hero_done"] and RT["hero_count"] < HERO_MAX_PER_SONG and RT["seg_idx"] != RT["hero_seg"]
                         and now_t() - RT["last_hero_t"] > HERO_COOLDOWN and random.random() < HERO_PROB))):
            RT["hero_count"] += 1
            RT["hero_seg"] = RT["seg_idx"]      # 记住本段已用掉 hero(同段块不再触发)
            RT["last_preset"] = "hero"
            RT["hold"] = HERO_PUSH_DUR + HERO_PULL_DUR + 0.5    # 停到长镜跑完再考虑下次切
            start_hero(cam)
            print(f"[切] {now_t():6.1f}s → {cam} hero长推({RT['hero_count']}/{HERO_MAX_PER_SONG}) 停{RT['hold']:.0f}s [{s}·{reason}] ♪ {RT['title']}")
            return
        preset, move = pick_shot(s, cam)        # 抽一个镜头(景别×运镜,按机位可用景别过滤)
        RT["last_preset"] = preset
        RT["hold"] = hold_for(s)                # 本镜停留时长(随机)
        apply_shot(preset, move, cam)
        print(f"[切] {now_t():6.1f}s → {cam} {preset:8s}/{move:6s} 停{RT['hold']:.0f}s [{s}·{reason}] ♪ {RT['title']}")

    print("状态机运行中(Ctrl+C 退出)。等 pc-service 有歌在放就会开始切镜。")
    last_state_print = None
    try:
        while True:
            dt = 1.0 / max(1, TICK_HZ)               # 每帧按 TICK_HZ 现值算(热重载可改帧率)
            time.sleep(dt)
            RT["_n"] = RT.get("_n", 0) + 1
            if RT["_n"] % 30 == 0:                   # 每 ~1s 检查配置文件 mtime,变了就热重载
                maybe_reload_config()
            if not RT["connected"]:
                continue
            if not RT["director_on"]:                # App 关闭自动切镜 → 手动主镜(锁 cam1+跟脸+cam_zoom 放大),不依赖歌
                with _lock:
                    manual_tick(obs)
                continue
            if RT["manual_active"]:                  # 刚从手动切回自动 → 复位让状态机立即重切
                RT["manual_active"] = False
                RT["force_cut"] = True
            # 自动模式待机:没点歌(song None)也不空转——进状态机会判 PAUSE,驱动 cam1 待机跟随(跟脸,同关闭自动模式)
            if args.demo and RT["song"] and now_t() > RT["song"]["end"] + 2:   # 演示曲循环重播
                RT["base_wall"] = time.monotonic()
                reset_director()
                last_state_print = None
                print("[DEMO] 循环重播")
            with _lock:
                t = now_t()
                RT["onset_cd"] = max(0.0, RT["onset_cd"] - dt)
                s = current_state(t)
                if s != RT["last_state"]:
                    RT["last_state"] = s
                    RT["seg_idx"] += 1          # 段块换号(每次状态切换即新段块 → "每段最多一次 hero")
                    RT["force_cut"] = True      # 状态一变 → 立刻切一个新镜头(响应段落切换)
                    if s != last_state_print:
                        print(f"[段] {t:6.1f}s  {s}")
                        last_state_print = s
                # 保证触发:到规划时刻且正在演唱段(VERSE/CHORUS,即不卡间奏/前奏/尾奏)→ 强制切主机跑一次 hero;
                # 到点若逢间奏(INTERLUDE)则条件不满足、留到下一句自动触发(顺延不卡间奏)。
                if (ENABLE_MOVES and HERO_ENABLE and HERO_GUARANTEE and not RT["hero_done"]
                        and 0 < RT["hero_target_t"] <= t and s in ("VERSE", "CHORUS")):
                    RT["hero_done"] = True
                    RT["force_hero"] = True
                    do_cut(HERO_CAM, "保证", s)
                    RT["force_hero"] = False
                else:
                    plan_cut(t, s, do_cut)
                if ENABLE_MOVES:               # 30Hz 插值当前 framing → OBS(只在有变化时发)
                    # 接管优先级:hero(招牌长镜) > closeup(日常动态特写) > 普通运镜插值(三者互斥,do_cut 已清)
                    if not (tick_hero(t) or tick_closeup(t)):
                        tick_move(t)
                    if RT["fdirty"]:
                        RT["fdirty"] = False
                        obs.set_framing(RT["active"], RT["framing"])
                # 低频心跳:长前奏/间奏期间只有状态没切镜,靠它看出"在工作而非卡死"(_n 在循环顶部自增)
                if RT["_n"] % 300 == 0:
                    print(f"[心跳] t={t:.0f}s pos={RT['base_pos']:.0f}s {s} 直播机位={RT['active']}")
    except KeyboardInterrupt:
        print("\n退出。")


if __name__ == "__main__":
    main()
