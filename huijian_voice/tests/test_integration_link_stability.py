"""集成侧链路稳定性回归钉（v1.0.40）。

背景（两条都是"真机上偶发、日志里看不见"的缺陷）：
- C：`huijian/ws_transport.py` 的 `ensure_connected` 在掉线退避窗内会重复 spawn
  连接循环（循环句柄从未记住）→ 两条循环互相覆盖 stream/`_current_ws`，先起那条
  的发送任务消费"孤儿 reader"变僵尸连接，现场是"这一轮 Response timeout / 没声"。
- E2：`assist_satellite.py` 的上行音频队列 `asyncio.Queue()` 无 maxsize →
  管线消费停顿时按 ≈32KB/s 无界涨 HA 内存。

本仓对 HA 依赖文件（不可本地 import）历来用"源码级钉 + CI e2e"。本文件在此之上
再进一步，给出**行为级证据**：
1) E2 用 AST 从出厂源码里抽出 `_queue_audio_chunk` 本体直接执行（不是抄一份）；
2) C 用桩模块注入后**真调** `WsTransport.ensure_connected`，数 spawn 次数。
"""
import ast
import asyncio
import importlib.util
import re
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "huijian_ai"
WS_TRANSPORT = INTEGRATION / "huijian" / "ws_transport.py"
ASSIST_SATELLITE = INTEGRATION / "assist_satellite.py"


# ── E2：有界音频队列（行为级，跑出厂源码本体）────────────────────
def _load_queue_helper():
    """从 assist_satellite.py 抽 `_queue_audio_chunk` 源码并执行（免 HA 依赖）。"""
    src = ASSIST_SATELLITE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    seg = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_queue_audio_chunk":
            seg = ast.get_source_segment(src, node)
            break
    assert seg, "assist_satellite.py 缺 _queue_audio_chunk（有界队列实现被改没了？）"
    ns = {"asyncio": asyncio}
    exec(compile(seg, str(ASSIST_SATELLITE), "exec"), ns)   # noqa: S102
    maxsize = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", "") == "_MAX_AUDIO_QUEUE_CHUNKS":
                    maxsize = ast.literal_eval(node.value)
    assert isinstance(maxsize, int) and maxsize > 0, "队列上限常量缺失或非正"
    return ns["_queue_audio_chunk"], maxsize


def test_audio_queue_is_bounded_and_drops_oldest():
    put_chunk, maxsize = _load_queue_helper()
    q = asyncio.Queue(maxsize=maxsize)
    for i in range(maxsize):
        assert put_chunk(q, bytes([i % 256])) is False, "未满不该报丢弃"
    assert q.qsize() == maxsize

    # 溢出：丢最旧、保最新，且必须如实上报（供限频告警）
    assert put_chunk(q, b"NEWEST") is True
    assert q.qsize() == maxsize, "溢出后仍须卡在上限（这就是本修复的全部意义）"
    items = [q.get_nowait() for _ in range(maxsize)]
    assert items[-1] == b"NEWEST"
    assert bytes([0]) not in items, "应丢最旧（第 0 块）"


def test_audio_queue_sentinel_never_lost_when_full():
    """哨兵 None 丢了会让管线永不收束（设备只能等自己的会话超时）——必须保入队。"""
    put_chunk, maxsize = _load_queue_helper()
    q = asyncio.Queue(maxsize=maxsize)
    for i in range(maxsize):
        put_chunk(q, b"x")
    assert put_chunk(q, None) is True
    assert q.qsize() == maxsize
    items = [q.get_nowait() for _ in range(maxsize)]
    assert items[-1] is None, "满队列下哨兵也必须入队"


def test_assist_satellite_all_enqueue_sites_are_bounded():
    """源码级钉：4 个入队点（API 音频 / UDP 数据 / 两个哨兵 + UDP error）全部
    走有界 helper，不许再有裸 `put_nowait` 逃逸。"""
    src = ASSIST_SATELLITE.read_text(encoding="utf-8")
    assert "maxsize=_MAX_AUDIO_QUEUE_CHUNKS" in src, "队列未设上限"
    assert "_queue_audio_chunk(self._audio_queue" in src
    assert "self._audio_queue.put_nowait(" not in src, (
        "仍有裸 put_nowait：满队列会抛 QueueFull / 无界增长"
    )


# ── C：ensure_connected 不得重复 spawn 连接循环（行为级）──────────
def _load_ws_transport():
    """注入 HA/anyio 桩后按包内模块加载出厂 ws_transport.py。"""
    def stub(name, **attrs):
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    exc_mod = stub("homeassistant.exceptions")
    exc_mod.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    stub("homeassistant")
    conf = stub("homeassistant.config_entries")
    conf.ConfigEntry = type("ConfigEntry", (), {})
    core = stub("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})

    anyio = stub("anyio")
    mem = stub("anyio.streams")
    streams = stub("anyio.streams.memory")
    streams.MemoryObjectReceiveStream = type("MemoryObjectReceiveStream", (), {})
    streams.MemoryObjectSendStream = type("MemoryObjectSendStream", (), {})
    # 类体注解/默认值在 import 期就会求值 → 用到的 anyio 名字都要在
    anyio.CancelScope = type("CancelScope", (), {})
    anyio.create_memory_object_stream = lambda *a, **k: (None, None)
    anyio.create_task_group = lambda *a, **k: None
    anyio.fail_after = lambda *a, **k: None
    anyio.streams = mem
    mem.memory = streams

    pkg = types.ModuleType("huijian")
    pkg.Dict = dict                      # `from . import Dict`
    pkg.__path__ = []                    # 令其成为包，相对导入可解析
    sys.modules["huijian"] = pkg

    spec = importlib.util.spec_from_file_location("huijian.ws_transport",
                                                 WS_TRANSPORT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["huijian.ws_transport"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeTask:
    def __init__(self, done=False):
        self._done = done

    def done(self):
        return self._done


class _FakeEntry:
    """记录 spawn 次数；不真跑连接循环（把协程关掉，避免 never-awaited 告警）。"""

    def __init__(self):
        self.spawns = 0

    def async_create_background_task(self, hass, coro, name):
        self.spawns += 1
        coro.close()
        return _FakeTask(done=False)


def _make_transport(mod):
    t = mod.WsTransport(hass=object(), entry=_FakeEntry(),
                        endpoint="ws://h:8000/x", attr_endpoint="stt_endpoint")
    return t


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    """ensure_connected 的等待窗是 150×0.1s——压成 0 让测试秒级完成。

    只影响 `asyncio.sleep`（连接循环退避用 `_wait_backoff`→`wait_for`，走 loop
    定时器，不受影响，故"叫醒"语义仍被真实验证）。
    """
    async def _no_sleep(_s):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)


def test_ensure_connected_does_not_spawn_second_loop():
    """核心钉：退避重连窗内（循环活着但未连上）再来请求 → 只等待、不再 spawn。"""
    mod = _load_ws_transport()
    t = _make_transport(mod)
    t._loop_task = _FakeTask(done=False)      # 模拟"循环正在退避重连"
    t._is_connected = False

    ok = asyncio.run(t.ensure_connected())

    assert ok is False
    assert t.entry.spawns == 0, "已有活循环时重复 spawn = 本 bug 复发"
    assert t._connect_now.is_set(), "必须叫醒正在退避的循环，否则白等满 15s 等待窗"


def test_wait_backoff_wakes_immediately_instead_of_sleeping_full_backoff():
    """v1.0.40 配套钉：叫醒必须真生效——已叫醒不打盹、睡中被唤醒即重试。

    （若只会"等既有循环"，而那条循环正睡 60s 退避，修复后反而比旧行为更慢——
    这正是本修复必须配 `_connect_now` 的原因，故用时间上界把它钉住。）
    """
    mod = _load_ws_transport()
    t = _make_transport(mod)

    async def scenario():
        # ① 进睡前已被叫醒 → 立刻返回（若真睡 5s，下面的时间断言会红）
        t._connect_now.set()
        start = time.monotonic()
        await t._wait_backoff(5)
        assert time.monotonic() - start < 1, "已叫醒还在睡 = 白等"
        assert not t._connect_now.is_set(), "叫醒是一次性的，用完要清"

        # ② 睡中被叫醒 → 提前返回
        async def nudge():
            await asyncio.sleep(0.05)
            t._connect_now.set()

        task = asyncio.ensure_future(nudge())
        start = time.monotonic()
        await t._wait_backoff(5)
        await task
        assert time.monotonic() - start < 4, "睡中叫醒没生效（退避没被打断）"
        assert not t._connect_now.is_set()

    asyncio.run(scenario())


def test_ensure_connected_spawns_when_no_loop_and_recovers_after_done():
    """反面钉：无循环要 spawn；循环已结束（done）要能再 spawn——别修成永久卡死。"""
    mod = _load_ws_transport()

    t1 = _make_transport(mod)
    t1._loop_task = None
    assert asyncio.run(t1.ensure_connected()) is False
    assert t1.entry.spawns == 1

    t2 = _make_transport(mod)
    t2._loop_task = _FakeTask(done=True)      # 上一条循环已退出
    assert asyncio.run(t2.ensure_connected()) is False
    assert t2.entry.spawns == 1, "循环结束后必须能重新 spawn（否则永不自愈）"


def test_ensure_connected_returns_true_when_loop_connects():
    """已连接的快路径不变。"""
    mod = _load_ws_transport()
    t = _make_transport(mod)
    t._is_connected = True
    t._current_ws = types.SimpleNamespace(closed=False)
    assert asyncio.run(t.ensure_connected()) is True
    assert t.entry.spawns == 0


def test_ws_transport_source_pins():
    """源码级钉（防后续重构悄悄退回旧写法）。"""
    src = WS_TRANSPORT.read_text(encoding="utf-8")
    assert "self._loop_task = None" in src
    assert "self._loop_task is not None and not self._loop_task.done()" in src
    assert "self._loop_task = self.entry.async_create_background_task(" in src
    # 句柄必须落进 self._loop_task；裸 `task = …`（旧写法，赋值即丢弃）不许回来
    assert not re.search(r"(?m)^\s*task = self\.entry\.async_create_background_task",
                         src), "句柄又被丢弃了——重复 spawn 的根因就是这个赋值不落地"
