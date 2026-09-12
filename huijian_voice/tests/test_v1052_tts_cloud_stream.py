"""v1.0.52 加载项侧钉桩：云 TTS **流式化** + 分段超时 + 首帧逐跳可观测。

范围：只钉 `core/tts.py` + `core/settings.py`（云档读路径）。
custom_components 侧（卫星/实体下行流式）由姊妹文件
`tests/test_v1052_tts_stream.py` 覆盖，本文件不涉。

现场病根（云档）：`TtsEngine._cloud_stream` 走的是
    raw = await r.read()  →  _unwrap_audio(raw)  →  _resample_encode(pcm, rate)
整包读完再拆封/重采样/编码——平台"先憋一会儿再吐"时，整段静音 = 整包网络时长；
超时又只有一个 `ClientTimeout(total=30, connect=8)`，云端点停摆要白等 30s 才抛，
云→本地回落形同虚设；且首包延迟全链无日志可对账。

修法：
  · `_cloud_stream` 改 `content.iter_chunked()` 增量读：RIFF 头增量解析到 data
    块，载荷即刻进**有状态**重采样（跨块保留滤波历史），攒满 60ms 即出 opus 包；
  · 超时拆 connect / first_byte / read(sock_read) / total 四段，全部可由
    `tts.cloud.*_timeout_s` 配置；
  · 新增日志：云首字节/首帧、模型未就绪本轮等待、Kokoro 加载耗时、本地首帧。

本文件钉：
 ① 真增量（含"旧整包形态必挂同一断言"的 A/B 反证）；
 ② 超时四段从 settings 取值、默认无 30s 硬闸、脏值回落默认；
 ③ 有状态重采样跨块不丢样（对照整包实现容差内一致 + 有牙的反例对照）；
 ④ RIFF 增量拆封（奇数块/前缀不足/块头跨块）且头字节绝不入音频；
    不可解码容器显式失败以便云→本地回落；
 ⑤ 首帧遥测形状钉 + 慢首块/本地等待/回落档行为钉；
 ⑥ 增量解码器单元钉（状态机边界）。
"""
import asyncio
import contextlib
import logging
import math
import os
import struct
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
import numpy as np
import pytest

# 锚 __file__：本仓有"相对路径只在某个 cwd 下能用"的踩坑史——
# `cd huijian_voice && pytest tests` 与仓根 `pytest huijian_voice/tests` 都必须绿。
ROOT = Path(__file__).resolve().parents[1]
TTS_SRC = ROOT / "core" / "tts.py"
SETTINGS_SRC = ROOT / "core" / "settings.py"
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="huijian_v1052_cloud_"))

from core import audio, const                                    # noqa: E402
from core.tts import TtsEngine, _CloudOpusStream, _StreamResampler   # noqa: E402

FRAME = const.FRAME_BYTES                    # 1920B = 60ms s16le@16k


def arun(coro):
    """跑完协程并**收干净**事件循环（含被放弃的异步生成器）。

    超时/格式报错路径会中途抛掉响应体迭代器，若直接丢 loop 会留一条
    "coroutine method 'aclose' was never awaited" 噪声警告（现场排障时
    警告噪声=干扰项，能消就消）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


def collect(agen):
    async def go():
        return [x async for x in agen]
    return go()


# ── 测试替身：可计时的流式响应 ───────────────────────────────────
class Ledger:
    """事件账本：把"块到了"与"包出了"按**发生顺序 + 发生时刻**记在一条时间线上。

    增量性的判据不能只看"有没有包"（整包形态也有包），必须看**先后**：
    首包是否出现在最后一块到达**之前**。顺序与时间双证据。"""

    def __init__(self):
        self.ev = []
        self.t0 = time.perf_counter()

    def mark(self, kind):
        self.ev.append((kind, time.perf_counter() - self.t0))

    def _idx(self, kind):
        return [i for i, (k, _) in enumerate(self.ev) if k == kind]

    def pos_first(self, kind):
        idx = self._idx(kind)
        return idx[0] if idx else None

    def pos_last(self, kind):
        idx = self._idx(kind)
        return idx[-1] if idx else None

    def first_t(self, kind):
        return next((t for k, t in self.ev if k == kind), None)

    def last_t(self, kind):
        return next((t for k, t in reversed(self.ev) if k == kind), None)


class FakeContent:
    """模拟 aiohttp StreamReader 的增量体：逐块产出，可加块间/首块延迟。"""

    def __init__(self, chunks, ledger=None, delay=0.0, first_delay=None):
        self.chunks = list(chunks)
        self.ledger = ledger
        self.delay = delay
        self.first_delay = first_delay

    def iter_chunked(self, n):
        async def gen():
            for i, c in enumerate(self.chunks):
                wait = self.first_delay if (i == 0 and self.first_delay) else self.delay
                if wait:
                    await asyncio.sleep(wait)
                if self.ledger is not None:
                    self.ledger.mark("chunk")
                yield c
        return gen()


class FakeResponse:
    """带 content.iter_chunked 的响应（新流式路径）；同时保留 read()/text()。"""

    def __init__(self, chunks, ledger=None, delay=0.0, first_delay=None, status=200):
        self.status = status
        self.content = FakeContent(chunks, ledger, delay, first_delay)
        self._body = b"".join(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return self._body

    async def text(self):
        return "boom"


def fake_session(resp, calls=None):
    class _Sess:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, headers=None, **kw):
            if calls is not None:
                calls.append({"url": url, "json": json, "headers": headers})
            return resp

    return _Sess()


class _Settings:
    def __init__(self, data):
        self._d = data

    def get(self, dotted, default=None):
        return self._d.get(dotted, default)


def engine(cloud, speed=1.0, provider="cloud_openai_compat"):
    return TtsEngine(_Settings({"tts.cloud": cloud, "tts.speed": speed,
                                "tts.provider": provider}), None)


def stub_local(eng, packets=(b"LOCALPKT",)):
    """把本地档桩化（离线）——只验"云失败→回落本地"的接线，不做真合成。"""
    eng.ensure_loaded = lambda: True
    eng._synth = lambda sent, sid, speed: b"\x00\x00" * 480
    eng._encode = lambda pcm: list(packets)
    return eng


# ── 信号/工具 ───────────────────────────────────────────────────
def tone(n_samples, rate=24000, freq=440.0):
    """n_samples 个 440Hz 正弦的 s16le（留幅度余量，避免裁顶掩盖重采样差异）。"""
    t = np.arange(n_samples) / float(rate)
    return (np.sin(2 * math.pi * freq * t) * 20000).astype(np.int16).tobytes()


def make_wav(rate=24000, data=b"", bits=16, extra=None):
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * bits // 8, bits // 8, bits)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    if extra is not None:
        cid, body = extra
        chunks += cid + struct.pack("<I", len(body)) + body
        if len(body) % 2:
            chunks += b"\x00"                 # 块按偶数字节对齐
    chunks += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def odd_chunks(data, sizes=(7, 13, 29, 31, 1021, 4093)):
    """按**奇数尺寸**循环切块：逼块头/半样本跨块（网络块边界本就是任意的）。"""
    out, i, k = [], 0, 0
    while i < len(data):
        n = sizes[k % len(sizes)]
        out.append(data[i:i + n])
        i += n
        k += 1
    return out


def frames_for(pcm16k_bytes):
    return (len(pcm16k_bytes) + FRAME - 1) // FRAME


def pcm16k_of(raw, rate):
    return audio.resample_pcm16(raw, rate, const.SAMPLE_RATE)


def maxdiff(a, b):
    n = min(len(a), len(b))
    if not n:
        return 0
    x = np.frombuffer(a[:n], dtype=np.int16).astype(np.int32)
    y = np.frombuffer(b[:n], dtype=np.int16).astype(np.int32)
    return int(np.abs(x - y).max())


@contextlib.contextmanager
def capture_logs(level=logging.INFO):
    """直接挂到 huijian.tts 上抓（不赌 root 传播/propagate 配置）。"""
    log = logging.getLogger("huijian.tts")
    lines = []

    class _Spy(logging.Handler):
        def emit(self, record):
            try:
                lines.append(record.getMessage())
            except Exception:                 # 格式化炸了也不能拖垮被测路径
                lines.append(str(record.msg))

    spy, old = _Spy(), log.level
    log.addHandler(spy)
    log.setLevel(level)
    try:
        yield lines
    finally:
        log.removeHandler(spy)
        log.setLevel(old)


# ══ ① 真增量（本文件的核心 A/B 钉）══════════════════════════════
def test_cloud_stream_first_packet_precedes_last_chunk(monkeypatch):
    """首包必须出现在**最后一个块到达之前**——顺序证据 + 时间证据双钉。"""
    led = Ledger()
    raw = tone(24000)                                   # 1s@24k
    chunks = [raw[i:i + 4096] for i in range(0, len(raw), 4096)]
    resp = FakeResponse(chunks, led, delay=0.02)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kw: fake_session(resp))
    eng = engine({"base_url": "https://x.example/v1"})

    async def go():
        out = []
        async for p in eng._cloud_stream("你好"):
            led.mark("pkt")
            out.append(p)
        return out

    pkts = arun(go())
    assert len(pkts) == frames_for(pcm16k_of(raw, 24000)) == 17, "增量化不得丢帧/多帧"
    assert led.pos_first("pkt") is not None and led.pos_last("chunk") is not None
    assert led.pos_first("pkt") < led.pos_last("chunk"), \
        "首包晚于最后一块 = 仍是整包语义"
    assert led.first_t("pkt") < led.last_t("chunk"), \
        "时间线上首包必须早于最后一块（真·边收边出）"


def test_old_batch_form_cannot_satisfy_incremental_assertion(monkeypatch):
    """A/B 反证：同一份假响应，**旧整包形态**必挂同一条断言。

    没有这条对照，上一条可能只是"碰巧过了"。这里把旧实现（read() 语义 =
    先把所有块收干，再 _unwrap_audio + _resample_encode）原样跑一遍，证明它在
    本桩下**不可能**满足增量判据。"""
    led = Ledger()
    raw = tone(24000)
    chunks = [raw[i:i + 4096] for i in range(0, len(raw), 4096)]
    resp = FakeResponse(chunks, led, delay=0.02)

    async def old_form():
        body = b""
        async for c in resp.content.iter_chunked(const.FRAME_BYTES):
            led.mark("chunk")
            body += c
        pcm, rate = TtsEngine._unwrap_audio(body, 24000)
        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(None, TtsEngine._resample_encode, pcm, rate)
        for _ in out:
            led.mark("pkt")
        return out

    pkts = arun(old_form())
    assert len(pkts) == 17
    assert led.pos_first("pkt") > led.pos_last("chunk"), \
        "旧形态本就不可能增量——这里若成立说明对照桩失效"
    assert not (led.first_t("pkt") < led.last_t("chunk"))


def test_streaming_output_is_bytewise_identical_to_batch(monkeypatch):
    """流式与整包（_resample_encode）逐比特同规格：16k/mono/60ms 裸 opus。"""
    raw = tone(35711)                                   # 非整帧长度 → 逼尾帧零填充
    chunks = [raw[i:i + 999] for i in range(0, len(raw), 999)]
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda **kw: fake_session(FakeResponse(chunks)))
    eng = engine({"base_url": "https://x.example/v1"})

    streamed = arun(collect(eng._cloud_stream("你好")))
    batch = TtsEngine._resample_encode(raw, 24000)
    assert streamed and streamed == batch, "流式输出必须与整包路径逐字节一致"


def test_streamed_frames_are_60ms_16k(monkeypatch):
    """协议 §2 硬约束钉：每个包解码回来必须是 960 样本@16k（=1920B）。"""
    if audio._load_opus() is None:
        pytest.skip("本机无 libopus 绑定")
    raw = tone(24000, rate=44100)                       # 源率换一个，走重采样支路
    chunks = [raw[i:i + 3000] for i in range(0, len(raw), 3000)]
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda **kw: fake_session(FakeResponse(chunks)))
    eng = engine({"base_url": "https://x.example/v1", "sample_rate": 44100})

    pkts = arun(collect(eng._cloud_stream("你好")))
    assert pkts
    dec = audio.OpusPcmDecoder()
    for p in pkts:
        assert len(dec.decode(p)) == FRAME, "帧长不是 60ms/960 样本"


# ══ ② 超时四段拆分 ════════════════════════════════════════════
def test_cloud_timeout_defaults_split_no_hard_30s():
    ct, first = TtsEngine._cloud_timeout({})
    assert ct.total != 30, "默认不得再是整包 30s 硬闸"
    assert ct.total == 120.0
    assert ct.connect == 8.0
    assert ct.sock_read == 10.0
    assert first == 6.0
    assert ct.total > const.TTS_STREAM_BUDGET_S, \
        "总闸必须宽于整流预算 55s：慢而持续产出的合成不得被误砍"


def test_cloud_timeout_parts_come_from_settings():
    ct, first = TtsEngine._cloud_timeout({"connect_timeout_s": 2.5,
                                          "first_byte_timeout_s": 1.5,
                                          "read_timeout_s": 3.0,
                                          "total_timeout_s": 9.0})
    assert (ct.connect, ct.sock_read, ct.total) == (2.5, 3.0, 9.0)
    assert first == 1.5, "首字节不是 aiohttp 字段，必须由本函数单独返回"


def test_cloud_timeout_dirty_values_fall_back():
    """云档超时配置写坏不得导致失去保护（空/0/负数/脏串一律回默认）。"""
    ct, first = TtsEngine._cloud_timeout({"connect_timeout_s": "坏",
                                          "first_byte_timeout_s": 0,
                                          "read_timeout_s": None,
                                          "total_timeout_s": -1})
    assert (ct.connect, ct.sock_read, ct.total, first) == (8.0, 10.0, 120.0, 6.0)


def test_settings_defaults_carry_split_timeout_keys():
    src = SETTINGS_SRC.read_text(encoding="utf-8")
    for key in ('"connect_timeout_s": 8.0', '"first_byte_timeout_s": 6.0',
                '"read_timeout_s": 10.0', '"total_timeout_s": 120.0'):
        assert key in src, f"tts.cloud 默认缺 {key}"
    assert '"response_format": "", "sample_rate": 0' in src, "既有默认键不得动"


def test_settings_merge_keeps_defaults_for_old_settings_json():
    """老 settings.json 无这四个键：深合并必须补齐默认（不落盘改动无意义）。"""
    from core.settings import DEFAULTS
    cloud = DEFAULTS["tts"]["cloud"]
    assert cloud["connect_timeout_s"] == 8.0
    assert cloud["first_byte_timeout_s"] == 6.0
    assert cloud["read_timeout_s"] == 10.0
    assert cloud["total_timeout_s"] == 120.0


def test_stalled_endpoint_fails_fast_and_falls_back_to_local(monkeypatch):
    """停摆端：首字节闸几秒内（此处按配置 0.05s）失败 → 云→本地回落即时触发。"""
    raw = tone(24000)
    resp = FakeResponse([raw], first_delay=0.6)          # 连上了但迟迟不给首个数据块
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda **kw: fake_session(resp))
    eng = stub_local(engine({"base_url": "https://x.example/v1",
                             "first_byte_timeout_s": 0.05}))
    eo = {}
    t0 = time.perf_counter()
    out = arun(collect(eng.stream_opus("你好", engine_out=eo)))
    dt = time.perf_counter() - t0
    assert out == [b"LOCALPKT"], "停摆端必须干净失败并回落本地"
    assert "云回落" in eo["engine"], eo
    assert dt < 0.5, f"回落耗时 {dt:.2f}s：首字节闸未生效（旧实现要白等 30s）"


def test_first_byte_timeout_message_names_the_knob():
    """报错要指名可调项，现场才知道去哪儿改（不改就是盲猜）。"""
    eng = engine({"base_url": "https://x.example/v1", "first_byte_timeout_s": 0.05})
    resp = FakeResponse([tone(2400)], first_delay=0.6)
    aiohttp.ClientSession, old = (lambda **kw: fake_session(resp)), aiohttp.ClientSession
    try:
        with pytest.raises(RuntimeError) as ei:
            arun(collect(eng._cloud_stream("你好")))
    finally:
        aiohttp.ClientSession = old
    assert "首字节超时" in str(ei.value)
    assert "first_byte_timeout_s" in str(ei.value)


# ══ ③ 有状态重采样 ════════════════════════════════════════════
def test_stateful_resample_no_sample_loss_across_chunks():
    raw = tone(12000)                                   # 0.5s@24k → 8000 样本=16000B
    one = audio.resample_pcm16(raw, 24000, const.SAMPLE_RATE)
    assert len(one) == 16000
    for step in (480, 2400, 4000, 12000):
        r = _StreamResampler(24000, const.SAMPLE_RATE)
        out = b""
        for i in range(0, len(raw), step):
            out += r.feed(raw[i:i + step])
        out += r.flush()
        assert len(out) == len(one) == 16000, f"step={step} 长度不符 = 跨块丢样"
        assert maxdiff(out, one) <= 1, f"step={step} 与整包实现差异过大"


def test_stateful_resample_survives_odd_and_halfsample_chunks():
    """奇数字节块（s16 半样本跨块）也不得丢样——网络块边界是任意的。"""
    raw = tone(9000)                                    # 0.375s@24k
    one = audio.resample_pcm16(raw, 24000, const.SAMPLE_RATE)
    r = _StreamResampler(24000, const.SAMPLE_RATE)
    out = b""
    for c in odd_chunks(raw):
        assert len(c) % 2 == 1
        out += r.feed(c)
    out += r.flush()
    assert len(out) == len(one)
    assert maxdiff(out, one) <= 1


def test_stateful_resample_passthrough_when_rates_equal():
    """源率=16k（平台已按我们要求给 16k）→ 逐字节直通，与整包同语义。"""
    raw = tone(6000, rate=16000)
    r = _StreamResampler(16000, const.SAMPLE_RATE)
    out = b"".join(r.feed(c) for c in odd_chunks(raw, (1001, 3))) + r.flush()
    assert out == raw


def test_naive_per_chunk_wholebuffer_resample_would_lose_samples():
    """反例对照：每块单独走整包实现 = 块缝丢样（证明上两条钉有牙）。"""
    raw = tone(12000)
    one = audio.resample_pcm16(raw, 24000, const.SAMPLE_RATE)
    naive = b"".join(audio.resample_pcm16(raw[i:i + 4000], 24000, const.SAMPLE_RATE)
                     for i in range(0, len(raw), 4000))
    assert len(naive) != len(one) or maxdiff(naive, one) > 100


# ══ ④ RIFF 增量拆封 / 不可解码格式 ════════════════════════════
def test_riff_streamed_in_odd_chunks_is_incremental_and_header_free(monkeypatch):
    led = Ledger()
    payload = tone(24000)                               # 1s@24k
    wav = make_wav(rate=24000, data=payload, extra=(b"LIST", b"x" * 25))
    chunks = odd_chunks(wav)
    assert sum(len(c) for c in chunks) == len(wav)
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda **kw: fake_session(FakeResponse(chunks, led)))
    eng = engine({"base_url": "https://x.example/v1"})

    async def go():
        out = []
        async for p in eng._cloud_stream("你好"):
            led.mark("pkt")
            out.append(p)
        return out

    pkts = arun(go())
    ref = TtsEngine._resample_encode(payload, 24000)
    assert pkts == ref, "44B/扩展头一旦混进音频流水，逐字节等价不可能成立"
    assert len(pkts) == 17
    assert led.pos_first("pkt") < led.pos_last("chunk"), "wav 也必须边收边出"


def test_riff_server_rate_beats_declared_rate_in_stream(monkeypatch):
    """平台无视 response_format 回 wav 时，wav 头真实采样率优先（流式同语义）。"""
    payload = tone(4410, rate=8000)
    wav = make_wav(rate=8000, data=payload)
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda **kw: fake_session(FakeResponse(odd_chunks(wav))))
    eng = engine({"base_url": "https://x.example/v1", "sample_rate": 24000})
    assert arun(collect(eng._cloud_stream("你好"))) == \
        TtsEngine._resample_encode(payload, 8000)


@pytest.mark.parametrize("body,needle", [
    (b"ID3\x04\x00\x00" + b"j" * 64, "mp3"),
    (bytes([255, 251, 144, 0]) + b"0" * 64, "mp3"),
    (b"OggS\x00\x02\x00\x00" + b"j" * 64, "opus"),
])
def test_unsupported_container_fails_cleanly(monkeypatch, body, needle):
    """mp3/ogg → 显式报错（含"改平台输出格式"指令），绝不当 PCM 播成噪音。"""
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda **kw: fake_session(FakeResponse([body])))
    eng = engine({"base_url": "https://x.example/v1"})
    with pytest.raises(RuntimeError) as ei:
        arun(collect(eng._cloud_stream("你好")))
    assert needle in str(ei.value) and "pcm 或 wav" in str(ei.value)


def test_unsupported_container_triggers_cloud_to_local_fallback(monkeypatch):
    """失败必须让 stream_opus 的云→本地回落照旧起火（换嗓而非哑掉）。"""
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda **kw: fake_session(
                            FakeResponse([b"ID3\x04\x00" + b"j" * 64])))
    eng = stub_local(engine({"base_url": "https://x.example/v1"}))
    eo = {}
    out = arun(collect(eng.stream_opus("你好", engine_out=eo)))
    assert out == [b"LOCALPKT"] and "云回落" in eo["engine"], eo


def test_http_error_still_raises_before_any_body_read(monkeypatch):
    """非 200 仍在读体之前抛出（旧语义），且报文含状态码。"""
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda **kw: fake_session(FakeResponse([b"nope"], status=401)))
    eng = engine({"base_url": "https://x.example/v1"})
    with pytest.raises(RuntimeError) as ei:
        arun(collect(eng._cloud_stream("你好")))
    assert "401" in str(ei.value)


def test_legacy_readonly_response_falls_back_to_batch(monkeypatch):
    """无流式 body 的响应对象（老式适配器）退回整包路径，行为与旧版逐字一致。

    兼容支路必须在位：仓内既有 `tests/test_cloud_presets.py` 的桩响应只有
    read()/text()，这条支路是它与流式路径共存的唯一入口。"""
    class _NoContentResp:
        status = 200

        def __init__(self, payload):
            self._p = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read(self):
            return self._p

        async def text(self):
            return ""

    raw = tone(24000)
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda **kw: fake_session(_NoContentResp(raw)))
    eng = engine({"base_url": "https://x.example/v1"})
    assert arun(collect(eng._cloud_stream("你好"))) == TtsEngine._resample_encode(raw, 24000)


def test_speed_still_travels_in_request_body(monkeypatch):
    """speed 仍走请求体（本地档也用它），流式化不得把这一路弄丢。"""
    calls = []
    raw = tone(4800)
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda **kw: fake_session(FakeResponse([raw]), calls))
    eng = engine({"base_url": "https://x.example/v1"}, speed=1.2)
    arun(collect(eng._cloud_stream("你好")))
    assert calls[0]["json"]["speed"] == 1.2
    assert calls[0]["url"] == "https://x.example/v1/audio/speech"


# ══ ⑤ 首帧遥测 ════════════════════════════════════════════════
def test_telemetry_log_lines_present_and_old_ones_untouched():
    src = TTS_SRC.read_text(encoding="utf-8")
    for marker in ('"[TTS] 云首字节 %dms / 首帧 %dms"',
                   '"[TTS] 模型未就绪，本轮等待 %dms"',
                   '"[TTS] Kokoro 引擎就绪，耗时 %dms"',
                   '"[TTS] 本地首帧 %dms"'):
        assert marker in src, f"缺遥测行：{marker}"
    # 既有日志字面量（现场按这些 grep）一律不得改写
    # v1.0.55 同步：云失败话术随"钉扎"机制改版（回落语义与 [TTS] 云合成失败
    # 前缀不变，尾部改为明示钉扎秒数），现场 grep 口径保持可辨。
    for kept in ('"[TTS] 云合成失败(%s) → 回落本地默认音色，并钉扎本地 %.0f 秒"',
                 '"[TTS] Kokoro multi-lang 已加载（%d 音色），sid=%s"',
                 '"[TTS] 模型已卸载（省电档）"',
                 '"[TTS] 合成产出空音频，原文: %r"',
                 '"[TTS] kokoro 模型未就绪"',
                 '"云 TTS 未配置 base_url"'):
        assert kept in src, f"既有日志被改写：{kept}"
    assert "ClientTimeout(total=30, connect=8)" not in src, "旧的整包 30s 硬闸残留"
    assert "content.iter_chunked(" in src, "缺增量读入口"


def test_slow_first_chunk_logs_cloud_first_byte_and_first_frame(monkeypatch):
    raw = tone(24000)
    chunks = [raw[i:i + 4096] for i in range(0, len(raw), 4096)]
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda **kw: fake_session(FakeResponse(chunks, first_delay=0.12)))
    eng = engine({"base_url": "https://x.example/v1"})
    with capture_logs() as lines:
        arun(collect(eng._cloud_stream("你好")))
    hit = [ln for ln in lines if "首帧" in ln]
    assert hit, f"慢首块未落首帧遥测：{lines}"
    assert "云首字节" in hit[0], hit[0]
    assert hit[0].startswith("[TTS] 云首字节 "), hit[0]


def test_local_path_logs_model_wait_and_first_frame():
    eng = stub_local(engine({}, provider="local_kokoro"))
    with capture_logs() as lines:
        out = arun(collect(eng.stream_opus("已打开。")))
    assert out == [b"LOCALPKT"]
    assert any("模型未就绪，本轮等待" in ln for ln in lines), lines
    assert any("本地首帧" in ln for ln in lines), lines

    # 第二轮命中缓存：不得再谎报"模型未就绪"，但每轮仍要有本地首帧
    with capture_logs() as lines2:
        out2 = arun(collect(eng.stream_opus("已打开。")))
    assert out2 == [b"LOCALPKT"]
    assert not any("模型未就绪" in ln for ln in lines2), lines2
    assert any("本地首帧" in ln for ln in lines2), lines2


def test_cloud_fallback_also_logs_local_first_frame(monkeypatch):
    """回落档：本地首帧数字 = 用户真正白等的静音（含云尝试），必须留痕。"""
    monkeypatch.setattr(aiohttp, "ClientSession",
                        lambda **kw: fake_session(
                            FakeResponse([b"OggS\x00\x02" + b"j" * 64])))
    eng = stub_local(engine({"base_url": "https://x.example/v1"}))
    with capture_logs() as lines:
        arun(collect(eng.stream_opus("你好")))
    assert any("云合成失败" in ln for ln in lines), lines
    assert any("本地首帧" in ln for ln in lines), lines


# ══ ⑥ 增量解码器单元钉（状态机边界）═══════════════════════════
def test_decoder_buffers_until_riff_decidable():
    """12B 前缀不足以判定 RIFF 时必须继续缓冲，不得把 RIFF 当裸 PCM 吐出来。"""
    payload = tone(24000)
    wav = make_wav(rate=24000, data=payload)
    dec = _CloudOpusStream(24000)
    pkts = []
    for c in odd_chunks(wav[:12], (1, 2, 3, 5)):        # 前 12B 再切成 1/2/3/5B
        pkts += dec.feed(c)
    assert pkts == [], "前缀缓冲期的字节不得进音频流水"
    for c in odd_chunks(wav[12:]):
        pkts += dec.feed(c)
    pkts += dec.flush()
    assert pkts == TtsEngine._resample_encode(payload, 24000)


def test_decoder_truncated_wav_header_raises():
    dec = _CloudOpusStream(24000)
    dec.feed(make_wav(rate=24000, data=b"AB" * 40)[:30])   # 切在 data 块头之前
    with pytest.raises(RuntimeError) as ei:
        dec.flush()
    assert "wav 头损坏" in str(ei.value)


def test_decoder_rejects_non_16bit_wav():
    """位深不支持：解析到 data 块头即报错（fail-loud，不得当 16bit 播成噪音）。"""
    dec = _CloudOpusStream(24000)
    with pytest.raises(RuntimeError) as ei:
        dec.feed(make_wav(rate=24000, data=b"0" * 64, bits=32))
    assert "16-bit" in str(ei.value)


def test_decoder_raw_pcm_shorter_than_sniff_window():
    """短到判不出容器的响应：按裸 PCM 宽容处理（与 _unwrap_audio 同宽容度）。"""
    raw = b"\x11\x22\x33\x44"
    dec = _CloudOpusStream(24000)
    assert dec.feed(raw) == []
    assert dec.flush() == TtsEngine._resample_encode(raw, 24000)


def test_decoder_unknown_rate_is_not_fatal():
    """源率写成 0/垃圾：按 16k 直通而不是除零崩（配置写坏不得打死播报）。"""
    dec = _CloudOpusStream(0)
    raw = tone(2400, rate=16000)
    pkts = dec.feed(raw) + dec.flush()
    assert pkts == TtsEngine._resample_encode(raw, 16000)
