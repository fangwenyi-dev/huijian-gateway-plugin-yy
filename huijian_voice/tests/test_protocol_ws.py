"""小智协议子集服务器契约——WS 级测试（aiohttp 真服务 + 真 WS 客户端）。

每条断言对应《小智协议子集-服务器契约.md》一节：
  C1 STT：stop ⇒ 恰好一条 {"type":"stt"}；无 binary；静音也回 text:""
  C2 TTS：detect ⇒ binary 帧流 + 恰一条 stop；JSON 帧不得含 error 键；新 detect 顶旧流无孤儿帧
  C3 LLM：detect ⇒ start → sentence_end(data 字段) → end（end 恒发且最后）
  C4 认证：错误 token=HTTP 401（握手层）；缺 token 在 require=false 放行；true 拒绝
  C5 通用：ping→pong；hello 可回 hello；服务端请求后不 close
"""
import asyncio
import json
import threading
from dataclasses import dataclass

import pytest
from aiohttp import ClientSession, WSMsgType, web

from core.ws_server import AppContext, make_ws_app


class FakeASR:
    def __init__(self, text="打开客厅的灯"):
        self.text = text
        self.last_used = 0
        self.calls = []

    def ready(self):
        return True

    async def transcribe_pcm(self, pcm):
        self.calls.append(len(pcm))
        return self.text


class FakeTts:
    def __init__(self, packets=3, delay=0.01, prefix=b"OPUS"):
        self.packets, self.delay, self.prefix = packets, delay, prefix

    def ready(self):
        return True

    async def stream_opus(self, text, engine_out=None):
        for i in range(self.packets):
            yield self.prefix + str(i).encode()
            await asyncio.sleep(self.delay)


class FakeDecoder:
    def decode(self, packet):
        return b"\x01\x02" * 960      # 假 pcm 定长


class FakePipeline:
    def __init__(self, reply="好的，客厅的灯打开了。还有别的吩咐？"):
        self.reply = reply

    async def handle(self, text, origin="", on_sentence=None):
        @dataclass
        class R:
            text: str
            source: str = "t0"
            streamed: bool = False
        return R(self.reply)


class FakeSettings:
    def __init__(self, require=False, token="sekret"):
        self.d = {"security": {"require_token": require, "ws_token": token}}

    def get(self, k, default=None):
        cur = self.d
        for part in k.split("."):
            if part not in cur:
                return default
            cur = cur[part]
        return cur


@pytest.fixture()
def server():
    """独立线程持续 pump 服务事件循环（早期版本让 loop 闲置=客户端永久挂起）。"""
    ctx = AppContext(settings=FakeSettings(), asr=FakeASR(), tts=FakeTts(),
                     pipeline=FakePipeline())
    ctx.decoder_factory = FakeDecoder
    app = make_ws_app(ctx)
    loop = asyncio.new_event_loop()
    holder = {}

    def run():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    th = threading.Thread(target=run, daemon=True)
    th.start()

    async def start():
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        holder["runner"] = runner
        return runner.addresses[0][1]

    port = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
    yield port, ctx
    asyncio.run_coroutine_threadsafe(holder["runner"].cleanup(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)
    th.join(5)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _connect(sess, port, channel, token="sekret"):
    q = f"?token={token}" if token is not None else ""
    return await sess.ws_connect(f"ws://127.0.0.1:{port}/xiaozhi/v1/{channel}{q}")


async def _collect(ws, n_text, timeout=6.0):
    texts, bins = [], []
    deadline = asyncio.get_event_loop().time() + timeout
    while len(texts) < n_text:
        remain = deadline - asyncio.get_event_loop().time()
        if remain <= 0:
            break
        msg = await ws.receive(remain)
        if msg.type == WSMsgType.TEXT:
            texts.append(json.loads(msg.data))
        elif msg.type == WSMsgType.BINARY:
            bins.append(msg.data)
        else:
            break
    return texts, bins


# ── C1 STT ─────────────────────────────────────────────────────
def test_stt_single_reply(server):
    port, ctx = server

    async def go():
        async with ClientSession() as sess:
            ws = await _connect(sess, port, "stt")
            await ws.send_str(json.dumps({"type": "hello", "audio_params": {"format": "opus"}}))
            await ws.send_str('{"type":"listen","state":"start","mode":"detect"}')
            for _ in range(5):
                await ws.send_bytes(b"\xfe\xfe" * 40)
            await ws.send_str('{"type":"listen","state":"stop"}')
            texts, bins = await _collect(ws, 2)
            await ws.close()
            stt = [t for t in texts if t.get("type") == "stt"]
            assert len(stt) == 1
            assert stt[0]["text"] == "打开客厅的灯"
            assert not bins                          # STT 通道禁 binary
            assert ctx.asr.calls == [5 * 1920]       # 5 帧假解码 ×1920B
    _run(go())


def test_stt_silence_replies_empty(server):
    port, _ = server

    async def go():
        async with ClientSession() as sess:
            ws = await _connect(sess, port, "stt", token=None)   # 缺 token 放行（require=false）
            await ws.send_str('{"type":"listen","state":"start"}')
            await ws.send_str('{"type":"listen","state":"stop"}')
            texts, _ = await _collect(ws, 1)
            await ws.close()
            assert texts and texts[0]["type"] == "stt" and texts[0]["text"] == ""
    _run(go())


def test_stt_ping_pong(server):
    port, _ = server

    async def go():
        async with ClientSession() as sess:
            ws = await _connect(sess, port, "stt")
            await ws.send_str('{"type":"ping"}')
            texts, _ = await _collect(ws, 1)
            await ws.close()
            assert texts[0].get("type") == "pong"
    _run(go())


# ── C2 TTS ─────────────────────────────────────────────────────
def test_tts_frames_order(server):
    port, _ = server

    async def go():
        async with ClientSession() as sess:
            ws = await _connect(sess, port, "tts")
            await ws.send_str('{"type":"tts","state":"detect","text":"好的"}')
            seen_bin, last = 0, None
            for _ in range(8):
                msg = await asyncio.wait_for(ws.receive(), 6)
                if msg.type == WSMsgType.BINARY:
                    assert last != "stop", "stop 后又来 binary（孤儿帧）"
                    seen_bin += 1
                elif msg.type == WSMsgType.TEXT:
                    obj = json.loads(msg.data)
                    assert "error" not in obj, "JSON 帧含 error 键=客户端判错"
                    if obj.get("state") == "stop":
                        last = "stop"
                        assert seen_bin == 3
                        break
            await ws.close()
            assert last == "stop"
    _run(go())


def test_tts_takeover_no_orphans(server):
    """连续两条 detect：第一条流被打断，只允许第二条流的收束 stop。"""
    port, ctx = server

    async def go():
        ctx.tts = FakeTts(packets=60, delay=0.05)
        async with ClientSession() as sess:
            ws = await _connect(sess, port, "tts")
            await ws.send_str('{"type":"tts","state":"detect","text":"第一条慢慢说"}')
            await asyncio.sleep(0.15)
            await ws.send_str('{"type":"tts","state":"detect","text":"第二条顶上来"}')
            stops = 0
            deadline = asyncio.get_event_loop().time() + 10
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.receive(), 8)
                except asyncio.TimeoutError:
                    break
                if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("state") == "stop":
                    stops += 1
                    break            # 第一条 stop=第二条流的收束；再多即孤儿
            await ws.close()
            assert stops == 1
    _run(go())


class _TtsEmptyAware:
    """真实引擎形态：空/纯空白文本 split 出 0 句=零帧收束（FakeTts 恒吐帧，
    测不出空 detect 语义，单独做一个忠实形态）。"""

    def __init__(self, packets=3, delay=0.01):
        self.packets, self.delay = packets, delay
        self.texts = []

    def ready(self):
        return True

    async def stream_opus(self, text, engine_out=None):
        self.texts.append(text)
        if not text.strip():
            return
        for i in range(self.packets):
            yield b"OPUS" + str(i).encode()
            await asyncio.sleep(self.delay)


def test_tts_empty_detect_replies_stop(server):
    """审查修复（2026-09-21）契约钉：自家契约「每 detect 必有 stop」——旧版
    `and text` 把空文本 detect 静默吞掉：客户端 fail_after(60) 白等一整分钟
    且全程持有播报通道锁（tts.speak message="" 可达，core schema 不拦空串）。
    现在空 detect/纯空白 detect 都必须零帧+恰一条干净 stop。"""
    port, ctx = server

    async def go():
        fake = _TtsEmptyAware()
        ctx.tts = fake
        async with ClientSession() as sess:
            for probe in ('{"type":"tts","state":"detect","text":""}',
                          '{"type":"tts","state":"detect","text":"   "}'):
                ws = await _connect(sess, port, "tts")
                await ws.send_str(probe)
                texts, bins = await _collect(ws, 1, timeout=6)
                await ws.close()
                assert not bins, "空 detect 不得产帧"
                assert len(texts) == 1 and texts[0].get("state") == "stop", \
                    f"空 detect 未收束 stop（契约破口回潮）: {texts}"
                assert "truncated" not in texts[0], "空文本是完整收束，不是截断"
        assert fake.texts == ["", ""], "整流必须真正走到 stream_opus（统一路径）"
    _run(go())


def test_tts_empty_detect_takeover_converges(server):
    """慢流在飞时空 detect 顶入：旧流截断（不发 stop），空流零帧收束恰一条
    stop——顶替语义对空文本同样成立，通道不得留下悬挂轮。"""
    port, ctx = server

    async def go():
        ctx.tts = FakeTts(packets=60, delay=0.05)
        async with ClientSession() as sess:
            ws = await _connect(sess, port, "tts")
            await ws.send_str('{"type":"tts","state":"detect","text":"第一条慢慢说"}')
            await asyncio.sleep(0.15)
            ctx.tts = _TtsEmptyAware()
            await ws.send_str('{"type":"tts","state":"detect","text":""}')
            stops = 0
            deadline = asyncio.get_event_loop().time() + 10
            while asyncio.get_event_loop().time() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.receive(), 8)
                except asyncio.TimeoutError:
                    break
                if msg.type == WSMsgType.TEXT:
                    obj = json.loads(msg.data)
                    if obj.get("state") == "stop":
                        stops += 1
                        assert "truncated" not in obj
                        break
            await ws.close()
            assert stops == 1, "空 detect 顶替后 stop 数量/形状不对"
    _run(go())


# ── C3 LLM ─────────────────────────────────────────────────────
def test_llm_frame_sequence(server):
    port, _ = server

    async def go():
        async with ClientSession() as sess:
            ws = await _connect(sess, port, "llm")
            await ws.send_str('{"type":"listen","state":"detect","mode":"prompt","text":"打开客厅的灯"}')
            frames = []
            while True:
                msg = await asyncio.wait_for(ws.receive(), 6)
                if msg.type == WSMsgType.TEXT:
                    obj = json.loads(msg.data)
                    frames.append(obj)
                    if obj.get("state") == "end":
                        break
                if len(frames) > 20:
                    raise AssertionError("帧数失控")
            await ws.close()
            assert frames[0] == {"type": "text", "state": "start"}
            assert frames[-1] == {"type": "text", "state": "end"}
            mids = frames[1:-1]
            assert mids and all(f["state"] == "sentence_end" for f in mids)
            assert all("data" in f and isinstance(f["data"], str) for f in mids)  # data 字段硬约束
            assert any("客厅" in f["data"] for f in mids)
    _run(go())


def test_llm_link_stays_open(server):
    """请求后服务端不得 close（持久连接，两回合验证）。"""
    port, _ = server

    async def go():
        async with ClientSession() as sess:
            ws = await _connect(sess, port, "llm")
            for _ in range(2):
                await ws.send_str('{"type":"listen","state":"detect","text":"测试"}')
                while True:
                    msg = await asyncio.wait_for(ws.receive(), 6)
                    if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("state") == "end":
                        break
            assert not ws.closed
            await ws.close()
    _run(go())


# ── C4 认证 ────────────────────────────────────────────────────
def test_bad_token_401_pre_upgrade(server):
    port, _ = server

    async def go():
        async with ClientSession() as sess:
            with pytest.raises(Exception) as ei:
                await sess.ws_connect(f"ws://127.0.0.1:{port}/xiaozhi/v1/stt?token=WRONG")
            assert "401" in str(ei.value)
    _run(go())


def test_require_token_mode(server):
    port, ctx = server
    ctx.settings.d["security"]["require_token"] = True

    async def go():
        async with ClientSession() as sess:
            with pytest.raises(Exception) as ei:
                await sess.ws_connect(f"ws://127.0.0.1:{port}/xiaozhi/v1/tts")
            assert "401" in str(ei.value)
            ws = await _connect(sess, port, "tts")   # 对 token 仍可
            await ws.close()
    _run(go())


# ── healthz ───────────────────────────────────────────────────
def test_healthz(server):
    port, _ = server

    async def go():
        async with ClientSession() as sess:
            async with sess.get(f"http://127.0.0.1:{port}/healthz") as r:
                assert r.status == 200
                obj = await r.json()
                assert obj["ok"] and obj["asr_ready"]
    _run(go())


# ── /discover 端点自描述（三项目适配判定书缺口2 的服务器侧修法）──────────


async def _get(port, path="/discover"):
    async with ClientSession() as sess:
        async with sess.get(f"http://127.0.0.1:{port}{path}") as r:
            return r.status, await r.json()


@pytest.fixture()
def discover_server():
    def _mk(require, token="sekret-token-abc"):
        ctx = AppContext(settings=FakeSettings(require=require, token=token))
        loop = asyncio.new_event_loop()
        holder = {}

        def run():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        threading.Thread(target=run, daemon=True).start()

        async def start():
            runner = web.AppRunner(make_ws_app(ctx), access_log=None)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            holder["runner"] = runner
            return runner.addresses[0][1]

        port = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
        return port, loop, holder
    return _mk



def test_discover_no_token_leak(discover_server):
    """免 token 模式：回端点路径+require_token=false，但响应体任何位置
    不得出现 token（无凭据发现面泄 token=安全设计归零）。"""
    port, loop, holder = discover_server(require=False)
    try:
        st, body = asyncio.run_coroutine_threadsafe(_get(port), loop).result(10)
        assert st == 200
        assert body["require_token"] is False
        assert set(body["endpoints"]) == {"stt", "tts", "llm"}
        assert all(u.endswith(f"/xiaozhi/v1/{k}") for k, u in body["endpoints"].items())
        assert body["audio"] == {"format": "opus", "sample_rate": 16000, "frame_ms": 60}
        assert "sekret-token-abc" not in json.dumps(body), "token 泄漏"
    finally:
        asyncio.run_coroutine_threadsafe(holder["runner"].cleanup(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)


def test_discover_require_token_mode(discover_server):
    """强制校验模式：require_token=true 如实上报（设备端据此决定带 token），
    同样零 token 泄漏。"""
    port, loop, holder = discover_server(require=True)
    try:
        st, body = asyncio.run_coroutine_threadsafe(_get(port), loop).result(10)
        assert st == 200 and body["require_token"] is True
        assert "sekret-token-abc" not in json.dumps(body)
    finally:
        asyncio.run_coroutine_threadsafe(holder["runner"].cleanup(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
