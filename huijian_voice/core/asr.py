"""STT 引擎（v4.2 定案：默认本地 SenseVoice-Small，Paraformer 降兼容回落档；云可配、失败自动回落本地）。

2026-09-08 实证裁定（晚到研究定案，两条都推翻过早期假设，代码以此为准）：
  1. `sherpa-onnx-paraformer-zh-int8-2025-10-07` 包实为 **WSChuan 四川话**模型
     （tar 内 README 实证）→ 剔除；主模型曾定 = **bilingual 流式 int8**（中英，标准普通话）。
  2. API 实测：from_paraformer(tokens, encoder, decoder, sample_rate=16000,
     **feature_dim=80**)——feature_dim=560 会**静默返回空串**（无异常，最恶坑）；
     int8 实测 RTF 0.042 / peak RSS ~400MB（x86，aarch64 预估 2-4× 仍宽裕）。

2026-09-13 A/B 台架裁定（_bench/bench_result.txt，用户批准换引擎）：
  - 默认本地引擎 = **SenseVoice-Small int8**（OfflineRecognizer，中英粤日韩）：
    真实录音不再丢尾（paraformer 实测「下午五」缺「点」）、粤语整句正确、中英
    code-switch/8k 电话带宽显著强；命令音频推理 56-94ms（paraformer 125-175ms）；
    单引擎 peak RSS 370MB（≤ 旧档 413MB）。必须用 **model.int8.onnx**（同包
    fp32 model.onnx 937MB→RSS 1.6GB+，4G 主机不可用，代码不引用）。
  - SenseVoice 输出带 <|zh|><|NEUTRAL|>… 标签 → 必须剥离（_STRIP_TAGS）。
  - 生产链路是 stop 后整句识别（session 契约「仅回一条 stt」），流式模型的增量
    优势本就未使用，换离线引擎零协议损失。
  - Paraformer **保留为兼容/回落档**：stt.local_model=paraformer 显式回退，或
    主档缺失/加载失败自动降级，语音链不断（fail-open）。
云档 = OpenAI 兼容 /audio/transcriptions（whisper 形态），任何异常回落本地（v4.1-②）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time

from . import audio, const

logger = logging.getLogger("huijian.asr")

_CHUNK = 1600   # 100ms 分块喂入（实测与整段喂入结果一致，分块省峰值内存）

# 引擎键位（models.lock.json 的 key）
KEY_SV = "asr_sensevoice_small"
KEY_PF = "asr_paraformer_bilingual"
_KIND_KEY = {"sensevoice": KEY_SV, "paraformer": KEY_PF}

# SenseVoice 输出的语言/情感/事件标签：zh/yue/EN/NEUTRAL/Speech/woitn 等
_STRIP_TAGS = re.compile(r"<\|[^<>|]*\|>")


class AsrEngine:
    def __init__(self, settings, model_store):
        self.settings = settings
        self.store = model_store
        self._rec = None
        self._lock = threading.Lock()
        self._busy = 0          # 在飞推理数（卸载避让；审查 F1）
        self._loading = False   # 冷启动双载闩（下载/构建期并发诉求直接 False，走礼貌话术）
        self.last_used = time.time()

    # ── 引擎选择 ────────────────────────────────────────────────
    def _primary_kind(self) -> str:
        k = str(self.settings.get("stt.local_model", "sensevoice") or "sensevoice")
        return k if k in _KIND_KEY else "sensevoice"

    @property
    def model_key(self) -> str:
        """主档 key（_loop_models 按此下载/预热；回落在载不改变主档诉求）。"""
        return _KIND_KEY[self._primary_kind()]

    def loaded_kind(self) -> str:
        with self._lock:
            return getattr(self._rec, "_hj_kind", "") if self._rec is not None else ""

    def stale_kind(self) -> bool:
        """在载引擎 ≠ 配置主档（切档后，或首载走了回落档）→ 主档就绪后换绑。"""
        return self.ready() and self.loaded_kind() != self._primary_kind()

    # ── 模型生命周期 ────────────────────────────────────────────
    def ready(self) -> bool:
        return self._rec is not None

    def _build_recognizer(self, kind: str, d):
        import sherpa_onnx
        if kind == "sensevoice":
            rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(d / "model.int8.onnx"),
                tokens=str(d / "tokens.txt"),
                num_threads=2,
                sample_rate=const.SAMPLE_RATE,
                language="",                      # 自动语种判定（含粤语）
                use_itn=False,                    # 与旧档对齐：不挂 ITN（paraformer 无 rule_fsts）
                provider="cpu",
            )
        else:
            rec = sherpa_onnx.OnlineRecognizer.from_paraformer(
                tokens=str(d / "tokens.txt"),
                encoder=str(d / "encoder.int8.onnx"),
                decoder=str(d / "decoder.int8.onnx"),
                num_threads=2,
                sample_rate=const.SAMPLE_RATE,
                feature_dim=80,                   # 硬约束：560 会静默空串
                enable_endpoint_detection=False,  # 断句由客户端 listen stop 驱动
                decoding_method="greedy_search",
                provider="cpu",
            )
        rec._hj_kind = kind    # 路径随 recognizer 走：快照语义 + 无标记替身默认 pf 路径
        return rec

    def _load_one(self, kind: str) -> bool:
        """单引擎加载：目录缺失先同步 ensure（升级过渡/首启兜底），失败 False。"""
        key = _KIND_KEY[kind]
        d = self.store.model_dir_for(key)
        if d is None:
            self.store.ensure(key)
            d = self.store.model_dir_for(key)
        if d is None:
            logger.warning("[STT] 模型未就绪(%s)", key)
            return False
        try:
            rec = self._build_recognizer(kind, d)
        except Exception as e:
            logger.error("[STT] 模型加载失败(%s): %s", kind, e)
            return False
        with self._lock:
            self._rec = rec
        self.last_used = time.time()
        logger.warning("[STT] %s 已加载 @ %s",
                       "SenseVoice-Small" if kind == "sensevoice" else "Paraformer 双语流式", d)
        return True

    def ensure_loaded(self) -> bool:
        """同步加载（调用方放 to_thread）。已加载直接 True。
        主档优先；主档缺失/损坏自动回落 Paraformer 兼容档（fail-open）。"""
        with self._lock:
            if self._rec is not None:
                return True
            if self._loading:
                return False
            self._loading = True
        try:
            primary = self._primary_kind()
            if self._load_one(primary):
                return True
            if primary == "sensevoice" and self._load_one("paraformer"):
                return True
            return False
        finally:
            with self._lock:
                self._loading = False

    def rebind_primary(self) -> bool:
        """回落档在载/切档后原地换绑主档（推理在飞跳过，_loop_models 下一轮再试）。"""
        kind = self._primary_kind()
        if self.store.model_dir_for(_KIND_KEY[kind]) is None:
            return False
        with self._lock:
            if self._busy or self._loading:
                return False
            if self._rec is not None and getattr(self._rec, "_hj_kind", "") == kind:
                return False
            self._loading = True
        try:
            ok = self._load_one(kind)
        finally:
            with self._lock:
                self._loading = False
        if ok:
            logger.warning("[STT] 已换绑主档 %s", kind)
        return ok

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
        # 绝不出现「旧 stream 喂新 recognizer」的跨代 UB。分派按 rec 自带
        # _hj_kind；无标记替身走 Paraformer 老路径（并发守卫测试兼容）。
        if not pcm_s16:
            return ""
        with self._lock:
            rec = self._rec
            if rec is None:
                return ""
            self._busy += 1
        try:
            samples = audio.pcm16_to_f32(pcm_s16)
            if getattr(rec, "_hj_kind", "paraformer") == "sensevoice":
                stream = rec.create_stream()
                stream.accept_waveform(const.SAMPLE_RATE, samples)
                rec.decode_stream(stream)
                text = _STRIP_TAGS.sub("", stream.result.text or "")
            else:
                stream = rec.create_stream()
                for i in range(0, len(samples), _CHUNK):
                    stream.accept_waveform(const.SAMPLE_RATE, samples[i:i + _CHUNK])
                # 尾部补 1s 静音（sherpa-onnx 流式 demo 同款 tail padding）：流式
                # Paraformer encoder 以 600ms 整块消费且带 look-ahead，input_finished
                # 时不足整块的语音尾巴直接被丢——台架仿真 2026-09-08 实测
                # 「打开办公室射灯」→「打开办公室射」、「打开射灯」→「打开」。
                # 补静音让末块凑整 + look-ahead 喂足，再收流。
                # （SenseVoice 为离线模型无此坑：台架整句喂入不补静音亦无丢尾。）
                stream.accept_waveform(const.SAMPLE_RATE, [0.0] * const.SAMPLE_RATE)
                stream.input_finished()
                while rec.is_ready(stream):
                    rec.decode_stream(stream)
                res = rec.get_result_all(stream)
                text = (getattr(res, "text", None) if res is not None else None) or ""
                text = text.replace("▁", " ")
            try:
                rec.reset(stream)
            except Exception:
                pass
            self.last_used = time.time()
            return text.replace("　", "").strip()
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
