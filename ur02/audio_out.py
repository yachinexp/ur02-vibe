# -*- coding: utf-8 -*-
"""音频输出: 解码后的 8kHz PCM 送 VB-CABLE 播放端点 (移植自已验证的 C# CableSink.cs)
- 自动找名字含 CABLE / VB-Audio 的输出设备
- 以设备默认采样率打开流, 8k -> 设备率线性插值上采样 (不赌 WASAPI 自动重采样)
"""
import array
import collections
import math
import threading

import numpy as np
import sounddevice as sd

from .consts import AUDIO_SAMPLE_RATE


def list_cable_outputs():
    """返回 [(index, name), ...] 名字含 CABLE/VB-Audio 的输出设备
    排序: 'CABLE Input'(官方播放端点名) 优先, WASAPI hostapi 优先"""
    out = []
    try:
        devs = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return out
    for i, d in enumerate(devs):
        if d.get("max_output_channels", 0) > 0:
            name = d.get("name", "")
            if "CABLE" in name.upper() or "VB-AUDIO" in name.upper():
                out.append((i, name, d.get("hostapi", 0)))
    def _rank(t):
        idx, name, hapi = t
        is_input = 0 if "input" in name.lower() else 1
        api_name = hostapis[hapi]["name"].lower() if hapi < len(hostapis) else ""
        is_wasapi = 0 if "wasapi" in api_name else 1
        return (is_input, is_wasapi, idx)
    out.sort(key=_rank)
    return [(i, n) for i, n, _ in out]


class CableSink:
    """2026-09-06 重写: 改成【回调式 + 环形缓冲】输出。

    旧实现是 BLE 通知线程里直接 stream.write(), 蓝牙包到达是突发的(成批到达后长时间
    静默), 声卡缓冲区周期性饿死 -> 断口/爆音 -> 实测 30ms 周期包络调制、高频能量暴涨
    ("变尖/夹嗓子")。回调式由 PortAudio 按设备时钟恒定取数据, 蓝牙抖动被环形缓冲吸收,
    音质不再受包到达节律影响。
    """
    PREBUFFER = 0.25          # 启动预缓冲秒数(吸收抖动)
    FADE = 240                # 断流/恢复时的渐入渐出样本数(~5ms, 彻底消爆音)
    TARGET_BUF = 0.30         # 目标缓冲水位(秒): PLL 自动把缓冲稳定在这个量
    # 2026-09-06: PLL 只负责【抖动/时钟漂移】, 供需缺口交给 PLC 补点(见 ui._plc_fill)。
    # 之前用 ±15% 去补 30% 的缺口, 结果基频被拉低 9%(声音发闷)、缺口还是补不满。
    # 实测遥控器是【真 8kHz 采样 + 丢样本】(内容基频不随组间隔变), 所以播放速率必须
    # 锁在标称 8k。v1.24 留 ±6% 会在 PLC 补多时被迫提速泄洪(实测顶到 +1.00, 音高 +6%),
    # 这里收窄到 ±3%: 补多的量交给 ui 侧的补点积分反馈(FILL_KP/FILL_KI)去收敛, 音高不动。
    PLL_K = 0.03              # 最大速率修正幅度 ±3%

    def __init__(self, on_status=None):
        self.on_status = on_status or (lambda s: None)
        self._stream = None
        self._lock = threading.Lock()
        self._dev_rate = 48000
        self.active = False
        self.device_name = ""
        # 环形缓冲 (存设备采样率下的 int16 块)
        self._buf = collections.deque()
        self._buf_samples = 0
        self._cur = None
        self._cur_pos = 0
        self._started = False            # 预缓冲是否已攒够
        self._pll_err = 0.0              # 当前缓冲偏差(-1..1), 诊断用
        self._last_out = 0               # 最后一个输出样本(断流渐出用)
        self._resume_fade = 0            # 恢复后需要渐入的块数
        # ---- 2026-09-06 写入诊断计数 ----
        self.written_samples = 0      # 送进缓冲的样本数(设备采样率下)
        self.write_calls = 0          # write_pcm 调用次数
        self.write_errors = 0         # 入缓冲/重采样抛异常次数
        self.skipped_calls = 0        # 流未打开导致的丢弃
        self.underruns = 0            # 缓冲被取空次数(真·断口)
        self.max_buf_samples = 0      # 缓冲峰值(观察抖动幅度)
        self.last_error = ""
        self.diag_capture = None      # 非 None 时, 记录【真正写进声卡的 48k 数据】

    # ---------- PortAudio 回调: 恒定速率取数据 ----------
    def _pull(self, n):
        """从缓冲取 n 个样本, 不够则返回 None(表示断流)"""
        got = []
        need = n
        while need > 0:
            if self._cur is not None and self._cur_pos < len(self._cur):
                take = min(need, len(self._cur) - self._cur_pos)
                got.append(self._cur[self._cur_pos:self._cur_pos + take])
                self._last_out = int(self._cur[self._cur_pos + take - 1])
                self._cur_pos += take
                need -= take
            elif self._buf:
                self._cur = self._buf.popleft()
                self._buf_samples -= len(self._cur)
                self._cur_pos = 0
            else:
                return None
        return np.concatenate(got) if len(got) > 1 else got[0]

    def buffer_error(self):
        """缓冲相对目标水位的偏差 (-1..1), >0 = 积压。
        给上层做【补点量反馈】用: 补多了缓冲就涨, 上层据此少补, 音高不受影响。"""
        target = max(1, int(self._dev_rate * self.TARGET_BUF))
        return max(-1.0, min(1.0, (self._buf_samples - target) / float(target)))

    def _callback(self, outdata, frames, time_info, status):
        out = outdata[:, 0]
        # ① 启动预缓冲: 攒够 PREBUFFER 才开始出声, 避免开流瞬间就断
        if not self._started:
            if self._buf_samples < int(self._dev_rate * self.PREBUFFER):
                out[:] = 0
                return
            self._started = True

        # 2026-09-06: _pull 会改 _buf/_buf_samples, 必须和 write_pcm 互斥,
        # 否则 PortAudio 线程与 BLE 线程并发改计数 -> 缓冲峰值算错(实测算出比净输入还大)
        with self._lock:
            blk = self._pull(frames)
        if blk is not None:
            if self._resume_fade > 0:                 # 断流恢复 -> 渐入, 消爆音
                k = min(self.FADE, frames)
                if k > 0:
                    blk = blk.astype(np.int32)
                    blk[:k] = (blk[:k] * np.linspace(0.0, 1.0, k)).astype(np.int32)
                self._resume_fade -= 1
            out[:] = blk
            return

        # ② 断流: 用最后的电平做 ~2ms 渐出到 0, 而不是硬切(硬切=哒的一声)
        self.underruns += 1
        self._started = False            # 重新攒缓冲
        self._resume_fade = 2
        k = min(self.FADE, frames)
        if k > 0:
            out[:k] = (self._last_out * np.linspace(1.0, 0.0, k)).astype(np.int16)
        if frames > k:
            out[k:] = 0

    def open(self, device_index=None):
        """打开 CABLE 输出。device_index=None 自动匹配。"""
        with self._lock:
            self.close()
            devs = list_cable_outputs()
            if not devs:
                self.on_status("audio:未安装 VB-CABLE(请先装虚拟声卡驱动)")
                return False
            if device_index is None:
                device_index, name = devs[0]
            else:
                name = sd.query_devices(device_index)["name"]
            try:
                self._dev_rate = int(sd.query_devices(device_index)["default_samplerate"])
                self._buf.clear()
                self._buf_samples = 0
                self._cur = None
                self._cur_pos = 0
                self._started = False
                self._last_out = 0
                self._resume_fade = 0
                self._stream = sd.OutputStream(
                    device=device_index, samplerate=self._dev_rate,
                    channels=1, dtype="int16",
                    blocksize=int(self._dev_rate * 0.02),   # 20ms 块, 回调式
                    latency=0.25,                           # 250ms 缓冲, 吸收蓝牙抖动
                    callback=self._callback,
                )
                self._stream.start()
                self.device_name = name
                self.active = True
                self.on_status("audio:输出 -> %s @%dHz" % (name, self._dev_rate))
                return True
            except Exception as e:
                self.on_status("audio:打开失败 %s" % e)
                self._stream = None
                self.active = False
                return False

    def close(self):
        self._buf.clear()
        self._buf_samples = 0
        self._cur = None
        self._cur_pos = 0
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        self._stream = None
        self.active = False

    def write_pcm(self, samples, src_rate=None):
        """写入 16bit 样本 list, 内部重采样到设备采样率。

        src_rate 默认 AUDIO_SAMPLE_RATE(8000, 2026-09-05 实测修正);
        重采样用 scipy 抗混叠(resample_poly), 无 scipy 时降级线性插值。
        !! 2026-09-05: 8k->48k 线性插值(6x)有严重频谱镜像, 听感"尖/沙",
        必须抗混叠滤波, 否则底噪问题无法根除。
        """
        self.write_calls += 1
        if not (self.active and self._stream):
            self.skipped_calls += 1
            return
        # 2026-09-06 PLL: 按缓冲水位微调播放速率, 自动锁定蓝牙的真实送达节奏。
        # 不用"测帧间隔"那套(会被突发包带偏, 实测把 32ms 测成 43.8ms)。
        target = max(1, int(self._dev_rate * self.TARGET_BUF))
        err = max(-1.0, min(1.0, (self._buf_samples - target) / float(target)))
        src = (src_rate or AUDIO_SAMPLE_RATE) * (1.0 + self.PLL_K * err)
        self._pll_err = err
        try:
            data = _resample_quality(samples, src, self._dev_rate)
            if self.diag_capture is not None:      # 记录真正送进声卡的数据
                self.diag_capture.append(np.asarray(data, dtype=np.int16).copy())
            with self._lock:
                self._buf.append(data)
                self._buf_samples += len(data)
                if self._buf_samples > self.max_buf_samples:
                    self.max_buf_samples = self._buf_samples
            self.written_samples += len(data)
        except Exception as e:      # 静默吞掉 = 整帧丢失 -> 必须计数
            self.write_errors += 1
            self.last_error = "%s: %s" % (type(e).__name__, e)

    def write_pcm8k(self, samples):
        """兼容旧调用: 按 8kHz 写入"""
        self.write_pcm(samples, 8000)


# ============================================================
#  2026-09-06 丢包补偿 (PLC, Packet Loss Concealment)
#  背景: 实测遥控器是【真 8kHz 实时采样 + 射频来不及发而丢样本】。
#   三份不同组间隔(36.3/43.6/52.2/52.9ms)的录音反推内容基频都落在 110~124Hz,
#   不随组间隔变化 -> 排除"遥控器整体放慢采样"。
#   所以每组之间【真的丢了 (组间隔*8000 - 244) 个样本】, 43.6ms 时 = 105 个 = 13ms。
#   这 13ms 必须补出来, 否则缓冲被抽干 -> 断流 -> 周期性"哒哒"爆破音。
# ============================================================
def plc_fill(hist, n, sr=8000):
    """补出 n 个被丢掉的样本, 返回 float64 ndarray (长度 n)。

    hist: 最近已送出的样本(8k, int 或 float), 越长越好, 至少 240。
    浊音段: 循环播放最近一个基频周期 —— 周期首尾天然连续, 听感是"托住的尾音"。
            比直线斜坡好太多(13ms 直线斜坡每秒重复 23 次 = 低频嗡哒声)。
    非浊音段(自相关 <0.30): 等能量噪声, 避免把沙沙声补成周期性嗡嗡。
    静音段(RMS < 8 LSB): 直接补 0, 不凭空造音。
    """
    if n <= 0:
        return np.zeros(0)
    h = np.asarray(hist, dtype=np.float64)
    if len(h) < 240:
        return np.zeros(n)
    tail = h[-320:]                                  # 最近 40ms
    tail = tail - tail.mean()
    eng = float(np.sqrt((tail ** 2).mean()))
    if eng < 8.0:
        return np.zeros(n)
    lo = int(sr / 300)                               # 300Hz
    hi = min(int(sr / 60), len(tail) - 65)           # 60Hz
    if hi <= lo:
        return np.zeros(n)
    lags = np.arange(lo, hi)
    segs = np.stack([tail[-(64 + L):-L] for L in lags])     # (nlags, 64)
    tmpl = tail[-64:]
    num = segs @ tmpl
    den = np.sqrt((segs * segs).sum(1) * float(tmpl @ tmpl) + 1e-12)
    c = num / den
    k = int(np.argmax(c))
    best_c, best_l = float(c[k]), int(lags[k])
    if best_c < 0.30:
        return np.random.randn(n) * eng
    cyc = tail[-best_l:]
    out = cyc[np.arange(n) % best_l]
    # 长缺口才衰减(短缺口衰减反而在接缝处造成幅度台阶):
    # 12 样本 -> 1.8%, 80 样本 -> 12%, 236 样本 -> 20%(封顶)
    d = min(0.20, 0.0015 * n)
    return out * (1.0 - d * np.arange(n) / float(n))


try:
    import numpy as _np
    from scipy.signal import resample_poly as _resample_poly
    from math import gcd as _gcd
    _HAVE_SCIPY = True
except Exception:                                   # pragma: no cover
    _HAVE_SCIPY = False


def _resample_quality(samples, src_rate, dst_rate):
    """高质量抗混叠重采样 (scipy resample_poly), 失败/无 scipy 降级线性。"""
    if src_rate == dst_rate:
        return array.array("h", samples)
    if _HAVE_SCIPY:
        try:
            arr = _np.asarray(samples, dtype=_np.float32) / 32768.0
            g = _gcd(int(src_rate), int(dst_rate))
            up, down = dst_rate // g, src_rate // g
            out = _resample_poly(arr, up, down)
            _np.clip(out, -1.0, 1.0, out=out)
            return (out * 32767.0).astype(_np.int16)
        except Exception:
            pass
    return _resample_linear(samples, src_rate, dst_rate)


def _resample_linear(samples, src_rate, dst_rate):
    """线性插值 8k -> dst_rate。纯 Python 逐样本, 8kHz 数据量小, 性能足够。"""
    if src_rate == dst_rate:
        return array.array("h", samples)
    ratio = src_rate / dst_rate
    n_out = int(len(samples) / ratio)
    if n_out <= 0:
        return array.array("h")
    out = array.array("h", bytes(2 * n_out))
    for j in range(n_out):
        pos = j * ratio
        i = int(pos)
        frac = pos - i
        a = samples[i]
        b = samples[i + 1] if i + 1 < len(samples) else a
        out[j] = int(a + (b - a) * frac)
    return out


# ============================================================
#  2026-09-05 新增: Biquad 滤波器 (电脑端去底噪/低频隆隆)
#  - 实测静音底噪 -33 dBFS, 94% 能量在 100Hz 以下 = 低频隆隆,
#    不是高频沙沙 → 加 80Hz 高通能砍掉一大半
#  - 80Hz 选 Butterworth Q=0.707 二阶 (RBJ Audio EQ Cookbook)
#  - Direct Form I, 状态连续, 跨调用保持 (biquad 长期在 ui 里)
# ============================================================
class Biquad:
    """二阶 IIR (biquad) 直接 I 型。b0/b1/b2/a1/a2 已归一化 (a0=1)。"""
    __slots__ = ("b0", "b1", "b2", "a1", "a2", "x1", "x2", "y1", "y2")

    def __init__(self, b0, b1, b2, a1, a2):
        self.b0, self.b1, self.b2 = b0, b1, b2
        self.a1, self.a2 = a1, a2
        self.x1 = self.x2 = 0.0
        self.y1 = self.y2 = 0.0

    def reset(self):
        self.x1 = self.x2 = 0.0
        self.y1 = self.y2 = 0.0

    def process(self, samples):
        """逐样本高通, 返回新 list (浮点 -> 仍是 16bit 范围, 调用方最后再 int/clamp)"""
        b0, b1, b2, a1, a2 = self.b0, self.b1, self.b2, self.a1, self.a2
        x1, x2, y1, y2 = self.x1, self.x2, self.y1, self.y2
        out = [0] * len(samples)
        for i, s in enumerate(samples):
            v = b0 * s + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1 = x1, s
            y2, y1 = y1, v
            out[i] = v
        self.x1, self.x2, self.y1, self.y2 = x1, x2, y1, y2
        return out


def highpass_80hz(fs=8000, Q=0.707):
    """80Hz 二阶 Butterworth 高通 biquad 系数 (RBJ Audio EQ Cookbook)。

    系数 (a0 归一化后):
      b0=b2=(1+cos w)/(2*(1+a)), b1=-(1+cos w)/(1+a), a1=-2cos w/(1+a), a2=(1-a)/(1+a)
      a = sin w / (2Q),   w = 2π·fc/fs
    """
    w = 2.0 * math.pi * 80.0 / fs
    c = math.cos(w)
    s = math.sin(w)
    a = s / (2.0 * Q)
    a0 = 1.0 + a
    b0 = (1.0 + c) / 2.0 / a0
    b1 = -(1.0 + c) / a0
    b2 = (1.0 + c) / 2.0 / a0
    a1 = -2.0 * c / a0
    a2 = (1.0 - a) / a0
    return Biquad(b0, b1, b2, a1, a2)
