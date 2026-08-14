# -*- coding: utf-8 -*-
"""分段变调工具:把曲库里一首歌的**指定时间段**升/降调(典型场景:副歌太高降全音、
主歌保持原调),生成一个**新曲目**入库,原曲不动。

为什么能"衔接好":
- 变调用 stftpitchshift(相位声码器)——输出样本数与输入**严格相等**,歌词/音准
  时间轴零漂移(播放器实时链路用的 WSOLA+重采样是流式近似,离线拼接不用它);
- 段边界放在**唱句间隙(纯伴奏间奏)**里,原调↔变调用等功率交叉淡化过渡,
  人声完全不经过过渡区;
- 伴奏、原唱两轨同样处理(播放中切原唱不穿帮);.note 音准在变调段同步移调,
  音准线跟音频一致。

用法(python 用 Python313 完整路径,见 CLAUDE.md):
  # 1. 先看歌的结构,挑段落边界(每句歌词的起止 + 音高范围 + 句间空隙):
  python segment_pitch.py analyze --mid 000iF8Rl3c36b4
  # 2. 生成新曲目(段格式 起-止:半音,单位秒,止可写 end;边界即淡化区起点):
  python segment_pitch.py build --mid 000iF8Rl3c36b4 \
      --segments 115.5-150.5:-2 196.0-227.0:-2 253.0-end:-2 \
      --fade 1.2 --suffix "副歌-2"
生成后**重启 pc-service 托盘服务**才会出现在曲库列表(清单 library.json 启动时载入;
服务运行中直接改会被内存清单覆盖,务必先关服务或改完重启)。
"""
import argparse
import json
import os
import shutil
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import assets
from wesing_pcm_key import PCM_XOR_KEY

LIB_DIR = r"D:\KaraokeLibrary"
SR = assets.SAMPLE_RATE

_PCMKEY = np.frombuffer(PCM_XOR_KEY, dtype=np.uint8)


def _write_pcm(audio: np.ndarray, path: str):
    """float32 (N,2) [-1,1] → int16 → XOR 加密裸 PCM(与曲库其余文件一致)。"""
    pcm16 = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    raw = np.ascontiguousarray(pcm16).view(np.uint8).ravel()
    ks = np.resize(_PCMKEY, len(raw))
    (raw ^ ks).tofile(path)


def _song_paths(mid, lib=LIB_DIR):
    base = os.path.join(lib, mid, mid)
    return {
        "accompany": base + "_accompany.pcm",
        "kongsinger": base + "_kongsinger.pcm",
        "note": base + ".note",
        "qrc": base + ".qrc",
        "meta": os.path.join(lib, mid, "meta.json"),
    }


# ---------------------------------------------------------------- analyze
def analyze(mid, lib=LIB_DIR):
    p = _song_paths(mid, lib)
    _, lines = assets.load_lyrics(p["qrc"])
    notes = assets.load_notes(p["note"])
    n_frames = os.path.getsize(p["accompany"]) // 4   # int16 双声道
    print(f"音频时长 {n_frames / SR:.1f}s | {len(lines)} 句 | {len(notes)} 音符")
    print(f"{'#':>2} {'起':>7} {'止':>7} {'句前空隙':>8} {'音高':>7}  歌词")
    prev_end = 0
    for i, L in enumerate(lines):
        ps = [n.midi for n in notes if n.start < L.end and n.end > L.start]
        hi, lo = (max(ps), min(ps)) if ps else (0, 0)
        print(f"{i:2d} {L.start / 1000:7.2f} {L.end / 1000:7.2f} "
              f"{(L.start - prev_end) / 1000:7.2f}s {lo:3d}-{hi:3d}  {L.text}")
        prev_end = L.end


# ---------------------------------------------------------------- build
def _parse_segments(specs, total_s):
    """["115.5-150.5:-2", "253-end:-2"] → [(s秒, e秒, 半音)],校验有序不重叠。"""
    segs = []
    for spec in specs:
        rng, semi = spec.rsplit(":", 1)
        a, b = rng.split("-")
        s = float(a)
        e = total_s if b.strip().lower() == "end" else float(b)
        if not (0 <= s < e <= total_s + 0.01):
            raise SystemExit(f"段越界: {spec} (音频共 {total_s:.1f}s)")
        segs.append((s, min(e, total_s), int(semi)))
    segs.sort()
    for (_, e1, _), (s2, _, _) in zip(segs, segs[1:]):
        if s2 < e1:
            raise SystemExit("段重叠,请检查 --segments")
    return segs


def _shift_region(audio, s0, s1, semi, pad_s=3.0):
    """对 audio[s0:s1](帧)整段移调 semi 半音,返回**等长**片段。
    前后各多取 pad 喂给声码器,丢弃 pad 消除边缘暂态。"""
    from stftpitchshift import StftPitchShift
    pad = int(pad_s * SR)
    a = max(0, s0 - pad)
    b = min(len(audio), s1 + pad)
    factor = 2.0 ** (semi / 12.0)
    sps = StftPitchShift(4096, 1024, SR)   # 4096 帧长:音乐低频分辨率够,离线不在乎耗时
    out = np.empty((b - a, 2), np.float32)
    for ch in range(2):
        out[:, ch] = sps.shiftpitch(audio[a:b, ch].astype(np.float64), factors=factor)
    out = out[s0 - a: s0 - a + (s1 - s0)]
    # 响度补偿:相位声码器输出 RMS 偏低约 2~3dB(实测),按段匹配原音频响度,
    # 否则副歌一进变调段突然变小声。限峰按 99.99 分位(声码器会造出极个别相位
    # 叠加尖峰,若按 max 限会把整段增益卡低 1~2dB;超出的万分之一样本写盘时削波)。
    src_rms = float(np.sqrt((audio[s0:s1] ** 2).mean()))
    out_rms = float(np.sqrt((out ** 2).mean()))
    if out_rms > 1e-9:
        gain = src_rms / out_rms
        p = float(np.percentile(np.abs(out), 99.99))
        if p * gain > 0.99:
            gain = 0.99 / p
        out *= gain
    return out


def _splice(audio, segs, fade_s):
    """原调 audio + 变调段列表 → 拼接输出。段 [s,e] 内是变调音频;
    [s, s+fade] 原→变、[e-fade, e] 变→原,等功率交叉淡化(段到音频末尾则无淡出)。"""
    out = audio.copy()
    n = len(audio)
    fade = int(fade_s * SR)
    for s_sec, e_sec, semi in segs:
        s, e = int(s_sec * SR), min(int(e_sec * SR), n)
        print(f"  变调段 {s_sec:.1f}s–{e_sec:.1f}s {semi:+d} 半音 …")
        shifted = _shift_region(audio, s, e, semi)
        out[s:e] = shifted
        # 淡入:原→变
        f = min(fade, e - s)
        th = np.linspace(0, np.pi / 2, f, dtype=np.float32)[:, None]
        out[s:s + f] = audio[s:s + f] * np.cos(th) + shifted[:f] * np.sin(th)
        # 淡出:变→原(段顶到末尾就不淡出,直接变调收尾)
        if e < n:
            out[e - f:e] = shifted[-f:] * np.cos(th) + audio[e - f:e] * np.sin(th)
    return out


def _shift_note(src, dst, segs):
    """.note 明文(每行 起ms 时长ms MIDI)→ 变调段内音高同步移调。"""
    out_lines = []
    for line in open(src, encoding="utf-8", errors="ignore").read().splitlines():
        p = line.split()
        if len(p) >= 3:
            try:
                start, dur, midi = int(p[0]), int(p[1]), int(p[2])
                for s_sec, e_sec, semi in segs:
                    if s_sec * 1000 <= start < e_sec * 1000:
                        midi += semi
                        break
                line = f"{start} {dur} {midi}"
            except ValueError:
                pass
        out_lines.append(line)
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out_lines) + "\n")


def build(mid, specs, fade, suffix, out_mid=None, lib=LIB_DIR):
    src = _song_paths(mid, lib)
    out_mid = out_mid or (mid + "seg")
    dst = _song_paths(out_mid, lib)
    if os.path.isdir(os.path.join(lib, out_mid)):
        raise SystemExit(f"目标已存在: {os.path.join(lib, out_mid)}(先删除或换 --out-mid)")

    meta = json.load(open(src["meta"], encoding="utf-8"))
    # 标记约定:歌名尾缀方括号 `[副歌-2]`——托盘/手机列表可见,播放器绿幕渲染时
    # 剥掉(player._viewer_title 只剥结尾方括号),不给观众展示。
    title = f"{meta.get('title', mid)}[{suffix}]"
    artist = meta.get("artist", "")
    print(f"源: {meta.get('title')} - {artist} → 新曲目: {title} (mid={out_mid})")

    acc = assets.load_pcm(src["accompany"])
    segs = _parse_segments(specs, len(acc) / SR)

    os.makedirs(os.path.join(lib, out_mid))
    for key, name in (("accompany", "伴奏"), ("kongsinger", "原唱")):
        audio = acc if key == "accompany" else assets.load_pcm(src[key])
        print(f"处理{name}轨:")
        _write_pcm(_splice(audio, segs, fade), dst[key])
    shutil.copy2(src["qrc"], dst["qrc"])
    _shift_note(src["note"], dst["note"], segs)
    json.dump({"title": title, "artist": artist, "needs_name": False,
               "source_mid": mid, "pitch_segments": list(specs), "pitch_fade": fade},
              open(dst["meta"], "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 登记清单(named=True:启动迁移不许按 QRC 重写这个标题)。
    # source_mid/pitch_segments/pitch_fade 纯留档:记录来源与烘焙参数,想微调过渡点时
    # 删目录、按原样改参数重跑即可复现(段格式即本工具 --segments 实参)。
    lib_json = os.path.join(lib, "library.json")
    man = json.load(open(lib_json, encoding="utf-8"))
    man[out_mid] = {"title": title, "artist": artist,
                    "added": time.time(), "named": True,
                    "source_mid": mid, "pitch_segments": list(specs), "pitch_fade": fade}
    json.dump(man, open(lib_json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"完成:{os.path.join(lib, out_mid)} 已入库。"
          f"**重启 pc-service 托盘服务**后可在曲库/手机点歌列表看到「{title}」。")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="打印逐句时间轴+音高,辅助挑段落边界")
    a.add_argument("--mid", required=True)
    a.add_argument("--lib", default=LIB_DIR)
    b = sub.add_parser("build", help="生成分段变调新曲目并入库")
    b.add_argument("--mid", required=True)
    b.add_argument("--segments", nargs="+", required=True,
                   metavar="起-止:半音", help="如 115.5-150.5:-2 253-end:-2 (秒)")
    b.add_argument("--fade", type=float, default=1.2, help="交叉淡化时长秒(默认1.2)")
    b.add_argument("--suffix", default="分段变调", help="新曲目标题后缀,如 副歌-2")
    b.add_argument("--out-mid", default=None, help="新曲目 id(默认 原id+seg)")
    b.add_argument("--lib", default=LIB_DIR)
    args = ap.parse_args()
    if args.cmd == "analyze":
        analyze(args.mid, args.lib)
    else:
        build(args.mid, args.segments, args.fade, args.suffix, args.out_mid, args.lib)


if __name__ == "__main__":
    main()
