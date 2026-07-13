# -*- coding: utf-8 -*-
"""
纯 ctypes 的 Windows MIDI 输出(走系统 winmm.dll)
不需要 python-rtmidi / mido,不需要 C++ 编译器。
loopMIDI 创建的虚拟端口会作为一个普通 MIDI 输出设备出现在这里。
"""

import ctypes
from ctypes import wintypes

_winmm = ctypes.WinDLL("winmm")
MAXPNAMELEN = 32


class MIDIOUTCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * MAXPNAMELEN),
        ("wTechnology", wintypes.WORD),
        ("wVoices", wintypes.WORD),
        ("wNotes", wintypes.WORD),
        ("wChannelMask", wintypes.WORD),
        ("dwSupport", wintypes.DWORD),
    ]


_winmm.midiOutGetNumDevs.restype = wintypes.UINT
_winmm.midiOutGetDevCapsW.argtypes = [ctypes.c_uint, ctypes.POINTER(MIDIOUTCAPS), wintypes.UINT]
_winmm.midiOutGetDevCapsW.restype = wintypes.UINT
_winmm.midiOutOpen.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.UINT,
                               wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
_winmm.midiOutOpen.restype = wintypes.UINT
_winmm.midiOutShortMsg.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_winmm.midiOutShortMsg.restype = wintypes.UINT
_winmm.midiOutClose.argtypes = [wintypes.HANDLE]
_winmm.midiOutClose.restype = wintypes.UINT


def list_output_names():
    """返回所有 MIDI 输出设备名列表。"""
    names = []
    n = _winmm.midiOutGetNumDevs()
    for i in range(n):
        caps = MIDIOUTCAPS()
        if _winmm.midiOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            names.append(caps.szPname)
    return names


class MidiOut:
    """打开一个 MIDI 输出端口并发送短消息。"""

    def __init__(self):
        self.handle = wintypes.HANDLE()
        self.device_id = None

    def open_by_name(self, keyword):
        """按名字(部分匹配,不区分大小写)打开端口。成功返回匹配到的名字,否则 None。"""
        kw = keyword.lower()
        n = _winmm.midiOutGetNumDevs()
        for i in range(n):
            caps = MIDIOUTCAPS()
            if _winmm.midiOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) != 0:
                continue
            if kw in caps.szPname.lower():
                if _winmm.midiOutOpen(ctypes.byref(self.handle), i, 0, 0, 0) == 0:
                    self.device_id = i
                    return caps.szPname
        return None

    def _short(self, status, data1, data2):
        if not self.handle:
            return False
        msg = (status & 0xFF) | ((data1 & 0xFF) << 8) | ((data2 & 0xFF) << 16)
        return _winmm.midiOutShortMsg(self.handle, msg) == 0

    def note_on(self, note, velocity=100, channel=0):
        return self._short(0x90 | (channel & 0x0F), note, velocity)

    def note_off(self, note, channel=0):
        return self._short(0x80 | (channel & 0x0F), note, 0)

    def cc(self, controller, value, channel=0):
        """控制变更(Control Change)。value 0-127 绝对值。"""
        return self._short(0xB0 | (channel & 0x0F), controller, value)

    def close(self):
        if self.handle:
            _winmm.midiOutClose(self.handle)
            self.handle = wintypes.HANDLE()
            self.device_id = None


# ── MIDI 输入(读 Studio One 回传的 Mackie 状态)─────────────
MAXPNAMELEN_IN = 32
CALLBACK_FUNCTION = 0x00030000
MIM_DATA = 0x3C3


class MIDIINCAPS(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * MAXPNAMELEN_IN),
        ("dwSupport", wintypes.DWORD),
    ]


_winmm.midiInGetNumDevs.restype = wintypes.UINT
_winmm.midiInGetDevCapsW.argtypes = [ctypes.c_uint, ctypes.POINTER(MIDIINCAPS), wintypes.UINT]
_winmm.midiInGetDevCapsW.restype = wintypes.UINT
_MIDIIN_CB = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wintypes.UINT,
                                ctypes.c_ulonglong, ctypes.c_ulonglong, ctypes.c_ulonglong)
_winmm.midiInOpen.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.UINT,
                              _MIDIIN_CB, ctypes.c_void_p, wintypes.DWORD]
_winmm.midiInOpen.restype = wintypes.UINT


class MidiIn:
    """打开一个 MIDI 输入端口,收到短消息时回调 on_message(status, data1, data2)。"""

    def __init__(self):
        self.handle = wintypes.HANDLE()
        self._cb = None  # 必须保留引用,否则回调被 GC

    def open_by_name(self, keyword, on_message):
        kw = keyword.lower()
        for i in range(_winmm.midiInGetNumDevs()):
            caps = MIDIINCAPS()
            if _winmm.midiInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) != 0:
                continue
            if kw in caps.szPname.lower():
                def _proc(h, msg, inst, p1, p2, _cb=on_message):
                    if msg == MIM_DATA:
                        _cb(p1 & 0xFF, (p1 >> 8) & 0xFF, (p1 >> 16) & 0xFF)
                self._cb = _MIDIIN_CB(_proc)
                if _winmm.midiInOpen(ctypes.byref(self.handle), i, self._cb, None,
                                     CALLBACK_FUNCTION) == 0:
                    _winmm.midiInStart(self.handle)
                    return caps.szPname
        return None

    def close(self):
        if self.handle:
            _winmm.midiInStop(self.handle)
            _winmm.midiInClose(self.handle)
            self.handle = wintypes.HANDLE()
