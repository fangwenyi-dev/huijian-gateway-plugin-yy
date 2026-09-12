"""v1.0.55 TTS 深审定案批钉桩（2026-09-21 三独立对抗核验全确认的修复）。

覆盖定案七项里的①②③⑤⑥⑦（④=云端首帧后串播已由 test_v1055_guard_and_voice
的半途收束守卫钉，本批补齐其"半截口必须声明截断"的后半段协议）：

① 直封支流式化：首块 PCM 就绪即出占位头+透传，**不得**再等源抽干；
② 截断收口协议：session 早退/引擎自报 → stop 帧带 truncated → 集成 transport
   以 Dict(error) 收口 + 断连清算（HA core 只在异常时 pop 缓存——普通 stop
   会把半截音频永久写进消息哈希缓存，内存+落盘、跨重启命中）；
③ default_options 容器：TTS 实体只挂 assist 条目，transport 在
   entry.runtime_data——用**仓内真实** get_entry_data 执行，杜绝再读
   hass.data[DOMAIN][entry_id] 的哑弹形态（v1.0.48 教训：字符串钉桩全绿拦不住）；
⑤ tts 节热变更复位云钉扎（main._on_settings_change 真函数执行）；
⑥ 冷态越界 sid 在模型就绪后复核回落（stream_opus 真函数执行）；
⑦ _CloudOpusStream 对声明长度可信的 data 块按 csz 截断（尾块元数据不进音频），
   0/0xFFFFFFFF 未知长维持"读到流尽"（与整包路径切片语义对齐）。

纪律：全部行为钉——真实源码抽出执行或真实模块直调；仅两处接线位用调用点
原文钉（_convert 的 streaming=True 传参），其语义本身有行为钉兜底。
"""
import ast
import asyncio
import contextlib
import io
import json
import logging
import struct
import time
import types
import wave
from collections.abc import AsyncGenerator, AsyncIterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CC = ROOT / "custom_components" / "huijian_ai"

FRAME = b"OPUSFRAME"


class _Ann:
    """注解占位：能吃下 X[...] 下标（≤3.13 在 def 时求值注解）。"""

    def __class_getitem__(cls, item):
        return cls


def _extract(path: Path, name: str, ns: dict):
    """从真实源码抽出指定函数/类执行（不复制逻辑），返回真身。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == name):
            seg = ast.get_source_segment(src, node)
            exec(compile(seg, str(path), "exec"), ns)
            return ns[name]
    raise AssertionError(f"{path.name} 里找不到 {name}")


def _segment(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{path.name} 里找不到函数 {name}")


# ── ① 直封支流式化（真实 async_convert_audio 执行）────────────────────
def _convert_fn():
    apy = CC / "huijian" / "audio.py"
    ns = {
        "HomeAssistant": _Ann,
        "AsyncIterable": AsyncIterable,
        "AsyncGenerator": AsyncGenerator,
        "io": io, "wave": wave, "asyncio": asyncio, "contextlib": contextlib,
        "_LOGGER": logging.getLogger("pin"),
        "ffmpeg": types.SimpleNamespace(
            get_ffmpeg_manager=lambda h: (_ for _ in ()).throw(
                AssertionError("直封支不得触碰 ffmpeg"))),
    }
    ns["wrap_pcm_as_wav"] = _extract(apy, "wrap_pcm_as_wav", ns)
    ns["wav_stream_header"] = _extract(apy, "wav_stream_header", ns)
    return _extract(apy, "async_convert_audio", ns)


def _chunks(n=4, size=1920):
    return [bytes([i % 251]) * 2 * (size // 2) for i in range(n)]


def _counting_gen(chunks, counter):
    async def g():
        for c in chunks:
            counter[0] += 1
            yield c
    return g()


def test_streaming_direct_wrap_emits_before_source_exhausted():
    """首块 PCM 就绪即产出（占位头+数据块），源抽干前必须已出块——
    旧 b"".join 形态在本钉下 cnt 恒等于 n，必红。"""
    conv = _convert_fn()
    ck = _chunks(4)
    cnt = [0]

    async def run():
        agen = conv(None, _counting_gen(ck, cnt), "s16le", to_extension="wav",
                    to_sample_rate=16000, to_sample_channels=1, to_sample_bytes=2,
                    input_params=["-ar", "16000", "-ac", "1"], streaming=True)
        out = [await agen.__anext__(), await agen.__anext__()]
        pulled_at_two = cnt[0]
        out += [c async for c in agen]
        return out, pulled_at_two

    out, pulled_at_two = asyncio.run(run())
    head, first = out[0], out[1]
    assert len(head) == 44 and head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    assert head[4:8] == b"\xff\xff\xff\xff" and head[40:44] == b"\xff\xff\xff\xff"
    w = wave.open(io.BytesIO(head + b"\x00" * 64), "rb")
    assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
    assert first == ck[0]
    assert pulled_at_two < len(ck), "首块出时源未抽干（增量性）"
    assert b"".join(out[1:]) == b"".join(ck), "PCM 字节必须原样透传"


def test_streaming_direct_wrap_closes_source_on_midstream_consumer_close():
    """消费者中途 aclose（core 取消/换轮）→ 流式支必须**确定性地**收口源，
    把 transport 的残帧隔离/清算拉回本 tick，不留给 GC 终结器赌时机。"""
    conv = _convert_fn()
    ck = _chunks(6)
    closed = []

    async def g():
        try:
            for c in ck:
                yield c
        finally:
            closed.append(True)

    async def run():
        agen = conv(None, g(), "s16le", to_extension="wav", streaming=True)
        await agen.__anext__()  # 占位头
        await agen.__anext__()  # 第一块 PCM
        await agen.aclose()
        return list(closed)

    assert asyncio.run(run()) == [True]


def test_streaming_direct_wrap_empty_pcm_fail_loud(caplog):
    conv = _convert_fn()

    async def empty():
        if False:
            yield b""

    async def run():
        return [c async for c in conv(None, empty(), "s16le", to_extension="wav",
                                       streaming=True)]

    with caplog.at_level(logging.INFO, logger="pin"):
        out = asyncio.run(run())
    assert out == [], "空 PCM 不得产出（连占位头都不行——纯头骗过实体 peek 闸）"
    assert "空 PCM" in caplog.text


def test_batch_direct_wrap_keeps_real_header():
    """默认批式支（整段路）字节级不变：真实长度头 + wave 可整读。"""
    conv = _convert_fn()
    ck = _chunks(3)

    async def run():
        return [c async for c in conv(None, _counting_gen(ck, [0]), "s16le",
                                       to_extension="wav")]

    out = asyncio.run(run())
    assert len(out) == 1
    w = wave.open(io.BytesIO(out[0]), "rb")
    assert w.getnframes() == sum(len(c) for c in ck) // 2
    assert w.readframes(w.getnframes()) == b"".join(ck)
    assert out[0][4:8] != b"\xff\xff\xff\xff", "批式路必须写真实 RIFF 长度"


# ── ③ default_options 容器（真实 get_entry_data + 真实 property 执行）──
def _default_options_fn():
    ns = {
        "get_entry_data": _extract(CC / "huijian" / "__init__.py", "get_entry_data",
                                   {"DOMAIN": "huijian_ai"}),
        "tts_transport": types.SimpleNamespace(ATTR_TRANSPORT="tts_transport"),
    }
    return _extract(CC / "tts.py", "default_options", ns)


def _self_of(entry, hass_data=None):
    return types.SimpleNamespace(hass=types.SimpleNamespace(data=hass_data or {}),
                                 entry=entry)


def test_default_options_reads_assist_runtime_data():
    """定案③主钉：assist 条目（本实体唯一装配形态）的 transport 在
    runtime_data——v1.0.48 旧实现读 hass.data 恒 KeyError=换嗓指纹哑弹。"""
    opt = _default_options_fn()
    entry = types.SimpleNamespace(entry_id="e1", data={"config_type": "assist"})
    entry.runtime_data = {"tts_transport": types.SimpleNamespace(voice_fp="local:sid45+c0")}
    assert opt(_self_of(entry)) == {"huijian_voice_fp": "local:sid45+c0"}


def test_default_options_edge_states_return_empty_not_raise():
    opt = _default_options_fn()
    e = types.SimpleNamespace(entry_id="e2", data={"config_type": "assist"})   # unload 窗口：无 runtime_data
    assert opt(_self_of(e)) == {}
    e2 = types.SimpleNamespace(entry_id="e3", data={"config_type": "assist"})
    e2.runtime_data = {}                                                       # 连接未建
    assert opt(_self_of(e2)) == {}


def test_default_options_device_container_also_resolves():
    """双容器语义（get_entry_data）都要读得到——device 形态防御性钉。"""
    opt = _default_options_fn()
    entry = types.SimpleNamespace(entry_id="e4", data={"config_type": "device"})
    hd = {"huijian_ai": {"e4": {"tts_transport": types.SimpleNamespace(voice_fp="cloud:naomi")}}}
    assert opt(_self_of(entry, hd)) == {"huijian_voice_fp": "cloud:naomi"}


# ── ① 实体接线：_convert 转传 streaming + 两条出口各自取值 ─────────────
def test_convert_forwards_streaming_flag():
    captured = {}

    def stub_convert(hass, gen, fext, **kw):
        captured.update(kw)
        return "GEN"

    ns = {"async_convert_audio": stub_convert}
    _convert = _extract(CC / "tts.py", "_convert", ns)
    self = types.SimpleNamespace(hass=None, opus_sample_rate=16000, opus_channels=1)
    _convert(self, "PCM", "wav", {"preferred_sample_rate": 16000}, streaming=True)
    assert captured.get("streaming") is True
    captured.clear()
    _convert(self, "PCM", "mp3", {})
    assert captured.get("streaming") is False, "整段路默认批式"


def test_entity_streaming_entry_uses_streaming_convert():
    """调用点接线钉（v1.0.56 闸形态）：streaming 由 _want_stream_wrap 按
    options 形态判定，流式入口不得硬编码 True（占位头落盘守卫）。"""
    seg = _segment(CC / "tts.py", "async_stream_tts_audio")
    assert "streaming = self._want_stream_wrap(fmt, options)" in seg
    assert "_convert(pcm, fmt, options, streaming=streaming)" in seg
    assert "streaming=True" not in seg
    get_src = _segment(CC / "tts.py", "async_get_tts_audio")
    assert "self._convert(pcm, fmt, options)" in get_src
    assert "streaming=True" not in get_src, "整段路不得开流式（占位头不进盘缓存消费者）"


# ── ② 集成 transport：截断 stop 转 error（真实 stream 方法执行）────────
def _transport_stream():
    import contextlib
    ns = {
        "asyncio": asyncio,
        "Dict": _extract(CC / "huijian" / "__init__.py", "Dict", {"json": json}),
        "anyio": types.SimpleNamespace(
            fail_after=lambda s: contextlib.nullcontext(),
            get_cancelled_exc_class=lambda: asyncio.CancelledError),
    }
    return _extract(CC / "huijian" / "tts_transport.py", "stream", ns), ns["Dict"]


class _Reader:
    def __init__(self, items):
        self._items = items

    def __aiter__(self):
        async def gen():
            for it in self._items:
                yield it
        return gen()


class _FakeTransport:
    def __init__(self, items):
        self._request_lock = asyncio.Lock()
        self._recv_reader = _Reader(items)
        self.logger = logging.getLogger("pin")
        self.restarts = []

    async def ensure_connected(self):
        return True

    async def send_message(self, m):
        pass

    async def _drain_stale(self):
        pass

    async def restart_connection(self, why):
        self.restarts.append(why)


def test_transport_truncated_stop_becomes_error():
    stream, Dict = _transport_stream()
    stop = Dict({"type": "tts", "state": "stop", "truncated": True})
    f = _FakeTransport([b"frame1", b"frame2", stop])

    async def run():
        return [x async for x in stream(f, "测试截断")]

    out = asyncio.run(run())
    assert out[0] == b"frame1" and out[1] == b"frame2"
    assert len(out) == 3 and getattr(out[2], "error", None), "截断收口必须追加 error"
    assert f.restarts, "截断轮不得留脏连接"


def test_transport_clean_stop_unchanged():
    stream, Dict = _transport_stream()
    stop = Dict({"type": "tts", "state": "stop"})
    f = _FakeTransport([b"frame1", stop])

    async def run():
        return [x async for x in stream(f, "正常轮")]

    out = asyncio.run(run())
    assert out == [b"frame1"] and not f.restarts, "无 truncated 键（旧加载项）→ 完全旧语义"


# ── ② 加载项 session：早退/引擎自报 → stop 帧声明截断 ──────────────────
def _bare_session(packets, delay=0.0, flag=False, send_ok=True, hang=False):
    from core.session import TtsSession

    s = TtsSession.__new__(TtsSession)
    s._gen = 0
    s._task = None
    sent = {"json": [], "bytes": 0}

    async def send_json(frame):
        sent["json"].append(frame)
        return True

    async def send_bytes(b):
        if not send_ok:
            return False
        sent["bytes"] += 1
        return True

    class Eng:
        async def stream_opus(self, text, engine_out=None):
            for i in range(packets):
                if delay:
                    await asyncio.sleep(delay)
                yield b"\xf8" + bytes([i])
            if flag and engine_out is not None:
                engine_out["truncated"] = True
            if hang:
                await asyncio.sleep(30)

    s.ctx = types.SimpleNamespace(tts=Eng())
    s.send_json = send_json
    s.send_bytes = send_bytes
    return s, sent


def _stops(sent):
    return [f for f in sent["json"] if f.get("state") == "stop"]


def test_session_normal_completion_has_no_truncated_flag():
    s, sent = _bare_session(3)
    asyncio.run(s._stream("正常收口", 0))
    stops = _stops(sent)
    assert len(stops) == 1 and "truncated" not in stops[0]


def test_session_engine_reported_truncation_marks_stop():
    s, sent = _bare_session(2, flag=True)
    asyncio.run(s._stream("引擎自报半截", 0))
    stops = _stops(sent)
    assert len(stops) == 1 and stops[0].get("truncated") is True


def test_session_budget_overrun_marks_stop():
    from core import const as core_const
    s, sent = _bare_session(1, hang=True)
    orig = core_const.TTS_STREAM_BUDGET_S
    core_const.TTS_STREAM_BUDGET_S = 0.05
    try:
        asyncio.run(s._stream("超预算", 0))
    finally:
        core_const.TTS_STREAM_BUDGET_S = orig
    stops = _stops(sent)
    assert len(stops) == 1 and stops[0].get("truncated") is True


def test_session_send_failure_marks_stop():
    s, sent = _bare_session(2, send_ok=False)
    asyncio.run(s._stream("断连", 0))
    stops = _stops(sent)
    assert len(stops) == 1 and stops[0].get("truncated") is True


# ── ② +④ 引擎 stream_opus：半途收束/空句自报截断（真实模块）───────────
class S:
    def __init__(self, d=None):
        self.d = {"tts.provider": "local_kokoro", "tts.speed": 1.0}
        self.d.update(d or {})

    def get(self, k, dv=None):
        return self.d.get(k, dv)


def _engine(**sd):
    from core.tts import TtsEngine
    eng = TtsEngine(S(sd), types.SimpleNamespace())
    return eng


def _collect(eng, text="好的，灯打开了。"):
    out, eng_out = [], {}

    async def go():
        async for pkt in eng.stream_opus(text, engine_out=eng_out):
            out.append(pkt)
    asyncio.run(go())
    return out, eng_out


def test_cloud_midstream_returns_mark_truncated():
    eng = _engine(**{"tts.provider": "cloud_openai_compat"})
    eng._tts = object()

    async def half(text):
        yield b"CLOUD-PKT"
        raise RuntimeError("stream reset")
    eng._cloud_stream = half
    out, e = _collect(eng)
    assert out == [b"CLOUD-PKT"], "半途断流不得缝本地嗓（双音色守卫）"
    assert e.get("truncated") is True, "半途收束必须自报（否则 HA 缓存毒化）"
    assert eng._cloud_fail_ts > 0, "半途失败同样开钉扎窗口"


def test_cloud_zero_frame_falls_back_and_pins():
    """v1.0.56 审计 B-gap：云"零帧正常收束"（空 body/钳位后一帧不剩）不得
    记作整流成功——须走失败路径：本地默认音色兜底 + 开钉扎，且整段完成
    故**不**声明截断。"""
    eng = _engine(**{"tts.provider": "cloud_openai_compat"})
    eng._tts = object()

    async def empty(text):
        if False:
            yield b""
    eng._cloud_stream = empty
    eng._synth = lambda sent, sid, speed: b"\x00\x00" * 960
    eng._encode = lambda pcm: [FRAME]
    out, e = _collect(eng, "开灯了。")
    # 终态标签含回缀：except 支先写 "local:fallback"，进本地循环时缝成
    # "local:sid18(云回落)"——现场可观测性更好，钉此形状。
    assert out == [FRAME] and e.get("engine") == "local:sid18(云回落)"
    assert "truncated" not in e, "本地整段补全了，不是半截口"
    assert eng._cloud_fail_ts > 0, "零帧=失败，开钉扎而非解除"


def test_empty_sentence_marks_truncated():
    eng = _engine()
    eng._tts = object()          # 热态：不加载，直接合成
    eng._synth = lambda sent, sid, speed: b""
    out, e = _collect(eng, "第一句。第二句。")
    assert out == [] and e.get("truncated") is True, "缺句=残缺音频，也要声明截断防毒缓存"


def test_pure_punctuation_segment_silent_not_truncated():
    """审计 P1：连排标点会拆出独立纯标点段（split_sentences 实跑产出），
    其本地合成**正常**产空——不得 latched truncated，否则整轮完整音频被
    永久误判为半截（流式路 raise+弃缓存+重连，每轮复发自卫）。"""
    from core.tts import split_sentences
    assert any(seg and not any(c.isalnum() for c in seg)
               for seg in split_sentences("第一句！！！第二句。")), \
        "P1 前提失效：分句器不再产出纯标点段，需重估本形态"
    eng = _engine()
    eng._tts = object()
    eng._synth = lambda sent, sid, speed: (
        b"" if not any(c.isalnum() for c in sent) else b"\x00\x00" * 960)
    eng._encode = lambda pcm: [FRAME]
    out, e = _collect(eng, "第一句！！！第二句。")
    assert out, "可读内容应完整出包"
    assert "truncated" not in e, "纯标点静音不是缺句"


def test_real_empty_sentence_still_truncated_amid_silent_segments():
    """P1 修不能反向吞掉定案②：静音段环绕下的真缺句照报截断。"""
    eng = _engine()
    eng._tts = object()
    eng._synth = lambda sent, sid, speed: (
        b"" if sent == "第二句。" else b"\x00\x00" * 960)
    eng._encode = lambda pcm: [FRAME]
    out, e = _collect(eng, "第一句！！！第二句。")
    assert e.get("truncated") is True


# ── v1.0.56 审计 B：占位头流式闸（实体静态方法真身）────────────────────
def _gate_fn():
    fn = _extract(ROOT / "custom_components" / "huijian_ai" / "tts.py",
                  "_want_stream_wrap", {})
    return getattr(fn, "__func__", fn)


def test_stream_wrap_gate_only_satellite_shape_gets_placeholder():
    gate = _gate_fn()
    sat = {"preferred_format": "wav", "preferred_sample_rate": 16000,
           "preferred_sample_channels": 1, "preferred_sample_bytes": 2}
    assert gate("wav", sat) is True, "卫星四件套（core needs_conversion 恒真）→ 真流式"
    assert gate("wav", {"preferred_sample_channels": 1}) is True, "任一转换参即必转"
    # 自由消费者形态一律批式真实头——占位头禁入无 TTL 的消息哈希盘缓存
    assert gate("wav", {"preferred_format": "wav"}) is False, "tts.speak 只要 wav"
    assert gate("wav", {}) is False
    assert gate("mp3", sat) is False, "非直封格式不设流式闸"
    assert gate("wav", {"preferred_sample_rate": None}) is False, "None≠申报"


def test_model_load_failure_marks_truncated():
    eng = _engine()
    eng._tts = None
    eng.ensure_loaded = lambda: False
    out, e = _collect(eng)
    assert out == [] and e.get("truncated") is True


# ── ⑥ 冷态越界 sid：模型就绪后复核回落 ────────────────────────────────
def test_cold_out_of_range_sid_rechecked_after_load():
    eng = _engine(**{"tts.sid": 999})
    seen = []

    class M:
        num_speakers = 103

    def fake_load():
        eng._tts = M()
        return True
    eng.ensure_loaded = fake_load
    eng._synth = lambda sent, sid, speed: (seen.append(sid), b"\x00\x00" * 480)[1]
    eng._encode = lambda pcm: [FRAME]
    out, e = _collect(eng, "开灯了。")
    assert seen == [18], "冷态越界 sid 必须在模型就绪后被复核拦下回落 18（定案⑥）"
    assert out == [FRAME] and e["engine"] == "local:sid18"


def test_recheck_warm_cache_replays_without_synth():
    """审计盲区收编（C1）：复核把 sid 改判后**重算键命中缓存**——必须回放
    旧包、绝不重复合成/重复 send（本地首帧遥测路径由回放分支自走）。"""
    eng = _engine(**{"tts.sid": 999})

    class M:
        num_speakers = 103

    def fake_load():
        eng._tts = M()
        return True
    eng.ensure_loaded = fake_load
    calls = []
    eng._synth = lambda *a: (calls.append(a), b"\x00\x00" * 480)[1]
    eng._cache[("开灯了。", 18, 1.0)] = ([FRAME], len(FRAME))   # 预热真 18 嗓
    eng._encode = lambda pcm: [b"SHOULD-NOT-BE-USED"]
    out, e = _collect(eng, "开灯了。")
    assert calls == [], "命中回放不得再合成"
    assert out == [FRAME] and e["engine"] == "local:sid18"
    assert eng.cache_hits == 1


def test_hot_valid_sid_untouched():
    eng = _engine(**{"tts.sid": 45})

    class M:
        num_speakers = 103
    eng._tts = M()
    seen = []
    eng._synth = lambda sent, sid, speed: (seen.append(sid), b"\x00\x00" * 480)[1]
    eng._encode = lambda pcm: [FRAME]
    out, e = _collect(eng, "开灯了。")
    assert seen == [45] and e["engine"] == "local:sid45"


# ── ⑤ tts 节热变更复位钉扎（main 真函数执行；v1.0.56 审计 D-1/D-2 语义）──
def _settings_app(tts_spy=None, rotator=None):
    ns = {"json": json, "logger": logging.getLogger("pin")}
    change = _extract(ROOT / "core" / "main.py", "_on_settings_change", ns)

    class TtsSpy:
        def __init__(self):
            self.resets = []

        def reset_cloud_pin(self, reason=""):
            self.resets.append(reason)

    def boom():
        raise OSError("磁盘炸了")
    app = types.SimpleNamespace(
        tts=tts_spy or TtsSpy(),
        textcnn=types.SimpleNamespace(set_thresholds_override=lambda d: None),
        _write_endpoints=lambda: None,
        _warn_local_nlu=lambda data: None,
        _rotate_voice_fp=rotator or (lambda: None),
    )
    return change, app


def test_settings_change_resets_cloud_pin():
    change, app = _settings_app()
    d1 = {"nlu": {}, "tts": {"provider": "cloud_openai_compat",
                             "cloud": {"base_url": "http://bad"}}}
    change(app, d1)       # D-1 修正：首调同样复位（开机后第一次保存往往正是
    assert app.tts.resets == ["tts 配置热更"]   # "修好云配置"那次；未钉时是 no-op）
    change(app, d1)       # 内容没变，不复位
    assert app.tts.resets == ["tts 配置热更"]
    d2 = json.loads(json.dumps(d1))
    d2["tts"]["cloud"]["base_url"] = "http://good"
    change(app, d2)       # 改对云配置 → 复位
    assert app.tts.resets == ["tts 配置热更", "tts 配置热更"]
    d3 = json.loads(json.dumps(d2))
    d3["nlu"]["thresholds_override"] = {"x": 1}
    change(app, d3)       # 只动 nlu → 不复位
    assert len(app.tts.resets) == 2


def test_reset_survives_upstream_hotapply_failure():
    """D-2：排在前面的解钉不得被后序步骤（_rotate_voice_fp 等）的异常吞掉。"""
    def boom():
        raise OSError("磁盘炸了")
    change, app = _settings_app(rotator=boom)
    d = {"nlu": {}, "tts": {"provider": "cloud_openai_compat"}}
    change(app, d)                          # 首调：解钉已发生，随后 rotate 抛
    assert app.tts.resets == ["tts 配置热更"]
    d2 = json.loads(json.dumps(d))
    d2["tts"]["speed"] = 1.2                # tts 变更 + 前序继续炸 → 仍要解钉
    change(app, d2)
    assert app.tts.resets == ["tts 配置热更", "tts 配置热更"]


def test_reset_cloud_pin_behavior():
    eng = _engine()
    eng._cloud_fail_ts = time.monotonic()
    eng.reset_cloud_pin("t")
    assert eng._cloud_fail_ts == 0.0
    eng.reset_cloud_pin("t")               # 已健康时幂等
    assert eng._cloud_fail_ts == 0.0


# ── ⑦ RIFF 流式解码：data 声明长度截断尾块元数据 ──────────────────────
def _wav_bytes(pcm, rate=16000, data_size=None):
    ds = len(pcm) if data_size is None else data_size
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", ds)) + pcm


def _stream_wav(raw, piece=7, default_rate=16000):
    from core.tts import _CloudOpusStream
    st = _CloudOpusStream(default_rate)
    frames = []
    for i in range(0, len(raw), piece):
        frames += st.feed(raw[i:i + piece])
    return frames + st.flush()


def test_declared_data_size_clamps_trailing_chunks():
    """定案⑦主钉：data 声明长度可信 → 尾随 LIST/pad 块不得被编成噪声帧。
    判据取**逐字节一致**：截断流必须与"从未收到尾块"的流完全同帧
    （钳位记账若双重递减，尾帧会短几字节——本钉实施时当场逮过一版）。"""
    pcm = bytes(range(256)) * 40                       # 10240B = 5 帧 + 640 尾
    junk = b"LIST" + struct.pack("<I", 24) + b"\x11" * 24
    assert _stream_wav(_wav_bytes(pcm) + junk) == _stream_wav(_wav_bytes(pcm))


def test_unknown_data_size_reads_to_stream_end():
    pcm = bytes(range(256)) * 40
    whole = _wav_bytes(pcm)
    ref = _stream_wav(whole)
    # 流式写头惯例：data 声明 0xFFFFFFFF，后续块仍是音频，读到流尽
    cut = 5000
    streaming_head = _wav_bytes(pcm[:cut], data_size=0xFFFFFFFF) + pcm[cut:]
    assert _stream_wav(streaming_head) == ref, "未知长度声明必须维持'读到流尽'语义"


def test_unknown_declared_zero_also_reads_to_end():
    """csz=0（另一族未知长）与 0xFFFFFFFF 同语义——审计盲区补钉。"""
    pcm = bytes(range(256)) * 40
    assert _stream_wav(_wav_bytes(pcm, data_size=0)) == _stream_wav(_wav_bytes(pcm))


def test_clamp_survives_resampler_24k():
    """审计盲区：_data_left 截断×24k→16k 重采样器滤波历史/carry 的交互
    （既有两钉全是 passthrough 率，截断若破坏 carry/丢弃窗口必在此现形）。"""
    pcm24 = bytes(range(256)) * 60
    junk = b"LIST" + struct.pack("<I", 24) + b"\x11" * 24
    assert _stream_wav(_wav_bytes(pcm24, rate=24000) + junk) == \
           _stream_wav(_wav_bytes(pcm24, rate=24000))


def test_clamp_on_single_chunk_full_blob():
    """审计盲区：首口 payload 恰吞满 csz（RIFF 头与全部载荷同块到达）——
    双重递减类缺陷在 piece=7 分片下可被掩盖，单块形态必须同样对齐。"""
    pcm = bytes(range(256)) * 40
    junk = b"LIST" + struct.pack("<I", 24) + b"\x11" * 24
    whole_junk = _stream_wav(_wav_bytes(pcm) + junk, piece=10 ** 9)
    whole_pure = _stream_wav(_wav_bytes(pcm), piece=10 ** 9)
    assert whole_junk == whole_pure
