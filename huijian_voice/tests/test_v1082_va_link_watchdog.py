"""v1.0.82 钉桩：VA 链路活性看门狗 + start 回调自计时（"HA 忙 8 秒"归因/自愈）。

现场 18:14 案残余面：设备 apiClients=1/vaSubscribed=1、keepalive 正常，但
VoiceAssistantRequest 无人应答且 ReconnectLogic 不重建（它只认 TCP 断，不认
应用层不应答）。本批两刀：manager 侧 ~90s device_info() 应用层往返探针（与
VA 请求同派发路径，6s 不回→强制整 client 重建）；satellite 侧 start 回调
>1s WARN（把迟滞从玄学变日志）。行为钉打 _arm 的门禁真身，循环体走源级结构钉。
"""
import ast
import asyncio
import logging
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CC = ROOT / "custom_components" / "huijian_ai"
MGR = CC / "manager.py"
SAT = CC / "assist_satellite.py"


def _extract_method(path: Path, name: str, extra_ns: dict | None = None):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            # 去装饰器（@callback 等在测试空间无意义）
            node.decorator_list = []
            ns: dict = {"asyncio": asyncio, "_LOGGER": logging.getLogger("pin")}
            if extra_ns:
                ns.update(extra_ns)
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), ns)
            return ns[name]
    raise AssertionError(f"{path.name} 缺方法 {name}")


class _FakeDi:
    def __init__(self, va):
        self._va = va

    def voice_assistant_feature_flags_compat(self, ver):
        return 0b111 if self._va else 0


def _fake_mgr(va=True, di=True, task=None):
    created = []

    def _mk(hass, coro, name):
        created.append(name)
        coro.close()
        return "TASK"

    entry = types.SimpleNamespace(
        entry_id="abc123",
        async_create_background_task=_mk,
    )
    self = types.SimpleNamespace(
        _va_watch_task=task,
        entry=entry,
        hass=None,
        _va_link_watchdog=lambda: asyncio.sleep(0),   # 行为钉只测门禁，循环体走源级
        entry_data=types.SimpleNamespace(
            device_info=_FakeDi(va) if di else None,
            api_version=1,
        ),
    )
    return self, created


# ── 行为钉：_arm 门禁（幂等 + 仅 VA 设备 + device_info 缺位不装）──────────

def test_arm_only_for_va_devices():
    arm = _extract_method(MGR, "_arm_va_link_watchdog")
    self, created = _fake_mgr(va=False)
    arm(self)
    assert created == [] and self._va_watch_task is None, "无 VA 能力设备不得装狗"


def test_arm_idempotent_and_creates_once():
    arm = _extract_method(MGR, "_arm_va_link_watchdog")
    self, created = _fake_mgr(va=True)
    arm(self)
    assert created == ["huijian-va-link-watchdog"], "首装必须挂任务"
    arm(self)
    assert len(created) == 1, "重复 on_connect 不得双装"


def test_arm_no_device_info_noop():
    arm = _extract_method(MGR, "_arm_va_link_watchdog")
    self, created = _fake_mgr(di=False)
    arm(self)
    assert created == []


# ── 源级结构钉：看门狗循环与接线 ──────────────────────────────────────

def test_watchdog_loop_structure():
    src = MGR.read_text(encoding="utf-8")
    i = src.index("async def _va_link_watchdog")
    body = src[i:src.index("async def on_connect_error")]
    assert "asyncio.wait_for(self.cli.device_info(), timeout=6.0)" in body, \
        "探针必须是应用层往返（与 VoiceAssistantRequest 同派发路径）"
    # v1.0.87 改档：现场 12:17:36 那次重建里 disconnect() 自己等满 10s 抛库级
    # ERROR 栈（半僵死连接本就回不了 ack），且 90s 节奏赶不上设备 8s×2=16s 的耐心。
    # 超时仍必须强制重建，但 3s 拿不到回执就转公开重载 API；节奏降到 45s。
    assert "except TimeoutError" in body and "timeout=3.0" in body, \
        "超时必须强制断连，且不得干等 10s 回执"
    assert "async_schedule_reload" in body, "断开失败必须转条目重载（重建不排队）"
    assert "await asyncio.sleep(45.0)" in body, "探活节奏必须保持 45s（90s 慢于设备熔断）"
    assert "if not self._link_up" in body, "断线中不得抢 ReconnectLogic 的地盘"
    assert "半僵死" in body, "重建动作必须留 WARN（现场可归因）"


def test_link_up_flags_wired():
    src = MGR.read_text(encoding="utf-8")
    assert "self._link_up = True" in src and "self._arm_va_link_watchdog()" in src, \
        "on_connect 成功路径必须置位+武装"
    assert "self._link_up = False" in src, "on_disconnect 必须停探"
    assert '"_va_watch_task"' in src and '"_link_up"' in src, "__slots__ 登记（v1051 同规）"


def test_start_callback_timing_pin():
    src = SAT.read_text(encoding="utf-8")
    assert "start 回调耗时" in src, "归因钉 B 丢失：HA 忙 8 秒重新变玄学"
    assert "_dt > 1.0" in src and "8s 应答预算" in src
    i = src.index("async def handle_pipeline_start(")
    body = src[i:src.index("async def _drain_stale_pipeline")]
    assert "asyncio.get_running_loop().time()" in body, "计时必须走 loop 单调钟（真身内）"


def test_slots_static_guard():
    """v1.0.51 静态校验复跑：manager 类内所有 self.X= 赋值都在 __slots__。"""
    tree = ast.parse(MGR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ESPHomeManager":
            slots = set()
            assigns = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and any(
                        t.id == "__slots__" for t in sub.targets if isinstance(t, ast.Name)):
                    for el in sub.value.elts:
                        slots.add(el.value)
                if isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Store) \
                        and isinstance(sub.value, ast.Name) and sub.value.id == "self":
                    assigns.add(sub.attr)
            missing = {a for a in assigns if not a.startswith("__")} - slots
            assert not missing, f"未登记 __slots__ 的新属性: {sorted(missing)}"
            return
    pytest.fail("未找到 ESPHomeManager")
