"""v1.0.45 播报流完整性回归钉（"经常少字/整段静音"根因的钉桩）。

现场症状链（2026-09 客户日志 + 台架复现）：一条共享 WS 上并发/被取消的
TTS 请求互相踩——
  · 加载项侧：新 detect 顶替旧流，旧流静默作废且不发 stop（契约防孤儿帧），
    但顶替截断零日志，对账只见"播报下发 N 帧"莫名变少；
  · 集成侧：两个消费端共抢同一帧流，快的那个抢到 stop 拿到半截杂流（现场
    =播报缺字），慢的那个 60s 无 stop 超时（现场=整段静音）；被取消消费
    留下的残帧残 stop 再毒化下一轮（错位链式传播）。
  台架实锤（真加载项+真 kokoro）：并发两请求 → 请求1 收 20 帧杂流+stop、
  请求2 TIMEOUT。
修法：TtsTransport.stream() 整段对话上锁串行 + 起流前排残料 + 未以 stop
收口即断连清算（WsTransport.restart_connection）；加载项三条静默截断路径
全上 WARN。

集成侧文件依赖 homeassistant → 沿用本仓"桩模块注入后真调"惯例；行为级
测试用真 anyio（内存流/超时语义必须在真库上跑，CI 无此依赖时自动跳过）。
"""
import ast
import asyncio
import importlib.util
import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "huijian_ai"
TTS_TRANSPORT = INTEGRATION / "huijian" / "tts_transport.py"
WS_TRANSPORT = INTEGRATION / "huijian" / "ws_transport.py"
TTS_ENTITY = INTEGRATION / "tts.py"
SESSION = ROOT / "core" / "session.py"


# ── 源码级钉（零第三方依赖，CI 必跑）────────────────────────────
def test_transport_has_serialized_stream_api():
    src = TTS_TRANSPORT.read_text(encoding="utf-8")
    assert "self._request_lock = asyncio.Lock()" in src, "并发 detect 未串行化"
    assert "async def stream(" in src, "缺整段对话 API stream()"
    assert "_drain_stale" in src, "缺起流前排残料"
    assert "restart_connection" in src, "缺未以 stop 收口时的断连清算"
    assert "if not clean:" in src, "stop 收口判定丢失"


def test_ws_transport_has_restart_connection():
    src = WS_TRANSPORT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = {n.name for n in ast.walk(tree)
          if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))}
    assert "restart_connection" in fn, "WsTransport 缺 restart_connection 原语"
    assert "_connect_now.set()" in src, "restart 必须叫醒退避中的循环"
    # reader 僵尸断根：交付必须走 _deliver；超时判死**仅 TTS 通道启用**
    # （stt/llm/mcp 有"连接即回声、消费端未挂上"的合法排队窗口）
    assert "await self._recv_writer.send(msg.data)" not in src, \
        "reader 裸 send 复发 = 消费端消失后整条连接循环永卡"
    assert "_CONSUMER_HANDOFF_TIMEOUT_S: float | None = None" in src, \
        "基类默认必须保持无限等（改其它通道语义=重连风暴风险）"
    tts_src = TTS_TRANSPORT.read_text(encoding="utf-8")
    assert "_CONSUMER_HANDOFF_TIMEOUT_S = 5.0" in tts_src, \
        "TTS 通道未启用交付超时 = 僵尸 reader 复发入口"


def test_tts_entity_uses_stream_and_deterministic_close():
    src = TTS_ENTITY.read_text(encoding="utf-8")
    assert "transport.stream(message)" in src, "实体未走序列化对话 API"
    assert "await stream.aclose()" in src, "对话生成器未确定性关停（赌 GC=残料泄漏）"
    # v1.0.52：实体层拆出 _async_pcm_stream()（加载项 PCM 流）与 _convert()
    # （容器转换流）两份生成器，且新增了流式出口 async_stream_tts_audio——
    # 三条路都必须显式关停，故逐个钉住（原先是单个 `gen` 变量名）。
    assert "await pcm.aclose()" in src, "PCM 生成器未确定性关停"
    assert "await converting.aclose()" in src, "整段路径的转换生成器未确定性关停"
    assert "await converted.aclose()" in src, "流式路径的转换生成器未确定性关停"
    assert '"state": "detect"' not in src, \
        "detect 发送不得残留在实体层（必须与消费同临界区，见 tts_transport.stream）"


def test_addon_silent_truncation_paths_log_warn():
    src = SESSION.read_text(encoding="utf-8")
    for marker in ("旧流被新播报顶替截断", "整流超预算截断",
                   "合成流停滞超预算", "播报连接中断，停止下发"):
        assert marker in src, f"静默截断路径缺 WARN：{marker}"
    # 音色漂移可观测：下发日志必须带引擎名（云⇄本地回落=换嗓）
    assert "播报下发：%s / %d 帧" in src, "播报下发未标引擎 = 男/女声漂移无日志可对账"
    engine_src = (ROOT / "core" / "tts.py").read_text(encoding="utf-8")
    assert "engine_out" in engine_src, "stream_opus 缺引擎出参"
    assert "云回落" in engine_src, "本地兜底未标记回落态"


# ── 加载项侧行为钉：截断路径必须留 WARN（对账可见性）─────────────
class _RecordingWS:
    def __init__(self, fail_after=-1):
        self.sent_bin = []
        self.sent_json = []
        self.fail_after = fail_after
        self.closed = False

    async def send_bytes(self, data):
        if self.fail_after >= 0 and len(self.sent_bin) >= self.fail_after:
            raise ConnectionResetError("peer gone")
        self.sent_bin.append(data)

    async def send_str(self, data):
        self.sent_json.append(data)


class _Ctx:
    def __init__(self, packets=6, delay=0.02):
        class _T:
            def __init__(self, packets, delay):
                self.packets, self.delay = packets, delay

            async def stream_opus(self, text, engine_out=None):
                for i in range(self.packets):
                    yield bytes([0xF0]) + str(i).encode()
                    await asyncio.sleep(self.delay)
        self.tts = _T(packets, delay)
        self.settings = None


def _make_session(ws, packets=6, delay=0.02):
    sys.path.insert(0, str(ROOT))
    from core.session import TtsSession
    return TtsSession(ws, _Ctx(packets, delay))


def test_send_failure_warns_with_partial_count(caplog):
    """连接断在半截：WARN 必须点名"播报连接中断"+已发帧数。"""
    async def scenario():
        ws = _RecordingWS(fail_after=2)
        s = _make_session(ws)
        await s.on_text('{"type":"tts","state":"detect","text":"办公室的温度是 25 度。"}')
        for _ in range(300):
            await asyncio.sleep(0.01)
            if s._task.done():
                break
    with caplog.at_level(logging.WARNING, logger="huijian.session"):
        asyncio.run(scenario())
    assert "播报连接中断，停止下发" in caplog.text
    assert "已发 2 帧" in caplog.text


def test_supersede_warns_then_new_stream_closes_with_stop(caplog):
    """顶替：旧流 WARN 留痕；新流正常收口恰好一条 stop（不补孤儿 stop）。"""
    box = {}

    async def scenario():
        ws = _RecordingWS()
        box["ws"] = ws
        s = _make_session(ws, packets=20, delay=0.05)   # 流要跑 ~1s
        await s.on_text('{"type":"tts","state":"detect","text":"第一句慢慢说"}')
        await asyncio.sleep(0.2)                        # 让流1先发几帧
        await s.on_text('{"type":"tts","state":"detect","text":"第二句插进来"}')
        await s._task
    with caplog.at_level(logging.WARNING, logger="huijian.session"):
        asyncio.run(scenario())
    assert "旧流被新播报顶替截断" in caplog.text
    ws = box["ws"]
    stops = [j for j in ws.sent_json if '"stop"' in j]
    assert len(stops) == 1, "旧流不得补发孤儿 stop，新流恰好一条收口"


def test_announcement_log_reports_engine(caplog):
    """行为钉：引擎名必须原样进播报日志（音色漂移一线可辨）。"""
    box = {}

    async def scenario():
        ws = _RecordingWS()
        box["ws"] = ws
        ctx = _Ctx(packets=3, delay=0.01)

        async def tagged(text, engine_out=None):
            if engine_out is not None:
                engine_out["engine"] = "local:sid18(云回落)"
            for i in range(3):
                yield b"\xf0" + bytes([i])
        ctx.tts.stream_opus = tagged
        sys.path.insert(0, str(ROOT))
        from core.session import TtsSession
        s = TtsSession(ws, ctx)
        await s.on_text('{"type":"tts","state":"detect","text":"收口句"}')
        await s._task
    caplog.set_level(logging.INFO, logger="huijian.session")
    asyncio.run(scenario())
    assert "播报下发：local:sid18(云回落) / 3 帧" in caplog.text


# ── 集成侧行为钉（真 anyio）：收口/隔离/排残料/串行 ──────────────
@pytest.fixture()
def _real_anyio():
    """隔离同目录其它文件对 sys.modules 的 anyio/huijian 桩注入。

    test_integration_link_stability 会 stub sys.modules["anyio"]（假 lambda），
    本文件的行为测试必须跑在**真 anyio** 上（内存流/fail_after 语义），故先摘
    桩再触发真导入，测试结束原样放回（不污染别人）。
    """
    keys = [k for k in list(sys.modules)
            if k == "anyio" or k.startswith("anyio.") or k == "huijian"
            or k.startswith("huijian.")]
    saved = {k: sys.modules[k] for k in keys}
    for k in keys:
        del sys.modules[k]
    yield
    for k in [k for k in list(sys.modules)
              if k == "anyio" or k.startswith("anyio.") or k == "huijian"
              or k.startswith("huijian.")]:
        del sys.modules[k]
    sys.modules.update(saved)


def _load_modules():
    pytest.importorskip("anyio")
    import anyio  # noqa: F401  真库必须在桩清除后再导入

    def stub(name, **attrs):
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    if "homeassistant" not in sys.modules:
        stub("homeassistant")
        conf = stub("homeassistant.config_entries")
        conf.ConfigEntry = type("ConfigEntry", (), {})
        core = stub("homeassistant.core")
        core.HomeAssistant = type("HomeAssistant", (), {})
        exc = stub("homeassistant.exceptions")
        exc.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})

    class Dict(dict):
        def __getattr__(self, item):
            return self.get(item)

    pkg = types.ModuleType("huijian")
    pkg.Dict = Dict
    pkg.EntryAuthFailedError = RuntimeError
    pkg.get_entry_data = lambda hass, entry, **kw: {}
    pkg.__path__ = []
    sys.modules["huijian"] = pkg

    spec = importlib.util.spec_from_file_location("huijian.ws_transport", WS_TRANSPORT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["huijian.ws_transport"] = mod
    spec.loader.exec_module(mod)

    spec2 = importlib.util.spec_from_file_location("huijian.tts_transport", TTS_TRANSPORT)
    mod2 = importlib.util.module_from_spec(spec2)
    sys.modules["huijian.tts_transport"] = mod2
    spec2.loader.exec_module(mod2)
    return mod2, Dict


class _FakeEntry:
    def async_create_background_task(self, hass, coro, name):
        coro.close()
        return types.SimpleNamespace(done=lambda: True)


def _bare_transport(mod2):
    import anyio
    t = mod2.TtsTransport(hass=object(), entry=_FakeEntry(),
                          endpoint="ws://h:8000/x", attr_endpoint="tts_endpoint")
    t._recv_writer, t._recv_reader = anyio.create_memory_object_stream(32)

    async def _aye():
        return True
    t.ensure_connected = _aye
    t.restart_calls = []

    async def restart(reason=""):
        t.restart_calls.append(reason)
    t.restart_connection = restart

    t.sent = []

    async def send_capture(msg):
        t.sent.append(msg)
    t.send_message = send_capture
    return t


def test_stream_clean_stop_no_restart(_real_anyio):
    mod2, Dict = _load_modules()

    async def scenario():
        import anyio
        t = _bare_transport(mod2)
        async def feed():
            await t._recv_writer.send(b"f1")
            await t._recv_writer.send(b"f2")
            await t._recv_writer.send(Dict({"type": "tts", "state": "stop"}))
        async with anyio.create_task_group() as tg:
            tg.start_soon(feed)
            got = [p async for p in t.stream("句1")]
        assert [g for g in got if isinstance(g, bytes)] == [b"f1", b"f2"]
        assert t.restart_calls == [], "以 stop 收口不该断连"
        assert t.sent == [{"type": "tts", "state": "detect", "text": "句1"}]
    asyncio.run(scenario())


def test_stream_abort_quarantines_connection(_real_anyio):
    mod2, Dict = _load_modules()

    async def scenario():
        import anyio
        t = _bare_transport(mod2)
        async def feed():
            await t._recv_writer.send(b"g1")
            await t._recv_writer.send(b"g2")
            await t._recv_writer.send(Dict({"type": "tts", "state": "stop"}))
        async with anyio.create_task_group() as tg:
            tg.start_soon(feed)
            agen = t.stream("句2")
            first = await agen.__anext__()
            assert first == b"g1"
            await agen.aclose()
            tg.cancel_scope.cancel()
        assert len(t.restart_calls) == 1, "中途取消必须 restart_connection"
    asyncio.run(scenario())


def test_stream_drains_stale_before_detect(_real_anyio):
    mod2, Dict = _load_modules()

    async def scenario():
        import anyio
        t = _bare_transport(mod2)
        # 上一轮被取消留下的残帧+残 stop（未走 restart 的兜底场景）
        await t._recv_writer.send(b"stale")
        await t._recv_writer.send(Dict({"type": "tts", "state": "stop"}))
        async def feed():
            await t._recv_writer.send(b"h1")
            await t._recv_writer.send(Dict({"type": "tts", "state": "stop"}))
        async with anyio.create_task_group() as tg:
            tg.start_soon(feed)
            got = [p async for p in t.stream("句3")]
        assert [g for g in got if isinstance(g, bytes)] == [b"h1"], \
            "残帧混进本轮 = 缺字/杂音错位复发"
    asyncio.run(scenario())


def test_stream_serializes_concurrent_requests(_real_anyio):
    """并发两请求：第二条 detect 必须在第一条 stop 收口后才发出（顶替复发钉）。"""
    mod2, Dict = _load_modules()

    async def scenario():
        import anyio
        t = _bare_transport(mod2)
        release = anyio.Event()
        second_seen = anyio.Event()
        orig_restart = t.restart_connection

        async def send_capture(msg):
            t.sent.append(msg)
            if msg["text"] == "乙":
                second_seen.set()
        t.send_message = send_capture

        async def feed():
            await t._recv_writer.send(b"x1")        # 甲的帧，不给 stop，等 release
            await release.wait()
            await t._recv_writer.send(Dict({"type": "tts", "state": "stop"}))
            await second_seen.wait()                # 乙的 round
            await t._recv_writer.send(b"y1")
            await t._recv_writer.send(Dict({"type": "tts", "state": "stop"}))

        out = {}

        async def c1():
            out["甲"] = [p async for p in t.stream("甲") if isinstance(p, bytes)]
        async def c2():
            out["乙"] = [p async for p in t.stream("乙") if isinstance(p, bytes)]

        async with anyio.create_task_group() as tg:
            tg.start_soon(feed)
            tg.start_soon(c1)
            await asyncio.sleep(0.05)
            tg.start_soon(c2)
            await asyncio.sleep(0.05)
            assert [m["text"] for m in t.sent] == ["甲"], "并发 detect 未被串行（踩流复发）"
            release.set()
        assert [m["text"] for m in t.sent] == ["甲", "乙"]
        assert out["甲"] == [b"x1"] and out["乙"] == [b"y1"], "帧流串话"
        assert t.restart_calls == [], "两条都以 stop 收口不该断连"
    asyncio.run(scenario())


def test_deliver_timeout_self_heals_reader(_real_anyio):
    """消费端消失时交付必须判死返回 False（reader 才能 break 走重连）。"""
    mod2, _ = _load_modules()

    async def scenario():
        import anyio
        t = _bare_transport(mod2)
        t._CONSUMER_HANDOFF_TIMEOUT_S = 0.1
        # 换成 buffer-0（与真 _create_streams 同构）：满缓冲会吞掉阻塞语义
        t._recv_writer, t._recv_reader = anyio.create_memory_object_stream(0)
        assert await t._deliver(t._recv_writer, b"orphan") is False, \
            "无消费者交付未判死 = 僵尸 reader 复发"
        # 有消费者时正常交付
        async with anyio.create_task_group() as tg:
            box = {}
            async def grab():
                box["v"] = await t._recv_reader.receive()
            tg.start_soon(grab)
            await asyncio.sleep(0.05)
            assert await t._deliver(t._recv_writer, b"ok") is True
            await asyncio.sleep(0.05)
            tg.cancel_scope.cancel()
        assert box["v"] == b"ok"
    asyncio.run(scenario())


# ── 音色归属定案（用户 2026-09-18 三条款）行为钉 ───────────────────
def _fb_engine(provider, sid_cfg):
    from core.tts import TtsEngine

    class S:
        _d = {"tts.provider": provider, "tts.sid": sid_cfg, "tts.speed": 1.0,
              "tts.cache_enabled": False,
              "tts.cloud": {"base_url": "http://x", "voice": "anna"}}

        def get(self, dotted, default=None):
            return self._d.get(dotted, default)

    eng = TtsEngine(S(), None)
    seen = []

    async def cloud_ok(text):
        if provider.startswith("cloud") and eng.settings._d.get("_boom"):
            raise RuntimeError("boom")
        yield b"\xfa"
    eng._cloud_stream = cloud_ok
    eng._synth = lambda sent, sid, speed: (seen.append(sid), b"\x00" * 640)[1]
    eng.ensure_loaded = lambda: True
    eng.ready = lambda: True
    return eng, seen


def _run_stream(eng):
    import asyncio
    eo = {}

    async def go():
        async for _ in eng.stream_opus("打开灯", engine_out=eo):
            pass
    asyncio.run(go())
    return eo


def test_cloud_fallback_forces_default_sid_not_web():
    """条款③：云失败切回本地=固定默认音色 18，web 设定（81）不参与回落。"""
    eng, seen = _fb_engine("cloud_openai_compat", 81)
    eng.settings._d["_boom"] = True
    eo = _run_stream(eng)
    assert seen and all(s == 18 for s in seen), seen
    assert eo["engine"] == "local:sid18(云回落)"


def test_cloud_success_uses_cloud_voice():
    """条款②：云正常=云嗓，本地引擎根本不进。"""
    eng, seen = _fb_engine("cloud_openai_compat", 81)
    eo = _run_stream(eng)
    assert seen == [] and eo["engine"] == "cloud:anna"


def test_local_provider_keeps_web_sid():
    """条款①：本地档=web 设定音色（数字直用）。"""
    eng, seen = _fb_engine("local_kokoro", 81)
    eo = _run_stream(eng)
    assert seen == [81] and eo["engine"] == "local:sid81"
