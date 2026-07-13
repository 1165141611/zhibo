# -*- coding: utf-8 -*-
"""独立子进程:用 winrt 读系统媒体会话(歌名/进度),每秒打印一行 JSON 到 stdout。
故意放在子进程里跑 —— winrt 的 COM 和主进程的 pycaw COM 彻底隔离,避免冲突崩溃。
主进程(server.py)读它的 stdout 更新状态。"""
import sys
import os
import json
import time
import asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smtc  # winrt 只在这个子进程里被导入


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    last = None
    while True:
        try:
            snap = asyncio.run(smtc.snapshot())
        except Exception:
            snap = None
        if snap is not None and snap != last:
            try:
                sys.stdout.write(json.dumps(snap) + "\n")
                sys.stdout.flush()
            except Exception:
                break  # 主进程关了管道,退出
            last = snap
        time.sleep(1.0)


if __name__ == "__main__":
    main()
