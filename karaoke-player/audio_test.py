# -*- coding: utf-8 -*-
"""测音工具:找出你监听那条干净的输出设备。
用法:
  python audio_test.py --list          列出所有输出设备
  python audio_test.py 25              向设备25播放2秒测试音(较小音量)
提示:先把麦克风监听/音箱关掉再测,避免回授炸麦。听到干净"哔——"的那个索引就是它。
"""
import sys
import numpy as np
import sounddevice as sd

SR = 44100


def list_devices():
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            api = sd.query_hostapis(d["hostapi"])["name"]
            print(f"[{i:2d}] {d['name'][:45]:45s} {d['max_output_channels']}ch  {api}")


def play_tone(idx):
    t = np.linspace(0, 2.0, int(SR * 2), False)
    # 440Hz + 淡入淡出,音量 0.2 避免炸
    tone = 0.2 * np.sin(2 * np.pi * 440 * t).astype(np.float32)
    env = np.minimum(np.minimum(t / 0.1, (2 - t) / 0.1), 1.0)
    tone *= env
    stereo = np.column_stack([tone, tone])
    name = sd.query_devices(idx)["name"]
    print(f"→ 向 [{idx}] {name} 播放2秒测试音…")
    sd.play(stereo, SR, device=idx)
    sd.wait()
    print("完成。听到干净的音了吗?")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2 or sys.argv[1] == "--list":
        list_devices()
    else:
        play_tone(int(sys.argv[1]))
