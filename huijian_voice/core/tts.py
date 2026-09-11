"""TTS 引擎（本地 Kokoro-82M multi-lang **v1.1 fp32 包 kokoro-multi-lang-v1_1**
——103 音色 web 全可选，默认 sid=18 zf_026 女声（声纹最似晓晓），用户知情拍板
2026-09-13；云可配回落本地）。

体验批 P0-4：句级 opus 缓存。控制类回复高度模板化（"好的，X打开了"），
按 (句, sid, speed) 缓存编码完成的裸 opus 帧——命中即首包归零、4C8G 卸载
省电档下不再为同一句话反复烧 Kokoro。云档不缓存（外部服务输出不稳定）；
模型换载/卸载即清空（缓存与当代模型同源）。

2026-09-13 声纹溯源与 v1.1 升级终案（证据链入 models.lock.json _comment）：
· CAM++ 说话人嵌入×两代包全 157 sid 逐一比对（脚本/结果留存 _dl voice_trace3*）：
  文章试听嗓=v1_0 sid47 晓晓(0.076~0.23)/sid52 云扬(0.17)——v1_1 **不含**这两嗓
  （最近 0.45/0.50=另一人），已明确告知用户，仍决定升级 v1.1 换 103 音色可选。
· 音色构成：0-2 英文具名(af_maple/af_sol/bf_vale)、3-57 女 zf_001…、58-102 男
  zm_009…；默认 sid18=zf_026（v1_1 女声最似晓晓）、sid81=zm_055（最似云扬），
  web 音色表已按声纹榜单标注。**升级后存量 sid45-52 配置指向 v1_1 新嗓**（重
  映射已知会，用户在 web 重选即可）。
· 选 fp32 弃 v1_1-int8：int8 同句实测 >8kHz 量化噪声能量 3.9×（用户试听否决
  int8 音质）+ RTF 更快（fp32 0.300 vs int8 0.720，x86 台架；两包声纹同源）。
· v1_1 合成 stderr 每句一条 "Unknown token: ❓"=本包 tokens.txt 句界标记，
  实证三型产出完整无害，非现网缺词根因（那已由 lang="" 修复，见下）。
· lang=""（语种自动路由）保留 v1.0.28 根修：lang="zh" 下 espeak-ng cmn 通道把
  英文片段静默丢弃；lang 矩阵（v1.0/v1.1 × 纯中/中英混/纯英）实证 lang=""
  三型全过、纯中逐比特同长。
· 加载器兼容 model.int8.onnx→回落 model.onnx：手动导入口放两代任意包皆可跑
  （回退 v1_0 零改码，旧包 sha 在 lock _comment 可查）。
定案链：v4.1 ②「本地默认+云可配自动回落」。
运行形态（sherpa-onnx 1.13.7 API 实测钉桩）：
  OfflineTts(OfflineTtsConfig(model=OfflineTtsModelConfig(kokoro=OfflineTtsKokoroModelConfig(
      model/voices/tokens/data_dir/dict_dir/lexicon/lang))))) → generate(text,sid,speed)
  → float32@24kHz → 重采样 16k → 裸 opus 60ms 帧（协议 §2：帧长 960 样本硬约束）。
句级流水线：逐句合成边产帧，首包目标 <1s；整流预算 55s（const.TTS_STREAM_BUDGET_S）。
"""
from __future__ import annotations

import asyncio
import logging
import os
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


_DEFAULT_SID = 18   # 本地定案默认音色 zf_026（云失败回落唯一用嗓，音色归属条款③）

# ── 自定义音色（对模型包布局无感）──────────────────────────────────
# 契约（与模型手动导入口同哲学）：投递目录 /data/tts_voices/*.bin，每文件
# **恰好一路音色**的纯 float32 风格向量流，字节数必须等于当前包的「单音尺寸」
# = 官方 voices.bin 字节数 ÷ 包音色数（models.lock.json voices_count 字段权威；
# 现役 v1_1 fp32 实测 53,790,720÷103=522,240B=510×256×4，无 magic 纯张量拼接，
# 与官方导出脚本一致）。命中即 sid = 官方音色数 + 文件名序（0 基），
# tts.sid 配置可直接填文件主名引用。
# 实现：加载时把官方 voices.bin 与合规自定义 bin 拼合成独立文件（tmp+replace
# 原子落盘、指纹复用），权重与前端一概不动。唯一未实证假设=sherpa 的
# num_speakers 只由 voices 文件推得——故加载后校验 num_speakers==官方+自定义，
# 不符即回退官方并大声报错（fail-soft：宁缺不崩）。
def merge_custom_voices(official, custom_dir, out, voices_count: int):
    """返回 (生效 voices 路径, {音色主名(小写): sid}, 跳过说明列表)。

    目录缺失/无 .bin/voices_count 未配置 → 原样返回官方文件，零副作用。"""
    from pathlib import Path
    official = Path(official)
    custom_dir = Path(custom_dir) if custom_dir else None
    out = Path(out)
    skipped: list[str] = []
    per_voice = 0
    if voices_count and voices_count > 0:
        per_voice = official.stat().st_size // voices_count
    if not custom_dir or not custom_dir.is_dir() or per_voice <= 0:
        return official, {}, skipped
    bins = sorted(p for p in custom_dir.glob("*.bin")
                  if p.is_file() and not p.name.startswith("."))
    usable: list[Path] = []
    for p in bins:
        size = p.stat().st_size
        if size != per_voice:
            skipped.append(f"{p.name}: {size}B ≠ 单音尺寸 {per_voice}B")
            continue
        usable.append(p)
    if not usable:
        return official, {}, skipped
    names = {p.stem.lower(): voices_count + i for i, p in enumerate(usable)}
    meta = out.with_suffix(".meta")
    fp = repr((official.stat().st_size, int(official.stat().st_mtime),
               [(p.name, p.stat().st_size, int(p.stat().st_mtime)) for p in usable]))
    if out.exists() and meta.exists() and meta.read_text(encoding="utf-8") == fp:
        return out, names, skipped   # 指纹未变：不重写盘
    try:
        tmp = out.with_suffix(".tmp")
        with tmp.open("wb") as w:
            for src in (official, *usable):
                with src.open("rb") as r:
                    while chunk := r.read(1 << 20):
                        w.write(chunk)
        os.replace(tmp, out)
        meta.write_text(fp, encoding="utf-8")
    except OSError as e:
        skipped.append(f"合并落盘失败：{e}")
        return official, {}, skipped
    return out, names, skipped


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
        self._cloud_voice_warned = False   # 云音色缺省告警只打一次
        self._custom_sids: dict[str, int] = {}   # 加载时注入的自定义音色名→sid

    def _voices_count(self, key: str) -> int:
        """官方音色数的异常安全读法：store 缺方法/返回垃圾一律 0（=注入关闭）。
        台架回归实锤：旧 fake store 无此方法曾直接把 ensure_loaded 打死。"""
        try:
            return int(self.store.voices_count_for(key) or 0)
        except Exception:
            return 0

    def voices_status(self) -> dict:
        """管理面板：官方音色数 / 当前注入的自定义区（名→sid）/ 投递目录预览。
        纯读，不触发加载。"""
        official_n = self._voices_count("tts_kokoro_multilang")
        preview = []
        per = 0
        try:
            d = self.store.model_dir_for("tts_kokoro_multilang")
        except Exception:
            d = None
        if d and official_n and (d / "voices.bin").is_file():
            per = (d / "voices.bin").stat().st_size // official_n
        if const.TTS_VOICES_DIR.is_dir():
            i = 0
            for p in sorted(const.TTS_VOICES_DIR.glob("*.bin")):
                if p.name.startswith("."):
                    continue
                ok = per > 0 and p.stat().st_size == per
                preview.append({"name": p.stem, "size": p.stat().st_size, "valid": ok,
                                "sid": official_n + i if ok else None})
                if ok:
                    i += 1
        return {"official_count": official_n, "per_voice_bytes": per,
                "dir": str(const.TTS_VOICES_DIR),
                "injected": dict(sorted(self._custom_sids.items(), key=lambda kv: kv[1])),
                "preview": preview}

    def resolve_sid(self) -> int:
        """tts.sid = 整数（官方 0..102 / 自定义 103+）**或自定义音色主名**。
        非法/越界一律回落默认 18 并 WARN——配置写坏绝不让播报哑掉。"""
        raw = self.settings.get("tts.sid", 18)
        sid = None
        try:
            sid = int(str(raw).strip())
        except (TypeError, ValueError):
            sid = self._custom_sids.get(str(raw).strip().lower())
            if sid is None:
                logger.warning("[TTS] tts.sid=%r 既非数字也不是已注入的自定义音色，"
                               "回落默认 18（可先「重新扫描」或检查投递目录）", raw)
        n = int(getattr(self._tts, "num_speakers", 0) or 0)
        if sid is None or sid < 0 or (n and sid >= n):
            if sid is not None:
                logger.warning("[TTS] tts.sid=%s 越界（本机共 %d 音色），回落 18", raw, n)
            sid = 18
        return sid

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
                # 两代命名都认：int8 包主模型叫 model.int8.onnx、fp32 包叫
                # model.onnx（现役定案=v1.1 fp32；导入口放哪种都收，审查换包免改码）。
                main = d / "model.int8.onnx"
                if not main.exists():
                    main = d / "model.onnx"
                k.model = str(main)
                # 自定义音色注入（投递口 const.TTS_VOICES_DIR）
                self._custom_sids, voices_path = {}, str(d / "voices.bin")
                try:
                    _off_n = self._voices_count(key)
                    merged, names, skipped = merge_custom_voices(
                        d / "voices.bin", const.TTS_VOICES_DIR,
                        d / "voices_custom_merged.bin", _off_n)
                    if names:
                        voices_path, self._custom_sids = str(merged), names
                        logger.info("[TTS] 自定义音色 %d 路已注入（sid≥%d）：%s",
                                    len(names), _off_n,
                                    "、".join(sorted(names)))
                    for why in skipped:
                        logger.warning("[TTS] 自定义音色被跳过：%s", why)
                except Exception as e:      # 注入失败绝不拖垮引擎
                    logger.warning("[TTS] 自定义音色合并失败（用官方表）：%s", e)
                k.voices = voices_path
                k.tokens = str(d / "tokens.txt")
                lex = [str(d / f) for f in ("lexicon-zh.txt", "lexicon-us-en.txt") if (d / f).exists()]
                k.lexicon = ",".join(lex)
                if (d / "espeak-ng-data").is_dir():
                    k.data_dir = str(d / "espeak-ng-data")
                if (d / "dict").is_dir():
                    k.dict_dir = str(d / "dict")
                k.lang = ""   # 语种自动：kokoro v1.1 multi-lang 前端自带中英路由。
                # 定案依据（2026-09-13 台架 lang 矩阵）：lang="zh" 时 espeak-ng 走 cmn
                # 通道，英文片段全部 "Failed to set eSpeak-ng voice" 静默丢弃——
                # 即现场遗留"英文实体名片段致句中缺词/整句静音"根因；lang="" 在
                # 纯中/中英混/纯英三型均完整产出（v1.0/v1.1 两代包同验，纯中零差异）。
                model_cfg = so.OfflineTtsModelConfig()
                model_cfg.kokoro = k
                model_cfg.num_threads = 2
                model_cfg.provider = "cpu"
                cfg = so.OfflineTtsConfig(model=model_cfg)
                fsts = [f for f in ("number-zh.fst", "date-zh.fst", "phone-zh.fst") if (d / f).exists()]
                if fsts:
                    cfg.rule_fsts = ",".join(str(d / f) for f in fsts)
                tts = so.OfflineTts(cfg)
                _vc = self._voices_count(key)
                if self._custom_sids and tts.num_speakers != _vc + len(self._custom_sids):
                    # 唯一未实证假设破功（某版 sherpa 不以 voices 文件推音色数）：
                    # 官方 sid 区完全不受影响（合并在尾部），只禁自定义区被选中。
                    logger.error("[TTS] 自定义音色未生效：num_speakers=%d 期望=%d——"
                                 "本 sherpa 版不支持追加音色区，已仅放行官方表",
                                 tts.num_speakers, _vc + len(self._custom_sids))
                    self._custom_sids = {}
                # fail-loud：kokoro multi-lang 对含英文/拼音字母的词走 espeak-ng
                # 音素通道，缺数据目录时 sherpa 只在 stderr 报
                # "Failed to set eSpeak-ng voice"、Python 侧全句静默失败
                # （2026-09-09 播报静音事故）。此处显式点名，不再让人猜。
                if not (d / "espeak-ng-data").is_dir():
                    logger.error("[TTS] 模型目录缺 espeak-ng-data/：含英文字母的句子"
                                 "将合成失败，请重导 kokoro-multi-lang 完整包")
                self._tts = tts
                self._cache.clear(); self._cache_bytes = 0   # 换代模型：旧音频作废
                self.last_used = time.time()
                logger.warning("[TTS] Kokoro multi-lang 已加载（%d 音色），sid=%s",
                               tts.num_speakers, self.settings.get("tts.sid", 18))
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
    async def stream_opus(self, text: str, engine_out: dict | None = None) -> AsyncIterator[bytes]:
        """逐句产裸 opus 帧（16k/mono/60ms）。云档失败回落本地（v4.1-②）。

        engine_out：可选出参 dict——本轮实际由哪个引擎发声写回
        engine_out["engine"]（"cloud:voice名" / "local:sid N"）。云⇄本地
        回落是**换嗓**的（云端可配男声、本地默认 sid18 女声），现场"第一句
        男声第二句女声"必须由日志一眼可辨，不再靠猜。
        """
        self.last_used = time.time()
        prov = str(self.settings.get("tts.provider", "local_kokoro"))
        fell_back = False
        if prov.startswith("cloud"):
            if engine_out is not None:
                engine_out["engine"] = "cloud:%s" % str(
                    (self.settings.get("tts.cloud") or {}).get("voice") or "alloy")
            try:
                async for pkt in self._cloud_stream(text):
                    yield pkt
                return
            except Exception as e:
                fell_back = True
                logger.warning("[TTS] 云合成失败(%s) → 回落本地默认音色（注意：音色会变化）", e)
                if engine_out is not None:
                    engine_out["engine"] = "local:fallback"
        loop = asyncio.get_running_loop()
        # 音色归属定案（用户 2026-09-18）：本地档=web 设定音色（数字或自定义主名）；
        # 云档=云端对应音色（可选）；**云失败回落=固定默认本地音色 sid18**——回落是
        # 应急通道，取最保守单音色，不跟 web 配置（可能是自定义名/云专属号）漂移。
        sid = _DEFAULT_SID if fell_back else self.resolve_sid()
        speed = float(self.settings.get("tts.speed", 1.0))
        if engine_out is not None:
            fb = engine_out.get("engine") == "local:fallback"
            engine_out["engine"] = f"local:sid{sid}" + ("(云回落)" if fb else "")
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
            if samples.size == 0:
                # espeak 音素失败等引擎内错误只打 stderr、不抛异常，全句空音频
                # 若无此告警则整链静默（播报静音最难查的一段）
                logger.warning("[TTS] 合成产出空音频，原文: %r", sent[:40])
                return b""
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
        sid = self.resolve_sid()
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
        if not str(cloud.get("voice") or "").strip() and not self._cloud_voice_warned:
            self._cloud_voice_warned = True
            logger.warning("[TTS] 云音色未配置，将以默认 voice=alloy 合成（英文男声念中文，"
                           "且与本地女声定案不同嗓）；请在控制台「音色名」填平台中文音色")
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
