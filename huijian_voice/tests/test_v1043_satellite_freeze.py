"""v1.0.43 卫星"连得上、不回答、永不回连"挂死根治回归钉。

现场签名（固件 v2.1.22 留痕实锤）：设备侧 send_request start=1 连打两次
"HA did not answer Request in time" → 强拆 stale HA connection → 之后
"No API client" 久无重连——不是网络，是 **HA 事件循环被 handle_pipeline_start
里的挑 pipeline 索引 while 环冻死**：该环的 `continue` 分支（wake_word select
实体在注册表但无状态——用户禁用该 CONFIG 实体、或重启窗口实体尚未 added）
不推进索引，而 get_wake_word_entity 对同一 index 恒返回同一 entity_id →
while True 死循环；环位于函数首个挂起点之前，纯同步自旋 → 整个 loop 冻结：
VoiceAssistantResponse 永不发出，aioesphomeapi 连 TCP 断开都读不到，更不重连。
官方 esphome 上游语义是**每轮无条件推进**（`while ww_entity_id := ...` +
循环尾 `+= 1`），本仓两份 huijian_ai 副本曾抄成"骨架 while True + continue"
并丢掉了推进——本批照抄上游语义重写为纯函数 _pick_wake_word_pipeline_index
并加硬上限兜底；另给 handle_pipeline_start 包一层异常→None（client 即回
error=True，设备侧从"8 秒黑屏"变"显式拒绝"，两类病因现场可再区分）。

钉桩纪律沿用 test_v1041_fixes：custom_components 依赖 homeassistant（测试环境
不装），目标方法用 ast 从真实源码外科摘出执行（_extract_func 同款占位命名空间），
其余做源码形态钉；两份副本（发布链 huijian_voice/custom_components 与商店工作
副本 yyjicheng/）同钉，防漂移。
"""
import ast
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ASAT_MAIN = ROOT / "custom_components" / "huijian_ai" / "assist_satellite.py"
ASAT_STORE = ROOT.parent / "yyjicheng" / "custom_components" / "huijian_ai" / "assist_satellite.py"


def _extract_method(path: Path, name: str, extra_ns: dict | None = None):
    """从类体内按名摘出单个方法为模块级函数执行（不复制逻辑）。

    注解/默认值在 def 时求值（≤3.13），非内建裸名字给可下标占位——与
    test_satellite_parity._extract_func 同规则；本函数额外遍历类体。
    """

    class _Ann:
        def __class_getitem__(cls, item):
            return None

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            ns: dict = {}
            args = node.args
            eager = [
                a.annotation
                for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
            ]
            if node.returns is not None:
                eager.append(node.returns)
            eager += list(args.defaults) + list(args.kw_defaults)
            for expr in eager:
                if expr is None:
                    continue
                for sub in ast.walk(expr):
                    if (
                        isinstance(sub, ast.Name)
                        and sub.id not in ns
                        and not hasattr(__import__("builtins"), sub.id)
                    ):
                        ns[sub.id] = _Ann
            if extra_ns:
                ns.update(extra_ns)
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)
            return ns[name]
    raise AssertionError(f"{path} 中未找到方法 {name}")


# ── 行为钉：真实源码的挑索引纯函数 ──────────────────────────────────────

def _pick(path: Path):
    ns = {"_MAX_WAKE_WORD_SELECTS": 16}  # 与源码同值：改源码常量须同步此处的上限语义
    return _extract_method(path, "_pick_wake_word_pipeline_index", ns)


class _States:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, entity_id):
        return self._m.get(entity_id)  # 缺 = None（禁用实体正是这个形态）


def _self_stub(ids, states, cap=None):
    """ids: dict index->entity_id|None；states: entity_id->State|None。"""
    hass = SimpleNamespace(states=_States(states))

    def get_wake_word_entity(index):
        if cap is not None and index >= cap:
            # 模拟注册表损坏：永远返回同一个 id（旧环在此挂死）
            return f"select.stuck_{index}"
        return ids.get(index)

    return SimpleNamespace(
        hass=hass,
        get_wake_word_entity=get_wake_word_entity,
    )


def _with_watchdog(fn, seconds=5):
    """自旋兜底探测：挂死即 TimeoutError（信号在字节码间投递，纯同步死环杀得掉）。"""

    def _boom(signum, frame):
        raise TimeoutError("事件循环自旋未退出（钉挂死缺陷复发）")

    old = signal.signal(signal.SIGALRM, _boom)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


@pytest.mark.parametrize("path", [ASAT_MAIN, ASAT_STORE], ids=["main", "store"])
class TestPickPipelineIndex:
    def test_terminates_when_entity_registered_but_stateless(self, path):
        """钉原炸点：两个 wake_word select 都在注册表、都无状态（禁用实体形态）
        ——旧环原地 continue 冻死整个 HA；现在必须限时返回默认 0。"""
        stub = _self_stub(
            {0: "select.sat_wake_word", 1: "select.sat_wake_word_2"},
            {},  # 注册表有 id、状态机无 state
        )
        pick = _pick(path)
        assert _with_watchdog(lambda: pick(stub, "你好小智")) == 0

    def test_broken_registry_never_spins(self, path):
        """注册表无限吐 id（cap=None 的极端形态）也必须靠硬上限收敛。"""
        stub = _self_stub({}, {}, cap=10_000)
        pick = _pick(path)
        assert _with_watchdog(lambda: pick(stub, "你好小智")) == 0

    def test_first_match_semantics_match_upstream(self, path):
        """有状态时与上游逐字同语义：首个 state==phrase 的索引，无匹配回落 0。"""
        hit = SimpleNamespace(state="你好小智")
        stub = _self_stub(
            {0: "select.a", 1: "select.b"},
            {"select.a": SimpleNamespace(state="小华"), "select.b": hit},
        )
        assert _pick(path)(stub, "你好小智") == 1

        stub_none = _self_stub({0: "select.a"}, {"select.a": SimpleNamespace(state="小华")})
        assert _pick(path)(stub_none, "你好小智") == 0

    def test_no_entities_returns_zero(self, path):
        stub = _self_stub({}, {})
        assert _pick(path)(stub, None) == 0


# ── 行为钉：wrapper 异常→None（显式 error 回设备，不再 8s 黑屏） ────────

def _wrapper(path: Path):
    ns = {"asyncio": asyncio, "_LOGGER": logging.getLogger("pin_v1043")}
    return _extract_method(path, "handle_pipeline_start", ns)


@pytest.mark.parametrize("path", [ASAT_MAIN, ASAT_STORE], ids=["main", "store"])
class TestStartWrapper:
    def test_impl_exception_returns_none(self, path):
        async def boom(self, *args):
            raise RuntimeError("tts 引擎缺键")

        stub = SimpleNamespace(_handle_pipeline_start_impl=boom)
        assert asyncio.run(_wrapper(path)(stub, "conv-1", 0, None, "你好小智")) is None

    def test_cancellation_propagates(self, path):
        """CancelledError 不得折成"正常拒绝"——那是 unsub 竞态，语义与异常不同。"""
        async def cancelled(self, *args):
            raise asyncio.CancelledError

        stub = SimpleNamespace(_handle_pipeline_start_impl=cancelled)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_wrapper(path)(stub, "conv-1", 0, None, None))

    def test_success_passes_port_through(self, path):
        async def ok(self, *args):
            return 0

        stub = SimpleNamespace(_handle_pipeline_start_impl=ok)
        assert asyncio.run(_wrapper(path)(stub, "conv-1", 0, None, None)) == 0


# ── 源码形态钉：两份副本同修同形，防"骨架回退"再引入不推进的 continue ──

@pytest.mark.parametrize("path", [ASAT_MAIN, ASAT_STORE], ids=["main", "store"])
def test_copies_share_the_fix(path):
    src = path.read_text(encoding="utf-8")
    assert "async def _handle_pipeline_start_impl(" in src, "wrapper 拆分丢失（异常兜底失效入口）"
    assert "_MAX_WAKE_WORD_SELECTS" in src, "硬上限常量丢失（自旋只剩无兜底语义）"
    assert "def _pick_wake_word_pipeline_index(" in src, "挑索引纯函数被回退成内联环"
    assert "maybe_pipeline_index" not in src, "旧内联环（不推进索引的 continue 形态）复发"
    # 真炸点逐字钉：state 缺失分支绝不允许裸 continue
    assert "if not (ww_state := self.hass.states.get(ww_entity_id)):\n                continue" not in src
    # 环内不得出现任何 continue（每轮唯一出路是 return / index += 1 / break）
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_pick_wake_word_pipeline_index":
            loops = [n for n in ast.walk(node) if isinstance(n, ast.While)]
            assert loops, "挑索引环丢失"
            for lp in loops:
                assert not any(isinstance(c, ast.Continue) for c in ast.walk(lp)), \
                    "挑索引环再现 continue（推进分支旁路）——挂死缺陷复发"
            break
    else:
        pytest.fail("_pick_wake_word_pipeline_index 未以 FunctionDef 形态存在")


# ── 对照实验：旧内联环在同一桩下必须被看门狗击毙（钉"病因属实"） ────────

def test_old_loop_form_really_spins():
    """A/B 对照：逐字复刻 v1.0.42 及以前的内联环（git 3 处同罪副本之一），
    同一"注册表有实体、状态机无 state"桩——旧环永不退出。若此测试反而通过
    （旧环能返回），说明本批对根因的归罪不成立，须重新排查。"""

    def old_loop(self, wake_word_phrase):
        active = 0
        maybe_pipeline_index = 0
        while True:
            if not (ww_entity_id := self.get_wake_word_entity(maybe_pipeline_index)):
                break
            if not (ww_state := self.hass.states.get(ww_entity_id)):
                continue
            if ww_state.state == wake_word_phrase:
                active = maybe_pipeline_index
                break
            maybe_pipeline_index += 1
        return active

    stub = _self_stub({0: "select.sat_wake_word"}, {})
    with pytest.raises(TimeoutError):
        _with_watchdog(lambda: old_loop(stub, "你好小智"), seconds=1)


def test_store_copy_present_or_skip():
    if not ASAT_STORE.exists():
        pytest.skip("yyjicheng 商店工作副本不在位")
    assert "async def handle_pipeline_start(" in ASAT_STORE.read_text(encoding="utf-8")
