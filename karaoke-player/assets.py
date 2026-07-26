# -*- coding: utf-8 -*-
"""资产加载:从全民K歌(WeSing/手机版)扒来的数据 → 播放器用的结构。
- 歌词:QRC(三重魔改DES+zlib),解析出逐行 + 逐字时间轴
- 音高:.note 纯文本 `起始ms 时长ms MIDI音高`
- 伴奏/原唱:裸 PCM 44.1kHz/16bit/立体声
"""
import re
import os
import json
import zlib
import wave
import numpy as np

from tripledes import DECRYPT, tripledes_crypt, tripledes_key_setup

QRC_KEY = b"!@#)(*$%123ZXC!@!@#)(NHL"

# 采样参数(WeSing 落盘的裸 PCM 固定格式)
SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_DTYPE = np.int16


# ---------------------------------------------------------------- QRC 歌词
def _qrc_decrypt(raw: bytes) -> str:
    """支持两种封装:
    - 手机缓存 CLOUD:整份是 hex 文本
    - PC 版:`[offset:0]\\n` 明文头 + 裸密文
    统一转成密文字节后 tripledes+zlib。
    """
    head = raw.lstrip()[:16]
    if all(c in b"0123456789abcdefABCDEF\r\n\t " for c in raw.strip()):
        cipher = bytearray.fromhex(raw.strip().decode("ascii"))
    else:
        # PC 版:去掉第一行明文头(如 [offset:0])
        nl = raw.find(b"\n")
        cipher = bytearray(raw[nl + 1:] if nl != -1 else raw)
    sch = tripledes_key_setup(QRC_KEY, DECRYPT)
    out = bytearray()
    for i in range(0, len(cipher), 8):
        out += tripledes_crypt(cipher[i:], sch)
    return zlib.decompress(out).decode("utf-8", "replace")


class Word:
    __slots__ = ("start", "dur", "text")

    def __init__(self, start, dur, text):
        self.start = start      # ms
        self.dur = dur          # ms
        self.text = text

    @property
    def end(self):
        return self.start + self.dur


class Line:
    __slots__ = ("start", "dur", "words", "text")

    def __init__(self, start, dur, words):
        self.start = start
        self.dur = dur
        self.words = words
        self.text = "".join(w.text for w in words)

    @property
    def end(self):
        return self.start + self.dur


def load_lyrics(qrc_path: str):
    """返回 (meta:dict, lines:list[Line])。逐行含逐字时间轴。"""
    raw = open(qrc_path, "rb").read()
    xml = _qrc_decrypt(raw)
    m = re.search(r'LyricContent="(.*?)"\s*/>', xml, re.S)
    content = m.group(1) if m else xml

    meta = {}
    for key in ("ti", "ar", "al"):
        mm = re.search(r"\[%s:(.*?)\]" % key, content)
        if mm:
            meta[key] = mm.group(1).strip()

    lines = []
    # 逐行:[行起ms,行时长ms] 后跟若干  字(字起ms,字时长ms)
    line_re = re.compile(r"\[(\d+),(\d+)\](.*)")
    word_re = re.compile(r"(.*?)\((\d+),(\d+)\)")
    for lm in line_re.finditer(content):
        lstart, ldur, body = int(lm.group(1)), int(lm.group(2)), lm.group(3)
        words = []
        for wm in word_re.finditer(body):
            txt, wstart, wdur = wm.group(1), int(wm.group(2)), int(wm.group(3))
            if txt:
                words.append(Word(wstart, wdur, txt))
        if not words:
            # 没有逐字信息时,整行当一个词
            txt = re.sub(r"\(\d+,\d+\)", "", body).strip()
            if not txt:
                continue
            words = [Word(lstart, ldur, txt)]
        lines.append(Line(lstart, ldur, words))
    lines.sort(key=lambda ln: ln.start)
    return meta, lines


# ---------------------------------------------------------------- .note 音高
class Note:
    __slots__ = ("start", "dur", "midi")

    def __init__(self, start, dur, midi):
        self.start = start      # ms
        self.dur = dur          # ms
        self.midi = midi        # MIDI 音高(60=中央C)

    @property
    def end(self):
        return self.start + self.dur


def _note_text(note_path: str) -> str:
    """读取音高文本。PC 版 `.note` 是明文;手机版 `.oke` 是 hex(与 QRC 同链 3DES+zlib 加密),
    自动识别并解密。统一返回明文 `起始ms 时长ms MIDI` 文本。"""
    raw = open(note_path, "rb").read()
    s = raw.strip()
    # 明文 .note 每行 3 个数字带空格;hex .oke 是无空格连续 hex。据此(或按 .oke 扩展名)区分。
    is_hex = bool(s) and b" " not in s and all(c in b"0123456789abcdefABCDEF\r\n\t" for c in s)
    if note_path.lower().endswith(".oke") or is_hex:
        return _qrc_decrypt(raw)      # 手机 .oke:hex→3DES(QRC_KEY)→zlib
    return raw.decode("utf-8", "ignore")


def load_notes(note_path: str):
    """解析 .note(PC 明文)或 .oke(手机加密)→ list[Note]。每行 `起始ms 时长ms MIDI音高`。"""
    notes = []
    for line in _note_text(note_path).splitlines():
        p = line.split()
        if len(p) >= 3:
            try:
                notes.append(Note(int(p[0]), int(p[1]), int(p[2])))
            except ValueError:
                pass
    notes.sort(key=lambda n: n.start)
    return notes


# ---------------------------------------------------------------- PCM 音频
# WeSing 的 _accompany.pcm / _kongsinger.pcm 是"加密PCM":
# 静态 256 字节重复 XOR 密钥(周期256),所有歌通用。见 wesing_pcm_key.py。
from wesing_pcm_key import PCM_XOR_KEY
_KEY = np.frombuffer(PCM_XOR_KEY, dtype=np.uint8)


def load_pcm(pcm_path: str, decrypt: bool = True) -> np.ndarray:
    """加密PCM → 解密 → float32 数组 shape (N, 2),范围 [-1,1]。"""
    raw = np.fromfile(pcm_path, dtype=np.uint8)
    if decrypt:
        pad = (-len(raw)) % len(_KEY)
        keystream = np.resize(_KEY, len(raw) + pad)[: len(raw)]
        raw = raw ^ keystream
    samples = np.frombuffer(raw.tobytes(), dtype=SAMPLE_DTYPE)
    if samples.size % CHANNELS:
        samples = samples[: samples.size - (samples.size % CHANNELS)]
    stereo = samples.reshape(-1, CHANNELS).astype(np.float32) / 32768.0
    return stereo


def save_wav(path: str, audio: np.ndarray, sr: int = SAMPLE_RATE):
    """float32 (N,2) → wav,便于试听调试。"""
    data = np.clip(audio, -1, 1)
    pcm16 = (data * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(pcm16.shape[1] if pcm16.ndim > 1 else 1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16.tobytes())


# ---------------------------------------------------------------- 一首歌打包
class Song:
    def __init__(self, res_dir: str, mid: str, qrc_path: str = None):
        self.mid = mid
        base = os.path.join(res_dir, mid, mid)  # Res\<mid>\<mid>_*
        self.accompany_path = base + "_accompany.pcm"
        self.kongsinger_path = base + "_kongsinger.pcm"
        self.note_path = base + ".note"
        # 歌词优先用传入的(可指定手机版 QRC),否则用 PC 版
        self.qrc_path = qrc_path or (base + ".qrc")

        self.meta, self.lines = load_lyrics(self.qrc_path)
        self.notes = load_notes(self.note_path)
        self._accompany = None

        # 入库时保存的歌名/歌手(pc-service 清洗 + 用户手动改名的结果,存 meta.json)优先于
        # QRC 原始解析:曲库对脏标题(带 -歌手-ktv 后缀 / 纯数字ID)清洗过、还能手动改名,
        # 播放器显示必须采用这份,而不是 QRC 的 [ti:]/[ar:] 原文。曲库外(Res 回退)则无此文件。
        self._named = {}
        mp = os.path.join(res_dir, mid, "meta.json")
        if os.path.isfile(mp):
            try:
                j = json.load(open(mp, encoding="utf-8"))
                self._named = {"title": (j.get("title") or "").strip(),
                               "artist": (j.get("artist") or "").strip()}
            except Exception:
                self._named = {}

    @property
    def title(self):
        # meta.json 有非空歌名 → 用入库保存的;否则回退 QRC 解析,再回退 mid
        t = self._named.get("title")
        return t if t else self.meta.get("ti", self.mid)

    @property
    def artist(self):
        # meta.json 存在(哪怕歌手为空,也是入库时清洗后的结果)→ 用它;否则回退 QRC 解析
        if "artist" in self._named:
            return self._named["artist"]
        return self.meta.get("ar", "")

    def accompany(self) -> np.ndarray:
        if self._accompany is None:
            self._accompany = load_pcm(self.accompany_path)
        return self._accompany

    def duration_ms(self):
        return int(self.accompany().shape[0] / SAMPLE_RATE * 1000)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    RES = r"D:\WeSingCache\WeSingDL\Res"
    MID = "0039DPnd48clp5"  # 吉姆餐厅
    QRC = os.path.join(
        r"C:\Users\11651\AppData\Local\Temp\claude"
        r"\E--bianchengwenjian-cursor-zhibo"
        r"\d7766944-3a42-4c60-b30c-450affd70042\scratchpad\qrc",
        MID + "_original.qrc",
    )
    song = Song(RES, MID, qrc_path=QRC)
    print("歌名:", song.title, "-", song.artist)
    print("行数:", len(song.lines), " 音符数:", len(song.notes))
    print("伴奏时长:", song.duration_ms() / 1000, "秒")
    print("\n前6行(逐字):")
    for ln in song.lines[:6]:
        ws = " ".join("%s[%d+%d]" % (w.text, w.start, w.dur) for w in ln.words)
        print("  %6dms  %s" % (ln.start, ws))
    print("\n前6个音符:")
    for n in song.notes[:6]:
        print("  %6dms +%dms  MIDI=%d" % (n.start, n.dur, n.midi))
    pit = [n.midi for n in song.notes]
    print("\n音高范围 MIDI %d~%d" % (min(pit), max(pit)))
