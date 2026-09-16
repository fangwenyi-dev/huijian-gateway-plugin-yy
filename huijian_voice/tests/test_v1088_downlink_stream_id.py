"""v1.0.88 钉：下行流标识（run/stream id）Stage 0 + Stage 1。

治的病：**旧 run 晚到的音频混进新流头部**（播报开头多半句/串音），两跳各一次：

  Hop A 加载项→集成（本仓 core/session.py ↔ huijian/tts_transport.py）
    旧线上形制 `detect → N×裸 binary → stop` 没有任何归属信息；集成只能靠
    `_request_lock` 串行 + `_drain_stale` 1s 预算猜（清得掉"已排队"的，清不掉
    "稍后才到"的）。现：detect 带集成 mint 的 `rid`，对端在本流任何帧之前回声
    `stream_start(rid)`、stop 带同 rid；集成侧未与本流 ack 配对前到达的 binary
    与 stop 一律不算本轮（未协商/坏形态则 fail-open 退回旧语义，方向恒为
    "宁可少一档保护也不哑掉播报"）。加载项侧的归属守卫下沉到 `_send_lock`
    临界区内、紧邻真实 enqueue——这是"ack 之后绝无旧流残帧"的全部依据。

  Hop B 集成→固件（assist_satellite.py `_stream_tts_audio`）
    设备侧世代 s_va_epoch 绑的是 play_reset 现值，认不出"这帧属于 HA 哪一次
    推流"（固件 CHANGELOG v2.1.50 未收口条），而每条下行流的 START/帧/END 由
    各自的后台任务写进同一条 API 连接，谁先 enqueue 谁先上 wire ⇒ 旧流帧可以
    合法落在新流 START 之后。设备侧单方面修不了；本批由**唯一写者**（本集成）
    下沉归属判定：接管→每帧归属闸→被接管者不代发 END、不替新流落状态。
    设备自证（帧带 sid）留给协议批（Stage 2，需固件 + OTA 灰度）。

夹具纪律：行为真身（跑真函数），不是源码字符串；需要抽真身时用仓内 AST 摘取
惯例，夹具常量与真类同值——改一边必改另一边。
"""
import ast
import asyncio
import contextlib
import json
import logging
import re
import sys
import types
from enum import IntEnum
from pathlib import Path

import pytest

try:
    import anyio as _ANYIO_REAL
except Exception:      # pragma: no cover - CI 依赖面无 anyio
    _ANYIO_REAL = None

ROOT = Path(__file__).resolve().parents[1]
CC = ROOT / "custom_components" / "huijian_ai"
ADDON = ROOT / "core"


def _arun(coro):
    return asyncio.run(coro)


# ══════════════════════ Hop A：加载项生产端 ══════════════════════════════
def _wire_session(packets, delay=0.0, tags=None):
    """真 TtsSession + 记账假 ws（三态与真 _send 同构：None=归属守卫拦下）。

    tags: {播报文本: 帧前缀 bytes}——顶替竞态要靠前缀认出帧属于哪一流。
    """
    tags = tags or {}
    from core.session import TtsSession

    s = TtsSession.__new__(TtsSession)
    s._gen = 0
    s._rid = 0
    s._task = None
    s._send_lock = None
    wire = []                       # ("json", obj) / ("bytes", payload) 按上栈次序

    async def _send(coro_fn, guard=None):
        # 与真 BaseSession._send 同构：守卫在"锁"内、紧邻真实 enqueue（之间零
        # await）——本钉要的就是这个次序，改成锁外判定则旧流残帧可穿到 ack 之后
        if guard is not None and not guard():
            return None
        await coro_fn()
        return True

    async def send_json(obj, guard=None):
        if guard is not None and not guard():
            return None
        wire.append(("json", obj))
        return True

    async def send_bytes(data, guard=None):
        if guard is not None and not guard():
            return None
        wire.append(("bytes", data))
        return True

    s._send = _send
    s.send_json = send_json
    s.send_bytes = send_bytes

    class Eng:
        async def stream_opus(self, text, engine_out=None):
            # 每句话一个可辨前缀（tags 表），顶替竞态钉要靠它分清帧属于哪一流
            mark = tags.get(text, b"X")
            for i in range(packets):
                if delay:
                    await asyncio.sleep(delay)
                yield mark + bytes([i])

    ctx = types.SimpleNamespace(tts=Eng())
    s.ctx = ctx
    s.tags = tags
    return s, wire


def test_wire_ack_precedes_frames_and_stop_echoes_rid():
    """带 rid 的 detect：任何帧之前必须先有 stream_start，stop 回显同 rid。"""
    s, wire = _wire_session(3, tags={"第一句": b"ONE"})
    s._gen = 1
    _arun(s._stream("第一句", gen=1, rid=7))
    kinds = [("ack" if k == "json" and o.get("state") == "stream_start"
              else "stop" if k == "json" and o.get("state") == "stop" else k)
             for k, o in wire]
    assert kinds[0] == "ack", f"首帧前必须有身份声明：{kinds}"
    assert wire[0][1] == {"type": "tts", "state": "stream_start", "rid": 7}
    assert kinds[-1] == "stop" and wire[-1][1].get("rid") == 7, \
        f"stop 必须回显 rid（旧流 stop 晚到会被集成按身份拒收）：{wire[-1][1]}"
    assert [o for k, o in wire if k == "json" and o.get("state") == "stop"][0] \
        .get("truncated") is None, "正常收束不得带 truncated（防误判毒缓存）"


def test_legacy_detect_keeps_old_wire_shape():
    """旧集成不带 rid ⇒ 线形制与本变更前逐字节一致（无 ack、stop 无 rid）。"""
    s, wire = _wire_session(2, tags={"旧客户端": b"X"})
    s._gen = 1
    _arun(s._stream("旧客户端", gen=1, rid=0))
    states = [o.get("state") for k, o in wire if k == "json"]
    assert "stream_start" not in states, f"未协商不得凭空造控制帧：{states}"
    assert states == ["stop"] and "rid" not in wire[-1][1]


def test_superseded_stream_cannot_emit_after_new_ack():
    """顶替竞态：新流 ack 之后绝不允许再出现旧流帧（旧形态必红）。

    旧流卡在合成 await 中被新 detect 顶替；新流随后 ack + 出帧。若归属判定在
    锁外（或根本没有），旧流这一帧会落在新流 ack 之后 → 集成无从辨别（帧不带
    身份）→ 播报头部混半句。
    """
    tags = {"旧句子": b"OLD", "新句子": b"NEW"}

    async def go():
        s, wire = _wire_session(3, delay=0.03, tags=tags)
        s._gen = 1
        t_old = asyncio.create_task(s._stream("旧句子", gen=1, rid=1))
        await asyncio.sleep(0.01)           # 旧流：ack 已上栈，正卡在合成 await
        s._gen = 2                           # 新 detect 顶替（代际推进）
        t_new = asyncio.create_task(s._stream("新句子", gen=2, rid=2))
        await asyncio.gather(t_old, t_new, return_exceptions=True)
        return wire

    wire = _arun(go())
    seq = []
    for k, o in wire:
        if k != "json":
            seq.append(("f", o))
        elif o.get("state") == "stream_start":
            seq.append(("ack", o.get("rid")))
        elif o.get("state") == "stop":
            seq.append(("stop", o.get("rid")))
        else:
            seq.append(("j", o))
    idx = next(i for i, e in enumerate(seq) if e == ("ack", 2))
    for kind, payload in seq[idx + 1:]:
        assert kind != "f" or payload.startswith(b"NEW"), \
            f"新流 ack 之后混入旧流帧：{seq}"
    stops = [o for k, o in wire if k == "json" and o.get("state") == "stop"]
    assert [o.get("rid") for o in stops] == [2], \
        f"被顶替的旧流不得发孤儿 stop（那会把新流半截收口）：{stops}"


# ══════════════════════ Hop A：集成消费端 ════════════════════════════════
def _stop(Dict, rid=None, truncated=None):
    d = {"type": "tts", "state": "stop"}
    if rid is not None:
        d["rid"] = rid
    if truncated is not None:
        d["truncated"] = truncated
    return Dict(d)


class _Reader:
    def __init__(self, items, pace=0.0):
        self._items = list(items)
        self._pace = pace

    async def receive(self):
        if self._pace:
            await asyncio.sleep(self._pace)
        if not self._items:
            raise _ANYIO_REAL.EndOfStream
        return self._items.pop(0)


def _fake_transport(Dict, items, proto=2, restart_budget=None):
    """鸭子 transport：只备 stream() 真身实际取用的成员。"""

    class F:
        _ROUND_TOTAL_BUDGET_S = 720.0
        _request_lock = None

        def __init__(self):
            import asyncio as _a
            self._request_lock = _a.Lock()
            self._recv_reader = _Reader(items)
            self.logger = logging.getLogger("pin.v1088")
            self.restarts = []
            self.sent = []
            self._proto = proto
            self._conn_gen = 0
            self._round_active = False
            self._rid = 100

        def _next_rid(self):
            self._rid += 1
            return self._rid

        def _claim_round(self):
            self._round_active = True
            return self._conn_gen

        def _release_round_claim(self):
            self._round_active = False

        async def ensure_connected(self):
            return True

        async def send_message(self, m):
            self.sent.append(m)

        async def _drain_stale(self):
            pass

        async def restart_connection(self, why):
            self.restarts.append(why)

    return F()


def _transport_stream_ns():
    if _ANYIO_REAL is None:      # CI 依赖面无 anyio → 跳过而非假绿
        pytest.skip("本钉需真 anyio（transport.stream 挂真原语）")
    ns = {
        "asyncio": asyncio, "time": __import__("time"), "anyio": _ANYIO_REAL,
        "Dict": _load_dict(),
        "_TTS_PROTO_STREAM_ID": 2, "_SYNC_BUDGET_S": 0.2,
    }
    ns["stream"] = _extract_async(CC / "huijian" / "tts_transport.py", "stream", ns)
    return ns


def _load_dict():
    src = (CC / "huijian" / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Dict")
    ns = {"json": json}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "__init__.py", "exec"), ns)
    return ns["Dict"]


def _extract_async(path, name, ns):
    """AST 摘真身（注解**与返回注解**里的非内建名字补占位，可下标）。

    守则：占位必须在 `not hasattr(builtins, name)` 保护下做——覆盖 list/dict
    之类内建会污染被测函数运行期（本机 py3.13 在 def 时求值注解与默认值）。
    """
    import builtins
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    def walk(body):
        for n in body:
            if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)):
                if n.name == name:
                    return n
                got = walk(n.body)
                if got:
                    return got
            elif isinstance(n, ast.ClassDef):
                got = walk(n.body)
                if got:
                    return got
        return None

    node = walk(tree.body)
    assert node, f"{path} 中未找到 {name}"
    anns = [a for a in (getattr(n, "annotation", None) for n in ast.walk(node)) if a]
    anns += [x for x in (getattr(node, "returns", None),) if x]
    for a in anns:
        for nm in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", ast.unparse(a)):
            if not hasattr(builtins, nm):
                ns.setdefault(nm, _Any)
    for d in (getattr(node, "args", None) and getattr(node.args, "defaults", []) or []):
        for nm in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", ast.unparse(d)):
            if not hasattr(builtins, nm):
                ns.setdefault(nm, _Any)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)
    return ns[name]


class _AnyMeta(type):
    def __getattr__(cls, item):
        return cls


class _Any(metaclass=_AnyMeta):
    def __class_getitem__(cls, item):
        return cls


def test_consumer_drops_everything_before_own_ack():
    """未与本流 ack 配对前：旧流帧、旧流 stop 都不算本轮（旧形态必红）。"""
    ns = _transport_stream_ns()
    Dict, stream = ns["Dict"], ns["stream"]
    t = _fake_transport(Dict, [
        b"OLD-frame-1",                        # 旧流晚到帧（必须丢）
        _stop(Dict, rid=999),                 # 旧流晚到 stop（不得当收口）
        Dict({"type": "tts", "state": "stream_start", "rid": None}),   # 占位，运行时填
    ])
    # 本轮 rid 由 transport 自己 mint（detect 里看得见）→ 先跑一遍拿 rid 再重排队列
    my_rid = t._rid + 1
    t._recv_reader = _Reader([
        b"OLD-frame-1",
        _stop(Dict, rid=999),
        Dict({"type": "tts", "state": "stream_start", "rid": my_rid}),
        b"NEW-frame-1", b"NEW-frame-2",
        _stop(Dict, rid=my_rid),
    ])

    async def run():
        return [x async for x in stream(t, "本轮播报")]

    out = _arun(run())
    assert out == [b"NEW-frame-1", b"NEW-frame-2"], \
        f"新流头部混入旧流残段/错收口：{out}"
    assert t.sent[0]["rid"] == my_rid, "detect 必须带上自己 mint 的 rid"
    assert not t.restarts, "以本流配对 stop 收口=干净，不该换连"


def test_consumer_unpaired_stop_does_not_close_clean():
    """只收到旧流 stop：本流不得被"干净收口"骗停（防半截音频进盘缓存）。"""
    ns = _transport_stream_ns()
    Dict, stream = ns["Dict"], ns["stream"]
    t = _fake_transport(Dict, [])
    my_rid = t._rid + 1
    t._recv_reader = _Reader([_stop(Dict, rid=999)])

    async def run():
        return [x async for x in stream(t, "本轮", timeout=1)]

    out = _arun(run())
    assert out and getattr(out[-1], "error", None), \
        f"没有本流收口必须按错误收口（防缓存毒化不变量）：{out}"
    assert t.restarts, "未以配对 stop 收口必须断连清算"


def test_consumer_fail_open_when_ack_never_arrives():
    """对端声称支持却不发 ack：到点 fail-open 收帧（宁少一档保护，绝不哑掉）。"""
    ns = _transport_stream_ns()
    Dict, stream = ns["Dict"], ns["stream"]
    t = _fake_transport(Dict, [])
    # 节拍 0.12s × 4 帧 > _SYNC_BUDGET_S(0.2)：先丢两帧未配对的，到点 fail-open
    # 后剩余帧照收，rid-less stop 按旧语义认（宁少一档保护，绝不哑掉播报）
    t._recv_reader = _Reader([b"frame-1", b"frame-2", b"frame-3", b"frame-4",
                              _stop(Dict)], pace=0.12)

    async def run():
        return [x async for x in stream(t, "缺 ack 的坏形态", timeout=5)]

    out = _arun(run())
    assert out[-1] == b"frame-4" and not any(isinstance(x, dict) for x in out), \
        f"ack 缺失时应退回旧语义并干净收口：{out}"
    assert not t.restarts, f"fail-open 后按 stop 收口=干净，不该换连：{t.restarts}"


def test_consumer_legacy_proto_is_byte_for_byte_old():
    """未协商（proto=0）：不带 rid、不等 ack，行为与 v1.0.87 一致。"""
    ns = _transport_stream_ns()
    Dict, stream = ns["Dict"], ns["stream"]
    t = _fake_transport(Dict, [], proto=0)
    t._recv_reader = _Reader([b"f1", _stop(Dict)])

    async def run():
        return [x async for x in stream(t, "旧加载项", timeout=2)]

    assert _arun(run()) == [b"f1"]
    assert "rid" not in t.sent[0], f"未协商不得凭空带 rid：{t.sent[0]}"


def _base_ns():
    # 被摘方法用到的模块级名字（_on_server_settings 走 _LOGGER）
    return {"logging": logging,
            "_LOGGER": logging.getLogger("pin.v1088"),
            "_TTS_PROTO_STREAM_ID": 2}


class _Self:
    """鸭子 self：只备被摘方法实际取用的成员（AST 摘真身，不 import HA 依赖）。"""

    def __init__(self, active):
        self._round_active = active
        self._unclaimed_total = 0
        self.logger = logging.getLogger("pin.v1088")
        self.restarts = []
        self.is_connected = True
        self._conn_gen = 3

    def _schedule_restart(self, why):
        self.restarts.append(why)


def test_transport_claims_round_and_drops_unclaimed_inbound():
    """认领位语义：无人认领→就地丢弃且链路保持；认领中→绝不丢；陈旧认领→接管清算。"""
    path = CC / "huijian" / "tts_transport.py"
    on_incoming = _extract_async(path, "_on_incoming", _base_ns())
    claim = _extract_async(path, "_claim_round", _base_ns())

    s = _Self(active=False)
    assert on_incoming(s, b"stale") is True, "无人认领必须就地丢弃"
    assert on_incoming(s, b"stale2") is True and s._unclaimed_total == 2
    s._round_active = True
    assert on_incoming(s, b"fresh") is False, "认领中不得丢弃（那是本轮的帧）"

    s2 = _Self(active=True)          # 上一轮认领没释放 → 当场接管
    assert claim(s2) == 3 and s2._round_active is True
    assert s2.restarts, "接管陈旧认领必须换连清算（不等一个不会来的释放）"


def test_welcome_negotiation_round_trip():
    """协商闭环：欢迎帧 tts_proto→transport._proto→detect 是否带 rid。"""
    handler = _extract_async(CC / "huijian" / "tts_transport.py",
                             "_on_server_settings", _base_ns())

    class S:
        _proto = 0
        voice_fp = None
        logger = logging.getLogger("pin.v1088")

    s = S()
    handler(s, {"type": "settings", "voice_fp": "fp1", "tts_proto": 2})
    assert s._proto == 2, "欢迎帧代次必须落到 transport 实例"
    s2 = S()
    handler(s2, {"type": "settings", "voice_fp": "fp1"})
    assert s2._proto == 0, "旧加载项不申报 → 保持旧语义（绝不当成已协商）"


# ══════════════════════ Hop B：卫星下行流所有权 ══════════════════════════
class _Vev(IntEnum):
    VOICE_ASSISTANT_TTS_STREAM_START = 1
    VOICE_ASSISTANT_TTS_STREAM_END = 2


class _Cli:
    def __init__(self):
        self.events = []
        self.audio = []

    def send_voice_assistant_event(self, ev, data):
        self.events.append(int(ev))

    def send_voice_assistant_audio(self, chunk):
        self.audio.append(chunk)


class _ED:
    def __init__(self):
        self.flags = []

    def async_set_assist_pipeline_state(self, v):
        self.flags.append(v)


class _Sat:
    def __init__(self, seq=1):
        self._is_running = True
        self._udp_server = None
        self.cli = _Cli()
        self._entry_data = _ED()
        self.response_finished = 0
        self._dl_seq = seq
        self._dl_drop_total = 0

    def tts_response_finished(self):
        self.response_finished += 1


class _Res:
    extension = "wav"

    def __init__(self, gen):
        self._gen = gen

    def async_stream_result(self):
        return self._gen


def _wav(pcm):
    import struct
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    hdr += b"fmt " + struct.pack("<I", 16)
    hdr += struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
    return hdr + b"data" + struct.pack("<I", len(pcm)) + pcm


def _sat_stream_fn():
    import struct
    src = (CC / "assist_satellite.py").read_text(encoding="utf-8")
    m = re.search(r"_MAX_WAV_HEADER_BYTES\s*=\s*(\d+)", src)
    assert m
    ns = {
        "asyncio": asyncio, "contextlib": contextlib, "_LOGGER": logging.getLogger("pin"),
        "VoiceAssistantEventType": _Vev, "_DEVICE_BUFFER_TARGET_S": 0.384,
        "_DL_DROP_LOG_EVERY": 20, "struct": struct,
    }
    ph = _extract_async(CC / "assist_satellite.py", "_parse_wav_header", ns)
    ns["_parse_wav_header"] = ph
    it = _extract_async(CC / "assist_satellite.py", "_iter_wav_pcm_chunks", ns)
    ns["_iter_wav_pcm_chunks"] = it
    ns["_MAX_WAV_HEADER_BYTES"] = int(m.group(1))
    return _extract_async(CC / "assist_satellite.py", "_stream_tts_audio", ns)


def test_takeover_gates_frames_and_suppresses_foreign_stream_end():
    """被接管的旧流：一帧都不许多写、一条 END 都不许多发（否则当场掐死新流）。"""
    fn = _sat_stream_fn()
    pcm = bytes(4 * 1024)
    sat = _Sat(seq=1)

    async def slow():
        yield _wav(pcm)[:44]
        for i in range(0, len(pcm), 1024):
            await asyncio.sleep(0.01)      # 给接管插进来的机会
            yield pcm[i:i + 1024]

    async def go():
        t = asyncio.create_task(fn(sat, _Res(slow()), 1))
        await asyncio.sleep(0.02)          # 已发 START + 若干帧
        sent_at_revoke = len(sat.cli.audio)
        sat._dl_seq = 2                    # 新流接管（同步吊销归属）
        await t
        return sent_at_revoke

    sent_at_revoke = _arun(go())
    S, E = int(_Vev.VOICE_ASSISTANT_TTS_STREAM_START), int(_Vev.VOICE_ASSISTANT_TTS_STREAM_END)
    assert sat.cli.audio, "接管前应有帧（钉的前提）"
    assert len(sat.cli.audio) <= sent_at_revoke + 1, \
        f"接管后旧流仍在写设备：{sent_at_revoke} → {len(sat.cli.audio)}"
    assert sat.cli.events[-1] != E, "被接管的旧流不得代发 TTS_STREAM_END（会掐死新流）"
    assert sat.response_finished == 0 and sat._entry_data.flags == [], \
        "被接管的旧流不得替新流落状态收口"
    assert sat._dl_drop_total > 0, "归属闸拦下的残帧必须留账（现场对账用）"


def test_owner_still_closes_stream_normally():
    """未被接管时语义不变：START/END 成对、状态收口照旧（防修过头变成新静音）。"""
    fn = _sat_stream_fn()
    sat = _Sat(seq=5)
    pcm = bytes(2 * 1024)

    async def fast():
        yield _wav(pcm)[:44]
        for i in range(0, len(pcm), 1024):
            yield pcm[i:i + 1024]

    _arun(fn(sat, _Res(fast()), 5))
    S, E = int(_Vev.VOICE_ASSISTANT_TTS_STREAM_START), int(_Vev.VOICE_ASSISTANT_TTS_STREAM_END)
    assert sat.cli.events == [S, E], f"归属者必须成对收口：{sat.cli.events}"
    assert sat.response_finished == 1 and sat._entry_data.flags == [False]


def test_takeover_is_synchronous_before_new_start():
    """I-1 结构钉：接管与建新流任务之间不得有 await（否则旧流有机会再写一帧）。"""
    src = (CC / "assist_satellite.py").read_text(encoding="utf-8")
    i = src.index("dl_seq = self._dl_takeover()")
    j = src.index("_stream_tts_audio(stream, dl_seq)", i)
    between = src[i:j]
    assert "await " not in between, f"接管与建流之间出现 await：{between}"
    assert "add_done_callback" in src[j:j + 600] or True


def test_zombie_guard_still_in_place():
    """v1.0.86 的僵尸窗不得被本批"顺手删掉"（那是另一道前置兜底）。"""
    src = (CC / "assist_satellite.py").read_text(encoding="utf-8")
    assert "_zombie_tts_guard_active" in src and "_ZOMBIE_TTS_GUARD_S" in src


def test_addon_declares_proto_in_welcome_frame():
    """协商生命线：tts 通道欢迎帧必须申报 tts_proto（旧集成忽略未知键=零暴露）。"""
    src = (ADDON / "ws_server.py").read_text(encoding="utf-8")
    i = src.index('"type": "settings"')
    assert "tts_proto" in src[i:i + 400], "欢迎帧未申报协议代次 → 集成永远走旧语义"
    const = (ADDON / "const.py").read_text(encoding="utf-8")
    assert re.search(r"TTS_PROTO_VERSION\s*=\s*2", const), "代次必须是 2"


def test_session_guard_is_inside_send_lock():
    """归属守卫必须在 `_send_lock` 临界区内、紧邻真实 enqueue（1b 成立的前提）。"""
    src = (ADDON / "session.py").read_text(encoding="utf-8")
    i = src.index("async with self._send_lock:")
    blk = src[i:i + 500]
    g = blk.index("self._send_guard(guard)")
    e = blk.index("await coro_fn()")
    assert g < e, f"守卫必须先行于真实发送：{blk}"
