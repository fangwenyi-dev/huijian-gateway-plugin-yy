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

云档流式（v1.0.52，仅本文件）：`_cloud_stream` 曾 `raw = await r.read()` 整包
读完再拆封/重采样/编码——首包=整段网络时长（平台慢即整段"哑着等"）。现改为
`iter_chunked` 增量读：RIFF 头按块增量解析到 data 块，载荷即刻进有状态重采样
（跨块保留滤波历史，块边不丢样），攒满 60ms 即出一个 opus 包。输出仍为
16k/mono/s16le/60ms 裸 opus，与 `stream_opus` 本地路径逐比特同规格。
超时同时从单一 `total=30` 拆为 connect/首字节/块间读/total 四段（可配，见
settings DEFAULTS tts.cloud.*_timeout_s）：停摆的云端点几秒内失败 → 云→本地
回落照旧即时触发。首字节与首帧两跳延迟全程打日志（[TTS] 云首字节/首帧、
模型未就绪本轮等待、本地首帧）。

2026-09-21 审查修复批（本文件）：①**语速入音色指纹**（HA 消息哈希盘缓存无
TTL，speed 不换键=模板句永远旧语速；云档另补 model/response_format/端点
host；api_key 永不入指纹），三处 speed 读取统一 `_speed()` 安全值，坏配置
不再逐句炸链；②**generate 并行互斥**（_gen_lock：试听与播报并发时
sherpa-onnx 前端共享状态互踩/崩溃，F1 只防跨代析构不防同代并发）；③云响应
**短于嗅探窗不得判成裸 PCM**（旧形态 4B "RIFF" 残响应产 1 帧垃圾还"云成功
解钉"；流式 _decide 与整包 _unwrap_audio 同闸）。
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from collections import OrderedDict
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

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

# ── v1.0.55 云失败钉扎（现场 2026-09-12「还是有两个 tts 音色」主修）──────
# 旧形态：每一轮都先试云、失败再本地——云持续不可用时 = **逐句换嗓**（男⇄女
# 随机交替）+ **逐句白等云超时**（first_byte 6s）双惩罚。钉扎 = 一次云失败后
# CLOUD_PIN_S 秒内所有轮次直接本地（引擎列打「云钉扎」可辨），到期自动放一行
# 试云：网络恢复不需要重启，云若还死也只多等一次超时。
_CLOUD_PIN_S = 300.0

# ── 云档读超时（v1.0.52：整包 total=30 硬超时 → 分段可配）────────────
# 现场形态：云端点"连上了但不出声"（平台排队/合成卡死/半开连接）时，旧实现
# 要等满 30s total 才抛 → 云→本地回落白等 30s，用户听到的是长静音后才出声。
# 拆四段后：连接慢 → connect；服务器不给首字节 → first_byte（几秒）；流中途
# 静默 → read（sock_read，块间）；整段总闸 total 放宽到 > TTS_STREAM_BUDGET_S
# （55s）——**慢而持续产出**的合成不得被误砍，只有"停摆"才快速失败。
_CLOUD_CONNECT_TIMEOUT_S = 8.0      # TCP/TLS 建连
_CLOUD_FIRST_BYTE_TIMEOUT_S = 6.0   # 响应头 + 首个数据块（停摆端 6s 内失败→快速回落本地）
_CLOUD_READ_TIMEOUT_S = 10.0        # 块间静默（aiohttp sock_read）
_CLOUD_TOTAL_TIMEOUT_S = 120.0      # 整流总闸（> const.TTS_STREAM_BUDGET_S）
_CLOUD_CHUNK_BYTES = 8192           # iter_chunked 块大小
_RIFF_MAX_HDR_BYTES = 8 << 20       # RIFF 头部缓冲硬闸（防 csz 撒谎导致无界缓冲）

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
                  if p.is_file() and not p.name.startswith(".")
                  and not p.name.startswith("voices_custom_merged"))   # 合并产物误落投递口时不得自吞
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


# ── 云档流式解码原语（v1.0.52）────────────────────────────────────

def _unsupported_format(data: bytes) -> Optional[str]:
    """mp3/ogg 等不可解码容器的显式指令文案（None=裸 PCM/可解码）。

    与 `_unwrap_audio` 同一判据同一文案：现场靠这句话知道"去平台改输出格式"，
    措辞不得改（整包路径与流式路径共用本函数）。"""
    if data[:3] == b"ID3" or (data[:1] == b"\xff" and len(data) > 1 and data[1] & 0xE0 == 0xE0):
        return "云 TTS 返回 mp3：请在该平台改输出格式为 pcm 或 wav"
    if data[:4] == b"OggS":
        return "云 TTS 返回 ogg/opus：请在该平台改输出格式为 pcm 或 wav"
    return None


async def _next_body_chunk(agen):
    """取下一块响应体；流尽返回 None。

    不直接在 `asyncio.wait_for(agen.__anext__(), ...)` 上等：StopAsyncIteration
    穿过 Task 边界的语义在不同 Python 版本上易踩坑，这里收口成 None。"""
    try:
        return await agen.__anext__()
    except StopAsyncIteration:
        return None


class _StreamResampler:
    """有状态重采样：任意源率 s16le mono → 目标率 s16le（云档跨块流式用）。

    `audio.resample_pcm16` 是**整包**语义：对每个输出点做 [-11,+12] 抽头的
    Hamming 窗 sinc，越界抽头按零贡献。流式每 8KB 调一次整包实现 = 每个块
    边界都从"信号起点"重新起算 → 块缝处丢样/断相 → 可闻咔哒。

    这里逐输出点复用**同一公式、同一全局下标空间**：块内只算"抽头已全部到齐"
    的输出点（base+12 ≤ 已到样本数-1），尾部样本留作下一块的滤波历史；flush()
    按同式补齐尾段（越界抽头照旧零贡献）。因此拼接结果与一次性整包重采样在
    容差内一致（同 tap 顺序同累加序，实测逐点等同）。
    """

    _HALF = 12          # 与 audio.resample_pcm16 的半带抽头数一致

    def __init__(self, src_rate: int, dst_rate: int):
        self.src_rate = int(src_rate) if int(src_rate or 0) > 0 else const.SAMPLE_RATE
        self.dst_rate = int(dst_rate) if int(dst_rate or 0) > 0 else const.SAMPLE_RATE
        self.passthrough = self.src_rate == self.dst_rate
        self.ratio = self.dst_rate / self.src_rate
        self.gain = min(1.0, self.ratio)
        self._buf = np.zeros(0, dtype=np.float32)   # 未消费输入（含滤波历史）
        self._buf_start = 0     # self._buf[0] 对应的全局样本下标
        self._n_in = 0          # 已喂入的全局样本数
        self._n_out = 0         # 已产出的全局输出点数
        self._carry = b""       # 块边界上的半个样本（s16le 按 2 字节对齐）

    # ── 输入 ────────────────────────────────────────────────────
    def feed(self, chunk: bytes) -> bytes:
        """喂一块源域 s16le，返回本次可确定的**已重采样** 16k s16le（可空）。"""
        if not chunk:
            return b""
        if self.passthrough:
            # 源率=目标率：**逐字节直通**，绝不做 float 往返——整包实现
            # (audio.resample_pcm16) 在同率时是 `return pcm` 原样返回，
            # 本路径必须逐比特对齐（±1LSB 都不许有）。
            self._n_in += len(chunk) // 2
            self._n_out = self._n_in
            return chunk
        if self._carry:
            chunk = self._carry + chunk
            self._carry = b""
        if len(chunk) % 2:                 # 半个样本留到下一块（网络块边界任意）
            self._carry = chunk[-1:]
            chunk = chunk[:-1]
        if not chunk:
            return b""
        x = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        self._buf = np.concatenate((self._buf, x)) if self._buf.size else x
        self._n_in += x.size
        return self._emit(ready_only=True)

    def flush(self) -> bytes:
        """响应体结束：补齐尾段（越界抽头零贡献，与整包实现同式）。"""
        if self.passthrough:
            return b""
        if self._carry:
            # 尾半个样本：低字节按原样保留、高字节补零（与 encode_stream 尾字节
            # 补零同法），不得整样本丢弃。
            x = np.frombuffer(self._carry[:1] + b"\x00", dtype=np.int16).astype(np.float32) / 32768.0
            self._carry = b""
            self._buf = np.concatenate((self._buf, x)) if self._buf.size else x
            self._n_in += 1
        return self._emit(ready_only=False)

    # ── 内部 ────────────────────────────────────────────────────
    def _emit(self, ready_only: bool) -> bytes:
        total_out = int(math.ceil(self._n_in * self.ratio))
        if ready_only:
            # 抽头全在已到样本内的输出点（floor(i/ratio)+HALF ≤ n_in-1）
            t_max = self._n_in - 1 - self._HALF
            if t_max < 0:
                return b""
            i_hint = int(np.floor((t_max + 1) * self.ratio)) + 1
            i_hint = min(i_hint, total_out)
            if i_hint <= self._n_out:
                return b""
            i = np.arange(self._n_out, i_hint, dtype=np.float64)
            base = np.floor(i / self.ratio).astype(np.int64)
            keep = int(np.count_nonzero(base + self._HALF <= t_max))
            if keep <= 0:
                return b""
            i, base = i[:keep], base[:keep]
        else:
            if total_out <= self._n_out:
                return b""
            i = np.arange(self._n_out, total_out, dtype=np.float64)
            base = np.floor(i / self.ratio).astype(np.int64)
        out = self._polyphase(i, base)
        self._n_out += i.size
        self._trim()
        return audio.f32_to_pcm16(out)

    def _polyphase(self, i: np.ndarray, base: np.ndarray) -> np.ndarray:
        """与 audio.resample_pcm16 逐式同构（同 tap 序、同累加序）。"""
        frac = i / self.ratio - base
        out = np.zeros(i.shape[0], dtype=np.float32)
        buf_len = self._buf.shape[0]
        for m in range(-self._HALF + 1, self._HALF + 1):
            pos = base + m
            valid = (pos >= 0) & (pos < self._n_in)
            t = (frac - m) * self.gain
            with np.errstate(divide="ignore", invalid="ignore"):
                st = np.pi * t
                sinc = np.where(
                    np.abs(t) < 1e-6, 1.0,
                    np.sin(np.where(valid, st, 0.0))
                    / np.where(valid & (np.abs(st) > 1e-6), st, 1.0))
            w = sinc * (0.54 + 0.46 * np.cos(
                np.pi * np.clip(t / (self._HALF * 2), -1, 1) * 2)) * self.gain
            if buf_len:
                vals = self._buf[np.clip(pos - self._buf_start, 0, buf_len - 1)]
            else:
                vals = np.zeros(i.shape[0], dtype=np.float32)
            out += np.where(valid, vals, 0.0) * w
        return out

    def _trim(self) -> None:
        """丢弃后续输出点再也用不到的输入（含滤波历史）。"""
        keep_from = max(0, int(np.floor(self._n_out / self.ratio)) - self._HALF + 1)
        drop = keep_from - self._buf_start
        if drop > 0:
            self._buf = self._buf[drop:]
            self._buf_start = keep_from


class _CloudOpusStream:
    """云响应体 → 16k/mono/60ms 裸 opus 帧的**增量**解码器。

    状态机：SNIFF（判定裸 PCM/RIFF/mp3-ogg 拒收）→ RIFF_HDR（增量解析块头直到
    data 块）→ PAYLOAD（有状态重采样 + 攒满 const.FRAME_BYTES 即编码出包）。

    与整包路径（`_unwrap_audio` + `_resample_encode`）同判据同输出规格：
    · 头部字节永不进音频流水（RIFF 44B/扩展头逐块解析到 data 块为止）；
    · 一个 opus 编码器实例贯穿整段（逐帧喂满帧 → 与一次性 encode_stream 逐比特一致）；
    · 尾帧零填充到 60ms（与 encode_stream 尾帧同法）；
    · data 块之后的字节按声明长度截断（v1.0.55 定案⑦：LIST/pad 元数据绝不
      编成噪声帧）。边界注记（v1.0.56 审计 A-doc）：声明 0 或 0xFFFFFFFF=
      未知长 → 读到流尽；奇数 csz 由 carry 补半样对齐。整包路径对这两种
      坏声明是"0 帧/ValueError 炸"，与之**不**逐一对齐——流式一侧是更安全的
      那一边，且整包分支仅在 `iter_chunked` 缺失时到达，真 aiohttp 永不走。
    """

    _SNIFF_BYTES = 12       # RIFF/WAVE 判定所需最短前缀

    def __init__(self, default_rate: int):
        self._default_rate = int(default_rate or const.SAMPLE_RATE)
        self._state = "sniff"
        self._head = bytearray()        # SNIFF 缓冲
        self._rbuf = bytearray()        # RIFF 未解析头部字节
        self._rate = 0
        self._bits = 0
        self._res = None
        self._enc = None
        self._pcm = bytearray()         # 待满帧的 16k s16le
        # v1.0.55（定案⑦）：data 块声明长度的剩余字节。None=不限（裸 PCM，
        # 或 wav 声明 0/0xFFFFFFFF 未知长——读到流尽，与整包路径同判据）；
        # 有值=data 之后的尾块字节一律不进音频流水。
        self._data_left: int | None = None

    # ── 输入 ────────────────────────────────────────────────────
    def feed(self, chunk: bytes) -> list:
        """喂一块响应体，返回本次可产出的完整 opus 帧（可能为空）。"""
        if self._state == "sniff":
            self._head += chunk
            if len(self._head) < self._SNIFF_BYTES:
                return []               # 前缀不足：等下一块（首块通常远大于 12B）
            data = bytes(self._head)
            self._head = bytearray()
            self._decide(data)          # RIFF→头 12B 已入 _rbuf；裸 PCM→已起流
            chunk = data if self._state == "payload" else b""
        if self._state == "riff_hdr":
            return self._parse_riff(chunk)   # chunk 可为空（前缀已由 _decide 存下）
        if self._state == "payload":
            return self._payload(chunk) if chunk else []
        return []

    def flush(self) -> list:
        """响应体结束：补完尾段 + 尾帧（零填充），并做头部完整性收口。"""
        out = []
        if self._state == "sniff":
            data = bytes(self._head)
            self._head = bytearray()
            if data:
                self._decide(data)          # 短响应：仍按同判据处理
                if self._state == "riff_hdr":
                    out += self._parse_riff(b"")
                elif self._state == "payload":
                    out += self._payload(data)
        if self._state == "riff_hdr":
            # 头都没解析到 data 块就收流了（截断/撒谎 csz）→ 与整包路径同文案
            raise RuntimeError("云 TTS wav 头损坏（缺 fmt/data 块）")
        if self._state == "payload":
            # ⚠ 这里只能喂**已重采样**的尾段给攒帧器；若误走 _payload（会再
            # 过一次 _res.feed）尾段会被二次重采样后整段吞掉（尾帧短 32B 的前科）。
            out += self._frame_pcm(self._res.flush())
            tail = bytes(self._pcm)
            self._pcm = bytearray()
            if tail:
                out.extend(self._enc.encode_stream(tail))   # 尾帧零填充（encode_stream 同法）
        return out

    # ── 内部 ────────────────────────────────────────────────────
    def _decide(self, data: bytes) -> None:
        # 审查修复（2026-09-21）：判形必须拿满嗅探窗——旧形态下 <12B 的残响应
        # （如 4 字节 "RIFF"）`data[8:12]` 切空判不进 WAVE，掉到"裸 PCM 缺省"
        # 分支：实测产 1 帧垃圾、`cloud_frames=1` 被记**云成功并解除钉扎**。
        # 短到无法判容器的响应是坏响应，必须响亮失败→回落本地+钉扎。
        if len(data) < self._SNIFF_BYTES:
            raise RuntimeError(
                f"云 TTS 响应过短（共 {len(data)}B，无法判定容器形态）")
        if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            # 12B（RIFF/size/WAVE）即容器头，**不进音频流水**；其余留待块头解析
            self._rbuf = bytearray(data[12:])
            self._state = "riff_hdr"
            return
        why = _unsupported_format(data)
        if why:
            raise RuntimeError(why)
        self._start_payload(self._default_rate)

    def _start_payload(self, rate: int) -> None:
        self._res = _StreamResampler(rate, const.SAMPLE_RATE)
        self._enc = audio.OpusPcmEncoder("voip")
        self._state = "payload"

    def _parse_riff(self, chunk: bytes) -> list:
        """增量解析 RIFF 块头，只缓冲到 data 块为止；载荷即刻流出。"""
        import struct
        self._rbuf += chunk
        while True:
            if len(self._rbuf) < 8:
                return []
            cid = bytes(self._rbuf[:4])
            csz = struct.unpack("<I", bytes(self._rbuf[4:8]))[0]
            if cid == b"data":
                if not self._rate:
                    raise RuntimeError("云 TTS wav 头损坏（缺 fmt/data 块）")
                if self._bits != 16:
                    raise RuntimeError(f"云 TTS wav 位深 {self._bits} 不支持（仅 16-bit）")
                payload = bytes(self._rbuf[8:])
                if 0 < csz < 0xFFFFFFFF:
                    # 定案⑦：可信声明长度 → 按其截断（尾随 LIST/pad 不进音频）。
                    # 钳位唯一发生在 _payload（含本次首口），此处只记总额——
                    # 若在切片处先扣、_payload 再扣一次=双重递减（本文件测试批
                    # 当场逮到的实施缺陷：尾帧少 5 字节）。
                    self._data_left = csz
                self._rbuf = bytearray()
                self._start_payload(self._rate)
                return self._payload(payload)
            need = 8 + csz + (csz & 1)          # 块按偶数字节对齐
            if need > _RIFF_MAX_HDR_BYTES:
                raise RuntimeError("云 TTS wav 头损坏（缺 fmt/data 块）")
            if len(self._rbuf) < need:
                return []                        # 块体不全：等下一块
            if cid == b"fmt " and csz >= 16:
                body = self._rbuf[8:24]
                self._rate = struct.unpack("<I", bytes(body[4:8]))[0]
                self._bits = struct.unpack("<H", bytes(body[14:16]))[0]
            del self._rbuf[:need]

    def _payload(self, chunk: bytes) -> list:
        """喂响应体载荷块（**源域** s16le）：重采样 → 攒满 60ms 即出包。"""
        if self._data_left is not None:
            if self._data_left <= 0:
                return []                   # 声明长度已尽：之后的字节是尾块元数据
            if len(chunk) > self._data_left:
                chunk = chunk[:self._data_left]
            self._data_left -= len(chunk)
        return self._frame_pcm(self._res.feed(chunk))

    def _frame_pcm(self, pcm16k: bytes) -> list:
        """喂**已重采样**的 16k s16le：攒满 const.FRAME_BYTES 即编码出包。"""
        if not pcm16k:
            return []
        self._pcm += pcm16k
        step = const.FRAME_BYTES
        out = []
        while len(self._pcm) >= step:
            frame = bytes(self._pcm[:step])
            del self._pcm[:step]
            out.extend(self._enc.encode_stream(frame))
        return out


class TtsEngine:
    def __init__(self, settings, model_store):
        self.settings = settings
        self.store = model_store
        self._tts = None
        self._lock = threading.Lock()
        self._busy = 0          # 在飞合成数（卸载避让；审查 F1）
        # 审查修复（2026-09-21）：generate **并行**互斥。F1 只防跨代析构、
        # 不防同代并发——sherpa-onnx OfflineTts 前端（espeak-ng/jieba/pinyin
        # 通道）持共享可变状态，并发 generate 轻则两路音频互踩、重则 C++ 层
        # 崩溃打死整个容器（播报+STT+LLM 全断）。可达触发：web「试听」
        # (synthesize_pcm) 与卫星播报在飞句并发；双客户端同理。4C 台架合成
        # 本就 CPU 饱和，串行化零实质吞吐损失（云端解码/opus 编码不受此锁）。
        self._gen_lock = threading.Lock()
        self.last_used = time.time()
        self.encoder_rate = const.SAMPLE_RATE
        # P0-4 (text, sid, speed) → (packets, bytes) LRU；仅本地档，asyncio 单线程
        # 内读写（编码在 executor，但回主循环后才入表），无需锁。
        self._cache: "OrderedDict[tuple, tuple]" = OrderedDict()
        self._cache_bytes = 0
        self.cache_hits = 0     # 观测计数（状态页/排障）
        self._cloud_voice_warned = False   # 云音色缺省告警只打一次
        # v1.0.55：上次云失败时刻（monotonic）；0=健康。见 _CLOUD_PIN_S 注释。
        self._cloud_fail_ts = 0.0
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
                if p.name.startswith(".") or p.name.startswith("voices_custom_merged"):
                    continue                       # 隐藏文件与合并产物（误落投递口）不进预览
                try:
                    sz = p.stat().st_size          # 上传中途被替换/删除属瞬态，跳过即可
                except OSError:
                    continue
                ok = per > 0 and sz == per
                preview.append({"name": p.stem, "size": sz, "valid": ok,
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

    def _speed(self) -> float:
        """语速安全读（审查修复 2026-09-21）：合成三处与指纹必须读**同一个**
        校验值——坏配置（非数/非正）回 1.0 并留一行 WARN，不再"每轮 float()
        炸穿 → 截断声明"。播报可用性优先。"""
        raw = self.settings.get("tts.speed", 1.0)
        try:
            v = float(raw)
        except (TypeError, ValueError):
            logger.warning("[TTS] tts.speed=%r 非数，按 1.0 合成", raw)
            return 1.0
        if v <= 0:
            logger.warning("[TTS] tts.speed=%r 非正，按 1.0 合成", raw)
            return 1.0
        return v

    def voice_fingerprint(self) -> str:
        """v1.0.48（P5，"多嗓音"第六路径收口）：HA core 的 TTS 缓存键=
        sha1(文本)_语言_options_实体id——加载项内部配置对它**完全不感知**，
        web 换嗓后同一句永远命中旧嗓缓存。修复：集成实体把本指纹并入
        default_options（core 将 default_options 合进 options 参与键计算），
        指纹变→键轮换→必然重合成；旧条目由 TTL/清理兜底。递送=WS
        settings 消息（tts 通道建连随欢迎发 + web 保存即推送）。
        2026-09-21 审查修复批（口径更正）：缓存存的是**渲染结果**，凡改变
        音频产出的服务端配置都得进键——初版"speed 不改嗓音，不入"把嗓音
        身份与音频身份混为一谈：语速滑条（web 0.6–2.0）改档后模板句永久
        命中旧语速盘缓存（消息哈希键无 TTL，仅 clear_cache 可清），与当初
        修的"换嗓不轮换"同族同病灶。speed 入指纹（本地+云）；云档另补
        model/response_format/base_url host（换平台同 voice 名=不同嗓）。
        **api_key 永不入指纹**（指纹随 WS 帧与 INFO 日志走，凭据不上链）。
        2026-09-22 审查批 P3-a：本地档再补**模型包身份**（lock sha256 前 8 位）——
        本指纹只认 sid 数值，Kokoro 换包（v1_0→v1_1 那次真实发生过：同 sid 不同嗓）
        不换键=HA 消息哈希盘缓存永远命中旧包音频，与 speed 漏入同族同病灶。
        取不到 lock（假件/缺条目）回落 "u"，行为确定。"""
        prov = str(self.settings.get("tts.provider", "local_kokoro"))
        speed_s = f"s{self._speed():g}"
        if prov.startswith("cloud"):
            cloud = self.settings.get("tts.cloud") or {}
            if not isinstance(cloud, dict):
                cloud = {}
            base = str(cloud.get("base_url") or "")
            host = urlparse(base).netloc or base
            return (f"cloud:{str(cloud.get('voice') or 'alloy')}"
                    f":{str(cloud.get('model') or 'tts-1')}"
                    f":{str(cloud.get('response_format') or 'pcm')}"
                    f":{host}"
                    f":{speed_s}")
        tag = "u"
        try:
            entry = self.store.lock_entry("tts_kokoro_multilang") if self.store else {}
            tag = str(entry.get("sha256") or "")[:8] or "u"
        except Exception:  # noqa: BLE001 假件 store/异常形制：指纹照出，回落 u
            pass
        return (f"local:sid{self.resolve_sid()}+c{len(self._custom_sids)}"
                f"+{speed_s}+m{tag}")

    def reset_cloud_pin(self, reason: str = "") -> None:
        """v1.0.55（定案⑤）：云失败钉扎的复位开关=云配置热变更。

        _cloud_fail_ts 是对"仍是那个坏端点"的保险丝，不是死刑：用户改对
        base_url/api_key、换 provider、修 timeout，都不该被旧失败时刻继续
        钉在 local:pinned 最长 _CLOUD_PIN_S 秒。热应用回调
        （main._on_settings_change）检测 tts 节变更时调用本方法。"""
        if self._cloud_fail_ts:
            self._cloud_fail_ts = 0.0
            logger.info("[TTS] 云钉扎已解除（%s）→ 下一轮恢复试云",
                        reason or "配置变更")

    # ── 生命周期 ────────────────────────────────────────────────
    def ready(self) -> bool:
        return self._tts is not None

    def ensure_loaded(self) -> bool:
        with self._lock:
            if self._tts is not None:
                return True
            t_load = time.perf_counter()          # v1.0.52：一次性加载耗时观测
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
                # v1.0.52：加载耗时独立成行（现场"首句慢"是加载还是合成，一眼可辨）
                logger.info("[TTS] Kokoro 引擎就绪，耗时 %dms",
                            int((time.perf_counter() - t_load) * 1000))
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
        # v1.0.52：本轮本地首帧观测起点。放在**函数入口**——云档失败回落时，
        # 这个数=用户真正白等的那段静音（云尝试 + 本地起流），正是要盯的指标。
        t_turn = time.perf_counter()
        local_first_ms = None
        prov = str(self.settings.get("tts.provider", "local_kokoro"))
        fell_back = False
        if prov.startswith("cloud"):
            # v1.0.55 云失败钉扎（现场「还是有两个 tts 音色」主修，见 _CLOUD_PIN_S）。
            if self._cloud_fail_ts and (time.monotonic() - self._cloud_fail_ts < _CLOUD_PIN_S):
                fell_back = True
                if engine_out is not None:
                    engine_out["engine"] = "local:pinned"
                logger.info("[TTS] 云钉扎中（剩 %.0fs）→ 本轮直接本地默认音色",
                            _CLOUD_PIN_S - (time.monotonic() - self._cloud_fail_ts))
            else:
                if engine_out is not None:
                    engine_out["engine"] = "cloud:%s" % str(
                        (self.settings.get("tts.cloud") or {}).get("voice") or "alloy")
                cloud_frames = 0
                try:
                    async for pkt in self._cloud_stream(text):
                        cloud_frames += 1
                        yield pkt
                    if not cloud_frames:
                        # v1.0.56（审计 B-gap）：零帧"正常收束"（空 body、或被
                        # 定案⑦钳位后一帧不剩）不得记成"整流成功"——那会解除
                        # 既有钉扎并让整轮静默。抛错走既有失败路径：本地默认
                        # 音色兜底 + 开钉扎窗口。
                        raise RuntimeError("云零帧返回")
                    self._cloud_fail_ts = 0.0      # 整流成功 = 解除钉扎
                    return
                except Exception as e:
                    # 断在首帧前或半途都记失败时刻、开启钉扎窗口。
                    self._cloud_fail_ts = time.monotonic()
                    if cloud_frames:
                        # 半途断流**不**再接本地嗓：一男一女的混播比"本轮少半句、
                        # 下轮起全本地"更伤（双音色观感的另一来源在此封死）。
                        # v1.0.55（定案②）：生成器"正常耗尽"收的半截口必须自报
                        # 截断——session 据此在 stop 帧声明 truncated，集成以
                        # error 收口，HA 才不把缺尾音频写进消息哈希缓存。
                        logger.warning("[TTS] 云端半途断流（已出 %d 帧：%s）→ 本轮就此收束，"
                                       "并钉扎本地 %.0f 秒", cloud_frames, e, _CLOUD_PIN_S)
                        if engine_out is not None:
                            engine_out["truncated"] = True
                        return
                    fell_back = True
                    logger.warning("[TTS] 云合成失败(%s) → 回落本地默认音色，并钉扎本地 %.0f 秒"
                                   "（到期自动再试云；防逐句换嗓+逐句白等超时）", e, _CLOUD_PIN_S)
                    if engine_out is not None:
                        engine_out["engine"] = "local:fallback"
        loop = asyncio.get_running_loop()
        # 音色归属定案（用户 2026-09-18）：本地档=web 设定音色（数字或自定义主名）；
        # 云档=云端对应音色（可选）；**云失败回落=固定默认本地音色 sid18**——回落是
        # 应急通道，取最保守单音色，不跟 web 配置（可能是自定义名/云专属号）漂移。
        sid = _DEFAULT_SID if fell_back else self.resolve_sid()
        speed = self._speed()
        if engine_out is not None:
            eng0 = engine_out.get("engine")
            engine_out["engine"] = f"local:sid{sid}" + (
                "(云钉扎)" if eng0 == "local:pinned"
                else ("(云回落)" if eng0 == "local:fallback" else ""))
        load_checked = self.ready()
        for sent in split_sentences(text):
            if not any(c.isalnum() for c in sent):
                # v1.0.56（审计 P1）：split_sentences 会把连排标点拆出独立
                # 纯标点段（"第一句！！！第二句。"→['第一句！','！','！','第二句。']），
                # emoji/符号行同理。这类段本地合成**正常**产出为空——把它当
                # 缺句 latched truncated，会把整轮完整音频永久判死（每轮误报、
                # 流式路 raise+弃缓存+重连且不自愈）。真词句空产出照报截断，
                # 定案②语义不丢。
                continue
            key = (sent, sid, speed)
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                self.cache_hits += 1
                self.last_used = time.time()
                for pkt in hit[0]:
                    local_first_ms = self._note_local_first(t_turn, local_first_ms)
                    yield pkt
                continue
            # 首错 miss 才拉模型：全命中回合在省电档卸载态也能完整播出
            if not load_checked:
                # v1.0.52：冷启动/预热门持锁时，本轮到底等了多久必须留痕
                t_wait = time.perf_counter()
                ok = await loop.run_in_executor(None, self.ensure_loaded)
                logger.info("[TTS] 模型未就绪，本轮等待 %dms",
                            int((time.perf_counter() - t_wait) * 1000))
                if not ok:
                    if engine_out is not None:
                        # 定案②：模型加载失败=其后各句全缺，半截口必须自报截断
                        engine_out["truncated"] = True
                    return
                load_checked = True
                # v1.0.55（定案⑥）：模型就绪后复核音色。冷态 _tts=None 时
                # resolve_sid 的越界检查被 `n and ...` 短路——坏配置写的越界
                # 数字原样返回，_synth 抛错被吞就是整轮静默（与热态"WARN+
                # 回落18"两态不一致）。n>0 才拦得住，故复核必须在这里做。
                # fell_back 恒 _DEFAULT_SID，无需复核。
                if not fell_back:
                    sid2 = self.resolve_sid()
                    if sid2 != sid:
                        logger.info("[TTS] 模型就绪后音色复核：%s → %s", sid, sid2)
                        sid = sid2
                        if engine_out is not None:
                            eng0 = engine_out.get("engine")
                            engine_out["engine"] = f"local:sid{sid}" + (
                                "(云钉扎)" if eng0 == "local:pinned"
                                else ("(云回落)" if eng0 == "local:fallback" else ""))
                        key = (sent, sid, speed)      # 复核后本句键重算
                        hit = self._cache.get(key)
                        if hit is not None:
                            self._cache.move_to_end(key)
                            self.cache_hits += 1
                            self.last_used = time.time()
                            for pkt in hit[0]:
                                local_first_ms = self._note_local_first(t_turn, local_first_ms)
                                yield pkt
                            continue
            pcm16 = await loop.run_in_executor(None, self._synth, sent, sid, speed)
            if not pcm16:
                # v1.0.55（定案②）：_synth 吞错产空句=整段播报缺一句，此前静默
                # continue、半截音频以普通 stop 收口——HA 把缺尾音频当完整结果
                # 缓存，同一句永久缺一截且不自愈。改为自报截断（缺一句也报）。
                if engine_out is not None:
                    engine_out["truncated"] = True
                logger.warning("[TTS] 句子合成空产出，本轮声明截断: %r", sent[:30])
                continue
            # F5：整句 opus 编码是 CPU 活，出事件循环
            packets = await loop.run_in_executor(None, self._encode, pcm16)
            if packets:
                self._cache_put(key, packets)
            for pkt in packets:
                local_first_ms = self._note_local_first(t_turn, local_first_ms)
                yield pkt
        self.last_used = time.time()

    @staticmethod
    def _note_local_first(t_turn: float, shown: Optional[int]) -> Optional[int]:
        """本地首帧观测：本轮第一个 opus 包距本轮起点的毫秒数（每轮只打一行）。"""
        if shown is None:
            shown = int((time.perf_counter() - t_turn) * 1000)
            logger.info("[TTS] 本地首帧 %dms", shown)
        return shown

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
            # 并行 generate 互斥（见 __init__ _gen_lock 注释）：F1 快照/计数
            # 语义不变，仅把 C++ 调用排队（两锁不嵌套、无死锁序）。
            with self._gen_lock:
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
        speed = self._speed()
        out = b""
        for sent in split_sentences(text):
            out += await loop.run_in_executor(None, self._synth, sent, sid, speed)
        return out

    # ── 云档（OpenAI 兼容 /audio/speech）────────────────────────
    @staticmethod
    def _cloud_timeout(cloud: dict) -> tuple:
        """把 tts.cloud.* 超时配置编成 (aiohttp.ClientTimeout, 首字节秒数)。

        aiohttp 的 ClientTimeout 只有 total/connect/sock_connect/sock_read 四个
        字段，**没有独立的"首字节"**；故 connect/块间读/总闸交给它，首字节由
        调用方用 `asyncio.wait_for` 施加在「响应头 + 首个数据块」上。
        缺省/空值/脏值一律回内置默认（云档不能因一处配置写坏而失去超时保护）。
        """
        import aiohttp

        def _f(key: str, dflt: float) -> float:
            try:
                v = float((cloud or {}).get(key))
            except (TypeError, ValueError):
                return dflt
            return v if v > 0 else dflt

        connect = _f("connect_timeout_s", _CLOUD_CONNECT_TIMEOUT_S)
        first = _f("first_byte_timeout_s", _CLOUD_FIRST_BYTE_TIMEOUT_S)
        read = _f("read_timeout_s", _CLOUD_READ_TIMEOUT_S)
        total = _f("total_timeout_s", _CLOUD_TOTAL_TIMEOUT_S)
        return aiohttp.ClientTimeout(total=total, connect=connect, sock_read=read), first

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
                "speed": self._speed()}
        # 平台预设透传：如硅基流动 pcm 默认 44.1kHz，须显式指定才与预期一致
        if sr_req := cloud.get("sample_rate"):
            body["sample_rate"] = int(sr_req)
        # v1.0.52：分段超时（原本 total=30 一把梭）。speed 仍走请求体，未动。
        timeout, first_byte_s = self._cloud_timeout(cloud)
        default_rate = int(cloud.get("sample_rate") or 24000)
        t0 = time.perf_counter()
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            req = sess.post(f"{base}/audio/speech", json=body, headers=headers)
            try:
                # 首字节闸上半段：响应头也不给 = 停摆，几秒内失败 → 云→本地回落
                r = await asyncio.wait_for(req.__aenter__(), first_byte_s)
            except asyncio.TimeoutError as e:
                raise RuntimeError(
                    f"云 TTS 首字节超时（>{first_byte_s:g}s 无响应，"
                    "可调 tts.cloud.first_byte_timeout_s）") from e
            try:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}: {(await r.text())[:160]}")
                content = getattr(r, "content", None)
                if content is None or not hasattr(content, "iter_chunked"):
                    # 无流式 body 的响应对象（老式适配器/测试桩，非 aiohttp）：
                    # 退回整包路径，行为与旧版逐字一致（_unwrap_audio + _resample_encode）
                    raw = await r.read()
                    pcm, src_rate = self._unwrap_audio(raw, default_rate)
                    loop = asyncio.get_running_loop()
                    for pkt in await loop.run_in_executor(
                            None, self._resample_encode, pcm, src_rate):
                        yield pkt
                    return
                # ── 增量读：每块一到就解码出帧，不等整包 ──────────────
                dec = _CloudOpusStream(default_rate)
                agen = content.iter_chunked(_CLOUD_CHUNK_BYTES)
                ttfb_ms = None
                first_ms = None
                while True:
                    if ttfb_ms is None:
                        # 首字节闸下半段：连上了但不给第一个数据块
                        try:
                            chunk = await asyncio.wait_for(
                                _next_body_chunk(agen), first_byte_s)
                        except asyncio.TimeoutError as e:
                            raise RuntimeError(
                                f"云 TTS 首字节超时（>{first_byte_s:g}s 无数据，"
                                "可调 tts.cloud.first_byte_timeout_s）") from e
                    else:
                        chunk = await _next_body_chunk(agen)   # 块间静默由 sock_read 兜底
                    if chunk is None:
                        break
                    if ttfb_ms is None:
                        ttfb_ms = int((time.perf_counter() - t0) * 1000)
                    for pkt in dec.feed(chunk):
                        if first_ms is None:
                            first_ms = int((time.perf_counter() - t0) * 1000)
                            logger.info("[TTS] 云首字节 %dms / 首帧 %dms", ttfb_ms, first_ms)
                        yield pkt
                for pkt in dec.flush():
                    if first_ms is None:
                        first_ms = int((time.perf_counter() - t0) * 1000)
                        logger.info("[TTS] 云首字节 %dms / 首帧 %dms",
                                    ttfb_ms if ttfb_ms is not None else first_ms, first_ms)
                    yield pkt
                if first_ms is None:
                    logger.warning("[TTS] 云合成无输出（首字节 %sms 后无完整帧）",
                                   ttfb_ms if ttfb_ms is not None else -1)
            finally:
                await req.__aexit__(None, None, None)

    @staticmethod
    def _unwrap_audio(raw: bytes, default_rate: int) -> tuple:
        """返回 (s16le mono pcm, rate)。RIFF/WAVE 拆封；mp3/ogg 显式报错。"""
        import struct
        # 与流式路 _decide 同闸（审查修复 2026-09-21）：短到无法判容器的整包
        # 响应是坏响应（0B 维持旧语义交给零帧收束政策）。
        if 0 < len(raw) < 12:
            raise RuntimeError(
                f"云 TTS 响应过短（共 {len(raw)}B，无法判定容器形态）")
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
        if why := _unsupported_format(raw):
            raise RuntimeError(why)
        return raw, default_rate

    @staticmethod
    def _resample_encode(raw: bytes, src_rate: int) -> list:
        pcm16k = audio.resample_pcm16(raw, src_rate, const.SAMPLE_RATE)
        return list(audio.OpusPcmEncoder("voip").encode_stream(pcm16k))
