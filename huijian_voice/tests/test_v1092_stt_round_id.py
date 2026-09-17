# -*- coding: utf-8 -*-
"""v1.0.92 STT 轮次身份（stt_proto+rid）钉。

现场链（2026-09-17 真机日志 + 前会话定案）：
  STT 事务发送相被 HA 拆掉 → 锁当场释放但连接保持 → 服务端 ≤52s 预算后仍按
  契约回一条 {"type":"stt"} → 新轮正持锁，旧回执被锁判放行落进新轮 reader
  队列 → 要么被新轮认成本轮转写（张冠李戴），要么旧回执挂住 buffer-0 交付
  → 30s 交付判死换连（现场"stt 通道消息交付超时 30s"反复条目）。
修法（TTS Stage-1 的对偶）：客户端 mint rid，listen start/stop 携带，服务端
回显进该轮回执；交付点(_on_incoming)+消费点双闸配对；未协商逐字节旧形态。
"""
import ast
import asyncio
import json
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

# anyio 只在集成运行期存在（HA 侧 aioesphomeapi 拉入），加载项 Lint 的精简
# requirements 不含它——顶层若硬 `import anyio` 会让 collection 直接
# ModuleNotFoundError、"1 error during collection" 拖垮整个 CI（v1.0.91 无此顶层
# import 故绿；本文件 v1.0.92 新加，实为集成侧代码的桩化测试）。⇒ importorskip：
# CI 无 anyio 时整模块优雅跳过（STT rid 属集成侧，由装有 anyio 的 dev/HA 覆盖），
# 有则照跑。"收集期取真身、防 test_integration_link_stability 运行期换 sys.modules
# 不还原"的 v1064 防御语义在有 anyio 的环境里完全不变。
anyio = pytest.importorskip("anyio")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.session import SttSession, TtsSession                  # noqa: E402

CC = ROOT / "custom_components" / "huijian_ai" / "huijian"
SRC = (CC / "stt_transport.py").read_text(encoding="utf-8")


class _WS:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_str(self, s):
        self.sent.append(json.loads(s))

    async def send_bytes(self, b):
        self.sent.append(b)


def _mk_ctx(text="开灯", slow=False):
    ctx = SimpleNamespace()

    async def transcribe(pcm):
        if slow:
            await asyncio.sleep(1.0)
        return text
    ctx.asr = SimpleNamespace(transcribe_pcm=transcribe)
    ctx.decoder_factory = lambda: SimpleNamespace(decode=lambda p: b"\x01" * 960)
    return ctx


def _drive(coro):
    return asyncio.run(coro)


def _await_stt(ws, tries=100):
    async def wait():
        for _ in range(tries):
            if any(isinstance(x, dict) and x.get("type") == "stt" for x in ws.sent):
                return True
            await asyncio.sleep(0.02)
        return False
    return _drive(wait())


def _first_stt(ws):
    return next(x for x in ws.sent if isinstance(x, dict) and x.get("type") == "stt")


# ────────────────────────── 服务端（真实 SttSession 行为） ────────────────────
def test_server_echoes_rid_from_start():
    ws = _WS()
    s = SttSession(ws, _mk_ctx("打开客厅灯"))
    _drive(s.on_text(json.dumps({"type": "listen", "state": "start", "rid": 77})))
    _drive(s.on_binary(b"\x00" * 40))
    _drive(s.on_text(json.dumps({"type": "listen", "state": "stop"})))
    assert _await_stt(ws), ws.sent
    reply = _first_stt(ws)
    assert reply["text"] == "打开客厅灯" and reply["rid"] == 77, \
        "start 带 rid ⇒ 回执必回显同 rid"


def test_server_legacy_shape_untouched():
    """不带 rid ⇒ 回执逐字节旧形状（只有 type/text 两键）。"""
    ws = _WS()
    s = SttSession(ws, _mk_ctx("hi"))
    _drive(s.on_text(json.dumps({"type": "listen", "state": "start"})))
    _drive(s.on_binary(b"\x00" * 40))
    _drive(s.on_text(json.dumps({"type": "listen", "state": "stop"})))
    assert _await_stt(ws)
    assert set(_first_stt(ws)) == {"type", "text"}


def test_server_stop_rid_counts_and_malformed_fails_open():
    ws = _WS()
    s = SttSession(ws, _mk_ctx())
    _drive(s.on_text(json.dumps({"type": "listen", "state": "start"})))
    _drive(s.on_binary(b"\x00" * 40))
    _drive(s.on_text(json.dumps({"type": "listen", "state": "stop", "rid": 5})))
    assert _await_stt(ws)
    assert _first_stt(ws).get("rid") == 5
    # 畸形 rid 折 0（按旧协议跑，绝不误带）
    assert SttSession._parse_rid({"rid": "-3"}) == 0
    assert SttSession._parse_rid({"rid": "abc"}) == 0
    assert SttSession._parse_rid({}) == 0
    assert TtsSession._parse_rid({"rid": 9}) == 9   # 上提 BaseSession 两通道共用


def test_server_preempt_reply_carries_preempted_round_rid():
    """被抢占的空收束回**被抢占轮**的 rid（新轮 rid 不得顶包）。"""
    ws = _WS()
    s = SttSession(ws, _mk_ctx(slow=True))

    async def main():
        await s.on_text(json.dumps({"type": "listen", "state": "start", "rid": 11}))
        await s.on_binary(b"\x00" * 40)
        await s.on_text(json.dumps({"type": "listen", "state": "stop"}))   # 轮A 起跑
        await asyncio.sleep(0.05)
        await s.on_text(json.dumps({"type": "listen", "state": "start", "rid": 12}))
        await s.on_binary(b"\x00" * 40)
        await s.on_text(json.dumps({"type": "listen", "state": "stop", "rid": 12}))
        first = [x for x in ws.sent if isinstance(x, dict) and x.get("type") == "stt"]
        assert first and first[0]["text"] == "" and first[0]["rid"] == 11, \
            f"被抢占轮应收束成 rid=11 空回执，实得 {ws.sent}"
    _drive(main())


# ───────────────────── 客户端（stt_transport 源码/行为钉） ────────────────────
def test_client_source_negotiation_wiring():
    assert "stt_proto" in SRC and "_STT_PROTO_RID" in SRC   # 协商捕获
    assert SRC.count('**({"rid": rid} if rid else {})') == 3  # start+stop+空轮stop
    assert "旧轮回执配错门" in SRC and "消费点跳过别轮回执" in SRC  # 双闸
    assert "Connection replaced mid-round" in SRC          # 换连不 (None,None)
    srv = (ROOT / "core" / "ws_server.py").read_text(encoding="utf-8")
    assert '"stt_proto": const.STT_PROTO_VERSION' in srv   # 建连申报
    c = (ROOT / "core" / "const.py").read_text(encoding="utf-8")
    assert "STT_PROTO_VERSION = 2" in c


class _Dictish(dict):
    __getattr__ = dict.get


def _extract(name, is_async=False):
    tree = ast.parse(SRC)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "SttTransport")
    t = ast.AsyncFunctionDef if is_async else ast.FunctionDef
    fn = next(n for n in cls.body if isinstance(n, t) and n.name == name)
    return fn


def _mk_self(locked, rid=42):
    lock = asyncio.Lock()

    async def acquire():
        await lock.acquire()
    if locked:
        _drive(acquire())
    return SimpleNamespace(
        _request_lock=lock, _round_rid=rid, _conn_gen=1,
        _crossed_total=0, _unclaimed_total=0, _proto=2,
        logger=SimpleNamespace(warning=lambda *a: None))


def test_client_on_incoming_drops_crossed_rid_keeps_legacy():
    mod = types.ModuleType("stub")
    body = [_extract("_rid_of"), _extract("_on_incoming")]
    exec(compile(ast.Module(body=body, type_ignores=[]), "<s>", "exec"),
         mod.__dict__)
    on_incoming = mod.__dict__["_on_incoming"]
    rid_of = mod.__dict__["_rid_of"]

    s = _mk_self(locked=True)
    assert on_incoming(s, _Dictish(type="stt", text="旧", rid=41)) is True, \
        "别轮回执必须交付点就地丢"
    assert on_incoming(s, _Dictish(type="stt", text="我的", rid=42)) is False
    assert on_incoming(s, _Dictish(type="stt", text="legacy")) is False, \
        "无 rid fail-open（旧服务端形态不哑管线）"
    assert on_incoming(s, _Dictish(type="settings", stt_proto=2)) is False
    s2 = _mk_self(locked=False)
    assert on_incoming(s2, _Dictish(type="stt", text="x", rid=42)) is True, \
        "无锁=旧判据（无消费者丢）"
    assert rid_of(s, _Dictish(type="stt", rid="9")) == 9
    assert rid_of(s, _Dictish(type="stt", rid=None)) == 0


def test_client_recognize_attaches_rid_when_negotiated():
    """AST 提取 recognize（v1064 同法）：协商轮 start/stop 带 rid，未协商旧形状。"""
    fn = _extract("recognize", is_async=True)
    seg = ast.get_source_segment(SRC, fn)
    seg = "\n".join(l[4:] if l.startswith("    ") else l for l in seg.splitlines())
    seg = seg.replace("_SEND_TIMEOUT_S", "0.3").replace("_FIRST_CHUNK_TIMEOUT_S", "30")
    ns = {"asyncio": asyncio, "anyio": anyio, "logging": logging,
          "_LOGGER": logging.getLogger("t"), "_STT_PROTO_RID": 2}
    exec(compile(seg, "<recognize>", "exec"), ns)
    recognize = ns["recognize"]

    async def chunks():
        yield b"\x00" * 8

    async def build(proto):
        sent = []
        st = SimpleNamespace(rid=100)

        async def sm(m):
            sent.append(m)

        async def ok():
            return True

        async def hello():
            return None

        async def restart(*a):
            return None

        class R:
            def __init__(self):
                self.items = [_Dictish(type="stt", text="开灯",
                                       **({"rid": st.rid} if proto >= 2 else {}))]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.items:
                    await asyncio.sleep(30)
                return self.items.pop(0)

        self = SimpleNamespace(
            _request_lock=asyncio.Lock(), _drain_stale=lambda: 0,
            ensure_connected=ok, send_hello=hello, send_message=sm,
            restart_connection=restart, _recv_reader=R(),
            logger=SimpleNamespace(warning=lambda *a: None,
                                   debug=lambda *a: None),
            _proto=proto, _next_rid=lambda: st.rid, _round_rid=0,
            _crossed_total=0, _conn_gen=1)
        text, err = await recognize(self, chunks(), timeout=2)
        return sent, text, err

    sent, text, err = asyncio.run(build(2))
    dicts = [m for m in sent if isinstance(m, dict)]
    assert dicts[0]["state"] == "start" and dicts[0]["rid"] == 100
    assert dicts[-1]["state"] == "stop" and dicts[-1]["rid"] == 100
    assert text == "开灯" and err is None
    sent, text, err = asyncio.run(build(0))
    dicts = [m for m in sent if isinstance(m, dict)]
    assert "rid" not in dicts[0] and "rid" not in dicts[-1], "旧协议逐字节不变"
    assert text == "开灯" and err is None


def test_client_consumer_skips_crossed_reply():
    """消费点兜底：先投别轮回执再投本轮 ⇒ 返回本轮文本，restart 不因它触发。"""
    fn = _extract("recognize", is_async=True)
    seg = ast.get_source_segment(SRC, fn)
    seg = "\n".join(l[4:] if l.startswith("    ") else l for l in seg.splitlines())
    seg = seg.replace("_SEND_TIMEOUT_S", "0.3").replace("_FIRST_CHUNK_TIMEOUT_S", "30")
    ns = {"asyncio": asyncio, "anyio": anyio, "logging": logging,
          "_LOGGER": logging.getLogger("t"), "_STT_PROTO_RID": 2}
    exec(compile(seg, "<recognize>", "exec"), ns)
    recognize = ns["recognize"]

    async def chunks():
        yield b"\x00" * 8

    async def build():
        sent = []
        restarts = []

        async def sm(m):
            sent.append(m)

        async def ok():
            return True

        async def hello():
            return None

        async def restart(*a):
            restarts.append(a)

        class R:
            def __init__(self):
                self.items = [
                    _Dictish(type="stt", text="旧轮的", rid=99),   # 先到的别轮
                    _Dictish(type="stt", text="本轮的", rid=100),
                ]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.items:
                    await asyncio.sleep(30)
                return self.items.pop(0)

        self = SimpleNamespace(
            _request_lock=asyncio.Lock(), _drain_stale=lambda: 0,
            ensure_connected=ok, send_hello=hello, send_message=sm,
            restart_connection=restart, _recv_reader=R(),
            logger=SimpleNamespace(warning=lambda *a: None,
                                   debug=lambda *a: None),
            _proto=2, _next_rid=lambda: 100, _round_rid=0,
            _crossed_total=0, _conn_gen=1)
        text, err = await recognize(self, chunks(), timeout=2)
        return text, err, restarts

    text, err, restarts = asyncio.run(build())
    assert text == "本轮的" and err is None
    assert not restarts, "别轮回执只丢不拆连（30s 判死换连根修的判据本体）"

