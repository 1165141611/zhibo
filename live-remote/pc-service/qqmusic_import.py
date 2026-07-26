# -*- coding: utf-8 -*-
"""QQ音乐 导入编排(扫描窗「QQ(无音准)」页签用)。

与全民K歌导入的区别:QQ音乐 是**在线搜索**流(ekey 不本地持久化,靠登录态问服务器),
不是扫本地缓存。且**不需要解密**——本账号有会员权限时,非加密音质直接返回明文文件:
  - 伴奏:`SpecialSongFileType.ACCOM` → 明文 OggS(真·卡拉OK伴奏 stem,与原唱等长对齐)
  - 原唱:`SongFileType.FLAC`(退 MP3_320/MP3_128)→ 明文
  - 歌词:`lyric.get_lyric(mid, qrc=True)` → 库内部已解密的**明文 QRC XML**(逐字)
QQ音乐 不提供音高数据,故产出四件套**减 .note**(写空文件,load_notes 返回空→播放器不显音准线)。

流程:
  login_qr()  一次性扫码登录(凭据存 config.QQ_CRED_PATH,复用/过期重扫)
  search()    在线搜索 → 轻量候选(仅元数据,秒回;去重:库里已有的 mid 跳过)
  prepare()   **确认入库时才跑**:下 ACCOM 伴奏 + 原唱 + 歌词 → ffmpeg 转 PCM → 写四件套到暂存
  然后交给 library.import_candidate(cand, title, artist) 拷进曲库(与 PC/手机来源同一入口)。

依赖:qqmusic-api-python(登录态 API)、imageio-ffmpeg、numpy、niquests。见 requirements.txt。
凭据/私有数据仅本机自用,勿分发(见 CLAUDE.md)。
"""
import os
import sys
import time
import asyncio
import threading

import numpy as np
import niquests
import imageio_ffmpeg

import config
import library

# 复用 karaoke-player 的伴奏 PCM XOR 静态密钥(与 WeSing/手机四件套同一张表,下游零改动)
sys.path.append(config.KARAOKE_DIR)
from wesing_pcm_key import PCM_XOR_KEY   # noqa: E402
_PCMKEY = np.frombuffer(PCM_XOR_KEY, dtype=np.uint8)

from qqmusic_api import Client, Credential                                   # noqa: E402
from qqmusic_api.modules.search import SearchType                            # noqa: E402
from qqmusic_api.modules.song import (                                       # noqa: E402
    SongFileInfo, SongFileType, SpecialSongFileType)
from qqmusic_api.models.login import QRLoginType, QRCodeLoginEvents          # noqa: E402

SAMPLE_RATE = 44100
CHANNELS = 2
CDN_HOST = "https://dl.stream.qqmusic.qq.com/"   # purl 若非完整 url 时补的下载主机
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW:pythonw 下不弹黑框

# 无歌词(纯音乐/接口空)时的占位 QRC(明文,load_lyrics/_qrc_meta 都能读,解析出 0 行)
_EMPTY_QRC = ('<?xml version="1.0" encoding="utf-8"?>\n<QrcInfos>\n'
              '<LyricInfo LyricCount="0"></LyricInfo>\n</QrcInfos>')


# ================================================================ 异步桥
def _run(coro):
    """在(worker)线程里同步跑一个协程。每次新建事件循环,简单稳妥(导入非高频)。"""
    return asyncio.run(coro)


# ================================================================ 登录凭据
def load_credential():
    """读本机凭据 → Credential;无/损坏 → None。"""
    try:
        return Credential.model_validate_json(open(config.QQ_CRED_PATH, encoding="utf-8").read())
    except Exception:
        return None


def _save_credential(cred):
    os.makedirs(os.path.dirname(config.QQ_CRED_PATH), exist_ok=True)
    tmp = config.QQ_CRED_PATH + ".tmp"
    open(tmp, "w", encoding="utf-8").write(cred.model_dump_json())
    os.replace(tmp, config.QQ_CRED_PATH)


def logged_in():
    """本机是否已有(未过期)凭据。纯本地判断,不发网络。"""
    cred = load_credential()
    if not cred:
        return False
    try:
        return not cred.is_expired()
    except Exception:
        return True


def logout():
    try:
        os.remove(config.QQ_CRED_PATH)
    except OSError:
        pass


async def _login_flow(login_type, on_qr, should_stop, progress, timeout):
    c = Client()
    try:
        qr = await c.login.get_qrcode(login_type)
        on_qr(qr.data)                       # PNG 字节 → UI 显示
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if should_stop and should_stop():
                return False
            res = await c.login.check_qrcode(qr)
            if res.event != last:
                progress(res.event.name)
                last = res.event
            if res.event == QRCodeLoginEvents.DONE and res.credential:
                _save_credential(res.credential)
                return True
            if res.event in (QRCodeLoginEvents.TIMEOUT, QRCodeLoginEvents.REFUSE):
                return False
            await asyncio.sleep(2)
        return False
    finally:
        await c.close()


def login_qr(login_type="QQ", on_qr=None, should_stop=None, progress=None, timeout=180):
    """阻塞式扫码登录(放 worker 线程里调)。
    on_qr(png_bytes):二维码图片就绪回调;progress(state):状态回调(SCAN/CONF/DONE…)。
    成功(已存凭据)→ True;超时/拒绝/取消 → False。"""
    lt = getattr(QRLoginType, str(login_type).upper(), QRLoginType.QQ)
    return _run(_login_flow(lt, on_qr or (lambda b: None),
                            should_stop, progress or (lambda s: None), timeout))


# ================================================================ 搜索
def _singer_str(song):
    s = song.get("singer")
    if isinstance(s, list):
        return "/".join(x.get("name", "") for x in s if x.get("name"))
    return s or ""


async def _search(keyword, num, page):
    c = Client()
    c.credential = load_credential()
    try:
        r = await c.search.search_by_type(keyword, search_type=SearchType.SONG, num=num, page=page)
        return [s.model_dump() for s in r.song]
    finally:
        await c.close()


def search(keyword, num=20, page=1, known_mids=None):
    """在线搜歌 → 候选列表(去重:库里已有的 mid 跳过)。每个候选:
    {mid, media_mid, song_type, source:"QQ", src_root, title, artist, interval, needs_name}。
    src_root 指向暂存目录(文件此刻还没下,prepare() 时才生成)。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    known = set(known_mids if known_mids is not None else library.manifest().keys())
    cands = []
    seen = set()
    for s in _run(_search(keyword, num, page)):
        mid = s.get("mid")
        if not mid or mid in known or mid in seen:
            continue
        seen.add(mid)
        f = s.get("file") or {}
        cands.append({
            "mid": mid, "media_mid": f.get("media_mid", ""),
            "song_type": s.get("type", 0), "source": "QQ",
            "src_root": config.QQ_STAGING_DIR,
            # 用 title(含"(Live)/(DJ版)"等版本后缀)兜底 name,否则同名不同版分不清
            "title": (s.get("title") or s.get("name") or "").strip(), "artist": _singer_str(s),
            "interval": s.get("interval", 0), "needs_name": False,
        })
    return cands


# ================================================================ 取 url + 歌词
def _full_url(purl):
    if not purl:
        return ""
    return purl if purl.startswith("http") else CDN_HOST + purl


async def _one_url(c, mid, media_mid, song_type, ft, cred):
    fi = SongFileInfo(mid=mid, file_type=ft, song_type=song_type, media_mid=media_mid)
    resp = await c.song.get_song_urls([fi], file_type=ft, credential=cred)
    it = resp.data[0]
    return _full_url(it.purl)


async def _fetch_urls_and_lyric(mid, media_mid, song_type, original_quality=None):
    """一次性取:伴奏 url(可能空)、原唱 url(按音质优先级取第一个明文)、歌词明文 XML。
    original_quality:原唱音质优先级列表(试听传 ['MP3_128'] 省流量;None=用 config.QQ_ORIGINAL_QUALITY)。"""
    c = Client()
    cred = load_credential()
    c.credential = cred
    try:
        accom = await _one_url(c, mid, media_mid, song_type, SpecialSongFileType.ACCOM, cred)
        original, orig_ext = "", ".mp3"
        for qn in (original_quality or config.QQ_ORIGINAL_QUALITY):
            ft = getattr(SongFileType, qn, None)
            if ft is None:
                continue
            u = await _one_url(c, mid, media_mid, song_type, ft, cred)
            if u:
                original, orig_ext = u, ft.value[1]
                break
        try:
            lyr = (await c.lyric.get_lyric(mid, qrc=True)).lyric or ""
        except Exception:
            lyr = ""
        return {"accom": accom, "original": original, "orig_ext": orig_ext, "lyric": lyr}
    finally:
        await c.close()


# ================================================================ 下载 + 转 PCM
_PREVIEW_MAX_BYTES = 900_000   # 试听只取开头 ~0.9MB(MP3_128≈55s / 伴奏ogg≈22s,截断照样解码),压低下载等待
_DL_CONNS = 5                  # QQ CDN 对 vkey 流**按连接限速**(实测单连接~210KB/s、多连接线性叠加),
                               # 故并行分块下载突破限速(5 连接≈1MB/s+)。见 DEV_LOG 十八·补。

def _range_get(url, start, end, timeout, report=None):
    """下载 [start,end] 闭区间字节(HTTP Range)。report(delta):每收到一块回调增量字节。"""
    r = niquests.get(url, timeout=timeout, stream=True,
                     headers={"Range": "bytes=%d-%d" % (start, end)})
    if r.status_code not in (200, 206):
        r.close(); raise RuntimeError("下载失败 HTTP %s" % r.status_code)
    want = end - start + 1
    buf = bytearray()
    for chunk in r.iter_content(65536):
        if chunk:
            buf += chunk
            if report:
                report(len(chunk))
        if len(buf) >= want:
            break
    r.close()
    return bytes(buf[:want])


def _download(url, timeout=120, max_bytes=None, conns=_DL_CONNS, on_progress=None):
    """并行分块下载(突破 QQ CDN 单连接限速)。
    - 先探总长度(Range 0-0 的 Content-Range);拿不到长度/不支持 Range → 退单连接。
    - max_bytes:只取开头这么多(试听);否则整文件。分成 conns 块并发抓,拼接。
    - on_progress(done, total):已下/总字节回调(驱动进度条);并行下各块增量经锁汇总。"""
    # 探总长度
    total = None
    try:
        p = niquests.get(url, timeout=timeout, stream=True, headers={"Range": "bytes=0-0"})
        cr = p.headers.get("Content-Range", "")
        p.close()
        if "/" in cr:
            total = int(cr.rsplit("/", 1)[-1])
    except Exception:
        total = None

    want = min(total, max_bytes) if (total and max_bytes) else (max_bytes or total)
    _done = [0]
    _lock = threading.Lock()

    def report(delta):
        if on_progress and want:
            with _lock:
                _done[0] += delta
                on_progress(min(_done[0], want), want)

    # 不支持 range / 拿不到长度 / 量太小:单连接直下
    if not total or not want or want < 512 * 1024 or conns <= 1:
        r = niquests.get(url, timeout=timeout, stream=True)
        if r.status_code not in (200, 206):
            r.close(); raise RuntimeError("下载失败 HTTP %s" % r.status_code)
        buf = bytearray()
        for chunk in r.iter_content(65536):
            if chunk:
                buf += chunk
                report(len(chunk))
            if max_bytes and len(buf) >= max_bytes:
                break
        r.close()
        if not buf:
            raise RuntimeError("下载为空")
        return bytes(buf[:max_bytes]) if max_bytes else bytes(buf)

    n = conns
    chunk = (want + n - 1) // n
    parts = [b""] * n
    err = [None]

    def grab(i):
        s = i * chunk
        e = min(want, s + chunk) - 1
        if s > e:
            return
        try:
            parts[i] = _range_get(url, s, e, timeout, report=report)
        except Exception as ex:
            err[0] = ex

    ths = [threading.Thread(target=grab, args=(i,)) for i in range(n)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    if err[0]:
        raise err[0]
    data = b"".join(parts)
    if not data:
        raise RuntimeError("下载为空")
    return data


def _audio_to_pcm_int16(raw, ext):
    """任意格式音频字节(ogg/flac/mp3/m4a)→ int16 立体声 (N,2) @44.1k。经临时文件喂 ffmpeg。"""
    import subprocess, tempfile
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    fd, tmp = tempfile.mkstemp(suffix=ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
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


def _write_pcm(pcm_int16, path):
    """int16 立体声 → 与 PC/手机四件套一致的 XOR 加密裸 PCM(load_pcm(decrypt=True) 还原)。"""
    raw = np.ascontiguousarray(pcm_int16).view(np.uint8).ravel()
    ks = np.resize(_PCMKEY, len(raw))
    (raw ^ ks).tofile(path)


def separation_available():
    """是否可做人声分离(装了 demucs)。没装 → prepare 无 ACCOM 时降级两轨都用原唱。"""
    try:
        import importlib.util
        return importlib.util.find_spec("demucs") is not None
    except Exception:
        return False


def separate_accompaniment(pcm_int16, progress_cb=None, pct_cb=None):
    """用 **Demucs**(htdemucs,--two-stems vocals)从原唱 int16 立体声分离出**伴奏**(no_vocals)。
    以子进程跑(torch 不常驻 pc-service;有 CUDA 自动用 GPU)。返回 int16 立体声 (N,2)。
    进度:实时解析 demucs stderr 的 tqdm 百分比,经 pct_cb('分离伴奏', pct) 回调驱动进度条。
    仅在 separation_available() 为真时被 prepare 调用。"""
    import subprocess, tempfile, wave, shutil, re
    if progress_cb:
        progress_cb("人声分离出伴奏(Demucs;GPU~15-30s / CPU 数分钟)")
    work = tempfile.mkdtemp(prefix="qqsep_")
    try:
        in_wav = os.path.join(work, "in.wav")
        with wave.open(in_wav, "wb") as w:
            w.setnchannels(CHANNELS); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
            w.writeframes(np.ascontiguousarray(pcm_int16).tobytes())
        outdir = os.path.join(work, "out")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", COLUMNS="80")
        proc = subprocess.Popen(
            [config.PLAYER_PYTHON, "-m", "demucs", "--two-stems", "vocals",
             "-n", "htdemucs", "-o", outdir, in_wav],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW, env=env)
        # demucs 用 tqdm 往 stderr 打进度条(\r 刷新);实时解析 `NN%` 报给进度条
        pat = re.compile(rb"(\d+)%")
        buf = bytearray(); last = -1
        while True:
            chunk = proc.stderr.read1(4096)
            if not chunk:
                break
            buf += chunk
            segs = re.split(rb"[\r\n]", buf)
            buf = bytearray(segs[-1])                # 留下不完整的尾段
            for seg in segs[:-1]:
                m = pat.search(seg)
                if m and pct_cb:
                    p = min(100, int(m.group(1)))
                    if p != last:
                        pct_cb("分离伴奏", p); last = p
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("Demucs 分离失败(退出码 %d)" % proc.returncode)
        nv = os.path.join(outdir, "htdemucs", "in", "no_vocals.wav")
        if not os.path.isfile(nv):
            raise RuntimeError("Demucs 未产出 no_vocals.wav")
        if pct_cb:
            pct_cb("分离伴奏", 100)
        return _audio_to_pcm_int16(open(nv, "rb").read(), ".wav")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def prepare(cand, progress_cb=None, preview=False, pct_cb=None):
    """下载并转换成四件套(减 .note)到 QQ_STAGING_DIR/<mid>/。
    - 入库(preview=False):有 ACCOM→伴奏=ACCOM、原唱=MP3_320(等长对齐);无 ACCOM→(装了 Demucs 则)人声分离出伴奏,
      否则两轨都用原唱。
    - 试听(preview=True):**只下一轨**(优先伴奏,退原唱),两轨共用,省流量/加快;够 preview_play 播伴奏。
    progress_cb(text):阶段文字;pct_cb(label, pct):当前下载文件百分比(驱动进度条)。
    完成后目录满足 library.import_candidate 的拷贝契约。"""
    def prog(m):
        if progress_cb:
            progress_cb(m)

    def _dl(url, ext, label, max_bytes=None):
        """下载一轨并解成 PCM,带阶段文字 + 百分比回调。"""
        prog(label + "…")
        def op(done, total):
            if pct_cb and total:
                pct_cb(label, int(done * 100 / total))
        return _audio_to_pcm_int16(_download(url, max_bytes=max_bytes, on_progress=op), ext)

    mid = cand["mid"]
    dst = os.path.join(config.QQ_STAGING_DIR, mid)
    os.makedirs(dst, exist_ok=True)

    prog("取地址…")
    # 试听:无伴奏时回退原唱用最小的 MP3_128(省流量、下得快),入库用 config.QQ_ORIGINAL_QUALITY
    info = _fetch_urls_and_lyric_sync(cand, ["MP3_128"] if preview else None)

    if preview:
        # 只下开头 ~0.9MB(Range),截断音频照样解码,试听秒级出声
        if info["accom"]:
            one = _dl(info["accom"], ".ogg", "下载伴奏(试听)", max_bytes=_PREVIEW_MAX_BYTES)
        elif info["original"]:
            one = _dl(info["original"], info["orig_ext"], "下载原唱(试听)", max_bytes=_PREVIEW_MAX_BYTES)
        else:
            raise RuntimeError("该歌曲拿不到可试听音频(可能无版权/需更高会员)")
        accompany = kongsinger = one
    else:
        acc_pcm = orig_pcm = None
        if info["accom"]:
            acc_pcm = _dl(info["accom"], ".ogg", "下载伴奏")
        if info["original"]:
            orig_pcm = _dl(info["original"], info["orig_ext"], "下载原唱")
        if acc_pcm is None and orig_pcm is None:
            raise RuntimeError("该歌曲拿不到可下载音频(可能无版权/需更高会员)")
        kongsinger = orig_pcm if orig_pcm is not None else acc_pcm
        if acc_pcm is not None:
            accompany = acc_pcm                       # QQ 官方伴奏 stem,最佳
        elif separation_available() and orig_pcm is not None:
            accompany = separate_accompaniment(orig_pcm, progress_cb=progress_cb, pct_cb=pct_cb)
        else:
            accompany = orig_pcm                       # 无伴奏也没装分离:两轨都用原唱

    prog("写伴奏…")
    _write_pcm(accompany, os.path.join(dst, mid + "_accompany.pcm"))
    prog("写原唱…")
    _write_pcm(kongsinger, os.path.join(dst, mid + "_kongsinger.pcm"))
    # 空音准(QQ音乐 无音高数据)+ 明文逐字歌词
    open(os.path.join(dst, mid + ".note"), "w", encoding="utf-8").close()
    with open(os.path.join(dst, mid + ".qrc"), "w", encoding="utf-8", newline="\n") as f:
        f.write(info["lyric"].strip() or _EMPTY_QRC)
    return cand


def _fetch_urls_and_lyric_sync(cand, original_quality=None):
    return _run(_fetch_urls_and_lyric(cand["mid"], cand.get("media_mid", ""),
                                      cand.get("song_type", 0), original_quality))
