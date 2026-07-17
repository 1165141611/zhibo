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
import time
import random
import argparse
import threading
import urllib.parse
import urllib.request

# ── 配置(按你的环境改这里)────────────────────────────────
PC_SERVICE = "localhost:8765"        # pc-service 地址(WS + HTTP 同一个)
OBS_HOST, OBS_PORT = "localhost", 4455
OBS_PASSWORD = ""   # 工具→WebSocket服务器设置;若你关了「启用身份验证」就留空,否则填密码

# 机位 → OBS 场景名。请在 OBS 里把三个场景分别命名为下面的值(先用占位色块/图片,之后换真摄像头源)。
SCENES = {"cam1": "cam1", "cam2": "cam2", "cam3": "cam3"}
# 机位物理角度(参考,当前编排靠数字景别派生多镜头,不再用角度差硬护栏)。对照方案文档 §5。
CAM_ANGLE = {"cam1": -20, "cam2": 40, "cam3": 0}

# ── 运镜(阶段2)────────────────────────────────────────────
# 平滑推/拉/平移/1:3前推:本循环 30Hz 插值 framing → OBS SetSceneItemTransform(不需 Move 插件)。
# framing = {z 放大, cx/cy 源上关注点, ax/ay 落到输出的锚点},与 director-sim.html 完全一致。
ENABLE_MOVES = True
CONTENT_NAME = "content_{cam}"   # 每个场景里要做变换的源名(见 obs 里加的机位图源)
# 人脸在各机位画面里的归一化位(眼中点)。作为 tracker 未就绪时的回退默认;开了实时跟踪后由 YuNet 更新。
CAM_FACE = {"cam1": (0.46, 0.29), "cam2": (0.44, 0.33), "cam3": (0.51, 0.48)}
CAM_FACE_LIVE = dict(CAM_FACE)   # 实时头部位(眼中点),start_tracker 线程更新;framing_for 用它锁脸
# ── 实时头部跟踪(YuNet):定时抓每台画面检测头,特写/三分锁定最新位置,防"头偏下"+跟人移动。──
TRACK_FACE = True
FACE_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "yunet.onnx")
TRACK_INTERVAL = 0.5     # 检测周期(秒)
TRACK_SMOOTH = 0.45      # 位置 EMA 平滑系数(越大越跟手)
# 每台可用景别。cam1(主)可到 xclose 贴脸超特写;cam3(大疆高远,人小)可推到 close(前推)
# 但不做 thirds/xclose(远机高倍放大会糊)。
CAM_PRESETS = {
    "cam1": {"full", "loose", "high", "close", "thirds_l", "thirds_r", "xclose"},
    "cam2": {"full", "loose", "high", "close", "thirds_l", "thirds_r"},
    "cam3": {"full", "loose", "high", "close"},
}

# ── 编排参数(基于参考视频分析:丰富感靠"景别/角度/节奏的变化",非单纯多切;
#    我们只有3台机位、没乐队可切,故用"数字景别+随机运镜"把3台派生成十几种镜头)──
INTERLUDE_GAP = 4.0      # 相邻字间隔 > 此值 = 间奏(秒)
SNAP_WINDOW = 0.15       # 切点吸附:字起始落在 [t-此值, t] 内才允许切(秒)
SNAP_GRACE = 2.5         # 到点后等不到字边界,超过此宽限直接切(器乐长段用)
TICK_HZ = 30             # 状态机评估频率
WIDE_BREATHER = 45.0     # 超过此秒数没广角 → 强制来个广角"呼吸"

# 各状态机位权重(用满3台、加权随机、不连切同机)
STATE_CAMS = {
    "INTRO":     {"cam3": 3, "cam1": 2, "cam2": 1},
    "VERSE":     {"cam1": 3, "cam2": 3, "cam3": 1},
    "CHORUS":    {"cam2": 3, "cam1": 2, "cam3": 1},
    "INTERLUDE": {"cam3": 3, "cam1": 1, "cam2": 1},
    "OUTRO":     {"cam3": 3, "cam1": 1},
    "PAUSE":     {"cam1": 1},
}

# 镜头调色板:每状态一组 (景别 preset, 运镜 move, 权重)。景别把3机位派生成多种镜头;
# 运镜比例按状态调(主歌少动、副歌多动、间奏缓动)。static=静止镜(占多数,像参考片)。
SHOT_PALETTE = {
    "INTRO":     [("full", "push", 2), ("loose", "pan", 2), ("loose", "drift", 2), ("full", "pull", 1),
                  ("close", "push", 1), ("loose", "trackLR", 1), ("high", "static", 1)],
    "VERSE":     [("loose", "static", 3), ("close", "static", 2), ("full", "static", 2),
                  ("close", "push", 2), ("loose", "push", 2), ("full", "pull", 1), ("loose", "drift", 2),
                  ("thirds_l", "static", 1), ("thirds_r", "static", 1), ("loose", "trackLR", 1), ("xclose", "static", 1)],
    "CHORUS":    [("close", "push", 3), ("thirds_l", "push", 2), ("thirds_r", "push", 2),
                  ("xclose", "push", 1), ("close", "trackLR", 1), ("close", "static", 1), ("loose", "push", 1)],
    "INTERLUDE": [("full", "pan", 3), ("loose", "drift", 2), ("close", "push", 2), ("full", "pull", 2),
                  ("loose", "trackLR", 1), ("high", "static", 1)],
    "OUTRO":     [("loose", "pull", 3), ("full", "pull", 2), ("close", "pull", 1), ("full", "drift", 1)],
    "PAUSE":     [("loose", "static", 1)],
}
# 停留时长范围(秒,每镜随机抽):主歌中速/副歌快/间奏慢
HOLD_RANGE = {"INTRO": (5, 9), "VERSE": (5, 9), "CHORUS": (3, 6),
              "INTERLUDE": (6, 11), "OUTRO": (5, 9), "PAUSE": (10, 14)}
LINGER_PROB = 0.18       # 概率:某镜"多停留一会儿"(避免节奏均匀)


def framing_for(preset, cam):
    """景别 preset → framing。close/thirds/xclose 用该机位实时头部位(眼中点);full/loose/high 用画面中心。
    ay(输出锚点竖直)给足头顶留白:眼睛落到上三分(~0.4),头不会压到画面下方。"""
    fx, fy = CAM_FACE_LIVE.get(cam, CAM_FACE.get(cam, (0.5, 0.5)))
    return {
        "full":     {"z": 1.0,  "cx": 0.5, "cy": 0.5,  "ax": 0.5,  "ay": 0.5},
        "loose":    {"z": 1.12, "cx": 0.5, "cy": 0.5,  "ax": 0.5,  "ay": 0.5},
        "high":     {"z": 1.12, "cx": 0.5, "cy": 0.42, "ax": 0.5,  "ay": 0.5},
        "close":    {"z": 1.3,  "cx": fx,  "cy": fy,   "ax": 0.5,  "ay": 0.42},  # 眼睛→上三分,头顶留白
        "xclose":   {"z": 1.75, "cx": fx,  "cy": fy,   "ax": 0.5,  "ay": 0.40},  # 贴脸超特写(仅 cam1)
        "thirds_l": {"z": 1.5,  "cx": fx,  "cy": fy,   "ax": 0.35, "ay": 0.40},
        "thirds_r": {"z": 1.5,  "cx": fx,  "cy": fy,   "ax": 0.65, "ay": 0.40},
    }.get(preset, {"z": 1.0, "cx": 0.5, "cy": 0.5, "ax": 0.5, "ay": 0.5})


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
        weights["cam3"] = weights.get("cam3", 1) + 8
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
        h += random.uniform(2, 5)
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
    if s == "PAUSE":                              # 暂停:回主机静止,不再切
        if RT["active"] != "cam1":
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


def reset_director():
    RT["active"] = "cam1"
    RT["last_cut_t"] = -99.0
    RT["last_state"] = None
    RT["onset_cd"] = 0.0
    RT["hold"] = 6.0
    RT["last_wide_t"] = -999.0
    RT["last_preset"] = None
    RT["force_cut"] = True


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
    """把抽中的镜头(景别+运镜)应用出去:静止=直接落位;运镜=设起点+启动插值。"""
    target = framing_for(preset, cam)
    if not ENABLE_MOVES or move == "static":
        RT["framing"] = dict(target)
        RT["mv_to"] = None
        RT["mv_kind"] = None
        RT["fdirty"] = True
        return
    if target["z"] < 1.14:                 # 运镜要放大留边距,否则被防黑边钳制看不出动
        target = dict(target)
        target["z"] = 1.14
    if move == "trackLR":                  # 主体从右滑到左(锚点 ax 0.63→0.37,z 留移动余量)
        target = dict(target)
        target["z"] = max(target["z"], 1.34)
        target["ax"] = 0.37
        start = dict(target)
        start["ax"] = 0.63
        dur, ease = random.uniform(5.0, 8.0), "linear"
        RT["framing"] = start
        start_move(move, target, dur, ease)
        return
    start = dict(target)
    if move == "push":                     # 缓推:起点更松更远 → 落到目标(幅度加大,前推更明显)
        start["z"] = max(1.0, target["z"] - 0.3)
        dur, ease = random.uniform(3.5, 5.5), "inOutCubic"
    elif move == "pull":                   # 缓拉/后撤:起点更紧 → 拉开(幅度加大)
        start["z"] = target["z"] + 0.32
        dur, ease = random.uniform(4.0, 6.0), "inOutCubic"
    elif move == "pan":                    # 平移:横向移动关注点
        start["cx"] = min(0.8, target["cx"] + 0.12)
        dur, ease = random.uniform(4.0, 7.0), "linear"
    else:                                  # drift 极慢微推,给静镜一点呼吸
        start["z"] = max(1.0, target["z"] * 0.93)
        dur, ease = random.uniform(6.0, 10.0), "linear"
    RT["framing"] = start
    start_move(move, target, dur, ease)


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
        det = cv2.FaceDetectorYN.create(FACE_MODEL, "", (320, 320), 0.6, 0.3, 5000)
        cl = obs.ReqClient(host=OBS_HOST, port=OBS_PORT, password=OBS_PASSWORD, timeout=5)
    except Exception as e:
        print(f"[TRACK] 初始化失败,退回静态人脸位: {e}")
        return
    print("[TRACK] YuNet 头部跟踪已启动")

    def run():
        while True:
            for cam in SCENES:
                try:
                    r = cl.get_source_screenshot("content_" + cam, "jpg", 1280, 720, -1)
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
        print(f"[DEMO] 内置演示曲({RT['dur']:.0f}s)循环驱动 OBS,不需 pc-service")
    else:
        start_ws()

    def do_cut(cam, reason, s):
        RT["active"] = cam
        RT["last_cut_t"] = now_t()
        RT["onset_cd"] = 0.5
        if cam == "cam3":                       # 记录广角时刻(周期性呼吸用)
            RT["last_wide_t"] = now_t()
        obs.cut(cam)
        preset, move = pick_shot(s, cam)        # 抽一个镜头(景别×运镜,按机位可用景别过滤)
        RT["last_preset"] = preset
        RT["hold"] = hold_for(s)                # 本镜停留时长(随机)
        apply_shot(preset, move, cam)
        print(f"[切] {now_t():6.1f}s → {cam} {preset:8s}/{move:6s} 停{RT['hold']:.0f}s [{s}·{reason}] ♪ {RT['title']}")

    print("状态机运行中(Ctrl+C 退出)。等 pc-service 有歌在放就会开始切镜。")
    dt = 1.0 / TICK_HZ
    last_state_print = None
    try:
        while True:
            time.sleep(dt)
            if not RT["connected"] or RT["song"] is None:
                continue
            if args.demo and now_t() > RT["song"]["end"] + 2:   # 演示曲循环重播
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
                    RT["force_cut"] = True      # 状态一变 → 立刻切一个新镜头(响应段落切换)
                    if s != last_state_print:
                        print(f"[段] {t:6.1f}s  {s}")
                        last_state_print = s
                plan_cut(t, s, do_cut)
                if ENABLE_MOVES:               # 30Hz 插值当前 framing → OBS(只在有变化时发)
                    tick_move(t)
                    if RT["fdirty"]:
                        RT["fdirty"] = False
                        obs.set_framing(RT["active"], RT["framing"])
                # 低频心跳:长前奏/间奏期间只有状态没切镜,靠它看出"在工作而非卡死"
                RT["_n"] = RT.get("_n", 0) + 1
                if RT["_n"] % 300 == 0:
                    print(f"[心跳] t={t:.0f}s pos={RT['base_pos']:.0f}s {s} 直播机位={RT['active']}")
    except KeyboardInterrupt:
        print("\n退出。")


if __name__ == "__main__":
    main()
