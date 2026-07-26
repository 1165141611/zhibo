# -*- coding: utf-8 -*-
"""手机版全民K歌资源 → PC 四件套转换器(pc-service 以子进程调用)。

用法:
    python mobile_convert.py <mid> <out_dir> <qrc> <oke> <tkm1> [<tkm2>]

产出 <out_dir>/<mid>/<mid>{_accompany.pcm,_kongsinger.pcm,.note,.qrc},与 PC 版曲库
四件套格式完全一致(下游 assets.Song / 播放器 / 点歌 / 升降调零改动)。逐行打印
`PROGRESS ...` / `OK <mid>` / `ERR <msg>` 供 pc-service 读进度。

三样资源的手机侧格式(见 KARAOKE_SYSTEM.md):
- `.qrc` 逐字歌词:hex 文本(3DES+zlib),原样拷贝(assets._qrc_decrypt 已认 hex)。
- `.oke` 音高:同链 hex→3DES→zlib → 明文 `起始ms 时长ms MIDI`,解密后写成 PC `.note`。
- `.tkm` 伴奏/原唱:**QQ音乐 QMCv1 静态密钥加密的 M4A**。密钥 KEY256 与 PC 伴奏 PCM 的
  XOR 静态密钥(wesing_pcm_key.PCM_XOR_KEY)是**同一张 256 字节表**,只是用法不同:
  PC = 直接 256 周期 XOR;手机 tkm = `mask128[i]=KEY256[(i*i+27)&0xff]` + 0x7FFF 环绕 keystream。
  解出即标准 M4A,ffmpeg 解成 44.1k/16bit 立体声,再用同一 KEY256 XOR 加密存(与 PC PCM 一致)。
"""
import os
import sys
import shutil
import subprocess
import tempfile

import numpy as np
import imageio_ffmpeg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from assets import SAMPLE_RATE, CHANNELS, _qrc_decrypt   # noqa: E402
from wesing_pcm_key import PCM_XOR_KEY                    # = QMC 静态 KEY256  # noqa: E402

# QMCv1 静态密钥:与 PC 伴奏 PCM 的 XOR 密钥同表(2026-07 破解一致)。
KEY256 = PCM_XOR_KEY
_MASK128 = bytes(KEY256[(i * i + 27) & 0xff] for i in range(128))
_PCMKEY = np.frombuffer(PCM_XOR_KEY, dtype=np.uint8)


def _progress(msg):
    print("PROGRESS " + msg, flush=True)


# ---------------------------------------------------------------- .tkm → M4A
def _qmc1_keystream(n: int) -> bytes:
    """QMCv1(Mask128)keystream:前 32768B = mask128*256(纯周期128);之后按 0x7FFF 环绕
    (startblk = firstblk + firstblk[1:-1] 共 65534B;commonblk = firstblk[:-1] 32767B 循环)。"""
    first = _MASK128 * 256                 # 32768
    start = first + first[1:-1]            # 65534
    common = first[:-1]                    # 32767
    if n <= len(start):
        return start[:n]
    rem = n - len(start)
    full, tail = divmod(rem, len(common))
    return start + common * full + common[:tail]


def tkm_to_m4a(tkm_path: str) -> bytes:
    d = open(tkm_path, "rb").read()
    ks = _qmc1_keystream(len(d))
    return (np.frombuffer(d, np.uint8) ^ np.frombuffer(ks, np.uint8)).tobytes()


# ---------------------------------------------------------------- M4A → PCM
def m4a_to_pcm_int16(m4a_bytes: bytes) -> np.ndarray:
    """M4A → int16 立体声 (N,2)。经临时文件喂 ffmpeg(mp4 需可 seek,管道不稳)。"""
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    fd, tmp = tempfile.mkstemp(suffix=".m4a")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(m4a_bytes)
        p = subprocess.run(
            [ff, "-v", "error", "-i", tmp, "-f", "s16le",
             "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE), "pipe:1"],
            capture_output=True)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if p.returncode != 0:
        raise RuntimeError("ffmpeg 解码失败: " + p.stderr.decode("utf-8", "replace")[:300])
    return np.frombuffer(p.stdout, np.int16).reshape(-1, CHANNELS)


# ---------------------------------------------------------------- 伴奏/原唱判别
def vocal_center_ratio(pcm_int16: np.ndarray, note_text: str) -> float:
    """按 .note 音符时间轴算 中置声道能量(音符段 / 间奏段)比。
    伴奏把中置人声消掉 → 音符段中置能量明显偏低 → 比值 <1(实测 伴奏≈0.49、原唱≈1.0)。"""
    hop = SAMPLE_RATE // 10
    n = len(pcm_int16) // hop
    if n == 0:
        return 1.0
    mid = (pcm_int16[:, 0].astype(np.float32) + pcm_int16[:, 1].astype(np.float32)) / 2.0
    env = np.sqrt((mid[:n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
    active = np.zeros(n, bool)
    for line in note_text.splitlines():
        p = line.split()
        if len(p) >= 2:
            try:
                s = int(p[0]) // 100
                d = max(1, int(p[1]) // 100)
            except ValueError:
                continue
            active[s:min(n, s + d)] = True
    if not active.any() or active.all():
        return 1.0
    return float(env[active].mean() / (env[~active].mean() + 1e-9))


# ---------------------------------------------------------------- PCM 写盘(XOR 加密)
def _write_pcm(pcm_int16: np.ndarray, path: str):
    """int16 立体声 → 与 PC 一致的 XOR 加密裸 PCM(load_pcm(decrypt=True) 可还原)。"""
    raw = np.ascontiguousarray(pcm_int16).view(np.uint8).ravel()
    ks = np.resize(_PCMKEY, len(raw))
    (raw ^ ks).tofile(path)


# ---------------------------------------------------------------- 主流程
def convert(mid, out_dir, qrc_path, oke_path, tkm_paths):
    dst = os.path.join(out_dir, mid)
    os.makedirs(dst, exist_ok=True)

    _progress("note")
    note_text = _qrc_decrypt(open(oke_path, "rb").read())   # .oke 与 QRC 同解密链
    with open(os.path.join(dst, mid + ".note"), "w", encoding="utf-8", newline="\n") as f:
        f.write(note_text)

    shutil.copy2(qrc_path, os.path.join(dst, mid + ".qrc"))  # 手机 hex QRC 原样(assets 认)

    pcms = []
    for i, t in enumerate(tkm_paths):
        _progress("tkm %d/%d" % (i + 1, len(tkm_paths)))
        pcms.append(m4a_to_pcm_int16(tkm_to_m4a(t)))

    if len(pcms) == 1:
        acc = kon = pcms[0]                                  # 只下到一轨:原唱切换退化为同轨
    else:
        r0 = vocal_center_ratio(pcms[0], note_text)
        r1 = vocal_center_ratio(pcms[1], note_text)
        _progress("detect r0=%.3f r1=%.3f" % (r0, r1))
        acc, kon = (pcms[0], pcms[1]) if r0 <= r1 else (pcms[1], pcms[0])  # 比值小=伴奏

    _progress("pcm accompany")
    _write_pcm(acc, os.path.join(dst, mid + "_accompany.pcm"))
    _progress("pcm kongsinger")
    _write_pcm(kon, os.path.join(dst, mid + "_kongsinger.pcm"))
    print("OK " + mid, flush=True)


def main():
    if len(sys.argv) < 6:
        print("ERR 用法: mobile_convert.py <mid> <out_dir> <qrc> <oke> <tkm1> [<tkm2>]", flush=True)
        sys.exit(2)
    mid, out_dir, qrc_path, oke_path = sys.argv[1:5]
    tkm_paths = [p for p in sys.argv[5:7] if p]
    convert(mid, out_dir, qrc_path, oke_path, tkm_paths)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        main()
    except Exception as e:
        print("ERR " + str(e), flush=True)
        sys.exit(1)
