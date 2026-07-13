# -*- coding: utf-8 -*-
"""显示 / 隐藏 Studio One 主窗口(音频引擎不受影响,只是藏 GUI)。"""
import win32gui
import win32con
import win32process
import psutil

STUDIO_PROC = "studio one.exe"
STUDIO_CLASS = "CCLWindowClass"   # Studio One 主窗口类名


def find_windows():
    """找到 Studio One 主窗口句柄(即使已隐藏也能找到)。"""
    hwnds = []

    def _enum(hwnd, _):
        try:
            if win32gui.GetClassName(hwnd) != STUDIO_CLASS:
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if psutil.Process(pid).name().lower() == STUDIO_PROC:
                hwnds.append(hwnd)
        except Exception:
            pass

    win32gui.EnumWindows(_enum, None)
    return hwnds


def hide():
    hwnds = find_windows()
    for h in hwnds:
        win32gui.ShowWindow(h, win32con.SW_HIDE)
    return bool(hwnds)


def show():
    hwnds = find_windows()
    for h in hwnds:
        win32gui.ShowWindow(h, win32con.SW_SHOW)   # 恢复到隐藏前的状态(保留最大化)
        try:
            win32gui.SetForegroundWindow(h)
        except Exception:
            pass
    return bool(hwnds)
