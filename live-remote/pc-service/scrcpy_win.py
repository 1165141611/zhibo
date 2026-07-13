# -*- coding: utf-8 -*-
"""
无线 adb 自动连接 + scrcpy 自动投屏
====================================
针对【安卓 11 无线调试】(端口动态,靠 adb 的 mDNS 发现)。

一次性手动前置(只需做一次):
  1. 手机:开发者选项 → 打开【无线调试】。
  2. 电脑与手机配对过一次(同一局域网):
        adb pair 手机IP:配对端口       # 端口/配对码在手机"无线调试"页里
     配对成功后,adb 会记住这台手机的密钥。

之后每次进入悬浮态,本模块自动:
  · adb start-server
  · 若设备尚未连上 → 用 adb mDNS 发现【连接端口】并 adb connect
  · scrcpy 未在跑 → 拉起 scrcpy(默认 --no-audio,只投屏不带声音)

注意:配对(pair)需要手机上现场显示的 6 位码,无法无人值守自动化,故不在此实现;
      无线调试的"开关"是否打开,由手机 App 端在进入悬浮前的闸门负责检查。
"""

import re
import time
import subprocess

import psutil

import config

CREATE_NO_WINDOW = 0x08000000

# 节流:scrcpy 没起来时,避免网页每次重连都疯狂重试 adb
_last_attempt = 0.0


def _run(args, timeout=6):
    """跑一条外部命令,返回合并后的输出文本(不抛异常)。"""
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW, encoding="utf-8", errors="ignore",
        )
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return f"__ERR__ {e}"


def _adb(*args, timeout=6):
    return _run([config.ADB_PATH, *args], timeout=timeout)


def is_scrcpy_running():
    """进程表里是否已有 scrcpy 在跑。"""
    for p in psutil.process_iter(["name"]):
        try:
            name = (p.info.get("name") or "").lower()
            if name.startswith("scrcpy"):
                return True
        except Exception:
            continue
    return False


def _list_devices():
    """解析 `adb devices` → {serial: state}。"""
    devs = {}
    for line in _adb("devices").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devs[parts[0]] = parts[1]
    return devs


def _connected_wireless_serial(phone_ip=None):
    """在已连接设备里找一个 `ip:port` 形态且 state=device 的无线设备。"""
    for serial, state in _list_devices().items():
        if state == "device" and re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", serial):
            if phone_ip is None or serial.startswith(phone_ip + ":"):
                return serial
    return None


def _discover_connect_endpoint(phone_ip=None):
    """用 adb mDNS 找 `_adb-tls-connect` 服务,返回 ip:port(优先匹配手机 IP)。"""
    out = _adb("mdns", "services", timeout=8)
    fallback = None
    for line in out.splitlines():
        if "_adb-tls-connect" not in line:
            continue
        m = re.search(r"(\d+\.\d+\.\d+\.\d+):(\d+)", line)
        if not m:
            continue
        ep = f"{m.group(1)}:{m.group(2)}"
        if phone_ip and m.group(1) == phone_ip:
            return ep
        fallback = fallback or ep
    return fallback


def _connect_device(phone_ip=None):
    """确保有一个可用设备(无线优先),返回 serial 或 None。不启动 scrcpy。"""
    _adb("start-server", timeout=10)

    # 0) 可能已有无线设备挂着(adb 断线重连 / mDNS 自动连)
    serial = _connected_wireless_serial(phone_ip)

    # A) 固定端口(adb tcpip 5555 之后):直接 connect,免配对码,最稳
    if serial is None and phone_ip and config.SCRCPY_FIXED_PORT:
        ep = f"{phone_ip}:{config.SCRCPY_FIXED_PORT}"
        _adb("connect", ep, timeout=6)
        if _list_devices().get(ep) == "device":
            serial = ep

    # B) 安卓11无线调试(动态端口):mDNS 发现连接端口再 connect
    if serial is None and config.SCRCPY_USE_MDNS:
        ep = _discover_connect_endpoint(phone_ip)
        if ep:
            _adb("connect", ep, timeout=8)
            serial = _connected_wireless_serial(phone_ip)
            if serial is None and _list_devices().get(ep) == "device":
                serial = ep

    return serial


def can_reach(phone_ip=None):
    """闸门用:PC 现在能否经无线 adb 连上手机(不启动 scrcpy)。返回 (ok, msg)。"""
    if is_scrcpy_running():
        return True, "scrcpy 已在运行"
    serial = _connect_device(phone_ip)
    if serial:
        return True, f"可连接 {serial}"
    return False, "PC 连不上手机(手机重启后需重跑 adb tcpip 5555;或确认同一 WiFi)"


def ensure_scrcpy(phone_ip=None, min_interval=8.0):
    """
    确保无线 adb 已连 + scrcpy 在跑。返回 (ok: bool, msg: str)。
    phone_ip 来自 WebSocket 对端(手机的局域网 IP),用于优先匹配 mDNS 结果。
    """
    global _last_attempt

    if is_scrcpy_running():
        return True, "scrcpy 已在运行"

    now = time.monotonic()
    if now - _last_attempt < min_interval:
        return False, "正在尝试连接…稍候"
    _last_attempt = now

    serial = _connect_device(phone_ip)
    if serial is None:
        hint = "手机重启后需重跑 adb tcpip 5555;或用无线调试则先 adb pair 配对一次"
        return False, f"未连上无线设备({hint})"

    args = [config.SCRCPY_PATH, "-s", serial, *config.SCRCPY_ARGS]
    try:
        # CREATE_NO_WINDOW 只压制控制台;scrcpy 自己的投屏窗口照常弹出
        subprocess.Popen(args, creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError:
        return False, "找不到 scrcpy(是否在 PATH?)"
    except Exception as e:
        return False, f"启动 scrcpy 失败: {e}"

    return True, f"已连接 {serial},scrcpy 启动中"
