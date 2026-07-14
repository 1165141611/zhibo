# -*- coding: utf-8 -*-
"""实时音频引擎:后台生产者线程做变调,喂环形队列;sounddevice 回调只取。
- 升降调:改 semitones 即秒切(延迟≈队列深度~0.2s)
- 变调核心:stftpitchshift 逐块处理 + 块间交叉淡化(消除相位不连续)
- semitones=0 时直通原始音频(最干净)
- 时钟:heard_frame = base + 已输出帧数 - 设备缓冲延迟(与所听内容对齐)
- 设备缓冲给足 ~120ms:回调是 Python 函数会被 GUI 抢 GIL 拖延,缓冲薄了会欠载吱吱声
"""
import threading
import queue
import time

import numpy as np
import sounddevice as sd
from audiotsm import wsola
from audiotsm.io.array import ArrayReader, ArrayWriter

SR = 44100
FEED = 4096       # 每次喂给管线的 source 样本数


class AudioEngine:
    def __init__(self, buffer: np.ndarray, device=None, start_stream=True):
        self.source = np.ascontiguousarray(buffer, np.float32)
        self.semitones = 0
        self.playing = False
        self.xruns = 0
        self._vol_pct = 100       # 音量档位 0..100(=手机媒体音量%);换歌不重置
        self._gain = 1.0          # 实际增益 = 感知曲线(_vol_pct);回调用它,float 赋值原子免锁

        self._lock = threading.Lock()
        self._q = queue.Queue(maxsize=6)
        self._pending = np.zeros((0, 2), np.float32)   # 消费者未用完的余量(仅回调线程访问)
        self._pending_gen = 0
        self._base = 0            # 当前播放段起始 source 帧(时钟基准)
        self._out = 0             # 自 base 起已输出帧数
        self._prod_pos = 0        # 生产者下一个要提交的 source 帧
        self._gen = 0             # 代数,seek/换源时+1 使旧数据作废
        self._run = True

        self._producer = threading.Thread(target=self._produce, daemon=True)
        self._producer.start()

        self.stream = None
        self._lat_frames = 0      # 设备缓冲延迟(帧),current_ms 用于显示时钟补偿
        if start_stream:
            # latency 给足(实测约180ms):回调是 Python 函数,GUI 会抢 GIL 拖它——
            # 窗口每次 show 的首帧曝光要 ~150ms,换行建字缓存 10~35ms;缓冲太薄
            # (WASAPI "high"≈46ms)就会欠载=断续吱吱声。代价是切调/seek 多 ~0.15s
            # 才被听到,可接受;显示时钟已用 _lat_frames 补偿,歌词不会提前。
            self.stream = sd.OutputStream(
                samplerate=SR, channels=2, dtype="float32",
                device=device, blocksize=1024, latency=0.16, callback=self._cb)
            self.stream.start()
            self._lat_frames = int(self.stream.latency * SR)
            dev = sd.query_devices(self.stream.device)["name"]
            print("音频输出 →", dev, "| 延迟 %.0fms" % (self.stream.latency * 1000))

    # ---------------------------------------------------- 生产者(连续流式 WSOLA 管线)
    def _produce(self):
        tsm = wsola(channels=2, speed=1.0)
        rbuf = np.zeros((2, 0), np.float32)   # WSOLA 拉伸后、待重采样的余量
        rpos = 0.0                            # 重采样读位置(rbuf 内)
        local_gen = -1
        cur_speed = None
        while self._run:
            with self._lock:
                playing = self.playing
                src = self.source
                pos = self._prod_pos
                semi = self.semitones
                gen = self._gen
            if gen != local_gen:              # seek/换源/切调:清空管线,从新位置重建
                tsm.clear()
                rbuf = np.zeros((2, 0), np.float32)
                rpos = 0.0
                local_gen = gen
                cur_speed = None
            factor = 2.0 ** (semi / 12.0)
            speed = 1.0 / factor
            if cur_speed != speed:
                tsm.set_speed(speed)
                cur_speed = speed
            if (not playing) or pos >= len(src) or self._q.full():
                # 播放中队列满 → 4ms 快轮询保供给;暂停/播完 → 50ms 慢轮询省 CPU
                # (服务托管下播放器 7×24 常驻,空闲自旋别白烧核)
                time.sleep(0.004 if playing else 0.05)
                continue

            end = min(pos + FEED, len(src))
            block = np.ascontiguousarray(src[pos:end])
            B = end - pos

            if semi == 0:
                commit = block.copy()
            else:
                # 连续喂入 WSOLA(维护状态),收集拉伸输出
                reader = ArrayReader(np.ascontiguousarray(block.T))
                writer = ArrayWriter(channels=2)
                finished = False
                while not (finished and reader.empty):
                    tsm.read_from(reader)
                    _, finished = tsm.write_to(writer)
                stretched = writer.data
                if stretched.shape[1]:
                    rbuf = np.concatenate([rbuf, stretched], axis=1)
                # 连续重采样(比率 factor)→ 最多产出 B 个样本
                avail = rbuf.shape[1]
                out = []
                while rpos + 1 < avail and len(out) < B:
                    i = int(rpos)
                    f = rpos - i
                    out.append(rbuf[:, i] * (1 - f) + rbuf[:, i + 1] * f)
                    rpos += factor
                drop = int(rpos)
                if drop:
                    rbuf = rbuf[:, drop:]
                    rpos -= drop
                commit = (np.asarray(out, np.float32) if out
                          else np.zeros((0, 2), np.float32))

            with self._lock:
                if gen == self._gen:
                    self._prod_pos = pos + B          # 已消费的 source
                    if len(commit):
                        try:
                            self._q.put((gen, commit), timeout=0.1)
                        except queue.Full:
                            pass

    # ---------------------------------------------------- 消费者(回调)
    def _cb(self, outdata, frames, time_info, status):
        if status:
            self.xruns += 1
        with self._lock:
            if not self.playing:
                outdata.fill(0)
                return
            gen = self._gen
            # 丢弃过期 pending
            if self._pending_gen != gen:
                self._pending = np.zeros((0, 2), np.float32)
                self._pending_gen = gen
            buf = self._pending
            # 从队列取够 frames
            while len(buf) < frames:
                try:
                    g, chunk = self._q.get_nowait()
                except queue.Empty:
                    break
                if g != gen:
                    continue
                buf = np.concatenate([buf, chunk], axis=0) if len(buf) else chunk
            take = min(frames, len(buf))
            if take:
                outdata[:take] = buf[:take] * self._gain
            if take < frames:
                outdata[take:] = 0
            self._pending = buf[take:]
            self._out += take
            # 结束判断
            if take == 0 and self._prod_pos >= len(self.source) and self._q.empty():
                self.playing = False

    # ---------------------------------------------------- 控制接口
    def set_semitones(self, n):
        with self._lock:
            if int(n) == self.semitones:
                return
            self.semitones = int(n)
            # 在当前所听位置刷新队列+重启管线,让新调快速生效
            self._reset_to(min(self._base + self._out, len(self.source)))

    def toggle(self):
        with self._lock:
            if self._base + self._out >= len(self.source):
                self._reset_to(0)
            self.playing = not self.playing

    def set_playing(self, playing):
        """绝对设置播放/暂停(供 IPC play/pause)。"""
        with self._lock:
            if playing and self._base + self._out >= len(self.source):
                self._reset_to(0)
            self.playing = bool(playing)

    def is_playing(self):
        return self.playing

    @staticmethod
    def _gain_for(pct):
        """档位%→增益:**感知(平方)曲线**。人耳近对数,线性映射时低档位仍显大(最小档~7%
        线性=-23dB还听得清);平方让低端更快衰减、控制更细(~7%→0.5%≈-46dB,50%→25%,
        100%→原样)。0=静音。"""
        p = max(0, min(100, int(pct))) / 100.0
        return p * p

    def set_volume(self, pct):
        """设输出音量档位(0-100)。经感知曲线得增益;float 赋值原子,无需锁;回调下一块即生效。"""
        self._vol_pct = max(0, min(100, int(pct)))
        self._gain = self._gain_for(self._vol_pct)

    @property
    def volume_pct(self):
        return self._vol_pct     # 报回档位(0-100),非增益——STATE/手机同步用的是档位

    def seek_ms(self, delta_ms):
        with self._lock:
            cur = self._base + self._out
            tgt = int(np.clip(cur + delta_ms * SR / 1000, 0, len(self.source)))
            self._reset_to(tgt)

    def seek_to_ms(self, ms):
        """绝对定位(供 IPC seek)。"""
        with self._lock:
            tgt = int(np.clip(ms * SR / 1000, 0, len(self.source)))
            self._reset_to(tgt)

    def load(self, source):
        """载入新歌:换源、归位到 0、清调、暂停。"""
        with self._lock:
            self.source = np.ascontiguousarray(source, np.float32)
            self.semitones = 0
            self.playing = False
            self._reset_to(0)

    def duration_ms(self):
        return len(self.source) / SR * 1000

    def swap_buffer(self, new_buf):
        with self._lock:
            cur = self._base + self._out
            self.source = np.ascontiguousarray(new_buf, np.float32)
            self._reset_to(min(cur, len(self.source)))

    def _reset_to(self, frame):
        """锁内调用:跳到 source 帧,清队列/pending,时钟归位。"""
        self._gen += 1
        self._base = frame
        self._out = 0
        self._prod_pos = frame
        self._pending = np.zeros((0, 2), np.float32)
        self._pending_gen = self._gen
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def current_ms(self):
        """所听位置(ms):已提交给声卡的帧数减去设备缓冲延迟,与耳朵对齐
        (缓冲加大到 ~120ms 后不补偿的话,歌词高亮会明显提前于人声)。"""
        with self._lock:
            return max(0, self._base + self._out - self._lat_frames) / SR * 1000

    def close(self):
        self._run = False
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass
