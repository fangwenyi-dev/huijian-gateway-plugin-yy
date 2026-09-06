"""音频层：裸 opus 帧编解码、PCM 变换、WAV 读写、重采样。

协议钉死约束（《小智协议子集-服务器契约.md》§2/§3）：
- 上行（集成→加载项 STT 通道）：每 WS 二进制帧 = 1 个裸 opus packet，16kHz/mono/60ms；
- 下行（加载项→集成 TTS 通道）：同为裸 opus packet，16k/mono/60ms/960 样本
  （集成侧 tts.py:78 硬编码 Decoder(16000,1)、:82 decode(resp, 960)，帧长不符直接丢帧）。
Kokoro(multi-lang) 输出 24kHz，需重采样 24k→16k（v4 §2-G）。
"""
from __future__ import annotations

import io
import logging
import math
import struct
import wave
from typing import Iterator, Optional

import numpy as np

from . import const

logger = logging.getLogger("huijian.audio")


class OpusError(RuntimeError):
    pass


def _load_opus():
    """opuslib-next（纯 ctypes → 系统 libopus）。加载失败由调用方降级
    （TTS 不可用/STT 收不到文本）。⚠ 模块名实证为 `opuslib_next`（1.3.1），
    早期笔记「import opus」有误——Windows 实测 ImportError。"""
    try:
        import opuslib_next
        return opuslib_next
    except Exception as e:  # pragma: no cover - 环境性分支
        logger.error("[音频] libopus 绑定不可用: %s", e)
        return None


class OpusPcmDecoder:
    """16k/mono 解码器：一帧(≤120ms)一次 decode。"""

    def __init__(self, frame_samples: int = const.FRAME_SAMPLES):
        opus = _load_opus()
        if opus is None:
            raise OpusError("opus 绑定缺失")
        self._d = opus.Decoder(const.SAMPLE_RATE, const.CHANNELS)
        self._frame_samples = frame_samples

    def decode(self, packet: bytes) -> bytes:
        """返回 s16le mono bytes（60ms=960 样本=1920B，末帧可短）。异常帧静音丢。
        ⚠ decode 的 frame_size 单位=每声道样本数（非字节）——传 1920 会让输出
        补零翻倍（真库首跑前潜伏的移植错误，API 源码实证修正）。"""
        try:
            return self._d.decode(packet, self._frame_samples)
        except Exception as e:
            logger.debug("[音频] opus 帧解码失败(%dB): %s", len(packet), e)
            return b""


class OpusPcmEncoder:
    """16k/mono/60ms 编码器。输入 s16le bytes 流，产出 opus packet 迭代。"""

    def __init__(self, application: Optional[str] = "voip"):
        opus = _load_opus()
        if opus is None:
            raise OpusError("opus 绑定缺失")
        # opuslib_next 接受 'voip'/'audio' 字符串（APPLICATION_TYPES_MAP 映射）
        self._e = opus.Encoder(const.SAMPLE_RATE, const.CHANNELS,
                               application or "voip")
        # 稳定音质优先于码率：60ms 帧、32kbps；complexity/bitrate 均为实测存在的
        # property（classes.py），signal 无此属性 → 逐项守卫保留
        for attr, val in (("bitrate", 32000), ("complexity", 8)):
            if val is None:
                continue
            try:
                setattr(self._e, attr, val)
            except Exception:
                logger.debug("[音频] encoder.%s 设置不可用，用默认值", attr)

    def encode_stream(self, pcm_s16: bytes) -> Iterator[bytes]:
        if len(pcm_s16) % 2:
            pcm_s16 += b"\x00"
        step = const.FRAME_BYTES
        for i in range(0, len(pcm_s16), step):
            chunk = pcm_s16[i:i + step]
            if len(chunk) < step:
                chunk = chunk + b"\x00" * (step - len(chunk))  # 尾帧零填充（集成侧 audio.py:152-154 同法）
            try:
                yield self._e.encode(chunk, frame_size=const.FRAME_SAMPLES)
            except Exception as e:  # 单帧失败=静音跳过，不中断整流
                logger.debug("[音频] opus 编码失败: %s", e)
                continue


# ── PCM 工具 ─────────────────────────────────────────────────────
def pcm16_to_f32(pcm: bytes) -> np.ndarray:
    return (np.frombuffer(pcm, dtype=np.int16).astype(np.float32)) / 32768.0


def f32_to_pcm16(samples: np.ndarray) -> bytes:
    x = np.clip(samples, -1.0, 1.0)
    return (x * 32767.0).astype(np.int16).tobytes()


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """多相窗口 sinc 重采样（int16 单声道）。
    24k→16k（kokoro）与任意云采样率→16k 统一走这里；线性插值会留混叠，
    语音可懂度影响小但音色明显——用 Hamming 窗 sinc，半带 24 taps（4 相位）。
    """
    if src_rate == dst_rate or not pcm:
        return pcm
    x = pcm16_to_f32(pcm)
    ratio = dst_rate / src_rate
    n_out = int(math.ceil(len(x) * ratio))
    half = 12
    taps_per_phase = 4
    # 简化实现：对每个输出点做源域窗口插值（向量化版按整数步长批处理）
    out = np.zeros(n_out, dtype=np.float32)
    gain = min(1.0, ratio)
    idx = np.arange(n_out, dtype=np.float64) / ratio
    base = np.floor(idx).astype(np.int64)
    frac = idx - base
    for m in range(-half + 1, half + 1):
        pos = base + m
        valid = (pos >= 0) & (pos < len(x))
        t = (frac - m) * gain
        # windowed sinc: sinc(t)*hamming
        with np.errstate(divide="ignore", invalid="ignore"):
            st = np.pi * t
            sinc = np.where(np.abs(t) < 1e-6, 1.0, np.sin(np.where(valid, st, 0.0)) / np.where(valid & (np.abs(st) > 1e-6), st, 1.0))
        w = sinc * (0.54 + 0.46 * np.cos(np.pi * np.clip(t / (half * 2), -1, 1) * 2)) * gain
        out += np.where(valid, x[np.clip(pos, 0, len(x) - 1)], 0.0) * w
    del taps_per_phase
    return f32_to_pcm16(out)


def pcm_to_wav(pcm: bytes, rate: int = const.SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(const.CHANNELS)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def read_wav_pcm16(data: bytes) -> tuple[bytes, int]:
    """WAV → (s16le mono pcm, rate)。非 WAV 入参按原始 PCM 返回（宽容）。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            rate = w.getframerate()
            n = w.getnframes()
            sw = w.getsampwidth()
            ch = w.getnchannels()
            raw = w.readframes(n)
        if sw != 2:
            raw = (np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483647.0 * 32767).astype(np.int16).tobytes()
        if ch == 2:
            samples = np.frombuffer(raw, dtype=np.int16).reshape(-1, 2).mean(axis=1).astype(np.int16)
            raw = samples.tobytes()
        return raw, rate
    except Exception:
        return data, const.SAMPLE_RATE
