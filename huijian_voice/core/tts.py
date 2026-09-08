"""TTS 引擎（默认本地 Kokoro-82M multi-lang，sid=45 小北；云可配回落本地）。

体验批 P0-4：句级 opus 缓存。控制类回复高度模板化（"好的，X打开了"），
按 (句, sid, speed) 缓存编码完成的裸 opus 帧——命中即首包归零、4C8G 卸载
省电档下不再为同一句话反复烧 Kokoro。云档不缓存（外部服务输出不稳定）；
模型换载/卸载即清空（缓存与当代模型同源）。

定案链：v4.1 ②「本地默认+云可配自动回落」；音色 sid45 中文女声（用户授权选定）。
运行形态（sherpa-onnx 1.13.7 API 实测钉桩）：
  OfflineTts(OfflineTtsConfig(model=OfflineTtsModelConfig(kokoro=OfflineTtsKokoroModelConfig(
      model/voices/tokens/data_dir/dict_dir/lexicon/lang))))) → generate(text,sid,speed)
  → float32@24kHz → 重采样 16k → 裸 opus 60ms 帧（协议 §2：帧长 960 样本硬约束）。
句级流水线：逐句合成边产帧，首包目标 <1s；整流预算 55s（const.TTS_STREAM_BUDGET_S）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from typing import AsyncIterator, Optional

import numpy as np

from . import audio, const

logger = logging.getLogger("huijian.tts")


def split_sentences(text: str) -> list[str]:
    parts, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "。！？；!?;\n":
            if buf.strip():
                parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    if not parts and text.strip():
        parts = [text.strip()]
    # 长句按逗号二次切，压首包延迟
    out = []
    for p in parts:
        if len(p) > 40 and ("，" in p or "," in p):
            seg = ""
            for ch in p:
                seg += ch
                if ch in "，,":
                    out.append(seg)
                    seg = ""
            if seg:
                out.append(seg)
        else:
            out.append(p)
    return out


_CACHE_MAX_ITEMS = 256          # 播报句集收敛得快，256 句封顶
_CACHE_MAX_BYTES = 4 << 20      # 4MB 硬闸（opus 32kbps×10s≈40KB/句，量级宽裕）


class TtsEngine:
    def __init__(self, settings, model_store):
        self.settings = settings
        self.store = model_store
        self._tts = None
        self._lock = threading.Lock()
        self._busy = 0          # 在飞合成数（卸载避让；审查 F1）
        self.last_used = time.time()
        self.encoder_rate = const.SAMPLE_RATE
        # P0-4 (text, sid, speed) → (packets, bytes) LRU；仅本地档，asyncio 单线程
        # 内读写（编码在 executor，但回主循环后才入表），无需锁。
        self._cache: "OrderedDict[tuple, tuple]" = OrderedDict()
        self._cache_bytes = 0
        self.cache_hits = 0     # 观测计数（状态页/排障）

    # ── 生命周期 ────────────────────────────────────────────────
    def ready(self) -> bool:
        return self._tts is not None

    def ensure_loaded(self) -> bool:
        with self._lock:
            if self._tts is not None:
                return True
            key = "tts_kokoro_multilang"
            d = self.store.model_dir_for(key) or (self.store.ensure(key) and self.store.model_dir_for(key))
            if not d:
                logger.error("[TTS] kokoro 模型未就绪")
                return False
            try:
                import sherpa_onnx as so
                k = so.OfflineTtsKokoroModelConfig()
                k.model = str(d / "model.onnx")
                k.voices = str(d / "voices.bin")
                k.tokens = str(d / "tokens.txt")
                lex = [str(d / f) for f in ("lexicon-zh.txt", "lexicon-us-en.txt") if (d / f).exists()]
                k.lexicon = ",".join(lex)
                if (d / "espeak-ng-data").is_dir():
                    k.data_dir = str(d / "espeak-ng-data")
                if (d / "dict").is_dir():
                    k.dict_dir = str(d / "dict")
                k.lang = "zh"
                model_cfg = so.OfflineTtsModelConfig()
                model_cfg.kokoro = k
                model_cfg.num_threads = 2
                model_cfg.provider = "cpu"
                cfg = so.OfflineTtsConfig(model=model_cfg)
                fsts = [f for f in ("number-zh.fst", "date-zh.fst", "phone-zh.fst") if (d / f).exists()]
                if fsts:
                    cfg.rule_fsts = ",".join(str(d / f) for f in fsts)
                tts = so.OfflineTts(cfg)
                self._tts = tts
                self._cache.clear(); self._cache_bytes = 0   # 换代模型：旧音频作废
                self.last_used = time.time()
                logger.warning("[TTS] Kokoro multi-lang 已加载（%d 音色），sid=%s",
                               tts.num_speakers, self.settings.get("tts.sid", 45))
                return True
            except Exception as e:
                logger.error("[TTS] 加载失败: %s", e)
                self._tts = None
                return False

    def unload(self) -> bool:
        with self._lock:
            if self._busy:
                logger.info("[TTS] 合成进行中，本轮跳过卸载")
                return False
            self._tts = None
            # 缓存刻意不清：省电档卸载后，高频模板句仍可秒回旧帧（同代模型
            # 重载输出逐比特一致；换代路径 ensure_loaded 已负责清空）。
            logger.warning("[TTS] 模型已卸载（省电档）")
            return True

    # ── 合成 ────────────────────────────────────────────────────
    async def stream_opus(self, text: str) -> AsyncIterator[bytes]:
        """逐句产裸 opus 帧（16k/mono/60ms）。云档失败回落本地（v4.1-②）。"""
        self.last_used = time.time()
        prov = str(self.settings.get("tts.provider", "local_kokoro"))
        if prov.startswith("cloud"):
            try:
                async for pkt in self._cloud_stream(text):
                    yield pkt
                return
            except Exception as e:
                logger.warning("[TTS] 云合成失败(%s) → 回落本地", e)
        loop = asyncio.get_running_loop()
        sid = int(self.settings.get("tts.sid", 45))
        speed = float(self.settings.get("tts.speed", 1.0))
        load_checked = self.ready()
        for sent in split_sentences(text):
            key = (sent, sid, speed)
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                self.cache_hits += 1
                self.last_used = time.time()
                for pkt in hit[0]:
                    yield pkt
                continue
            # 首错 miss 才拉模型：全命中回合在省电档卸载态也能完整播出
            if not load_checked:
                if not await loop.run_in_executor(None, self.ensure_loaded):
                    return
                load_checked = True
            pcm16 = await loop.run_in_executor(None, self._synth, sent, sid, speed)
            if not pcm16:
                continue
            # F5：整句 opus 编码是 CPU 活，出事件循环
            packets = await loop.run_in_executor(None, self._encode, pcm16)
            if packets:
                self._cache_put(key, packets)
            for pkt in packets:
                yield pkt
        self.last_used = time.time()

    def _cache_put(self, key: tuple, packets: list) -> None:
        if not self.settings.get("tts.cache_enabled", True):
            return
        size = sum(len(p) for p in packets)
        if size > _CACHE_MAX_BYTES:
            return
        old = self._cache.pop(key, None)
        if old:
            self._cache_bytes -= old[1]
        self._cache[key] = (packets, size)
        self._cache_bytes += size
        while len(self._cache) > _CACHE_MAX_ITEMS or self._cache_bytes > _CACHE_MAX_BYTES:
            _k, (_pkt, b) = self._cache.popitem(last=False)
            self._cache_bytes -= b

    @staticmethod
    def _encode(pcm16: bytes) -> list:
        return list(audio.OpusPcmEncoder("voip").encode_stream(pcm16))

    def _synth(self, sent: str, sid: int, speed: float) -> bytes:
        """线程内同步合成 → s16le@16k。（F1：锁内快照当代对象+busy，防跨代析构）"""
        with self._lock:
            tts = self._tts
            if tts is None:
                return b""
            self._busy += 1
        try:
            audio_obj = tts.generate(sent, sid=sid, speed=speed)
            samples = np.asarray(audio_obj.samples, dtype=np.float32)
            rate = int(audio_obj.sample_rate)
            pcm = audio.f32_to_pcm16(samples)
            return audio.resample_pcm16(pcm, rate, const.SAMPLE_RATE)
        except Exception as e:
            logger.warning("[TTS] 合成异常(%r): %s", sent[:20], e)
            return b""
        finally:
            with self._lock:
                self._busy -= 1

    async def synthesize_pcm(self, text: str) -> bytes:
        """整段 16k s16（管理台试听 wav 用）。"""
        loop = asyncio.get_running_loop()
        if not self.ready() and not await loop.run_in_executor(None, self.ensure_loaded):
            return b""
        sid = int(self.settings.get("tts.sid", 45))
        speed = float(self.settings.get("tts.speed", 1.0))
        out = b""
        for sent in split_sentences(text):
            out += await loop.run_in_executor(None, self._synth, sent, sid, speed)
        return out

    # ── 云档（OpenAI 兼容 /audio/speech）────────────────────────
    async def _cloud_stream(self, text: str) -> AsyncIterator[bytes]:
        import aiohttp
        cloud = self.settings.get("tts.cloud") or {}
        base = str(cloud.get("base_url", "")).rstrip("/")
        if not base:
            raise RuntimeError("云 TTS 未配置 base_url")
        headers = {"Content-Type": "application/json"}
        if key := str(cloud.get("api_key") or ""):
            headers["Authorization"] = f"Bearer {key}"
        body = {"model": str(cloud.get("model") or "tts-1"),
                "voice": str(cloud.get("voice") or "alloy"),
                "input": text,
                "response_format": str(cloud.get("response_format") or "pcm"),
                "speed": float(self.settings.get("tts.speed", 1.0))}
        # 平台预设透传：如硅基流动 pcm 默认 44.1kHz，须显式指定才与预期一致
        if sr_req := cloud.get("sample_rate"):
            body["sample_rate"] = int(sr_req)
        timeout = aiohttp.ClientTimeout(total=30, connect=8)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(f"{base}/audio/speech", json=body, headers=headers) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}: {(await r.text())[:160]}")
                raw = await r.read()
        # 部分 OpenAI 兼容平台无视 response_format 直接回 wav，甚至 mp3——
        # RIFF 嗅探自动拆封取真实采样率；不可解码格式报明确指令改配置
        pcm, src_rate = self._unwrap_audio(raw, int(cloud.get("sample_rate") or 24000))
        # F5：整段重采样+编码为秒级 CPU 活，出事件循环
        loop = asyncio.get_running_loop()
        for pkt in await loop.run_in_executor(None, self._resample_encode, pcm, src_rate):
            yield pkt

    @staticmethod
    def _unwrap_audio(raw: bytes, default_rate: int) -> tuple:
        """返回 (s16le mono pcm, rate)。RIFF/WAVE 拆封；mp3/ogg 显式报错。"""
        import struct
        if len(raw) >= 44 and raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
            pos, sr, bits, data = 12, 0, 0, None
            while pos + 8 <= len(raw):
                cid = raw[pos:pos + 4]
                csz = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
                body = raw[pos + 8:pos + 8 + csz]
                if cid == b"fmt " and len(body) >= 16:
                    sr = struct.unpack("<I", body[4:8])[0]
                    bits = struct.unpack("<H", body[14:16])[0]
                elif cid == b"data":
                    data = body
                    break
                pos += 8 + csz + (csz & 1)      # 块按偶数字节对齐
            if data is None or not sr:
                raise RuntimeError("云 TTS wav 头损坏（缺 fmt/data 块）")
            if bits != 16:
                raise RuntimeError(f"云 TTS wav 位深 {bits} 不支持（仅 16-bit）")
            return data, sr
        if raw[:3] == b"ID3" or (raw[:1] == b"\xff" and len(raw) > 1 and raw[1] & 0xE0 == 0xE0):
            raise RuntimeError("云 TTS 返回 mp3：请在该平台改输出格式为 pcm 或 wav")
        if raw[:4] == b"OggS":
            raise RuntimeError("云 TTS 返回 ogg/opus：请在该平台改输出格式为 pcm 或 wav")
        return raw, default_rate

    @staticmethod
    def _resample_encode(raw: bytes, src_rate: int) -> list:
        pcm16k = audio.resample_pcm16(raw, src_rate, const.SAMPLE_RATE)
        return list(audio.OpusPcmEncoder("voip").encode_stream(pcm16k))
