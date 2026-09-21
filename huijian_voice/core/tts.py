"""TTS 引擎（本地 Kokoro-82M multi-lang **v1.1 fp32 包 kokoro-multi-lang-v1_1**
——103 音色 web 全可选，默认 sid=28 zf_044 女声、默认语速 1.25（用户拍板
2026-09-19，取代 2026-09-13 的 sid18 zf_026 定案；云可配回落本地）。

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
import contextlib
import hashlib
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from collections import OrderedDict
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

import numpy as np

from . import audio, const

logger = logging.getLogger("huijian.tts")


#: v1.0.91：出帧/合成单元的目标字数上限（≈3.4s 音频）。见 split_sentences 内注释。
_CHUNK_CHARS = 20


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
    # v1.0.91（F4-B 出帧单元切小）：现场探针实测本机本地 Kokoro **RTF≈1.32**
    # （63 字答复：首帧 4.02s；三个分句各自"一次性倒完 44~50 帧"，分句之间空
    # 3.48s / 3.68s；总音频 8.46s 用了 11.19s 产出）。旧切法只在 >40 字且有逗号
    # 时才二次切，于是一句 20~28 字的分句要"合成 4~5 秒 → 播放 3 秒"，播放侧
    # 必然半路抽干 ⇒ 用户听感＝"一句话分好几次说完，中间有卡顿"。
    # 出帧单元降到 ~20 字（≈3.4s 音频）后：单段空洞 ≈ 3.4×(RTF−1) ≈ 1.1s，落在
    # "设备播放队列 2.05s + HA 预灌 1.536s"的吸收范围内；首帧也从 4.9s 级降到
    # ~2.2s 级。优先在逗号/顿号处切、凑够半程才切（防切出碎片），无标点才硬切，
    # 全程只改分段不改内容——拼接必须逐字还原（有钉）。
    # （台架 x86 RTF 0.30 时本改动同样无害：段间停顿由 Kokoro 韵律决定。）
    out2 = []
    for p in out:
        if len(p) <= _CHUNK_CHARS:
            out2.append(p)
            continue
        pieces, cur = [], ""
        for ch in p:
            cur += ch
            if ch in "，,、；;：: " and len(cur) >= _CHUNK_CHARS // 2:
                pieces.append(cur)
                cur = ""
        if cur:
            pieces.append(cur)
        # 边界切完仍可能有一段超长（逗号只出现在 60 字外的场景），逐段再硬切，
        # 保证**任何**出帧单元 ≤_CHUNK_CHARS（否则空洞回到 4~5s，本钉就白做）。
        for piece in pieces:
            while len(piece) > _CHUNK_CHARS:
                out2.append(piece[:_CHUNK_CHARS])
                piece = piece[_CHUNK_CHARS:]
            if piece:
                out2.append(piece)
    out = out2
    # v1.0.65（TTS 深审 F1 第二道闸）：无标点超长句强制按长度切块。generate
    # 非流式——单句长度=引擎持锁时长，session 入口截到 4000 字后仍可能是一整
    # 句 4000 字（分钟级持锁照样冻死播报通道）。300 字/块：每块合成有界可
    # 中断，块间由整流间隙窗逐块裁决（v1.0.83：停滞才截，慢而持续不砍），前块已
    # 出声——把"整段黑洞"换成"截尾可播"。
    hard = []
    for p in out:
        while len(p) > 300:
            hard.append(p[:300])
            p = p[300:]
        if p:
            hard.append(p)
    return hard


_CACHE_MAX_ITEMS = 256          # 播报句集收敛得快，256 句封顶
_CACHE_MAX_BYTES = 4 << 20      # 4MB 硬闸（opus 32kbps×10s≈40KB/句，量级宽裕）
_SPEED_MAX = 2.0                # v1.0.65 F10：与 Web 滑条上限对齐的服务端钳位
# 2026-09-27 深审 R2 #1（F10 镜像残留）：补对称下界。generate 时长 ∝ 1/speed
# 且不可取消（executor 线程+持 _gen_lock）：speed=0.05 → 单句 20×，session
# 整流预算（现 52s，见 const ⑧算术）只能截协程侧，线程照跑——逐轮漏占池线程
# 会饿死他人；v1.0.70 ⑤起 TTS 走自建 2 工位池，漏占不再外溢，但闸本身保留。
# Web 滑条 0.6 的服务端对应物（留半档容差取 0.5）。
_SPEED_MIN = 0.5
# 2026-09-27 深审 R2 #1b：排队等 _gen_lock 必须有界——前手是"坏 speed 的
# 小时级 generate"或冷下载时，后来者无限排队=同一条漏线程路径。
# v1.0.93（D3 按轮次恶化根修）：50s→30s，对齐整流 52s 帧间隙窗。旧值下本句
# 白等 50s 再合成，句间合计必然穿破 52s 窗（设备 T_DL_STALL=48s 先手拆流），
# 等待本身成为"第 4/5 轮播报不完整"的放大器（2026-09-18 真机签名）。30s 是
# 折中：合法突发（长句排队尾）仍放行，真堵死时早 20s 认输、余句还有救。
_GEN_WAIT_S = 30.0
# 2026-09-27 深审 R2 #4：采样率合理域单点闸。fmt rate 是外部字节（F13 同一
# 威胁模型：坏端点/中间盒），rate=1 → ratio=16000 → 单块 8KB 触发 6.5×10⁷ 点
# np.arange ≈1.5GB 峰值分配 = 一条响应打死容器。8k–192k 覆盖全部现实 TTS 输出。
_RATE_MIN, _RATE_MAX = 8000, 192000
# 2026-09-27 深审 R2 #5：data 块"多付"容忍。F16 只防声明>实付；声明<实付
# （provider 把样本数当字节数写=2× 误差这类）时后半音频被当"尾块元数据"静默
# 吞掉且记完整成功=缺尾毒化从反方向漏进。
# v1.0.85（P2）收紧：旧单一绝对 64KB 让"样本数谎"在短句（实付≤128KB）恰好
# 溜过、又低于 O4 比值闸的 60 字豁免线=双闸失守。真尾元数据（LIST/INFO）
# 量级 <10KB：绝对底线 16KB 放行一切诚实容器；相对项 20%×实付兜住任意规模
# 的撒谎者（样本数谎恒为 50%，任何尺寸必 Catch）。
_DROP_TOLERANCE = 16384         # 绝对底线（保持旧名兼容测试引用）
_DROP_TOLERANCE_RATIO = 0.2     # 相对项：多付/实付 超此比按撒谎处理


_DEFAULT_SID = 28   # 本地定案默认音色 zf_044（用户拍板 2026-09-19；云失败回落唯一用嗓，音色归属条款③）

# ── v1.1.5 多本地引擎（provider → 模型键 / 默认音色）─────────────────────
# 2026-09-21 四引擎台架横评（bench_tts_20260921）：Kokoro 维持默认（103 音色）；
# Matcha 中英（RTF 0.022/首包 32ms）与 MeloTTS 中英（RTF 0.197）入可选档，
# 均为**单说话人 sid0**。ZipVoice 实测 RTF 0.9~1.66+首包 2.1s+非增量回调出局
# （CPU 流式预算不可达，接入=现场半速卡顿观感）。
PROVIDER_MODEL_KEYS = {
    "local_kokoro": "tts_kokoro_multilang",
    "local_matcha": "tts_matcha_zh_en",
    "local_melo": "tts_melo_zh_en",
}

def _default_sid_for(provider: str) -> int:
    return _DEFAULT_SID if provider == "local_kokoro" else 0

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

# ── 本地 Kokoro 推理线程数（引擎提速开放项，v1.0.92 起可显式抬）──────
_TTS_THREADS_DEFAULT = 2
# 台架实测（2026-09-17，主机直跑、与现场同 v1.1 fp32 包 65 字文本、RTF）：
#   threads2=0.42 │ threads4=0.29 │ threads8=0.23 —— 提速真实存在。
# 但现场加载项跑在 HAOS 容器里、CPU 配额未知，盲目抬线程会跟 HA 主进程抢核
# （验证规矩：不得拿客户当测试）⇒ **默认保持 2 不变**，只开显式覆盖通道：
# HAOS 加载项「配置→高级→环境变量」填 HUIJIAN_TTS_THREADS=4，无需发版。
# 解析不了的正整数（空/abc/0/-1）一律回落默认。永不抛。
def _engine_threads() -> int:
    try:
        n = int(os.environ.get("HUIJIAN_TTS_THREADS", "") or 0)
    except ValueError:
        return _TTS_THREADS_DEFAULT
    return n if n > 0 else _TTS_THREADS_DEFAULT


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
    """返回 (生效 voices 路径, {音色主名(小写): sid}, 跳过说明列表, 内容指纹串)。

    内容指纹 fp=（官方包 size/mtime + 各自音色 name/size/mtime）的 repr——
    v1.0.65（深审 F6）起随返回值带出，供 voice_fingerprint 并入：同名重传
    改良版 bin（数量不变、sid 不变、模型 lock sha 不变）也必须轮换 HA 消息
    哈希盘缓存键（无 TTL），否则模板句永久旧嗓——与已修的 speed/model 漏入
    同族同病灶。目录缺失/无 .bin/未配置 → 指纹为空串。
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
        return official, {}, skipped, ""
    bins = sorted(p for p in custom_dir.glob("*.bin")
                  if p.is_file() and not p.name.startswith(".")
                  and not p.name.startswith("voices_custom_merged"))   # 合并产物误落投递口时不得自吞
    usable: list[Path] = []
    seen_stems: dict[str, str] = {}
    for p in bins:
        size = p.stat().st_size
        if size != per_voice:
            skipped.append(f"{p.name}: {size}B ≠ 单音尺寸 {per_voice}B")
            continue
        # v1.0.65（TTS 深审 F7）：大小写异体同名（Amy.bin/amy.bin）旧版进
        # names 按 stem.lower() 去重只剩 1 项，merged 却拼 2 路 → num_speakers
        # 对不上 → 误诊"sherpa 不支持追加"、整个自定义区被禁。上传侧已补 409，
        # 这里防旁路投递（手工拷目录）：碰撞保留先者、跳后者并留痕。
        stem = p.stem.lower()
        if stem in seen_stems:
            skipped.append(f"{p.name}: 与 {seen_stems[stem]} 同名（大小写折叠），跳过")
            continue
        seen_stems[stem] = p.name
        usable.append(p)
    if not usable:
        return official, {}, skipped, ""
    names = {p.stem.lower(): voices_count + i for i, p in enumerate(usable)}
    meta = out.with_suffix(".meta")
    fp = repr((official.stat().st_size, int(official.stat().st_mtime),
               [(p.name, p.stat().st_size, int(p.stat().st_mtime)) for p in usable]))
    if out.exists() and meta.exists() and meta.read_text(encoding="utf-8") == fp:
        return out, names, skipped, fp   # 指纹未变：不重写盘
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
        # v1.0.65（F9）：落盘失败清 tmp（典型 ENOSPC，54MB 残体恰加剧空间紧张）
        # ——对齐 main._atomic_write 的 F6 纪律。
        with contextlib.suppress(OSError):
            tmp.unlink()
        skipped.append(f"合并落盘失败：{e}")
        return official, {}, skipped, ""
    return out, names, skipped, fp


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
        # 2026-09-27 深审 R2 #3：fmt.nChannels 与位深同闸（此前只校验 bits，立体声
        # 交错被当单声道解=半速变调噪声"成功"入缓存——协议不变量 16k/mono 漏一半）。
        self._nch = 0
        # 2026-09-27 深审 R2 #5：data 声明长度之外被钳掉的字节计数（多付侦账）。
        self._dropped = 0
        self._payload_bytes = 0   # v1.0.85（P2）：data 声明内实付计数（相对闸分母）
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
            # v1.0.65（深审 F16）：干净收尾（chunked 正常结束、无传输错误）但
            # data 块声明长度未付满=服务端撒谎截断——旧版零填充尾帧后计**完整
            # 成功**并解除钉扎，缺尾音频进 HA 盘缓存（truncated 协议要防的正是
            # 这一形态，此前只防了断流没防"声明 vs 实付"不一致）。raise 走既有
            # 失败路径（云→本地回落+钉扎），不静默。未知长声明（0/0xFFFFFFFF）
            # _data_left=None，不进本闸（见类注释 v1.0.56 定案）。
            if self._data_left is not None and self._data_left > 0:
                raise RuntimeError(
                    f"云 TTS wav 声明长度未付满（data 块尚欠 {self._data_left}B，"
                    "疑似服务端撒谎截断，按云故障处理）")
            # 深审 R2 #5（反方向同闸）+ v1.0.85（P2）相对化：多付超
            # max(16KB, 20%×实付) 按撒谎收——绝对容忍单独用会让"样本数谎"
            # 在短句恰好溜过（且短句被 O4 豁免=双闸失守、缺尾进无 TTL 盘缓存）。
            _cap = max(_DROP_TOLERANCE, int(self._payload_bytes * _DROP_TOLERANCE_RATIO))
            if self._data_left is not None and self._dropped > _cap:
                raise RuntimeError(
                    f"云 TTS wav 声明长度后多付 {self._dropped}B（超出尾元数据"
                    "量级容忍，疑似按样本数撒谎声明=后半音频被吞，按云故障处理）")
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
        # v1.0.65（深审 F13）：HTTP 200 + ≥12B 的**非音频体**防线——部分
        # OpenAI 兼容网关/透明代理会把错误做成 200+JSON（或撞 portal 的 200+
        # HTML），旧版判不成容器就掉进"裸 PCM 缺省"分支：垃圾字节被重采样编码
        # 成噪声帧发往卫星，且 cloud_frames>0 → 记**云成功解除钉扎**，每轮重放
        # 噪声。判据保守：只拦容器特征（ASCII 结构前缀），二进制 PCM 第一帧
        # 是均匀分布的样本对，出现"前 12B 全可打印且以 { [ < 起手"的概率可忽略
        # （false-reject 走既有云故障→本地回落，永远比播噪声安全）。
        printable = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13))
        if data[:1] in (b"{", b"[", b"<") and printable >= len(data) * 0.85:
            raise RuntimeError(
                "云 TTS 返回 200 但正文是文本而非音频（疑似网关/代理错误页）："
                f"{data[:60].decode(errors='replace')!r}")
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
                # 深审 R2 #3/#4：声道与采样率入域闸（按云故障走既有回落+钉扎）。
                if self._nch > 1:
                    raise RuntimeError(
                        f"云 TTS wav 声道数 {self._nch} 不支持（仅单声道；"
                        "立体声交错样本会被当单声道解成变速噪声）")
                if not _RATE_MIN <= self._rate <= _RATE_MAX:
                    raise RuntimeError(
                        f"云 TTS wav 采样率 {self._rate} 超出合理域 "
                        f"[{_RATE_MIN},{_RATE_MAX}]（坏 fmt 头，防重采样无界分配）")
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
                self._nch = struct.unpack("<H", bytes(body[2:4]))[0]
                self._bits = struct.unpack("<H", bytes(body[14:16]))[0]
            del self._rbuf[:need]

    def _payload(self, chunk: bytes) -> list:
        """喂响应体载荷块（**源域** s16le）：重采样 → 攒满 60ms 即出包。"""
        if self._data_left is not None:
            if self._data_left <= 0:
                # 声明长度已尽：之后的字节按尾块元数据处置——但**多付侦账**
                # （深审 R2 #5）：真元数据量级小，超出容忍=声明撒谎、后半音频
                # 被吞，flush 时按云故障收（防缺尾音频以"完整成功"进 HA 盘缓存）。
                self._dropped += len(chunk)
                return []
            if len(chunk) > self._data_left:
                self._dropped += len(chunk) - self._data_left
                chunk = chunk[:self._data_left]
            self._data_left -= len(chunk)
            self._payload_bytes += len(chunk)   # v1.0.85（P2）实付计数
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
        # v1.0.84（B1）：整轮在飞计数——v1.0.83 把整流墙钟 52s 放开到 660s
        # 总闸后，长播报轮可跨 reaper 的 60s tick：_busy 只罩单句 generate
        # 瞬间，句间空隙 + last_used 只在轮首尾刷新 = 省电档轮中卸载、余句
        # 全空产缺尾。stream_opus 入口/出口各 ±1（只在事件循环线程读写，
        # 不持 _lock——unload 侧只读比较，晚一拍下一次 tick 自会跳过）。
        self._round_busy = 0
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
        # 深审 R2 #6：模型加载/下载引擎级单飞门（后来者即返，不排队占线程）。
        self._load_gate = threading.Lock()
        self._loading = False
        # v1.0.70（深审⑤）：合成/编码专用线程池——懒建（纯测试/云档引擎零线程）。
        # 旧形态全部 run_in_executor(None,…) 挤 asyncio 默认 8 线程池（4C 机），
        # 播报风暴期与 ASR 转写/TextCNN 预估/模型加载同池排队：识别一起停摆
        # （"看得见连接听不见回答"的服务器版）。
        # v1.0.93（D3）：2→4 工位。2 工位=单轮严丝合缝（1 合成+1 编码），但
        # generate **不可取消**——被打断轮的在飞句连线程带 _gen_lock 一起拖到
        # 自然跑完，此后任何"第二位"任务（他轮 _synth/_encode/ensure）即排队；
        # 连续对话里每打断一次就多压一层尸体工时，轮次越后句间越迟——真机
        # 2026-09-18 签名"第 1/2 轮完整、第 4/5 轮不完整"的池侧放大器。4 工位
        # =打断残局 1 + 双活轮合成/编码 2 + 余量 1；generate 真身仍由
        # _gen_lock 峰值=1 串行（多工位不抬并发 CPU 占用），配合本轮 abort
        # 闸（未起跑句锁口即死）与 _GEN_WAIT_S=30 收口。卸载不 shutdown：
        # 常驻停泊线程，换取重载即时可用，也免掉 shutdown/重建竞态。
        self._exec: Optional[ThreadPoolExecutor] = None
        self._exec_lock = threading.Lock()
        # 深审 R2 #2/#7：指纹变更通知钩子（Service 布线；任何线程可调，
        # 实现方自带线程安全与幂等去重）。加载完成/钉扎置位与解除时触发。
        self.on_fp_change = None

    def _notify_fp_change(self) -> None:
        cb = self.on_fp_change
        if cb is None:
            return
        try:
            cb()
        except Exception:  # noqa: BLE001 通知失败≠播报失败；welcome 帧兜底补值
            logger.debug("[TTS] 指纹变更通知失败（不阻断）", exc_info=True)

    def _pin_active(self) -> bool:
        return bool(self._cloud_fail_ts) and \
            (time.monotonic() - self._cloud_fail_ts) < _CLOUD_PIN_S

    def _voices_count(self, key: str) -> int:
        """官方音色数的异常安全读法：store 缺方法/返回垃圾一律 0（=注入关闭）。
        台架回归实锤：旧 fake store 无此方法曾直接把 ensure_loaded 打死。"""
        try:
            return int(self.store.voices_count_for(key) or 0)
        except Exception:
            return 0

    def _provider(self) -> str:
        """当前本地引擎档（v1.1.5）：local_kokoro/local_matcha/local_melo；
        未知 local_* 值回落 kokoro（与云档 startswith 双吃同纪律，配置写坏
        绝不哑播）。云档判定在调用侧（startswith("cloud")）。"""
        prov = str(self.settings.get("tts.provider", "local_kokoro"))
        return prov if prov in PROVIDER_MODEL_KEYS else "local_kokoro"

    def model_key(self) -> str:
        """当前档需要就绪的模型键（main._loop_models 预下载/换绑判定用）。"""
        return PROVIDER_MODEL_KEYS[self._provider()]

    def loaded_provider(self) -> str:
        """在载引擎档；未标（旧桩/升级瞬态）按历史默认 kokoro。"""
        return getattr(self, "_loaded_prov", None) or "local_kokoro"

    def ready_for_current_provider(self) -> bool:
        return self._tts is not None and self.loaded_provider() == self._provider()

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
            # 深审 R2 #9：预览必须复刻 merge 侧的 F7 大小写折叠跳数——旁路投递
            # （手工拷目录绕过上传口 409）Amy.bin+amy.bin 时，merge 跳后者，
            # 旧预览两个都排 sid → 碰撞对之后全部错位 +1，用户照面板填数字拿错嗓。
            seen_stems: set[str] = set()
            for p in sorted(const.TTS_VOICES_DIR.glob("*.bin")):
                if p.name.startswith(".") or p.name.startswith("voices_custom_merged"):
                    continue                       # 隐藏文件与合并产物（误落投递口）不进预览
                try:
                    sz = p.stat().st_size          # 上传中途被替换/删除属瞬态，跳过即可
                except OSError:
                    continue
                ok = per > 0 and sz == per
                collide = ok and p.stem.lower() in seen_stems
                if collide:
                    ok = False
                if ok:
                    seen_stems.add(p.stem.lower())
                preview.append({"name": p.stem, "size": sz, "valid": ok,
                                "sid": official_n + i if ok else None,
                                **({"note": "与同名异体大小写冲突（merge 跳后者）"}
                                   if collide else {})})
                if ok:
                    i += 1
        return {"official_count": official_n, "per_voice_bytes": per,
                "dir": str(const.TTS_VOICES_DIR),
                "injected": dict(sorted(self._custom_sids.items(), key=lambda kv: kv[1])),
                "preview": preview}

    def resolve_sid(self) -> int:
        """tts.sid = 整数（官方 0..102 / 自定义 103+）**或自定义音色主名**。
        非法/越界一律回落默认音色 _DEFAULT_SID（现 sid28 zf_044）并 WARN——
        配置写坏绝不让播报哑掉。"""
        raw = self.settings.get("tts.sid", _DEFAULT_SID)
        sid = None
        try:
            sid = int(str(raw).strip())
        except (TypeError, ValueError):
            sid = self._custom_sids.get(str(raw).strip().lower())
            if sid is None:
                logger.warning("[TTS] tts.sid=%r 既非数字也不是已注入的自定义音色，"
                               "回落默认 %d（可先「重新扫描」或检查投递目录）",
                               raw, _DEFAULT_SID)
        n = int(getattr(self._tts, "num_speakers", 0) or 0)
        if sid is None or sid < 0 or (n and sid >= n):
            # v1.1.5：默认音色按引擎档取。Kokoro 的 sid28 残留对单音色档是
            # **预期态**（切引擎但没改音色），静默钳 0 不 WARN；其余越界照报。
            if sid is not None and sid != _DEFAULT_SID:
                logger.warning("[TTS] tts.sid=%s 越界（本机共 %d 音色），回落默认",
                               raw, n)
            sid = _default_sid_for(self._provider())
            if n and sid >= n:
                sid = 0
        return sid

    def _speed(self) -> float:
        """语速安全读（审查修复 2026-09-21）：合成三处与指纹必须读**同一个**
        校验值——坏配置（非数/非正）回 1.0 并留一行 WARN，不再"每轮 float()
        炸穿 → 截断声明"。播报可用性优先。
        v1.0.65（深审 F10）：补上界钳位——API 直写 speed=50 旧版原样进引擎与
        云请求体（本地档产出狂嗓、云侧多半 400→钉扎风暴）；Web 滑条 0.6–2.0
        的服务端对应物就是这里（所有消费点含指纹都走本函数，钳位即全链一致）。"""
        raw = self.settings.get("tts.speed", 1.0)
        try:
            v = float(raw)
        except (TypeError, ValueError):
            logger.warning("[TTS] tts.speed=%r 非数，按 1.0 合成", raw)
            return 1.0
        if v <= 0:
            logger.warning("[TTS] tts.speed=%r 非正，按 1.0 合成", raw)
            return 1.0
        if v > _SPEED_MAX:
            logger.warning("[TTS] tts.speed=%r 超上限，按 %g 钳位合成", raw, _SPEED_MAX)
            return _SPEED_MAX
        if v < _SPEED_MIN:
            # 深审 R2 #1：下界与上界对称——小 speed=generate 时长爆炸（∝1/v）且
            # 不可取消，0.05 一档就够把 executor 池拖穿（播报+STT 停摆）。
            logger.warning("[TTS] tts.speed=%r 低于下限，按 %g 钳位合成", raw, _SPEED_MIN)
            return _SPEED_MIN
        return v

    _CLOUD_RATE_DEFAULT = 24000

    def _cloud_rate(self, cloud: dict) -> int:
        """tts.cloud.sample_rate 安全读（v1.0.65 深审 F10）：与 _cloud_timeout
        同纪律——云档不能因一处配置写坏而失去保护。脏值（如 "44.1k"）旧版
        `int()` ValueError 穿出 _cloud_stream，被误当云故障钉扎 300s、每 5 分钟
        重炸且日志不点名配置项；现在消毒回 0（=未配置，不发 sample_rate、
        解码按默认率）并 WARN 点名。"""
        raw = (cloud or {}).get("sample_rate")
        if raw is None or raw == "" or raw == 0:
            return 0
        try:
            v = int(raw)
        except (TypeError, ValueError):
            logger.warning("[TTS] tts.cloud.sample_rate=%r 非整数，按未配置处理"
                           "（解码回默认 %d Hz；请改配置或走「重载配置」）",
                           raw, self._CLOUD_RATE_DEFAULT)
            return 0
        if v <= 0:
            return 0
        # 深审 R2 #4：配置侧同域闸——sample_rate=1 会直进裸 PCM 解码率与请求体，
        # ratio=16000 的重采样=单块 GB 级分配。域外按未配置消毒（回默认率）。
        if not _RATE_MIN <= v <= _RATE_MAX:
            logger.warning("[TTS] tts.cloud.sample_rate=%d 超出合理域 [%d,%d]，"
                           "按未配置处理（回默认 %d Hz）", v, _RATE_MIN, _RATE_MAX,
                           self._CLOUD_RATE_DEFAULT)
            return 0
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
            # v1.0.65（深审 F5）：sample_rate 是文档明示的产出改变项（硅基流动
            # pcm 默认 44.1k，须显式指定才与预期一致）——既进请求体又是裸 PCM
            # 解码率，改配置后产出变了而键不换 = 模板句永久旧速音频。同族补键。
            sr = self._cloud_rate(cloud)
            fp = (f"cloud:{str(cloud.get('voice') or 'alloy')}"
                  f":{str(cloud.get('model') or 'tts-1')}"
                  f":{str(cloud.get('response_format') or 'pcm')}"
                  f":{host}"
                  f":{speed_s}"
                  f":sr{sr or 0}")
            # 深审 R2 #2（P5 的运行时维度缺口）：钉扎窗口内各轮**实际产出是
            # 本地默认 sid28 兜底嗓**，但指纹纯配置推导=还是云键——干净 stop 的
            # 兜底音频被 HA 按云嗓键写进无 TTL 消息哈希盘缓存，解钉后模板句
            # 永久播兜底嗓（现场"两个音色"以缓存形态复发，仅 clear_cache 可
            # 解）。钉扎期键加 :fb 后缀隔离兜底音频；置位/解除经 on_fp_change
            # 推送轮换（main._rotate_voice_fp 幂等去重）。残余窗口：设钉的
            # 第一轮在键尚为裸云键时开跑，该轮兜底音频仍入云键缓存——HA 键在
            # 请求开始即定，无法追溯；后续轮全部隔离，且解钉即轮换。
            if self._pin_active():
                fp += ":fb"
            return fp
        tag = "u"
        prov = self._provider()
        try:
            entry = self.store.lock_entry(PROVIDER_MODEL_KEYS[prov]) if self.store else {}
            tag = str(entry.get("sha256") or "")[:8] or "u"
        except Exception:  # noqa: BLE001 假件 store/异常形制：指纹照出，回落 u
            pass
        # v1.1.5：引擎档入指纹前缀——Kokoro 保持 "local:"（存量钉与 HA 盘缓存
        # 键不轮换），matcha/melo 各带本名；换引擎=换嗓=换键，模板句必重合成。
        prefix = "local" if prov == "local_kokoro" else prov
        # v1.0.65（深审 F6）：自定义区并入**内容摘要**（merge 已算好的官方包+
        # 各音色 (name,size,mtime) 指纹串的短哈希）——旧版只取数量 c{len}，
        # 同名重传改良版 bin（数量/sid/模型 lock sha 全不变）指纹不动 →
        # HA 盘缓存（无 TTL）模板句永久 v1 嗓。c{len}+h{hash8}：数量与内容双钉。
        cfp = getattr(self, "_custom_fp", "")
        ch = hashlib.sha1(cfp.encode("utf-8")).hexdigest()[:8] if cfp else "0"
        return (f"{prefix}:sid{self.resolve_sid()}+c{len(self._custom_sids)}h{ch}"
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
            # 深审 R2 #2：配置热更解钉同样要推键回收（:fb → 裸云键）
            self._notify_fp_change()

    # ── 生命周期 ────────────────────────────────────────────────
    @staticmethod
    def _model_gen_key(main_path: str, voices_path) -> tuple:
        """v1.0.85（P6a）：模型代次指纹（主模型+voices 的 size/mtime_ns）。
        同代=省电档卸载再载（输出逐比特一致是既有定案，缓存可留）；任何文件
        替换（导入口/音色上传+重载）两个度量几乎必变→按换代清缓存。"""
        def _st(p):
            if not p:
                return None
            try:
                s = Path(p).stat()
                return (s.st_size, s.st_mtime_ns)
            except OSError:
                return "gone"
        return (str(main_path or ""), _st(main_path), _st(voices_path))

    def ready(self) -> bool:
        return self._tts is not None

    def _pool(self) -> ThreadPoolExecutor:
        """专用合成/编码池（懒建，见 __init__ ⑤注释）。"""
        if self._exec is None:
            with self._exec_lock:
                if self._exec is None:
                    self._exec = ThreadPoolExecutor(
                        max_workers=4, thread_name_prefix="huijian-tts")
        return self._exec

    def ensure_loaded(self) -> bool:
        # 深审 R2 #6（F3 残留邻接）：把下载挪出 `_lock` 解决了"持锁跨下载"，
        # 但没解决"等待者堆积占线程"——store.ensure 的 per-key 锁是**阻塞等待**
        # 语义（实读 model_store.py:222-230），冷下载（348MB、分钟级）窗口内
        # 播报 miss 轮/试听连点各漏占一条 executor 线程陪等：4C 机默认池 8
        # 线程堆满 = asr/textcnn/to_thread 全排队，F3 想根治的"下载中饿死
        # STT"以新形态存活。引擎级单飞：已在飞行，后来者**立即**回 False
        # （本轮按"模型未就绪"截尾收束，日志点名），不陪等不占线程。
        # v1.1.5：判据从"有引擎"收紧为"有**当前档**引擎"——web 切 provider 后
        # 旧引擎在载也必须走门（inner 里做在飞避让式换绑）。
        if self.ready_for_current_provider():
            return True
        with self._load_gate:
            if self._loading:
                logger.warning("[TTS] 模型加载/下载已在飞行（他人轮次），"
                               "本轮按未就绪收束，不再排队占线程")
                return False
            self._loading = True
        try:
            ok = self._ensure_loaded_inner()
        finally:
            with self._load_gate:
                self._loading = False
        if ok:
            # 深审 R2 #7：加载态是指纹的隐性输入（自定义表注入/复核改 sid），
            # 首载完成若不重算推送，welcome 的冷值（c0h0/sid 回落 28）会一直
            # 骑到下一次 save/重连——同键先后两种嗓=缓存在嗓上漂移。
            self._notify_fp_change()
        return ok

    def _ensure_loaded_inner(self) -> bool:
        # v1.0.65（深审 F3）：store.ensure 的冷下载（kokoro 包 348MB、分钟级）
        # 挪出引擎 `_lock`——旧版持锁跨下载，期间每个并发 _synth/试听/ensure 各
        # 占 1 个 executor 线程堵在锁上（默认池 8 线程），云档不预载 = 首次试听
        # 即"下载中全栈饿死 STT"。store 的 per-key single-flight（其 F2 纪律）
        # 本就防重复下载，本锁只护对象换装。
        prov = self._provider()
        if self._tts is not None and self.loaded_provider() == prov:
            return True
        if self._tts is not None:
            # v1.1.5 引擎换绑（web 切 provider）：在飞合成让位在载引擎先干完，
            # 本轮保持旧嗓（不断播报），下一轮 models 循环（≤60s）再换。
            with self._lock:
                if self._busy or self._round_busy:
                    logger.info("[TTS] 引擎换绑 %s→%s 推迟（合成/整轮在飞），下一轮重试",
                                self.loaded_provider(), prov)
                    return True
                self._tts = None
            self._cache.clear()
            self._cache_bytes = 0
            logger.warning("[TTS] 引擎换绑：%s → %s（句级缓存清空，跨引擎音频不作废即用）",
                           self.loaded_provider(), prov)
        key = PROVIDER_MODEL_KEYS[prov]
        d = self.store.model_dir_for(key)
        if not d:
            try:
                if self.store.ensure(key):
                    d = self.store.model_dir_for(key)
            except Exception as e:  # noqa: BLE001 下载异常按未就绪上报，不穿锁
                logger.error("[TTS] %s 模型下载异常: %s", key, e)
        if not d:
            # 现场 grep 口径（v1052 钉）：kokoro 行字面量不得改写，他档另起新行
            if prov == "local_kokoro":
                logger.error("[TTS] kokoro 模型未就绪")
            else:
                logger.error("[TTS] %s 模型未就绪", key)
            return False
        with self._lock:
            if self._tts is not None and \
                    self.loaded_provider() == prov:   # double-check
                return True
            t_load = time.perf_counter()          # v1.0.52：一次性加载耗时观测
            try:
                import sherpa_onnx as so
                model_cfg = so.OfflineTtsModelConfig()
                main = None
                voices_path = None
                self._custom_sids, self._custom_fp = {}, ""   # 非 kokoro 档不注入
                if prov == "local_matcha":
                    # Matcha 中英（dengcunqin 导出）：声学+独立声码器，单说话人。
                    # vocos 缺失=sherpa C++ 构造终止进程（台架实锤），required_files
                    # 已在 lock 硬闸，这里再显式存在性检查兜手动导入的残缺树。
                    if not (d / "vocos-16khz-univ.onnx").exists():
                        logger.error("[TTS] matcha 缺声码器 vocos-16khz-univ.onnx"
                                     "（模型页重新下载或放 import/）")
                        return False
                    k = so.OfflineTtsMatchaModelConfig()
                    main = d / "model-steps-3.onnx"
                    k.acoustic_model = str(main)
                    k.vocoder = str(d / "vocos-16khz-univ.onnx")
                    k.tokens = str(d / "tokens.txt")
                    k.lexicon = str(d / "lexicon.txt")
                    if (d / "espeak-ng-data").is_dir():
                        k.data_dir = str(d / "espeak-ng-data")
                    model_cfg.matcha = k
                    fsts = [f for f in ("number-zh.fst", "date-zh.fst", "phone-zh.fst")
                            if (d / f).exists()]
                elif prov == "local_melo":
                    k = so.OfflineTtsVitsModelConfig()
                    # fp32 优先（音质定案同 Kokoro 弃 int8 口径）；仅 fp32 缺失才回落
                    main = d / "model.onnx"
                    if not main.exists():
                        main = d / "model.int8.onnx"
                    k.model = str(main)
                    k.tokens = str(d / "tokens.txt")
                    k.lexicon = str(d / "lexicon.txt")
                    if (d / "dict").is_dir():
                        k.dict_dir = str(d / "dict")
                    if (d / "espeak-ng-data").is_dir():
                        k.data_dir = str(d / "espeak-ng-data")
                    model_cfg.vits = k
                    fsts = [f for f in ("number.fst", "date.fst", "phone.fst",
                                        "new_heteronym.fst") if (d / f).exists()]
                else:
                    k = so.OfflineTtsKokoroModelConfig()
                    # 两代命名都认：int8 包主模型叫 model.int8.onnx、fp32 包叫
                    # model.onnx（现役定案=v1.1 fp32；导入口放哪种都收，审查换包免改码）。
                    main = d / "model.int8.onnx"
                    if not main.exists():
                        main = d / "model.onnx"
                    k.model = str(main)
                    # 自定义音色注入（投递口 const.TTS_VOICES_DIR）
                    voices_path = str(d / "voices.bin")
                    try:
                        _off_n = self._voices_count(key)
                        merged, names, skipped, cont_fp = merge_custom_voices(
                            d / "voices.bin", const.TTS_VOICES_DIR,
                            d / "voices_custom_merged.bin", _off_n)
                        if names:
                            voices_path, self._custom_sids = str(merged), names
                            self._custom_fp = cont_fp
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
                    model_cfg.kokoro = k
                    fsts = [f for f in ("number-zh.fst", "date-zh.fst", "phone-zh.fst")
                            if (d / f).exists()]
                model_cfg.num_threads = _engine_threads()   # v1.0.92：默认仍 2，可 env 显式抬
                model_cfg.provider = "cpu"
                cfg = so.OfflineTtsConfig(model=model_cfg)
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
                # v1.1.5：判据仅 Kokoro——melo 包结构本就不带 espeak-ng-data
                # （英文走自身 lexicon），matcha 带之（2026-09-21 冒烟实锤误报）。
                if prov == "local_kokoro" and not (d / "espeak-ng-data").is_dir():
                    logger.error("[TTS] 模型目录缺 espeak-ng-data/：含英文字母的句子"
                                 "将合成失败，请重导 kokoro-multi-lang 完整包")
                self._tts = tts
                self._loaded_prov = prov
                # v1.0.85（P6a）：旧形态每次成功加载都无条件清缓存——与 unload
                # 侧「同代重载输出逐比特一致、缓存刻意保留（省电档秒回旧帧）」
                # 的承诺直接矛盾：省电档首个含 miss 的轮一过，整张 LRU 白丢。
                # 代次指纹=主模型+voices 的 (size, mtime)；上传换嗓走「重载模型」
                # =unload→load，内容变指纹必变→照清，语义与旧行为在换代面等价。
                _gk = self._model_gen_key(str(main), voices_path)
                if _gk == getattr(self, "_loaded_gen_key", None):
                    logger.info("[TTS] 同代模型重载（省电档回来），保留句级缓存")
                else:
                    self._cache.clear(); self._cache_bytes = 0   # 换代模型：旧音频作废
                self._loaded_gen_key = _gk
                self.last_used = time.time()
                if prov == "local_kokoro":
                    logger.warning("[TTS] Kokoro multi-lang 已加载（%d 音色），sid=%s",
                                   tts.num_speakers,
                                   self.settings.get("tts.sid", _DEFAULT_SID))
                else:
                    _lbl = {"local_matcha": "Matcha zh-en",
                            "local_melo": "MeloTTS zh_en"}[prov]
                    logger.warning("[TTS] %s 已加载（%d 音色），sid=%s",
                                   _lbl, tts.num_speakers,
                                   self.settings.get("tts.sid", _default_sid_for(prov)))
                # v1.0.52：加载耗时独立成行（现场"首句慢"是加载还是合成，一眼可辨）
                logger.info("[TTS] Kokoro 引擎就绪，耗时 %dms",
                            int((time.perf_counter() - t_load) * 1000))
                # v1.0.92（引擎提速开放项，独立新行不改上面既有字面量——
                # 现场 grep 口径受 test_v1052 钉保护）：带出实际生效线程数，
                # 抬没抬 HUIJIAN_TTS_THREADS 一眼可辨，不再靠猜配置漂移归因。
                logger.info("[TTS] Kokoro 推理线程 threads=%d", model_cfg.num_threads)
                return True
            except Exception as e:
                logger.error("[TTS] 加载失败: %s", e)
                self._tts = None
                return False

    def unload(self) -> bool:
        with self._lock:
            if self._busy or self._round_busy:
                # v1.0.84（B1）：_round_busy=有播报轮整体在飞（句间空隙也算）
                logger.info("[TTS] 合成/整轮在飞，本轮跳过卸载")
                return False
            self._tts = None
            # 缓存刻意不清：省电档卸载后，高频模板句仍可秒回旧帧（同代模型
            # 重载输出逐比特一致；换代路径 ensure_loaded 已负责清空）。
            logger.warning("[TTS] 模型已卸载（省电档）")
            return True

    # ── 合成 ────────────────────────────────────────────────────
    async def stream_opus(self, text: str, engine_out: dict | None = None) -> AsyncIterator[bytes]:
        """v1.0.84（B1）：薄包装=整轮在飞闸。aclose/异常/正常耗尽三径均由
        finally 释放；session 侧「每 detect 必有 aclose 收口」的既有纪律
        （v1.0.48）保证本 finally 确定性落地，不赌 GC。"""
        self._round_busy += 1
        try:
            async for pkt in self._stream_opus_round(text, engine_out):
                yield pkt
        finally:
            self._round_busy -= 1

    async def _stream_opus_round(self, text: str, engine_out: dict | None = None) -> AsyncIterator[bytes]:
        """逐句产裸 opus 帧（16k/mono/60ms）。云档失败回落本地（v4.1-②）。

        engine_out：可选出参 dict——本轮实际由哪个引擎发声写回
        engine_out["engine"]（"cloud:voice名" / "local:sid N"）。云⇄本地
        回落是**换嗓**的（云端可配男声、本地默认 sid28 女声），现场"第一句
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
            if self._pin_active():
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
                    # 深审 R2 #2：解除即推裸云键（:fb 隔离期结束，恢复云嗓）
                    self._notify_fp_change()
                    # v1.0.84（O4）：云"干净收尾但只合成前半"（服务端限长、
                    # 字节自洽）旧形态记完整成功+入 HA 无 TTL 盘缓存=同句永久
                    # 缺尾。比值闸：实际秒数 vs 文本/语速预期（4.5 字/s 账同
                    # 固件 v2.1.44 语速自适应），<0.35× 按缺尾收口并开钉；
                    # <60 字豁免（英文/符号/URL 文本预期虚高，防误杀正常轮）。
                    audio_s = cloud_frames * const.FRAME_MS / 1000.0
                    exp_s = len(text) / (4.5 * max(self._speed(), _SPEED_MIN))
                    if len(text) >= 60 and audio_s < 0.35 * exp_s:
                        logger.warning(
                            "[TTS] 云产出时长/文本比异常（%.1fs vs 预期≈%.0fs，"
                            "%d 字）→ 按云端限长缺尾收口（truncated+钉扎）: %r",
                            audio_s, exp_s, len(text), text[:30])
                        if engine_out is not None:
                            engine_out["truncated"] = True
                        self._cloud_fail_ts = time.monotonic()
                        self._notify_fp_change()
                    return
                except Exception as e:
                    # 断在首帧前或半途都记失败时刻、开启钉扎窗口。
                    self._cloud_fail_ts = time.monotonic()
                    # 深审 R2 #2：置钉即推 :fb 键——兜底嗓音频从此与云键隔离
                    # （半途断流轮同理：其已发帧走 truncated，不入缓存）。
                    self._notify_fp_change()
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
        # v1.1.5：句级缓存键含引擎档——换绑让位窗口内旧引擎在载时，同 (句,sid,
        # 语速) 不得跨引擎复用音频；引擎维度入键，宁缺不混。
        prov_key = self._provider()
        # 音色归属定案（用户 2026-09-18）：本地档=web 设定音色（数字或自定义主名）；
        # 云档=云端对应音色（可选）；**云失败回落=固定默认本地音色 sid28**——回落是
        # 应急通道，取最保守单音色，不跟 web 配置（可能是自定义名/云专属号）漂移。
        # （默认音色身份自 2026-09-19 拍板起=zf_044/sid28，见 _DEFAULT_SID。）
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
            key = (prov_key, sent, sid, speed)
            hit = self._cache_get(key)
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
                ok = await loop.run_in_executor(self._pool(), self.ensure_loaded)
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
                        key = (prov_key, sent, sid, speed)   # 复核后本句键重算
                        hit = self._cache_get(key)
                        if hit is not None:
                            self._cache.move_to_end(key)
                            self.cache_hits += 1
                            self.last_used = time.time()
                            for pkt in hit[0]:
                                local_first_ms = self._note_local_first(t_turn, local_first_ms)
                                yield pkt
                            continue
            pcm16 = await loop.run_in_executor(self._pool(), self._synth, sent, sid, speed)
            if not pcm16:
                # v1.0.55（定案②）：_synth 吞错产空句=整段播报缺一句，此前静默
                # continue、半截音频以普通 stop 收口——HA 把缺尾音频当完整结果
                # 缓存，同一句永久缺一截且不自愈。改为自报截断（缺一句也报）。
                if engine_out is not None:
                    engine_out["truncated"] = True
                logger.warning("[TTS] 句子合成空产出，本轮声明截断: %r", sent[:30])
                continue
            # F5：整句 opus 编码是 CPU 活，出事件循环
            packets = await loop.run_in_executor(self._pool(), self._encode, pcm16)
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

    def _cache_get(self, key: tuple):
        """v1.0.65（深审 F14）：cache_enabled=False 语义=不吃缓存。旧版只停写
        不停读，既有 ≤256 条内存缓存照常命中（且 unload 刻意保留、跨重启盘缓存
        另算）——用户"关缓存求新鲜合成"只对新句生效，旧句仍旧嗓，语义不完整。
        读路径与写路径 `_cache_put` 同闸（顺带 miss 时清掉存量，关一次即净）。"""
        if not self.settings.get("tts.cache_enabled", True):
            if self._cache:
                self._cache.clear()
                self._cache_bytes = 0
            return None
        return self._cache.get(key)

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
            # 深审 R2 #1b：排队必须有界——前手若是坏 speed 的小时级 generate
            # 或冷下载，无限排队=每个后来者漏占一条池线程（正是 F3 要根治的
            # 池尽形态）。超时按本句失败收（空产→truncated/礼貌失败）。
            if not self._gen_lock.acquire(timeout=_GEN_WAIT_S):
                logger.warning("[TTS] 合成排队超 %gs（引擎被长任务占用），"
                               "本句按失败收束，不占用池线程陪等", _GEN_WAIT_S)
                return b""
            try:
                audio_obj = tts.generate(sent, sid=sid, speed=speed)
            finally:
                self._gen_lock.release()
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
        # v1.0.85（P5）：与播报轮同闸——旧形态试听不在 _round_busy 内，
        # 句间被省电档 reaper 卸模型 → 快照 None 空产、半截 wav 无告警发回。
        self._round_busy += 1
        try:
            return await self._synthesize_pcm_round(text)
        finally:
            self._round_busy -= 1

    async def _synthesize_pcm_round(self, text: str) -> bytes:
        loop = asyncio.get_running_loop()
        if not self.ready() and not await loop.run_in_executor(self._pool(), self.ensure_loaded):
            return b""
        sid = self.resolve_sid()
        speed = self._speed()
        out = b""
        for sent in split_sentences(text):
            out += await loop.run_in_executor(self._pool(), self._synth, sent, sid, speed)
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
        # （v1.0.65 F10：经 _cloud_rate 消毒——脏值不再 int() 炸链误钉扎）
        sr = self._cloud_rate(cloud)
        if sr:
            body["sample_rate"] = sr
        # v1.0.52：分段超时（原本 total=30 一把梭）。speed 仍走请求体，未动。
        timeout, first_byte_s = self._cloud_timeout(cloud)
        default_rate = sr or self._CLOUD_RATE_DEFAULT
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
                    # v1.0.65（F12）：错误体截断读（旧版 r.text() 整读后才 [:160]
                    # ——base_url 误指大文件服务/慢速滴流端点时无上限进内存）。
                    err_body = ""
                    with contextlib.suppress(Exception):
                        err_body = (await r.content.read(8192)).decode(
                            errors="replace")
                    raise RuntimeError(f"HTTP {r.status}: {err_body[:160]}")
                # 深审 R2 #11（F13 增强）：起手符号白名单可绕（"Error: ..."、
                # "rate limit exceeded" 类纯文本不以 { [ < 起手）——Content-Type
                # 是更直接的判据，text/* 与 application/json 一律按"200 文本体"
                # 拒（噪声帧计成功→解钉→入无 TTL 缓存的路径从源头掐断）。
                # 音频 MIME（audio/* / application/octet-stream / 缺省）不受影响。
                ct = str(getattr(r, "content_type", "") or "").lower()
                if ct.startswith("text/") or ct.startswith("application/json"):
                    err_body = ""
                    with contextlib.suppress(Exception):
                        err_body = (await r.content.read(8192)).decode(
                            errors="replace")
                    raise RuntimeError(
                        f"云 TTS 返回 200 但 Content-Type={ct} 非音频"
                        f"（疑似网关/代理错误页）：{err_body[:60]!r}")
                content = getattr(r, "content", None)
                if content is None or not hasattr(content, "iter_chunked"):
                    # 无流式 body 的响应对象（老式适配器/测试桩，非 aiohttp）：
                    # 退回整包路径，行为与旧版逐字一致（_unwrap_audio + _resample_encode）
                    raw = await r.read()
                    pcm, src_rate = self._unwrap_audio(raw, default_rate)
                    loop = asyncio.get_running_loop()
                    for pkt in await loop.run_in_executor(
                            self._pool(), self._resample_encode, pcm, src_rate):
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
            pos, sr, bits, nch, data = 12, 0, 0, 0, None
            while pos + 8 <= len(raw):
                cid = raw[pos:pos + 4]
                csz = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
                body = raw[pos + 8:pos + 8 + csz]
                if cid == b"fmt " and len(body) >= 16:
                    sr = struct.unpack("<I", body[4:8])[0]
                    nch = struct.unpack("<H", body[2:4])[0]
                    bits = struct.unpack("<H", body[14:16])[0]
                elif cid == b"data":
                    data = body
                    break
                pos += 8 + csz + (csz & 1)      # 块按偶数字节对齐
            if data is None or not sr:
                raise RuntimeError("云 TTS wav 头损坏（缺 fmt/data 块）")
            # 深审 R2 #3/#4：与流式 _parse_riff 同闸（声道/合理域）。
            if nch > 1:
                raise RuntimeError(
                    f"云 TTS wav 声道数 {nch} 不支持（仅单声道）")
            if not _RATE_MIN <= sr <= _RATE_MAX:
                raise RuntimeError(
                    f"云 TTS wav 采样率 {sr} 超出合理域 [{_RATE_MIN},{_RATE_MAX}]")
            if bits != 16:
                raise RuntimeError(f"云 TTS wav 位深 {bits} 不支持（仅 16-bit）")
            return data, sr
        if raw[:4] == b"RIFF":
            # 深审 R2（A1 收口）：12–43B 的 RIFF 残响应——旧版 >=44 门槛让它
            # 掉进裸 PCM 缺省分支产垃圾帧还记"云成功"，与 _decide 短响应闸
            # 同判据（流式路同形态在 sniff 即炸，两路必须同规）。
            raise RuntimeError(
                f"云 TTS wav 响应过短（共 {len(raw)}B，RIFF 容器不完整）")
        if why := _unsupported_format(raw):
            raise RuntimeError(why)
        # v1.0.65（F13 同闸）：200+文本体不得当裸 PCM（判据与流式 _decide 一致）
        printable = sum(1 for b in raw[:12] if 32 <= b < 127 or b in (9, 10, 13))
        if raw[:1] in (b"{", b"[", b"<") and printable >= len(raw[:12]) * 0.85:
            raise RuntimeError(
                "云 TTS 返回 200 但正文是文本而非音频（疑似网关/代理错误页）："
                f"{raw[:60].decode(errors='replace')!r}")
        return raw, default_rate

    @staticmethod
    def _resample_encode(raw: bytes, src_rate: int) -> list:
        pcm16k = audio.resample_pcm16(raw, src_rate, const.SAMPLE_RATE)
        return list(audio.OpusPcmEncoder("voip").encode_stream(pcm16k))
