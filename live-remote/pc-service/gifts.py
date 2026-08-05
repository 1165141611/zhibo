# -*- coding: utf-8 -*-
"""抖音礼物目录:抓取 webcast/gift/list → 缓存目录 JSON + 下载图标 PNG(离线优先)。

用途:托盘「礼物菜单配置」窗据此列出全部礼物(缩略图 + 名 + 抖币价)供勾选;选中的
{id, 自定义文字} 存 STATE["gifts"],由 server._push_gifts 解析成 {图标绝对路径, 文字} 推给
播放器,在绿幕左侧竖排显示"礼物→权益"引导条(如 🎈点歌 / 🍰插队)。

礼物列表基本静态:有本地缓存就直接用(离线优先),fetch_catalog(refresh=True) 才重新联网;
联网失败一律回退缓存,绝不因抓不到而报错影响服务。图标按 id 落盘,用到才下载一次。
"""
import json
import os
import urllib.request

import config

# 带个常规浏览器 UA;裸请求偶尔被挡,加上更稳(该接口匿名可取,无需 cookie/签名)。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def _http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _load_cached():
    """读盘上的礼物目录(缺失/损坏返回 [])。"""
    try:
        with open(config.GIFT_CATALOG_JSON, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_cached(items):
    try:
        os.makedirs(os.path.dirname(config.GIFT_CATALOG_JSON), exist_ok=True)
        tmp = config.GIFT_CATALOG_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, config.GIFT_CATALOG_JSON)
    except Exception as e:
        print("[GIFTS] 目录缓存写入失败:", e)


def fetch_catalog(refresh=False):
    """返回礼物目录 [{id, name, diamond, icon_url}]。
    非 refresh 且有缓存 → 直接读盘(离线优先);否则联网抓取并落盘,失败回退缓存。"""
    if not refresh:
        cached = _load_cached()
        if cached:
            return cached
    try:
        raw = _http_get(config.GIFT_LIST_URL)
        data = json.loads(raw)
        gifts = ((data or {}).get("data") or {}).get("gifts") or []
        out = []
        for g in gifts:
            gid = g.get("id")
            if gid is None:
                continue
            icon = g.get("icon") or {}
            urls = icon.get("url_list") or (g.get("image") or {}).get("url_list") or []
            out.append({
                "id": int(gid),
                "name": (g.get("name") or "").strip(),
                "diamond": int(g.get("diamond_count") or 0),
                "icon_url": urls[0] if urls else "",
            })
        if out:
            _save_cached(out)
            return out
    except Exception as e:
        print("[GIFTS] 抓取失败,回退缓存:", e)
    return _load_cached()


def _url_for(gid):
    for it in _load_cached():
        try:
            if int(it.get("id", -1)) == int(gid):
                return it.get("icon_url", "")
        except Exception:
            continue
    return ""


def icon_path(gid, icon_url=""):
    """返回礼物图标本地 PNG 路径(不存在则下载一次)。下载失败/无 URL 返回 None。
    调用方(配置窗缩略图 / _push_gifts 解析)拿到路径即用;失败自行降级(不显图或占位)。"""
    try:
        gid = int(gid)
    except Exception:
        return None
    os.makedirs(config.GIFT_ICONS_DIR, exist_ok=True)
    p = os.path.join(config.GIFT_ICONS_DIR, "%d.png" % gid)
    if os.path.exists(p) and os.path.getsize(p) > 0:
        return p
    url = icon_url or _url_for(gid)
    if not url:
        return None
    try:
        data = _http_get(url, timeout=15)
        if not data:
            return None
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
        return p
    except Exception as e:
        print("[GIFTS] 图标下载失败", gid, e)
        return None


def resolve(items):
    """把 STATE["gifts"](=[{id, text}] 按显示顺序)解析成推给播放器的
    [{icon: 图标绝对路径 or "", text}]。图标缺失(下载失败)则 icon 留空,播放器仍可只显文字。"""
    out = []
    cat = {int(it["id"]): it for it in _load_cached() if it.get("id") is not None}
    for g in items or []:
        try:
            gid = int(g.get("id"))
        except Exception:
            continue
        url = (cat.get(gid) or {}).get("icon_url", "")
        p = icon_path(gid, url) or ""
        out.append({"icon": p, "text": str(g.get("text", "")).strip()})
    return out


if __name__ == "__main__":   # 手动自检:抓取目录、下第一个图标,打印路径
    cat = fetch_catalog(refresh=True)
    print("礼物数:", len(cat))
    for it in cat[:5]:
        print(" ", it["id"], it["name"], it["diamond"])
    if cat:
        print("图标:", icon_path(cat[0]["id"], cat[0]["icon_url"]))
