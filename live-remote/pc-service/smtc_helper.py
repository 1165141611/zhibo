# -*- coding: utf-8 -*-
"""独立子进程:用 winrt 读 QQ音乐 媒体会话(歌名/进度/状态),并接收父进程经 **stdin** 下发的
播放控制指令(play/pause/next/prev/toggle),对 QQ音乐 会话做**有方向**控制。
故意放在子进程里跑 —— winrt 的 COM 和主进程(server.py)的 pycaw COM 彻底隔离,避免冲突崩溃。

stdout 协议(父进程读):
  - 每行一个 JSON 快照。**每约 1s 无条件重发一帧**(哪怕内容没变):暂停后进度不再流动、
    快照不再变化,老写法"只在变化时才发"会把那帧 bgm_playing=false 永久丢掉 → 状态从此漂移
    (演唱联动不恢复 BGM、手动播放/暂停方向反打)。无条件重发彻底根治(父进程侧自会去重广播)。
  - 以 `#` 开头的行是诊断日志(父进程只写进 server.log,不解析)。
stdin 协议(父进程写):每行一个指令 play / pause / next / prev / toggle。

winrt 全在**主线程单个常驻事件循环**上跑(`loop.run_until_complete`,**绝不每帧 asyncio.run 新建/销毁**
——那会让 winrt 线程池膨胀到数百线程反复 churn,拖垮系统调度器致全桌面卡顿);MediaManager 也**缓存复用**,
不每帧 request_async。stdin 线程只做 IO 塞队列、绝不碰 winrt,避免跨线程/跨套间的 COM 问题。
"""
import sys
import os
import json
import time
import queue
import asyncio
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smtc   # winrt 只在这个子进程里被导入
try:
    import config
    smtc.set_hint(getattr(config, "QQMUSIC_SMTC_HINT", "qqmusic"))
except Exception:
    pass

_cmd_q = queue.Queue()


def _stdin_reader():
    """后台线程:把父进程经 stdin 下发的指令塞进队列(只做 IO,不碰 winrt)。
    父进程关闭 stdin(退出)时循环自然结束,线程退出。"""
    try:
        for line in sys.stdin:
            line = line.strip()
            if line:
                _cmd_q.put(line)
    except Exception as e:
        _emit("#STDIN reader died: " + repr(e))


def _emit(s):
    """写一行到 stdout。管道断(父进程关了)返回 False,调用方据此退出。"""
    try:
        sys.stdout.write(s + "\n")
        sys.stdout.flush()
        return True
    except Exception:
        return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    threading.Thread(target=_stdin_reader, daemon=True).start()

    # ★ 单个常驻事件循环(绝不每帧 asyncio.run 新建/销毁):asyncio.run 每调一次都建拆一个事件循环,
    #   配合每帧 request_async 会让 winrt 线程池膨胀到数百线程反复 churn,拖垮系统调度器 → 全桌面卡。
    #   现全程复用这一个 loop + 缓存的 MediaManager。
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    _mgr = [None]   # 缓存的 SMTC 会话管理器(request 一次,长期复用;失败置 None 下轮重取)

    def _run(coro):
        return loop.run_until_complete(coro)

    def _ensure_mgr():
        if _mgr[0] is None:
            try:
                _mgr[0] = _run(smtc.get_manager())
            except Exception:
                _mgr[0] = None
        return _mgr[0]

    # 启动时把所有会话 AUMID 打进 server.log,方便作者确认 QQMUSIC_SMTC_HINT 是否匹配得上
    try:
        aumids = _run(smtc.list_aumids())
        _emit("#SMTC 媒体会话 AUMID: " + json.dumps(aumids, ensure_ascii=False))
    except Exception:
        pass

    last_snap_at = 0.0
    last_full_emit_at = 0.0
    last_key = None
    while True:
        # 1) 优先排空控制指令(有方向控制;单线程单事件循环,不跨线程碰 winrt)
        drained = False
        try:
            while True:
                cmd = _cmd_q.get_nowait()
                drained = True
                try:
                    if _ensure_mgr() is not None:
                        _run(smtc.control(_mgr[0], cmd))
                except Exception:
                    _mgr[0] = None   # 管理器可能失效,下轮重取
        except queue.Empty:
            pass

        # 2) 约每 0.35s 探一次快照,以便**快速捕捉换歌**(歌名/播放态一变立即发)——自动切歌时
        #    QQ 会把会话音量重置回 100,父进程要尽快知道去压回,越快炸响窗越短。进度(pos)类
        #    变化仍按每 1s 无条件重发一帧(防丢帧漂移,又不至于每 0.35s 刷屏)。
        now = time.monotonic()
        if drained or now - last_snap_at >= 0.35:
            last_snap_at = now
            snap = None
            try:
                if _ensure_mgr() is not None:
                    snap = _run(smtc.snapshot(_mgr[0]))
            except Exception:
                snap = None
                _mgr[0] = None
            key = (snap.get("bgm_title"), snap.get("bgm_playing")) if snap else (None, None)
            # 歌名/播放态变了 → 立即发;否则每 1s 无条件重发一帧(含 pos + 心跳,防管道断成孤儿)
            if drained or key != last_key or now - last_full_emit_at >= 1.0:
                last_key = key
                last_full_emit_at = now
                if not _emit(json.dumps(snap if snap is not None else {})):
                    break
        time.sleep(0.03)


if __name__ == "__main__":
    main()
