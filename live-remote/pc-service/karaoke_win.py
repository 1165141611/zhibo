# -*- coding: utf-8 -*-
"""显示 / 隐藏 K歌播放器窗口(音频引擎不受影响,只是藏 GUI)。
播放器是独立 python 子进程,进程名无区分度,故按窗口标题匹配。仿 studio_win.py。"""
import win32gui
import win32con

import config


def find_windows():
    """按窗口标题找 K歌播放器句柄(即使已隐藏也能枚举到)。"""
    hwnds = []

    def _enum(hwnd, _):
        try:
            if win32gui.GetWindowText(hwnd) == config.PLAYER_TITLE:
                hwnds.append(hwnd)
        except Exception:
            pass

    win32gui.EnumWindows(_enum, None)
    return hwnds


def show():
    hwnds = find_windows()
    for h in hwnds:
        win32gui.ShowWindow(h, win32con.SW_SHOW)
        try:
            win32gui.SetForegroundWindow(h)   # 取焦点,快捷键才生效
        except Exception:
            pass
    return bool(hwnds)


def hide():
    hwnds = find_windows()
    for h in hwnds:
        win32gui.ShowWindow(h, win32con.SW_HIDE)
    return bool(hwnds)


def is_visible():
    """窗口当前是否真的可见(实时查,不信缓存)。"""
    return any(win32gui.IsWindowVisible(h) for h in find_windows())
