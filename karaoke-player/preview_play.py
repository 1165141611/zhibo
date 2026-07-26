# -*- coding: utf-8 -*-
"""扫描窗口「试听」用的轻量预览播放器(pc-service 以子进程拉起)。

- 走**系统默认输出**(用户自己听的电脑通道,不进直播链路)播伴奏,音量压低;
- 一个**纯文本歌词窗**:当前行高亮 + 上一行/下一行淡显,随播放推进;
- `←` `→` 步退/步进 5 秒,`Esc` 退出。

只为快速预览"是不是这首歌 / 伴奏对不对",不涉及绿幕/音准/变调(那是 player.py 的活)。

用法: python preview_play.py <song_dir> <mid> [--volume 0.4]
"""
import os
import sys
import argparse
import threading

import numpy as np
import sounddevice as sd
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from assets import Song, SAMPLE_RATE   # noqa: E402

STEP_MS = 5000   # ←/→ 步进步退


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("song_dir")
    ap.add_argument("mid")
    ap.add_argument("--volume", type=float, default=0.4)
    a = ap.parse_args()

    song = Song(a.song_dir, a.mid)
    audio = song.accompany()                 # float32 (N,2)(伴奏)
    total = audio.shape[0]
    vol = max(0.0, min(1.0, a.volume))
    lines = song.lines
    st = {"pos": 0}
    lock = threading.Lock()

    def _cb(outdata, frames, t, status):     # sounddevice 音频回调:从 pos 取块、推进
        with lock:
            pos = st["pos"]
            st["pos"] = min(pos + frames, total)
        end = min(pos + frames, total)
        n = end - pos
        if n > 0:
            outdata[:n] = audio[pos:end] * vol
        if n < frames:
            outdata[n:] = 0

    stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=2, dtype="float32", callback=_cb)
    stream.start()

    def cur_ms():
        with lock:
            return int(st["pos"] / SAMPLE_RATE * 1000)

    def seek(dms):
        with lock:
            st["pos"] = max(0, min(total, st["pos"] + int(dms / 1000 * SAMPLE_RATE)))

    def idx_at(ms):                          # 最后一个 start<=ms 的行(-1=前奏)
        lo = -1
        for i, ln in enumerate(lines):
            if ln.start <= ms:
                lo = i
            else:
                break
        return lo

    # ---- 纯文本歌词窗 ----
    root = tk.Tk()
    root.title("试听预览 · " + (song.title or a.mid))
    root.configure(bg="#111111")
    root.geometry("580x300")
    root.attributes("-topmost", True)
    tk.Label(root, text="%s — %s" % (song.title or a.mid, song.artist or ""),
             fg="#8a8a8a", bg="#111111", font=("Microsoft YaHei", 11)).pack(pady=(16, 6))
    lbl_prev = tk.Label(root, text="", fg="#5a5a5a", bg="#111111", font=("Microsoft YaHei", 13))
    lbl_prev.pack()
    lbl_cur = tk.Label(root, text="♪", fg="#ffffff", bg="#111111",
                       font=("Microsoft YaHei", 20, "bold"), wraplength=540)
    lbl_cur.pack(pady=10)
    lbl_next = tk.Label(root, text="", fg="#5a5a5a", bg="#111111", font=("Microsoft YaHei", 13))
    lbl_next.pack()
    tk.Label(root, text="←  →  步退/进 5 秒        Esc  退出",
             fg="#4a4a4a", bg="#111111", font=("Microsoft YaHei", 9)).pack(side="bottom", pady=6)
    lbl_time = tk.Label(root, text="", fg="#4a4a4a", bg="#111111", font=("Consolas", 10))
    lbl_time.pack(side="bottom")

    def tick():
        ms = cur_ms()
        i = idx_at(ms)
        lbl_prev.config(text=lines[i - 1].text if i - 1 >= 0 else "")
        lbl_cur.config(text=(lines[i].text if 0 <= i < len(lines) else ("♪ 前奏 ♪" if i < 0 else "♪")))
        lbl_next.config(text=lines[i + 1].text if 0 <= i + 1 < len(lines) else "")
        tot_s = total // SAMPLE_RATE
        lbl_time.config(text="%d:%02d / %d:%02d" %
                        (ms // 60000, (ms // 1000) % 60, tot_s // 60, tot_s % 60))
        root.after(80, tick)

    def quit_(*_):
        try:
            stream.stop(); stream.close()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    root.bind("<Left>", lambda e: seek(-STEP_MS))
    root.bind("<Right>", lambda e: seek(STEP_MS))
    root.bind("<Escape>", quit_)
    root.protocol("WM_DELETE_WINDOW", quit_)
    tick()
    root.after(100, root.focus_force)
    root.mainloop()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
