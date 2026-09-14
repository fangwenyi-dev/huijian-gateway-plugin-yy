"""根因①钉桩：TtsTransport.stream 的 anyio cancel scope 绝不横跨 yield（v1.0.69）。

现场（2026-09-14 日志三处炸点）：
  ERROR [homeassistant.components.tts] Error getting audio for 已经帮你执行了:
        huijian TTS 读取失败: Attempted to exit cancel scope in a different task
        than it was entered in
  ERROR [custom_components.huijian_ai.assist_satellite] [TTS] 下行流异常：同上
  WARNING [huijian.tts_transport] TTS 对话读取异常: 同上
根因：旧实现 `with anyio.fail_after(timeout):` 把 `yield data` 包在 scope 内。
anyio CancelScope 任务仿射，而 HA TTS 管线天然跨任务驱动本生成器——进入
=provider 调用任务（tts.py:287 peek 首块），续跑/收口=core
`async_create_background_task(_load_data_into_cache)` 后台任务 → `__exit__`
与 `__enter__` 异任务 → RuntimeError → 被 except 吞成「读取失败」→ 整句播报
作废（卫星路每条收口必炸、盘缓存在竞态下被毒）。

本钉不复制逻辑：AST 从真实源码摘出 stream 方法执行（真 anyio + 假 transport
部件），场景逐一对账旧行为，另钉一条「旧形态必须炸」的前提证伪守卫——若
anyio 将来不再任务仿射，该前提测试红，提醒重审本修复而非静默漂移。
"""
import ast
import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "custom_components" / "huijian_ai" / "huijian" / "tts_transport.py"


class _Dict(dict):
    """同化 huijian.Dict 的用法：stream() 只 yield Dict(error=…) 与 bytes。"""


def _extract_stream():
    """从真实源码摘 TtsTransport.stream 为模块级协程函数（不复制逻辑）。"""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "TtsTransport")
    fn = next(n for n in cls.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "stream")
    mod = ast.Module(body=[fn], type_ignores=[])
    ns = {"anyio": anyio, "asyncio": asyncio, "time": time, "Dict": _Dict}
    exec(compile(mod, str(SRC), "exec"), ns)
    return ns["stream"]


stream = _extract_stream()


class FakeTransport:
    def __init__(self, timeout_ok=True):
        self._request_lock = asyncio.Lock()
        self.sent = []
        self.restarts = []
        self.logger = logging.getLogger("test.scope")
        self._drained = False

    async def ensure_connected(self):
        return True

    async def _drain_stale(self):
        self._drained = True

    async def send_message(self, payload):
        self.sent.append(payload)

    async def restart_connection(self, reason):
        self.restarts.append(reason)


def _stop(truncated=None):
    s = SimpleNamespace(state="stop")
    if truncated:
        s.truncated = truncated
    return s


def _run(coro):
    return asyncio.run(coro)


# ── 前提守卫：旧形态（scope 包 yield）跨任务收口必炸 ──────────────────
async def _old_style(recv):
    with anyio.fail_after(30):
        async for data in recv:
            yield data


def test_premise_anyio_scope_across_yield_is_task_bound():
    """钉住修复成立的前提：async generator 在任务A进入 fail_after 并停在
    yield，任务B aclose → anyio 必抛现场同款 RuntimeError。若哪天 anyio 行
    为变了，本测试红 = 重审 stream() 结构，而不是静默失效。"""
    async def scenario():
        send, recv = anyio.create_memory_object_stream(1)
        gen = _old_style(recv)
        await send.send(b"f1")
        # 任务A：驱动到首块 yield（scope 在 A 进入、随挂起存活）
        assert await gen.__anext__() == b"f1"
        # 任务B：收口（scope 在 B 退出）→ 现场报错原文
        async def closer():
            try:
                await gen.aclose()
                return None
            except BaseException as e:  # noqa: BLE001
                return e
        err = await asyncio.create_task(closer())
        await send.aclose()
        await recv.aclose()
        return err
    err = _run(scenario())
    assert isinstance(err, RuntimeError)
    assert "cancel scope in a different task" in str(err)


# ── 新形态：跨任务收口安全 + 全部旧收口语义保留 ─────────────────────
def test_cross_task_close_no_cancel_scope_error():
    """任务A peek 首块、任务B aclose：绝不许再抛 scope 错；且非 stop 收口
    照常断连清算（v1.0.45 不变量）。"""
    async def scenario():
        send, recv = anyio.create_memory_object_stream(10)
        t = FakeTransport()
        t._recv_reader = recv
        gen = stream(t, "你好", 30)
        await send.send(b"f1")
        assert await gen.__anext__() == b"f1"          # 任务A 进入
        async def closer():
            try:
                await gen.aclose()                     # 任务B 收口
                return None
            except BaseException as e:  # noqa: BLE001
                return e
        err = await asyncio.create_task(closer())
        assert err is None, f"跨任务收口仍炸: {err!r}"
        assert len(t.restarts) == 1, "非 stop 收口必须断连清算"
        await send.aclose(); await recv.aclose()
    _run(scenario())


def test_stop_ends_clean_without_restart():
    async def scenario():
        send, recv = anyio.create_memory_object_stream(10)
        t = FakeTransport()
        t._recv_reader = recv
        gen = stream(t, "你好", 30)
        await send.send(b"f1")
        await send.send(_stop())
        assert await gen.__anext__() == b"f1"
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
        assert t.restarts == [], "stop 收口不得断连"
        assert t.sent and t.sent[0]["state"] == "detect"
    _run(scenario())


def test_truncated_stop_raises_error_and_restarts():
    """v1.0.55 定案②语义原样保留。"""
    async def scenario():
        send, recv = anyio.create_memory_object_stream(10)
        t = FakeTransport()
        t._recv_reader = recv
        gen = stream(t, "你好", 30)
        await send.send(_stop(truncated=True))
        last = None
        async for item in gen:
            last = item
        assert isinstance(last, _Dict) and "截断" in last["error"]
        assert len(t.restarts) == 1
    _run(scenario())


def test_eof_midstream_errors_not_clean_exhaustion():
    """v1.0.65 T1：EOF=未收到 stop，必须 error 收口防缓存毒化。"""
    async def scenario():
        send, recv = anyio.create_memory_object_stream(10)
        t = FakeTransport()
        t._recv_reader = recv
        gen = stream(t, "你好", 30)
        await send.send(b"f1")
        await send.aclose()                            # 静默断流
        assert await gen.__anext__() == b"f1"
        last = None
        async for item in gen:
            last = item
        assert isinstance(last, _Dict) and "未收到 stop" in last["error"]
        assert len(t.restarts) == 1
    _run(scenario())


def test_total_deadline_preserved():
    """timeout 语义不变：预算从 detect 起算一次，耗尽 → Response timeout +
    断连清算（对齐旧整轮 fail_after）。timeout=0 → 首圈即超时无 receive。"""
    async def scenario():
        send, recv = anyio.create_memory_object_stream(10)
        t = FakeTransport()
        t._recv_reader = recv
        gen = stream(t, "你好", 0)
        first = await gen.__anext__()
        assert isinstance(first, _Dict) and first["error"] == "Response timeout"
        # 真实链：实体见 error raise 后 finally aclose → 从 yield 恢复走
        # return/展开 → finally 断连清算落地
        await gen.aclose()
        assert len(t.restarts) == 1
        await send.aclose(); await recv.aclose()
    _run(scenario())
