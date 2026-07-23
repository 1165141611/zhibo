# -*- coding: utf-8 -*-
"""把一首歌的卡拉OK数据(QRC 逐字时间轴 + .note 音高线)解析成 JSON,推给手机演唱页。
镜像 karaoke-player/assets.load_lyrics + load_notes,但只用 tripledes(不引 numpy),与 library.py 同风格。
音高归一化到 [0,1](全曲 MIDI lo/hi),供手机 KaraokeStage 直接画音符块。"""
import os
import re
import sys
import json
import zlib

import config

sys.path.append(config.KARAOKE_DIR)
import tripledes  # noqa: E402

_QRC_KEY = b"!@#)(*$%123ZXC!@!@#)(NHL"
_CACHE = {}   # mid -> payload dict(解析结果缓存,曲库不变则复用)

_LINE_RE = re.compile(r"\[(\d+),(\d+)\](.*)")
_WORD_RE = re.compile(r"(.*?)\((\d+),(\d+)\)")


def _song_dir(mid):
    """优先永久曲库,回退 WeSing 缓存 Res。"""
    lib = os.path.join(config.KARAOKE_LIBRARY_DIR, mid)
    if os.path.isfile(os.path.join(lib, mid + ".qrc")):
        return lib
    res = os.path.join(config.WESING_RES_DIR, mid)
    if os.path.isfile(os.path.join(res, mid + ".qrc")):
        return res
    return None


def _qrc_decrypt(raw: bytes) -> str:
    """两种封装:手机 hex 文本 / PC `[offset:0]\\n`+裸密文 → tripledes + zlib → XML。"""
    if all(c in b"0123456789abcdefABCDEF\r\n\t " for c in raw.strip()):
        cipher = bytearray.fromhex(raw.strip().decode("ascii"))
    else:
        nl = raw.find(b"\n")
        cipher = bytearray(raw[nl + 1:] if nl != -1 else raw)
    sch = tripledes.tripledes_key_setup(_QRC_KEY, tripledes.DECRYPT)
    out = bytearray()
    for i in range(0, len(cipher), 8):
        out += tripledes.tripledes_crypt(cipher[i:], sch)
    return zlib.decompress(out).decode("utf-8", "replace")


def _load_lines(qrc_path):
    """→ [{start,end,chars:[{text,start,dur}]}](逐字,绝对 ms)。"""
    xml = _qrc_decrypt(open(qrc_path, "rb").read())
    m = re.search(r'LyricContent="(.*?)"\s*/>', xml, re.S)
    content = m.group(1) if m else xml
    lines = []
    for lm in _LINE_RE.finditer(content):
        lstart, ldur, body = int(lm.group(1)), int(lm.group(2)), lm.group(3)
        chars = []
        for wm in _WORD_RE.finditer(body):
            txt, wstart, wdur = wm.group(1), int(wm.group(2)), int(wm.group(3))
            if txt:
                chars.append({"text": txt, "start": wstart, "dur": wdur})
        if not chars:
            txt = re.sub(r"\(\d+,\d+\)", "", body).strip()
            if not txt:
                continue
            chars = [{"text": txt, "start": lstart, "dur": ldur}]
        lines.append({"start": lstart, "end": lstart + ldur, "chars": chars})
    lines.sort(key=lambda ln: ln["start"])
    return lines


def _load_notes(note_path):
    """→ [(start,dur,midi)]。每行 `起始ms 时长ms MIDI`。"""
    notes = []
    try:
        with open(note_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                p = line.split()
                if len(p) >= 3:
                    try:
                        notes.append((int(p[0]), int(p[1]), int(p[2])))
                    except ValueError:
                        pass
    except OSError:
        pass
    notes.sort(key=lambda n: n[0])
    return notes


def _load_chorus(song_dir):
    """副歌区间 [[start_ms,end_ms],...] —— 来自 meta.json 的 `chorus` 键(手动标注,状态机
    唯一无法自动推导的数据;自动切镜用)。无标注则空列表。"""
    try:
        with open(os.path.join(song_dir, "meta.json"), "r", encoding="utf-8") as f:
            meta = json.load(f)
        ch = meta.get("chorus") or []
        return [[int(a), int(b)] for a, b in ch]
    except Exception:
        return []


def song_karaoke(mid):
    """返回 {mid, lines, notes:[{start,dur,pitch0..1}], chorus:[[s,e]...]} 或 None(找不到 QRC)。
    lines/notes 供手机演唱页;chorus 为副歌区间标注(原供已移除的自动切镜,现暂无消费方,保留字段)。"""
    if not mid:
        return None
    if mid in _CACHE:
        return _CACHE[mid]
    d = _song_dir(mid)
    if d is None:
        return None
    try:
        lines = _load_lines(os.path.join(d, mid + ".qrc"))
    except Exception:
        return None
    raw_notes = _load_notes(os.path.join(d, mid + ".note"))
    # 音高归一化(全曲 lo/hi → [0,1])
    notes = []
    if raw_notes:
        los = min(n[2] for n in raw_notes)
        his = max(n[2] for n in raw_notes)
        span = (his - los) or 1
        notes = [{"start": s, "dur": du, "pitch": round((mi - los) / span, 3)}
                 for (s, du, mi) in raw_notes]
    payload = {"mid": mid, "lines": lines, "notes": notes, "chorus": _load_chorus(d)}
    _CACHE[mid] = payload
    return payload
