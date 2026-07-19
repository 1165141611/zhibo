# -*- coding: utf-8 -*-
"""auto-director 切镜/运镜参数的 schema —— 单一事实源
=====================================================
这里集中定义**所有**可调参数的：默认值 / 类型 / 范围(min,max) / 单位 / 分组 / 中文标签 / 说明。
- `default_config()`：产出一份 json 友好的默认配置(== director.py 迁移前的原值,行为不变)。
- `validate(raw)`：把外部 JSON 按 schema **强制类型、范围钳位、枚举校验、跨字段规则**校正,
  返回 `(cfg, warnings)`；任何非法项都**只告警+自动纠正,绝不抛异常**(坏配置不该搞崩 director)。
- 后期可视化 UI 直接读本 schema(type/min/max/label/group)自动渲染滑块/表单,写回 director_config.json。

JSON 无 set/tuple → 一律用 list 存;director.py 的 `_apply_cfg` 再转回 set/tuple。
`framing` 的 cx/cy 用字符串 `"face"` 表示"用实时人脸位 CAM_FACE_LIVE",其余为数值。

live=True 的参数支持热重载即时生效;live=False 的(连接/跟踪线程/模型)改后需重启 director。
"""
import copy

# ── 枚举全集(校验用)────────────────────────────────────────
CAMS = ["cam1", "cam2", "cam3"]
STATES = ["INTRO", "VERSE", "CHORUS", "INTERLUDE", "OUTRO", "PAUSE"]
PRESETS = ["full", "loose", "high", "close", "xclose", "thirds_l", "thirds_r"]
MOVES = ["static", "push", "pull", "pan", "drift", "trackLR", "closeup", "trackpan", "follow", "descend", "ascend"]
EASES = ["linear", "inOutCubic", "outCubic"]

# ── 标量字段:group → key → 规格 ────────────────────────────
SCALARS = {
    "rhythm": {
        "tick_hz":       {"type": "int",   "default": 30,   "min": 10,  "max": 60,  "unit": "Hz", "label": "状态机频率",   "live": True,  "desc": "每秒评估/插值次数"},
        "interlude_gap": {"type": "float", "default": 4.0,  "min": 1.0, "max": 10.0, "unit": "s",  "label": "间奏判定间隔", "live": True,  "desc": "相邻字间隔>此值判为间奏"},
        "snap_window":   {"type": "float", "default": 0.15, "min": 0.0, "max": 1.0,  "unit": "s",  "label": "切点吸附窗",   "live": True,  "desc": "字起始落在[t-此值,t]才允许切"},
        "snap_grace":    {"type": "float", "default": 2.5,  "min": 0.0, "max": 10.0, "unit": "s",  "label": "吸附宽限",     "live": True,  "desc": "等不到字边界超此宽限直接切"},
        "wide_breather": {"type": "float", "default": 45.0, "min": 10.0, "max": 120.0, "unit": "s", "label": "广角呼吸间隔", "live": True,  "desc": "超此久没广角则强抬cam3"},
        "wide_boost":    {"type": "int",   "default": 8,    "min": 0,   "max": 30,   "unit": "",   "label": "广角提权",     "live": True,  "desc": "呼吸触发时给cam3加的权重"},
        "linger_prob":   {"type": "float", "default": 0.18, "min": 0.0, "max": 1.0,  "unit": "",   "label": "多停留概率",   "live": True,  "desc": "某镜多停留一会儿的概率"},
        "linger_add_lo": {"type": "float", "default": 2.0,  "min": 0.0, "max": 10.0, "unit": "s",  "label": "多停留下限",   "live": True,  "desc": "多停留加时的下限"},
        "linger_add_hi": {"type": "float", "default": 5.0,  "min": 0.0, "max": 15.0, "unit": "s",  "label": "多停留上限",   "live": True,  "desc": "多停留加时的上限"},
        "enable_moves":  {"type": "bool",  "default": True,                                        "label": "启用运镜",     "live": True,  "desc": "关则只切镜不运镜"},
    },
    "closeup": {
        "head":         {"type": "float", "default": 0.50, "min": 0.2, "max": 0.9,  "unit": "", "label": "特写头占比",   "live": True, "desc": "close 目标:人头占画面高"},
        "xhead":        {"type": "float", "default": 0.62, "min": 0.2, "max": 0.95, "unit": "", "label": "超特写头占比", "live": True, "desc": "xclose 目标头占比(更紧)"},
        "z_min":        {"type": "float", "default": 1.15, "min": 1.0, "max": 2.0,  "unit": "", "label": "特写z下限",    "live": True, "desc": "特写最小放大"},
        "z_max":        {"type": "float", "default": 2.6,  "min": 1.2, "max": 4.0,  "unit": "", "label": "特写z上限",    "live": True, "desc": "特写最大放大(防糊)"},
        "push":         {"type": "float", "default": 0.12, "min": 0.0, "max": 0.5,  "unit": "", "label": "特写推拉幅度", "live": True, "desc": "镜头内轻推/拉的z幅度"},
        "sway_amp":     {"type": "float", "default": 0.012, "min": 0.0, "max": 0.1, "unit": "", "label": "特写晃动幅度", "live": True, "desc": "拟人晃动幅度"},
        "sway_period":  {"type": "float", "default": 5.0,  "min": 1.0, "max": 15.0, "unit": "s","label": "特写晃动周期", "live": True, "desc": "拟人晃动主周期"},
        "follow_alpha": {"type": "float", "default": 0.03, "min": 0.0, "max": 0.5,  "unit": "", "label": "锁脸跟随系数", "live": True, "desc": "越大跟得越快但易抖;0=锁死"},
        "ay":           {"type": "float", "default": 0.42, "min": 0.2, "max": 0.6,  "unit": "", "label": "特写头顶留白", "live": True, "desc": "眼中点落到输出竖直位"},
        "pan_ax_lo":    {"type": "float", "default": 0.28, "min": 0.1, "max": 0.5,  "unit": "", "label": "横移左端",     "live": True, "desc": "横移时人像左端位置"},
        "pan_ax_hi":    {"type": "float", "default": 0.72, "min": 0.5, "max": 0.9,  "unit": "", "label": "横移右端",     "live": True, "desc": "横移时人像右端位置"},
        "pan_zmin":     {"type": "float", "default": 1.20, "min": 1.0, "max": 2.0,  "unit": "", "label": "正常横移最小z","live": True, "desc": "正常景别横移留横向余量"},
    },
    "manual": {
        "follow_alpha": {"type": "float", "default": 0.03, "min": 0.0, "max": 0.5, "unit": "", "label": "手动主镜跟随系数", "live": True, "desc": "关闭自动切镜后 cam1 锁脸跟随的低通系数(同 closeup)"},
        "ay":           {"type": "float", "default": 0.42, "min": 0.2, "max": 0.6, "unit": "", "label": "手动主镜头顶留白", "live": True, "desc": "眼中点落到输出的竖直位"},
        "zoom_lowpass": {"type": "float", "default": 0.2,  "min": 0.02, "max": 1.0, "unit": "", "label": "放大平滑",       "live": True, "desc": "z 跟 cam_zoom 滑块的低通;越大越跟手、越小越顺"},
        "z_max":        {"type": "float", "default": 2.6,  "min": 1.2, "max": 4.0, "unit": "", "label": "手动放大上限",   "live": True, "desc": "cam_zoom→z 的护栏上限"},
    },
    "hero": {
        "enable":        {"type": "bool",  "default": True,                                       "label": "启用招牌长镜", "live": True, "desc": "关则不做hero"},
        "push_dur":      {"type": "float", "default": 15.0, "min": 3.0, "max": 40.0, "unit": "s", "label": "慢推时长",     "live": True, "desc": "阶段1慢推"},
        "pull_dur":      {"type": "float", "default": 3.0,  "min": 1.0, "max": 15.0, "unit": "s", "label": "速拉时长",     "live": True, "desc": "阶段2速拉"},
        "head_near":     {"type": "float", "default": 0.75, "min": 0.4, "max": 0.95, "unit": "",  "label": "超特写头占比", "live": True, "desc": "阶段1终点头占~3/4"},
        "head_end":      {"type": "float", "default": 0.45, "min": 0.2, "max": 0.8,  "unit": "",  "label": "收尾头占比",   "live": True, "desc": "阶段2终点头占<1/2"},
        "ay_far":        {"type": "float", "default": 0.50, "min": 0.3, "max": 0.7,  "unit": "",  "label": "起点竖直位",   "live": True, "desc": "全景眼中点竖直位"},
        "ay_near":       {"type": "float", "default": 0.36, "min": 0.2, "max": 0.5,  "unit": "",  "label": "超特写竖直位", "live": True, "desc": "靠上给头顶留白"},
        "sway_amp":      {"type": "float", "default": 0.018, "min": 0.0, "max": 0.1, "unit": "",  "label": "晃动幅度",     "live": True, "desc": "手持摇晃幅度"},
        "sway_period":   {"type": "float", "default": 4.5,  "min": 1.0, "max": 15.0, "unit": "s", "label": "晃动周期",     "live": True, "desc": "主摇晃周期"},
        "head_factor":   {"type": "float", "default": 1.5,  "min": 1.0, "max": 2.5,  "unit": "",  "label": "头/脸框比",    "live": True, "desc": "全头高≈脸框高×此系数"},
        "z_max":         {"type": "float", "default": 3.4,  "min": 1.5, "max": 5.0,  "unit": "",  "label": "z上限",        "live": True, "desc": "防推过头糊"},
        "z_near_min":    {"type": "float", "default": 1.7,  "min": 1.0, "max": 3.0,  "unit": "",  "label": "近景z下限",    "live": True, "desc": "保证可见推进"},
        "pull_ratio":    {"type": "float", "default": 0.62, "min": 0.1, "max": 0.95, "unit": "",  "label": "后拉比例",     "live": True, "desc": "收尾z≤近景z×此值"},
        "faceh_clamp_lo":{"type": "float", "default": 0.11, "min": 0.02, "max": 0.3, "unit": "",  "label": "脸框高钳下限", "live": True, "desc": "防误检小框甩飞z"},
        "faceh_clamp_hi":{"type": "float", "default": 0.33, "min": 0.1, "max": 0.6,  "unit": "",  "label": "脸框高钳上限", "live": True, "desc": "防误检大框甩飞z"},
        "follow_alpha":  {"type": "float", "default": 0.03, "min": 0.0, "max": 0.5,  "unit": "",  "label": "锁脸跟随系数", "live": True, "desc": "低通跟随(滤抖又跟人)"},
        "cooldown":      {"type": "float", "default": 75.0, "min": 0.0, "max": 300.0, "unit": "s","label": "额外冷却",     "live": True, "desc": "两次hero最小间隔"},
        "prob":          {"type": "float", "default": 0.55, "min": 0.0, "max": 1.0,  "unit": "",  "label": "额外触发概率", "live": True, "desc": "保证之外的额外机会概率"},
        "guarantee":     {"type": "bool",  "default": True,                                       "label": "每首保证一次", "live": True, "desc": "每首歌至少触发一次"},
        "edge_head":     {"type": "float", "default": 8.0,  "min": 0.0, "max": 60.0, "unit": "s", "label": "避开开口",     "live": True, "desc": "触发窗口避开开唱后秒数"},
        "edge_tail":     {"type": "float", "default": 6.0,  "min": 0.0, "max": 60.0, "unit": "s", "label": "避开歌尾",     "live": True, "desc": "触发窗口尾部缓冲"},
        "max_per_song":  {"type": "int",   "default": 2,    "min": 1,   "max": 5,    "unit": "",  "label": "每首上限",     "live": True, "desc": "一首歌hero次数上限"},
    },
    "tracking": {
        "track_face": {"type": "bool",  "default": True,                                        "label": "启用头部跟踪", "live": False, "desc": "改后需重启"},
        "interval":   {"type": "float", "default": 0.5,  "min": 0.1, "max": 3.0, "unit": "s",   "label": "检测周期",     "live": True,  "desc": "YuNet抓帧检测周期"},
        "smooth":     {"type": "float", "default": 0.45, "min": 0.0, "max": 1.0, "unit": "",    "label": "跟踪平滑",     "live": True,  "desc": "位置EMA系数"},
        "model_path": {"type": "str",   "default": "models/yunet.onnx",                         "label": "模型路径",     "live": False, "desc": "相对本目录或绝对路径"},
        "det_score":  {"type": "float", "default": 0.6,  "min": 0.0, "max": 1.0, "unit": "",    "label": "检测置信阈",   "live": False, "desc": "改后需重启"},
        "det_nms":    {"type": "float", "default": 0.3,  "min": 0.0, "max": 1.0, "unit": "",    "label": "NMS阈值",      "live": False, "desc": "改后需重启"},
        "det_topk":   {"type": "int",   "default": 5000, "min": 100, "max": 10000, "unit": "",  "label": "候选上限",     "live": False, "desc": "改后需重启"},
        "shot_w":     {"type": "int",   "default": 1280, "min": 320, "max": 1920, "unit": "px", "label": "抓帧宽",       "live": True,  "desc": "检测用截图宽"},
        "shot_h":     {"type": "int",   "default": 720,  "min": 240, "max": 1080, "unit": "px", "label": "抓帧高",       "live": True,  "desc": "检测用截图高"},
    },
    "connection": {
        "pc_service":   {"type": "str", "default": "localhost:8765",           "label": "pc-service地址", "live": False, "desc": "改后需重启"},
        "obs_host":     {"type": "str", "default": "localhost",                "label": "OBS主机",        "live": False, "desc": "改后需重启"},
        "obs_port":     {"type": "int", "default": 4455, "min": 1, "max": 65535, "label": "OBS端口",       "live": False, "desc": "改后需重启"},
        "obs_password": {"type": "str", "default": "",                         "label": "OBS密码",        "live": False, "desc": "改后需重启"},
    },
}

# ── 结构化默认(json 友好:全 list,无 set/tuple)──────────────
STRUCT_DEFAULTS = {
    "cameras": {
        "content_name": "content_{cam}",
        "hero_cam": "cam1",
        "closeup_cams": ["cam1", "cam2"],
        "list": {
            "cam1": {"scene": "cam1", "angle": -20, "face": [0.46, 0.29], "face_h": 0.16,
                     "presets": ["full", "loose", "high", "close", "thirds_l", "thirds_r", "xclose"]},
            "cam2": {"scene": "cam2", "angle": 40, "face": [0.44, 0.33], "face_h": 0.20,
                     "presets": ["full", "loose", "high", "close", "thirds_l", "thirds_r"]},
            "cam3": {"scene": "cam3", "angle": 0, "face": [0.51, 0.48], "face_h": 0.10,
                     "presets": ["full", "loose", "high", "close"]},
        },
    },
    "state_cams": {
        "INTRO":     {"cam3": 3, "cam1": 2, "cam2": 1},
        "VERSE":     {"cam1": 3, "cam2": 3, "cam3": 1},
        "CHORUS":    {"cam2": 3, "cam1": 2, "cam3": 1},
        "INTERLUDE": {"cam3": 3, "cam1": 1, "cam2": 1},
        "OUTRO":     {"cam3": 3, "cam1": 1},
        "PAUSE":     {"cam1": 1},
    },
    "hold_range": {
        "INTRO": [5, 9], "VERSE": [5, 9], "CHORUS": [3, 6],
        "INTERLUDE": [6, 11], "OUTRO": [5, 9], "PAUSE": [10, 14],
    },
    "palette": {
        "INTRO":     [["full", "descend", 2], ["full", "push", 1], ["loose", "pan", 2], ["loose", "drift", 2],
                      ["full", "pull", 1], ["close", "push", 1], ["loose", "trackLR", 1], ["high", "static", 1]],
        "VERSE":     [["loose", "static", 2], ["close", "closeup", 3], ["close", "trackpan", 2], ["loose", "trackpan", 2],
                      ["close", "static", 1], ["full", "static", 1], ["loose", "push", 1], ["full", "pull", 1],
                      ["loose", "drift", 1], ["thirds_l", "static", 1], ["thirds_r", "static", 1], ["xclose", "closeup", 1]],
        "CHORUS":    [["close", "closeup", 3], ["close", "trackpan", 3], ["xclose", "closeup", 1], ["loose", "trackpan", 1],
                      ["thirds_l", "push", 1], ["thirds_r", "push", 1], ["close", "static", 1], ["loose", "push", 1]],
        "INTERLUDE": [["full", "descend", 2], ["full", "ascend", 1], ["full", "pan", 2], ["loose", "drift", 2],
                      ["close", "push", 2], ["full", "pull", 1], ["loose", "trackLR", 1], ["high", "static", 1]],
        "OUTRO":     [["full", "ascend", 2], ["loose", "pull", 2], ["full", "pull", 2], ["close", "pull", 1], ["full", "drift", 1]],
        "PAUSE":     [["loose", "follow", 1]],
    },
    "framing": {
        "full":     {"z": 1.0,  "cx": 0.5,    "cy": 0.5,    "ax": 0.5,  "ay": 0.5},
        "loose":    {"z": 1.12, "cx": 0.5,    "cy": 0.5,    "ax": 0.5,  "ay": 0.5},
        "high":     {"z": 1.12, "cx": 0.5,    "cy": 0.42,   "ax": 0.5,  "ay": 0.5},
        "close":    {"z": 1.3,  "cx": "face", "cy": "face", "ax": 0.5,  "ay": 0.42},
        "xclose":   {"z": 1.75, "cx": "face", "cy": "face", "ax": 0.5,  "ay": 0.40},
        "thirds_l": {"z": 1.5,  "cx": "face", "cy": "face", "ax": 0.35, "ay": 0.40},
        "thirds_r": {"z": 1.5,  "cx": "face", "cy": "face", "ax": 0.65, "ay": 0.40},
    },
    "moves": {
        "min_move_z": 1.14,
        "push":    {"z_start_delta": -0.30, "dur": [3.5, 5.5], "ease": "inOutCubic"},
        "pull":    {"z_start_delta": 0.32,  "dur": [4.0, 6.0], "ease": "inOutCubic"},
        "pan":     {"cx_start_delta": 0.12, "cx_start_max": 0.8, "dur": [4.0, 7.0], "ease": "linear"},
        "drift":   {"z_start_factor": 0.93, "dur": [6.0, 10.0], "ease": "linear"},
        "trackLR": {"z_min": 1.34, "ax_start": 0.63, "ax_end": 0.37, "dur": [5.0, 8.0], "ease": "linear"},
        # cam3 广角电影感组合运镜:前推伴随取景下移(机位下降感)+ 偶尔横移(pan_prob 概率、pan_amp 幅度、方向随机)。
        # z/cy 同步走(z 越大垂直余量越足,cy 下移越可见);后退版 ascend 反向(从低到高)。tick_move 五维插值实现。
        "descend": {"z_from": 1.12, "z_to": 1.5, "cy_from": 0.40, "cy_to": 0.60, "pan_prob": 0.5, "pan_amp": 0.14, "dur": [9.0, 15.0], "ease": "inOutCubic"},
        "ascend":  {"z_from": 1.5, "z_to": 1.12, "cy_from": 0.60, "cy_to": 0.40, "pan_prob": 0.5, "pan_amp": 0.14, "dur": [9.0, 15.0], "ease": "inOutCubic"},
    },
}


def default_config():
    """一份 json 友好的默认配置(== 迁移前原值)。"""
    cfg = {}
    for group, fields in SCALARS.items():
        cfg[group] = {k: spec["default"] for k, spec in fields.items()}
    for group, d in STRUCT_DEFAULTS.items():
        cfg[group] = copy.deepcopy(d)
    return cfg


# ── 校验小工具 ────────────────────────────────────────────
def _numf(v, d):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _clamp01(v):
    return min(1.0, max(0.0, _numf(v, 0.5)))


def _clampf(v, lo, hi, d):
    return min(hi, max(lo, _numf(v, d)))


def _coerce(v, spec, path, warns):
    t = spec["type"]
    try:
        if t == "int":
            v = int(round(float(v)))
        elif t == "float":
            v = float(v)
        elif t == "bool":
            v = bool(v)
        elif t == "str":
            v = str(v)
    except (TypeError, ValueError):
        warns.append(f"{path} 类型错误,用默认 {spec['default']}")
        return spec["default"]
    if t in ("int", "float"):
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and v < lo:
            warns.append(f"{path}={v} <{lo} 已钳到 {lo}"); v = lo
        if hi is not None and v > hi:
            warns.append(f"{path}={v} >{hi} 已钳到 {hi}"); v = hi
    return v


def _ov_cameras(rc, cfg, warns):
    if not isinstance(rc, dict):
        return
    cam = cfg["cameras"]
    if "content_name" in rc:
        cam["content_name"] = str(rc["content_name"])
    if "hero_cam" in rc:
        if rc["hero_cam"] in CAMS:
            cam["hero_cam"] = rc["hero_cam"]
        else:
            warns.append(f"cameras.hero_cam 未知机位 {rc['hero_cam']},保留默认")
    if isinstance(rc.get("closeup_cams"), list):
        cc = [c for c in rc["closeup_cams"] if c in CAMS]
        cam["closeup_cams"] = cc
    if isinstance(rc.get("list"), dict):
        for c, rec in rc["list"].items():
            if c not in CAMS:
                warns.append(f"cameras.list 未知机位 {c},忽略"); continue
            if not isinstance(rec, dict):
                continue
            tgt = cam["list"][c]
            if "scene" in rec:
                tgt["scene"] = str(rec["scene"])
            if "angle" in rec:
                tgt["angle"] = _numf(rec["angle"], tgt["angle"])
            if isinstance(rec.get("face"), list) and len(rec["face"]) == 2:
                tgt["face"] = [_clamp01(rec["face"][0]), _clamp01(rec["face"][1])]
            if "face_h" in rec:
                tgt["face_h"] = _clampf(rec["face_h"], 0.02, 0.6, tgt["face_h"])
            if isinstance(rec.get("presets"), list):
                ps = [p for p in rec["presets"] if p in PRESETS]
                if ps:
                    tgt["presets"] = ps
                else:
                    warns.append(f"cameras.list.{c}.presets 全非法,保留默认")


def _ov_state_cams(rc, cfg, warns):
    if not isinstance(rc, dict):
        return
    for s, wt in rc.items():
        if s not in STATES:
            warns.append(f"state_cams 未知状态 {s},忽略"); continue
        if not isinstance(wt, dict):
            continue
        clean = {}
        for c, w in wt.items():
            if c not in CAMS:
                warns.append(f"state_cams.{s} 未知机位 {c},忽略"); continue
            w = _numf(w, 0)
            if w < 0:
                warns.append(f"state_cams.{s}.{c} 负权重钳0"); w = 0
            clean[c] = w
        if clean and any(v > 0 for v in clean.values()):
            cfg["state_cams"][s] = clean
        else:
            warns.append(f"state_cams.{s} 全0,保留默认")


def _ov_hold(rc, cfg, warns):
    if not isinstance(rc, dict):
        return
    for s, rng in rc.items():
        if s not in STATES:
            warns.append(f"hold_range 未知状态 {s},忽略"); continue
        if isinstance(rng, list) and len(rng) == 2:
            lo = max(0.0, _numf(rng[0], 5)); hi = max(0.0, _numf(rng[1], 9))
            if lo > hi:
                warns.append(f"hold_range.{s} lo>hi 已交换"); lo, hi = hi, lo
            cfg["hold_range"][s] = [lo, hi]


def _ov_palette(rc, cfg, warns):
    if not isinstance(rc, dict):
        return
    for s, lst in rc.items():
        if s not in STATES:
            warns.append(f"palette 未知状态 {s},忽略"); continue
        if not isinstance(lst, list):
            continue
        clean = []
        for item in lst:
            if not isinstance(item, (list, tuple)) or len(item) < 3:
                continue
            p, m, w = item[0], item[1], item[2]
            if p not in PRESETS:
                warns.append(f"palette.{s} 未知景别 {p},丢弃"); continue
            if m not in MOVES:
                warns.append(f"palette.{s} 未知运镜 {m},丢弃"); continue
            w = _numf(w, 0)
            if w <= 0:
                warns.append(f"palette.{s} 权重≤0,丢弃"); continue
            clean.append([p, m, w])
        if clean:
            cfg["palette"][s] = clean
        else:
            warns.append(f"palette.{s} 无有效项,保留默认")


def _ov_framing(rc, cfg, warns):
    if not isinstance(rc, dict):
        return
    for p, spec in rc.items():
        if p not in PRESETS:
            warns.append(f"framing 未知景别 {p},忽略"); continue
        if not isinstance(spec, dict):
            continue
        tgt = cfg["framing"][p]
        for key in ("z", "ax", "ay"):
            if key in spec:
                tgt[key] = _numf(spec[key], tgt[key])
        for key in ("cx", "cy"):
            if key in spec:
                tgt[key] = "face" if spec[key] == "face" else _clamp01(spec[key])
        if tgt["z"] < 1.0:
            warns.append(f"framing.{p}.z<1 已钳到1"); tgt["z"] = 1.0


def _ov_moves(rc, cfg, warns):
    if not isinstance(rc, dict):
        return
    tgt = cfg["moves"]
    if "min_move_z" in rc:
        tgt["min_move_z"] = _clampf(rc["min_move_z"], 1.0, 2.0, tgt["min_move_z"])
    for mv in ("push", "pull", "pan", "drift", "trackLR", "descend", "ascend"):
        if not isinstance(rc.get(mv), dict):
            continue
        for k, v in rc[mv].items():
            if k == "ease":
                if v in EASES:
                    tgt[mv]["ease"] = v
                else:
                    warns.append(f"moves.{mv}.ease 非法 {v},保留默认")
            elif k == "dur" and isinstance(v, list) and len(v) == 2:
                lo = max(0.1, _numf(v[0], tgt[mv]["dur"][0]))
                hi = max(0.1, _numf(v[1], tgt[mv]["dur"][1]))
                if lo > hi:
                    warns.append(f"moves.{mv}.dur lo>hi 已交换"); lo, hi = hi, lo
                tgt[mv]["dur"] = [lo, hi]
            elif k in tgt[mv] and isinstance(tgt[mv][k], (int, float)):
                tgt[mv][k] = _numf(v, tgt[mv][k])


def _cross_field(cfg, warns):
    """跨字段规则:只告警+自动纠正。"""
    h = cfg["hero"]
    if h["head_end"] >= h["head_near"]:
        h["head_end"] = round(h["head_near"] * 0.6, 3)
        warns.append(f"hero.head_end≥head_near 已下调到 {h['head_end']}")
    if h["z_near_min"] >= h["z_max"]:
        h["z_near_min"] = round(h["z_max"] * 0.6, 3)
        warns.append(f"hero.z_near_min≥z_max 已下调到 {h['z_near_min']}")
    if h["faceh_clamp_lo"] >= h["faceh_clamp_hi"]:
        h["faceh_clamp_lo"], h["faceh_clamp_hi"] = 0.11, 0.33
        warns.append("hero.faceh_clamp 区间非法 已复位为(0.11,0.33)")
    cl = cfg["closeup"]
    if cl["z_min"] >= cl["z_max"]:
        cl["z_min"] = round(cl["z_max"] * 0.7, 3)
        warns.append(f"closeup.z_min≥z_max 已下调到 {cl['z_min']}")
    if cl["pan_ax_lo"] >= cl["pan_ax_hi"]:
        cl["pan_ax_lo"], cl["pan_ax_hi"] = 0.28, 0.72
        warns.append("closeup.pan_ax 区间非法 已复位为(0.28,0.72)")
    r = cfg["rhythm"]
    if r["linger_add_lo"] > r["linger_add_hi"]:
        r["linger_add_lo"], r["linger_add_hi"] = r["linger_add_hi"], r["linger_add_lo"]
        warns.append("rhythm.linger_add lo>hi 已交换")


def validate(raw):
    """把外部 JSON 校正为合法配置。返回 (cfg, warnings)。永不抛异常。"""
    warns = []
    cfg = default_config()
    if not isinstance(raw, dict):
        raw = {}
    for group, fields in SCALARS.items():
        rg = raw.get(group)
        rg = rg if isinstance(rg, dict) else {}
        for key, spec in fields.items():
            if key in rg:
                cfg[group][key] = _coerce(rg[key], spec, f"{group}.{key}", warns)
    _ov_cameras(raw.get("cameras"), cfg, warns)
    _ov_state_cams(raw.get("state_cams"), cfg, warns)
    _ov_hold(raw.get("hold_range"), cfg, warns)
    _ov_palette(raw.get("palette"), cfg, warns)
    _ov_framing(raw.get("framing"), cfg, warns)
    _ov_moves(raw.get("moves"), cfg, warns)
    _cross_field(cfg, warns)
    return cfg, warns
