"""v1.0.83 播报截尾根治钉（"有时不能完整播报"三 bug 的回归面）。

根因（2026-09-15 审计）：
  #1 播报链的预算是**每轮墙钟**（加载项整流 52s、集成 transport 60s），而
     固件 v2.1.42/44 已把设备端改成"帧间隙心跳"语义并按 4000 字≈950s 音频
     推导 1200s 硬顶——三端账不拢，长文本必然在第 52 秒被"整流超预算截断"，
     truncated 以 error 收口 → STREAM_END 早发 → 固件只播前半段。
     修法：52s/60s 语义收窄为**最大帧间隙窗**（合法最坏单句间隙 46s 仍被
     覆盖，v2.1.44 账），另设**宽松整轮总闸**（加载项 660s < 集成 720s <
     固件 1200s，保持"服务端先收口、客户端只兜底"的既有排序）。
  #2 _drain_stale_pipeline 超时后旧 run 仍活着，其迟到 TTS_END 会在
     on_pipeline_event 无条件起推流任务，把上一轮音频灌进新轮（现场=半句
     播报/串音）。修法：轮任务身份甄别（事件恒在当前轮 task 内联派发）。
  #3 _tts_streaming_task 自然完成后从不回写 None，RUN_END 的
     "本轮无 TTS"判据永久失效，assist_pipeline_state 卡 True。

钉面：
  A/B 行为钉（真 anyio）：trickle 流不得被截断（旧形态必红）、间隙真死
     必须截断（语义存续）、总闸兜底必须截断；
  C 算术钉（新⑧口径，改任何一项必须重算）；
  D/E 形态钉 + _zombie_tts_guard_active（v1.0.86 撤身份闸后的窄僵尸窗）/
     _clear_tts_streaming_task 行为；
  F _round_outer_task 记账（core 会重绑 _pipeline_task 为内层任务，
     done-callback 身份判据必须同时认外层）。
"""
import ast
import asyncio
import importlib.util
import json
import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "huijian_ai"
TTS_TRANSPORT = INTEGRATION / "huijian" / "tts_transport.py"
WS_TRANSPORT = INTEGRATION / "huijian" / "ws_transport.py"
SAT = INTEGRATION / "assist_satellite.py"
SESSION = ROOT / "core" / "session.py"
CONST = ROOT / "core" / "const.py"


def _load_transport_modules():
    """桩注入后真调 TtsTransport（与 test_v1045 同惯例）。"""
    def stub(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    try:
        import homeassistant  # noqa: F401
    except ImportError:
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


pytest.importorskip("anyio", reason="需真 anyio（内存流/超时语义）")


@pytest.fixture()
def _real_anyio():
    """隔离同目录其它文件对 sys.modules 的 anyio/huijian 桩注入（v1045 同款
    纪律：test_integration_link_stability 运行期会把 sys.modules["anyio"]
    换成假 lambda 垫片，懒 import 拿到即瘫——行为钉必须跑在真库上）。"""
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


# ── A：慢而持续的帧流不得被截断（旧"整轮墙钟"形态在此必红）──────
def test_trickle_frames_must_not_truncate(_real_anyio):
    """帧间隙 0.18s < 间隙窗 0.8s，但 8 帧总跨 ≈1.4s > 旧整轮 0.8s。
    新语义：全部帧交付 + 以 stop 干净收口（无 restart、无 error 帧）。
    （窗/距取"全量套件负载下仍宽裕"的量级：判别结构=总跨>窗>帧距。）"""
    mod2, Dict = _load_transport_modules()

    async def scenario():
        import anyio
        t = _bare_transport(mod2)

        async def feed():
            await anyio.sleep(0.05)   # 让消费端先进入 receive
            for i in range(8):
                await t._recv_writer.send(b"f%02d" % i)
                await anyio.sleep(0.18)
            await t._recv_writer.send(Dict({"type": "tts", "state": "stop"}))
        async with anyio.create_task_group() as tg:
            tg.start_soon(feed)
            got = [p async for p in t.stream("句", timeout=0.8)]
        return t, got

    t, got = asyncio.run(scenario())
    assert got == [b"f%02d" % i for i in range(8)], (
        f"#1 回潮：慢而持续的流被整轮墙钟截断（收到 {got!r}）")
    assert not t.restart_calls, "以 stop 收口不得断连清算"


# ── A2：帧间隙真死仍必须按超时收口（间隙语义存续）────────────
def test_idle_gap_violation_still_times_out(_real_anyio):
    mod2, Dict = _load_transport_modules()

    async def scenario():
        import anyio
        t = _bare_transport(mod2)

        async def feed():
            await anyio.sleep(0.05)
            await t._recv_writer.send(b"only-one")
            # 之后永远静默：间隙 > timeout → 必须 error 收口
        async with anyio.create_task_group() as tg:
            job = tg.start_soon(feed)
            out = []
            async for p in t.stream("句", timeout=0.8):
                out.append(p)
                if not isinstance(p, bytes):
                    tg.cancel_scope.cancel()
            return out

    out = asyncio.run(scenario())
    errs = [o for o in out if not isinstance(o, bytes)]
    assert errs and getattr(errs[0], "error", None) and "timeout" in errs[0].error.lower(), \
        f"间隙真死未按超时收口：{out!r}"


# ── B：加载项 session 逐帧间隙预算（旧形态：第 0.2s 腰斩，必红）──
def _bare_session(packets, delay=0.0, hang=False, send_ok=True):
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
            if hang:
                await asyncio.sleep(30)

    s.ctx = types.SimpleNamespace(tts=Eng())
    s.send_json = send_json
    s.send_bytes = send_bytes
    return s, sent


def _stops(sent):
    return [f for f in sent["json"] if f.get("state") == "stop"]


def test_session_trickle_completion_not_truncated():
    """8 帧、帧间 0.08s < 间隙窗 0.5s，总跨 0.64s > 旧整轮 0.5s：
    新语义必须完整交付且 stop 不带 truncated。"""
    from core import const as core_const
    s, sent = _bare_session(8, delay=0.08)
    orig = core_const.TTS_STREAM_BUDGET_S
    core_const.TTS_STREAM_BUDGET_S = 0.5
    try:
        asyncio.run(s._stream("长播报 trickle", 0))
    finally:
        core_const.TTS_STREAM_BUDGET_S = orig
    assert sent["bytes"] == 8, (
        f"#1 回潮：整轮墙钟把持续产帧的长播报腰斩（只发了 {sent['bytes']} 帧）")
    stops = _stops(sent)
    assert len(stops) == 1 and "truncated" not in stops[0]


def test_session_frame_still_marks_truncated_on_real_stall():
    """单帧后永挂 = 间隙真死：必须按截尾收口（防缓存毒化语义存续）。"""
    from core import const as core_const
    s, sent = _bare_session(1, hang=True)
    orig = core_const.TTS_STREAM_BUDGET_S
    core_const.TTS_STREAM_BUDGET_S = 0.05
    try:
        asyncio.run(s._stream("停滞", 0))
    finally:
        core_const.TTS_STREAM_BUDGET_S = orig
    stops = _stops(sent)
    assert len(stops) == 1 and stops[0].get("truncated") is True


def test_session_round_total_gate_backstops_forever_trickle():
    """帧帧合法但永不停产：整轮总闸兜底截尾。"""
    from core import const as core_const

    class Eng:
        async def stream_opus(self, text, engine_out=None):
            i = 0
            while True:
                await asyncio.sleep(0.02)
                i += 1
                yield b"\xf8" + bytes([i % 256])

    s, sent = _bare_session(0)
    s.ctx = types.SimpleNamespace(tts=Eng())
    o_gap, o_total = core_const.TTS_STREAM_BUDGET_S, getattr(
        core_const, "TTS_STREAM_TOTAL_BUDGET_S", None)
    core_const.TTS_STREAM_BUDGET_S = 2.0
    core_const.TTS_STREAM_TOTAL_BUDGET_S = 0.15
    try:
        asyncio.run(s._stream("僵尸流水", 0))
    finally:
        core_const.TTS_STREAM_BUDGET_S = o_gap
        if o_total is not None:
            core_const.TTS_STREAM_TOTAL_BUDGET_S = o_total
    stops = _stops(sent)
    assert len(stops) == 1 and stops[0].get("truncated") is True, (
        f"整轮总闸丢失（帧流永动不得截断也必须有界收口）：{stops!r}")


# ── C：三端预算算术对账（新⑧口径；改任何一项必须重算）──────────
def test_budget_arithmetic_three_tier():
    sys.path.insert(0, str(ROOT))
    from core import const

    assert const.TTS_STREAM_BUDGET_S == 52.0, (
        "间隙窗=52s：固件 T_DL_STALL=48s 必须小于它（流真死由加载项显性截断先行）")
    assert const.TTS_STREAM_BUDGET_S >= 46.0 + 3.0, (
        "间隙窗必须覆盖 v2.1.44 合法最坏间隙 46s（300 字/speed 0.5/RTF 0.33）+ 发送余量")
    send = 3.0  # TtsSession._SEND_TIMEOUT_S
    # 逐帧：间隙 52 + 在飞帧 2×3 + 网络余量 2 ≤ 集成逐帧窗 60（默认排序不变）
    assert const.TTS_STREAM_BUDGET_S + 2 * send <= 60 - 2, "逐帧预算超集成间隙窗"
    total = getattr(const, "TTS_STREAM_TOTAL_BUDGET_S", None)
    assert total is not None, "缺整轮总闸（间隙语义下必须有界收口的第二道）"
    # 整轮：总闸 660 + 在飞 2×3 + 余量 2 ≤ 集成整轮闸
    src = TTS_TRANSPORT.read_text(encoding="utf-8")
    assert "_ROUND_TOTAL_BUDGET_S = 720.0" in src, "集成整轮总闸丢失/改值未同步本钉"
    assert total + 2 * send <= 720 - 2, f"加载项整轮最坏收口 {total}+2×{send} 超集成总闸"
    # 固件反僵尸硬顶 1200s（voice_assistant.cpp T_LIVE_HARD_CAP）> 集成总闸：
    # 设备端会话必须严格晚于 HA 链收口，绝不抢在协议收口前动手（v2.1.42 纪律）。
    assert 720.0 < 1200.0


# ── D：#2 串轮甄别（行为 + 接线）────────────────────────────────
def _extract_method(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            node.decorator_list = []
            ns: dict = {"asyncio": asyncio, "logging": logging}
            exec(compile(ast.Module(body=[node], type_ignores=[]),
                         str(path), "exec"), ns)
            return ns[name]
    raise AssertionError(f"{path} 缺 {name}")


# ── D：#2 串轮防护（v1.0.86 契约：撤身份闸，留窄僵尸窗）──────────
def test_identity_gate_must_be_gone():
    """v1.0.83 的任务身份闸在 09:37 案把健康轮 run-start→run-end 全序列误杀
    （事件的实际派发任务上下文与"内联于轮任务"的假设不符）。v1.0.86 起该闸
    必须整体不存在——只准拦 TTS 建流，不准拦任何事件。"""
    src = SAT.read_text(encoding="utf-8")
    assert "_is_stale_round_event" not in src, "身份闸回潮=健康轮可能被整扇误杀"


def test_zombie_guard_only_arms_on_drain_timeout():
    src = SAT.read_text(encoding="utf-8")
    i = src.index("async def _drain_stale_pipeline")
    body = src[i:src.index("async def _handle_pipeline_start_impl", i)]
    assert "self._zombie_tts_guard_until =" in body, "僵尸窗未在 drain 超时分支 arm"
    assert src.count("self._zombie_tts_guard_until =") == 1, \
        "arm 点必须唯一（只在实锤僵尸处；__init__ 声明用注解形式不计赋值）"


def test_zombie_guard_active_window_behavior():
    fn = _extract_method(SAT, "_zombie_tts_guard_active")

    class Self:
        _zombie_tts_guard_until = 0.0

    async def scenario():
        s = Self()
        assert fn(s) is False, "未 arm 不得生效（默认=全部放行）"
        s._zombie_tts_guard_until = asyncio.get_running_loop().time() + 8.0
        assert fn(s) is True
        s._zombie_tts_guard_until = asyncio.get_running_loop().time() - 0.01
        assert fn(s) is False, "窗过期必须自动失效"

    asyncio.run(scenario())


def test_zombie_guard_wired_only_at_tts_spawn():
    src = SAT.read_text(encoding="utf-8")
    i = src.index("if self._zombie_tts_guard_active():")
    # 消费点必须落在 TTS_END 建流分支内（其下紧邻建流路）
    assert "async_create_background_task" in src[i:i + 1400]
    assert src.count("self._zombie_tts_guard_active()") == 1, \
        "僵尸窗只准消费一次（TTS 建流点），不得扩面到事件转发"


# ── E：#3 推流任务句柄确定性清零 ────────────────────────────────
def test_tts_task_handle_cleared_on_completion():
    fn = _extract_method(SAT, "_clear_tts_streaming_task")

    class Self:
        _tts_streaming_task = None

    s = Self()
    t1, t2 = types.SimpleNamespace(), types.SimpleNamespace()
    s._tts_streaming_task = t1
    fn(s, t1)
    assert s._tts_streaming_task is None, "自然完成后句柄未清：RUN_END 判据将永久失真"
    s._tts_streaming_task = t2
    fn(s, t1)   # 陈旧回调（被替换的上一任务）不得动当前句柄
    assert s._tts_streaming_task is t2


def test_tts_task_spawn_registers_clear_callback():
    src = SAT.read_text(encoding="utf-8")
    assert "add_done_callback(self._clear_tts_streaming_task)" in src, (
        "推流任务未挂清零回调")


# ── F：外层轮任务记账（core 重绑 _pipeline_task 的坑）───────────
def test_round_outer_task_bookkeeping():
    src = SAT.read_text(encoding="utf-8")
    assert "self._round_outer_task = self._pipeline_task" in src, (
        "开轮时未记住外层任务（core accept 会把 _pipeline_task 重绑为内层任务）")
    assert "task is not self._round_outer_task" in src, (
        "handle_pipeline_finished 身份判据未同时认外层（v1.0.49 判据在内层重绑下失真）")
