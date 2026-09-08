"""STT 引擎（v4.1 定案：默认本地 Paraformer，云可配、失败自动回落本地）。

2026-09-08 实证裁定（晚到研究定案，两条都推翻过早期假设，代码以此为准）：
  1. `sherpa-onnx-paraformer-zh-int8-2025-10-07` 包实为 **WSChuan 四川话**模型
     （tar 内 README 实证）→ 剔除；主模型 = **bilingual 流式 int8**（中英，标准普通话，
     现网小智服务器同款），整句喂入 OnlineRecognizer。
  2. API 实测：from_paraformer(tokens, encoder, decoder, sample_rate=16000,
     **feature_dim=80**)——feature_dim=560 会**静默返回空串**（无异常，最恶坑）；
     int8 实测 RTF 0.042 / peak RSS ~400MB（x86，aarch64 预估 2-4× 仍宽裕）。
云档 = OpenAI 兼容 /audio/transcriptions（whisper 形态），任何异常回落本地（v4.1-②）。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from . import audio, const

logger = logging.getLogger("huijian.asr")

_CHUNK = 1600   # 100ms 分块喂入（实测与整段喂入结果一致，分块省峰值内存）


class AsrEngine:
    def __init__(self, settings, model_store):
        self.settings = settings
        self.store = model_store
        self._rec = None
        self._lock = threading.Lock()
        self._busy = 0          # 在飞推理数（卸载避让；审查 F1）
        self.last_used = time.time()
        self.model_key = "asr_paraformer_bilingual"

    # ── 模型生命周期 ────────────────────────────────────────────
    def ready(self) -> bool:
        return self._rec is not None

    def ensure_loaded(self) -> bool:
        """同步加载（调用方放 to_thread）。已加载直接 True。"""
        with self._lock:
            if self._rec is not None:
                return True
            d = self.store.model_dir_for(self.model_key)
            if d is None:
                self.store.ensure(self.model_key)
                d = self.store.model_dir_for(self.model_key)
            if d is None:
                logger.error("[STT] 模型未就绪(%s)，无法本地识别", self.model_key)
                return False
            try:
                import sherpa_onnx
                self._rec = sherpa_onnx.OnlineRecognizer.from_paraformer(
                    tokens=str(d / "tokens.txt"),
                    encoder=str(d / "encoder.int8.onnx"),
                    decoder=str(d / "decoder.int8.onnx"),
                    num_threads=2,
                    sample_rate=const.SAMPLE_RATE,
                    feature_dim=80,                  # 硬约束：560 会静默空串
                    enable_endpoint_detection=False,  # 断句由客户端 listen stop 驱动
                    decoding_method="greedy_search",
                    provider="cpu",
                )
                self.last_used = time.time()
                logger.warning("[STT] Paraformer 双语流式已加载 @ %s", d)
                return True
            except Exception as e:
                logger.error("[STT] 模型加载失败: %s", e)
                self._rec = None
                return False

    def unload(self) -> bool:
        """True=已卸载；False=推理在飞本轮跳过（调用方下一轮再试）。"""
        with self._lock:
            if self._busy:
                logger.info("[STT] 推理进行中，本轮跳过卸载")
                return False
            self._rec = None
            logger.warning("[STT] 模型已卸载（省电档）")
            return True

    # ── 识别 ────────────────────────────────────────────────────
    async def transcribe_pcm(self, pcm_s16: bytes) -> str:
        """整句 s16le@16k → 文本。云档优先（若配置），失败回落本地。"""
        self.last_used = time.time()
        prov = str(self.settings.get("stt.provider", "local_paraformer"))
        if prov.startswith("cloud"):
            cloud = self.settings.get("stt.cloud") or {}
            try:
                return await self._cloud_transcribe(pcm_s16, cloud)
            except Exception as e:
                logger.warning("[STT] 云识别失败(%s) → 回落本地（v4.1-②）", e)
        if not self.ready():
            loop = asyncio.get_running_loop()
            ok = await loop.run_in_executor(None, self.ensure_loaded)
            if not ok:
                return ""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._local_transcribe, pcm_s16)

    def _local_transcribe(self, pcm_s16: bytes) -> str:
        # F1 双保险：锁内快照当代 rec + busy 计数。此后即便 reaper/reload 把
        # self._rec 置 None，本地快照仍持引用（旧对象析构推迟到本调用返回），
        # 绝不出现「旧 stream 喂新 recognizer」的跨代 UB。
        if not pcm_s16:
            return ""
        with self._lock:
            rec = self._rec
            if rec is None:
                return ""
            self._busy += 1
        try:
            samples = audio.pcm16_to_f32(pcm_s16)
            stream = rec.create_stream()
            for i in range(0, len(samples), _CHUNK):
                stream.accept_waveform(const.SAMPLE_RATE, samples[i:i + _CHUNK])
            # 尾部补 1s 静音（sherpa-onnx 流式 demo 同款 tail padding）：流式
            # Paraformer encoder 以 600ms 整块消费且带 look-ahead，input_finished
            # 时不足整块的语音尾巴直接被丢——台架仿真 2026-09-08 实测
            # 「打开办公室射灯」→「打开办公室射」、「打开射灯」→「打开」。
            # 补静音让末块凑整 + look-ahead 喂足，再收流。
            stream.accept_waveform(const.SAMPLE_RATE, [0.0] * const.SAMPLE_RATE)
            stream.input_finished()
            while rec.is_ready(stream):
                rec.decode_stream(stream)
            res = rec.get_result_all(stream)
            text = (getattr(res, "text", None) if res is not None else None) or ""
            try:
                rec.reset(stream)
            except Exception:
                pass
            self.last_used = time.time()
            return text.replace("▁", " ").replace("　", "").strip()
        except Exception:
            logger.exception("[STT] 本地识别异常")
            return ""
        finally:
            with self._lock:
                self._busy -= 1

    async def _cloud_transcribe(self, pcm_s16: bytes, cloud: dict) -> str:
        import aiohttp
        base = str(cloud.get("base_url", "")).rstrip("/")
        if not base:
            raise RuntimeError("云 STT 未配置 base_url")
        wav = audio.pcm_to_wav(pcm_s16)
        form = aiohttp.FormData()
        form.add_field("file", wav, filename="utterance.wav", content_type="audio/wav")
        form.add_field("model", str(cloud.get("model") or "whisper-1"))
        if lang := str(cloud.get("language") or self.settings.get("stt.language") or ""):
            form.add_field("language", "zh" if lang.startswith("zh") else lang)
        headers = {}
        if key := str(cloud.get("api_key") or ""):
            headers["Authorization"] = f"Bearer {key}"
        timeout = aiohttp.ClientTimeout(total=float(cloud.get("timeout", 12)))
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(f"{base}/audio/transcriptions", data=form, headers=headers) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}: {(await r.text())[:160]}")
                obj = await r.json(content_type=None)
                return str(obj.get("text", "")).strip()
