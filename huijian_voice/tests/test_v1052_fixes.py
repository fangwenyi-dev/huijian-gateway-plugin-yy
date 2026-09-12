# -*- coding: utf-8 -*-
"""v1.0.52 修复批的行为钉 + 形态钉。

覆盖（全部来自 2026-09-21 双审计、逐条源码实锤的真实缺陷）：
- A-F1 卫星自愈重载风暴：探针与实体 add 的同 tick 竞态 + 实例级限频跨 reload
  失效。行为钉直跑 manager.py 真实函数（AST 摘取，不 import homeassistant）。
- A-F2 所有窗户部分失败折叠成全成功：_press_multi_buttons/_all_window_result
  三态裁决钉。
- A-F3 hacs.json 最低 HA 版本 ≥2024.8（entry.runtime_data 落地线）。
- A-F4 _start_udp_server 先停旧实例（形态钉）。
- A-F5 query.py _entity_area 访问全守卫（形态钉）。
"""
import ast
import asyncio
import json
import logging
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CC = ROOT / "custom_components" / "huijian_ai"
MANAGER = CC / "manager.py"
WINDOW = CC / "intent_window_control.py"


# --------------------------------------------------------------------------
# AST 摘函数执行基建
# --------------------------------------------------------------------------
def _module_source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_func(path: Path, names: list[str], extra_ns: dict) -> dict:
    """从真实源码摘取模块级常量 + 指定（含类内）函数，exec 进干净命名空间。

    返回 namespace，其中含 names 里的可调用（类内方法以普通函数形态返回，
    调用时显式传 self）。manager.py 有 `from __future__ import annotations`，
    注解全是字符串不求值，只需补运行期真实触碰的名字。
    """
    src = _module_source(path)
    tree = ast.parse(src)
    wanted = set(names)
    found: dict[str, ast.AST] = {}
    kept_assigns: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name in wanted:
            found[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            t = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(t, ast.Name) and t.id.startswith("_SATELLITE_"):
                kept_assigns.append(node)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and sub.name in wanted:
                    found[sub.name] = sub
    missing = wanted - set(found)
    assert not missing, f"{path.name} 未找到函数：{sorted(missing)}"
    # 铁律：≤3.13 在 def 时求值注解——摘出来的函数必须带上原文件的
    # `from __future__ import annotations`（manager.py 首行即有），否则
    # EsphomeDeviceInfo 等注解名直接 NameError。
    future = ast.ImportFrom(module="__future__",
                            names=[ast.alias(name="annotations", asname=None)],
                            level=0)
    body = [future] + kept_assigns + [found[n] for n in names]
    mod = ast.Module(body=body, type_ignores=[])
    for f in body:
        ast.increment_lineno(f, n=0)
    ns = {
        "__builtins__": __builtins__,
        "time": time,
        "asyncio": asyncio,
        "_LOGGER": logging.getLogger("v1052pin"),
    }
    ns.update(extra_ns)
    exec(compile(ast.fix_missing_locations(mod), str(path), "exec"), ns)
    return ns


# --------------------------------------------------------------------------
# A-F1：卫星自愈——行为钉（直跑真实函数）
# --------------------------------------------------------------------------
class FakeHass:
    def __init__(self):
        self.is_stopping = False
        self.scheduled: list = []
        self.reloaded: list = []
        self.config_entries = types.SimpleNamespace()

        async def _reload(entry_id):
            self.reloaded.append(entry_id)

        self.config_entries.async_reload = _reload

    def async_create_task(self, coro):
        self.scheduled.append(coro)
        return None


class FakeEntry:
    def __init__(self, entry_id, title, state):
        self.entry_id = entry_id
        self.title = title
        self.state = state


class FakeEntryData:
    def __init__(self):
        self.assist_satellite_set_wake_words_callbacks: list = []


class FakeDeviceInfo:
    def voice_assistant_feature_flags_compat(self, api_version):
        return True


class FakeManager:
    """按 __init__ 真实字段最小化装配（探针/延迟任务只碰这些）。"""
    def __init__(self, hass, entry, entry_data):
        self.hass = hass
        self.entry = entry
        self.entry_data = entry_data


def _bind(ns, mgr):
    """把摘出的真实函数绑成实例方法（探针内部还会调 self._async_reload_...）。"""
    for fname in ("_async_selfheal_missing_satellite",
                  "_async_reload_entry_after_delay"):
        setattr(mgr, fname, types.MethodType(ns[fname], mgr))
    return mgr


LOADED = "LOADED"
NOT_LOADED = "SETUP_IN_PROGRESS"


class ConfigEntryState:  # 运行期占位：源码只用 `is not ConfigEntryState.LOADED`
    LOADED = LOADED


def _selfheal_ns():
    ns = _extract_func(
        MANAGER,
        ["_async_selfheal_missing_satellite", "_async_reload_entry_after_delay"],
        {
            "ConfigEntryState": ConfigEntryState,
            "callback": lambda f: f,          # @callback 装饰器占位
        },
    )
    return ns


def _drive(ns, self_, delay=None):
    """把探针排入的 task 跑完（用缩短后的 delay）。"""
    async def run():
        for coro in list(self_.hass.scheduled):
            await coro
    asyncio.run(run())


def test_v1052_f1_cold_boot_never_heals_must_not_be_swallowed():
    """冷启动哨兵钉：monotonic 基准=开机时刻，uptime<600s 的机器上"从未自愈"
    （dict 无键）绝不可被 `get(eid, 0.0)` 误判成限频中——断电重启后恰是最需要
    自愈的窗口（本钉在 uptime=30s 的模拟下必须仍排入复查任务）。"""
    fake_time = types.SimpleNamespace(monotonic=lambda: 30.0)
    ns = _extract_func(
        MANAGER,
        ["_async_selfheal_missing_satellite", "_async_reload_entry_after_delay"],
        {"ConfigEntryState": ConfigEntryState, "callback": lambda f: f,
         "time": fake_time},
    )
    hass = FakeHass()
    entry = FakeEntry("e-cold", "HUIJIAN-cold", LOADED)
    mgr = _bind(ns, FakeManager(hass, entry, FakeEntryData()))
    mgr._async_selfheal_missing_satellite(FakeDeviceInfo(), None)
    assert len(hass.scheduled) == 1, "uptime 30s 时首次自愈被 0.0 哨兵吞掉 = 缺陷复发"


def test_v1052_f1_first_connect_race_no_reload():
    """首连竞态：探针同 tick 必见空回调，但 5s 窗口内实体完成注册 → 不得重载。"""
    ns = _selfheal_ns()
    ns["_SATELLITE_SELFHEAL_DELAY"] = 0.05   # 缩短，避免测试睡 5s
    hass = FakeHass()
    entry = FakeEntry("e-race", "HUIJIAN-race", LOADED)
    ed = FakeEntryData()
    mgr = _bind(ns, FakeManager(hass, entry, ed))

    mgr._async_selfheal_missing_satellite(FakeDeviceInfo(), None)
    assert len(hass.scheduled) == 1, "探针应只排入一个延迟复查任务"

    # 实体 add 任务在复查前完成注册（真实时序：推迟任务毫秒级收尾）
    ed.assist_satellite_set_wake_words_callbacks.append(lambda w: None)
    _drive(ns, mgr)
    assert hass.reloaded == [], "复查通过仍重载 = 风暴原缺陷复发"


def test_v1052_f1_true_missing_reloads_once_and_cooldown_survives_instances():
    """真缺失：复查仍无回调 → 恰好一次重载；跨"新实例"（模拟 reload 重建）限频仍生效。"""
    ns = _selfheal_ns()
    ns["_SATELLITE_SELFHEAL_DELAY"] = 0.05
    ns["_SATELLITE_SELFHEAL_LAST"] = {}      # 每次独立
    hass = FakeHass()
    entry = FakeEntry("e-miss", "HUIJIAN-miss", LOADED)
    ed = FakeEntryData()
    mgr1 = _bind(ns, FakeManager(hass, entry, ed))

    mgr1._async_selfheal_missing_satellite(FakeDeviceInfo(), None)
    _drive(ns, mgr1)
    assert hass.reloaded == ["e-miss"], "真缺失必须重载"

    # reload 重建 manager（新实例、同 entry_id、回调仍空）→ 600s 限频必须拦住
    hass2 = FakeHass()
    ed2 = FakeEntryData()
    mgr2 = _bind(ns, FakeManager(hass2, entry, ed2))
    mgr2._async_selfheal_missing_satellite(FakeDeviceInfo(), None)
    assert hass2.scheduled == [], (
        "冷却时戳若仍随实例清零，这里会再排一次重载 → 风暴；本断言即 A/B 反证")


def test_v1052_f1_entry_not_loaded_skips_reload():
    """条目非 LOADED（别的重载/卸载在跑）→ 跳过叠加操作。"""
    ns = _selfheal_ns()
    ns["_SATELLITE_SELFHEAL_DELAY"] = 0.05
    hass = FakeHass()
    entry = FakeEntry("e-busy", "HUIJIAN-busy", NOT_LOADED)
    ed = FakeEntryData()
    mgr = _bind(ns, FakeManager(hass, entry, ed))
    mgr._async_selfheal_missing_satellite(FakeDeviceInfo(), None)
    _drive(ns, mgr)
    assert hass.reloaded == []


def test_v1052_f1_shape_pins_no_instance_timestamp_and_recheck():
    """形态钉：①实例级 `_satellite_selfheal_at` 不得复活；②延迟任务里必须复查回调。"""
    src = _module_source(MANAGER)
    import re as _re
    assert not _re.search(r"self\._satellite_selfheal_at\s*=", src), \
        "实例级限频属性赋值复活 = 风暴回归通道重开"
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) \
                and node.name == "_async_reload_entry_after_delay":
            body_src = ast.get_source_segment(src, node) or ""
            assert "assist_satellite_set_wake_words_callbacks" in body_src, \
                "延迟任务不复查回调 = 首连竞态直接重载，禁止"
            assert "sleep" in body_src
            break
    else:
        pytest.fail("_async_reload_entry_after_delay 不在了——本钉前提失效")


# --------------------------------------------------------------------------
# A-F2：所有窗户三态裁决
# --------------------------------------------------------------------------
def _window_ns():
    return _extract_func(
        WINDOW,
        ["_press_multi_buttons", "_all_window_result"],
        {
            "BUTTON_DOMAIN": "button",
            "SERVICE_PRESS_BUTTON": "press",
            "ATTR_ENTITY_ID": "entity_id",
            "ACTION_CHINESE": {"open": "开启", "close": "关闭",
                               "pause": "暂停", "a": "内倒"},
        },
    )


class FakeServices:
    def __init__(self, fail_ids):
        self.fail_ids = set(fail_ids)
        self.pressed = []

    async def async_call(self, domain, service, data, **kw):
        eid = data["entity_id"]
        if eid in self.fail_ids:
            raise RuntimeError("服务调用失败")
        self.pressed.append(eid)


class FakeState:
    def __init__(self, name):
        self.attributes = {"friendly_name": name}


def _fake_window_hass(fail_ids, names=None):
    h = types.SimpleNamespace()
    h.services = FakeServices(fail_ids)
    states = {k: FakeState(v) for k, v in (names or {}).items()}
    h.states = types.SimpleNamespace(get=states.get)
    return h


def test_v1052_f2_partial_failure_is_voiced():
    """5 扇败 2 扇：message 必须带成功数与失败数/清单——旧版这里报"已所有窗户关闭"。"""
    ns = _window_ns()
    # 压掉 0.5s 节奏等待
    ns["asyncio"] = types.SimpleNamespace(
        sleep=lambda s: asyncio.sleep(0),
        asyncio_mod=None)
    hass = _fake_window_hass(
        fail_ids={"button.w3_close", "button.w4_close"},
        names={"button.w3_close": "厨房窗", "button.w4_close": "卫生间窗"})
    ids = [f"button.w{i}_close" for i in range(1, 6)]
    results, failed = asyncio.run(
        ns["_press_multi_buttons"](hass, None, "close", ids))
    assert results == ["button.w1_close", "button.w2_close", "button.w5_close"]
    assert len(failed) == 2
    verdict = ns["_all_window_result"]("展厅", "close", results, failed)
    assert verdict["success"] is True
    msg = verdict["message"]
    assert "3" in msg and "2" in msg, f"部分失败话术缺数：{msg}"
    assert "未成功" in msg, f"部分失败话术须可复述：{msg}"
    assert "厨房窗" in msg


def test_v1052_f2_all_success_and_all_failure():
    ns = _window_ns()
    ns["asyncio"] = types.SimpleNamespace(
        sleep=lambda s: asyncio.sleep(0), asyncio_mod=None)
    hass_ok = _fake_window_hass(fail_ids=set())
    results, failed = asyncio.run(
        ns["_press_multi_buttons"](hass_ok, None, "close", ["button.a", "button.b"]))
    verdict = ns["_all_window_result"]("展厅", "close", results, failed)
    assert verdict == {"success": True,
                       "message": "已展厅的所有窗户关闭",
                       "buttons": ["button.a", "button.b"]}

    hass_bad = _fake_window_hass(fail_ids={"button.a"})
    results, failed = asyncio.run(
        ns["_press_multi_buttons"](hass_bad, None, "close", ["button.a"]))
    verdict = ns["_all_window_result"]("展厅", "close", results, failed)
    assert verdict["success"] is False
    assert "未能关闭任何窗户" == verdict["error"]


def test_v1052_f2_shape_pins_both_call_sites_destructure():
    """两处调用点必须接住 (results, failed)——漏一处 = 该路继续折叠失败。"""
    src = _module_source(WINDOW)
    n_destr = src.count("results, failed_msgs = await _press_multi_buttons")
    assert n_destr == 2, f"全窗调用点应 2 处解包，实见 {n_destr}"
    assert "return results, failed_msgs" in src
    # A/B 反证：旧形态（只回成功列表）不得回来
    assert "    return results\n" not in src


# --------------------------------------------------------------------------
# A-F3：hacs.json 最低版本
# --------------------------------------------------------------------------
def test_v1052_f3_hacs_floor_matches_runtime_data():
    hacs = Path(__file__).resolve().parents[2] / "yyjicheng" / "hacs.json"
    # yyjicheng/ 是嵌套独立 git 仓（HACS 商店源），CI/提交树无该副本——
    # 与本仓"带副本参数化、无副本计缺省"口径一致（同 1.0.52 集成流式那组）。
    if not hacs.exists():
        pytest.skip("CI 树无 yyjicheng/ 商店副本；下限钉在商店仓本地跑时生效")
    data = json.loads(hacs.read_text(encoding="utf-8"))
    ver = tuple(int(x) for x in data["homeassistant"].split("."))
    assert ver >= (2024, 8, 0), (
        "entry.runtime_data 为 2024.8 起公开稳定 API；声明低于实依 = "
        "老版本用户装完 setup 即 AttributeError")


# --------------------------------------------------------------------------
# A-F4：UDP server 覆盖泄漏
# --------------------------------------------------------------------------
def test_v1052_f4_start_udp_stops_old_first():
    src = _module_source(CC / "assist_satellite.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_start_udp_server":
            first = node.body[0]
            # 允许 docstring 在前
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                first = node.body[1] if len(node.body) > 1 else node.body[0]
            ok = (isinstance(first, ast.Expr)
                  and isinstance(first.value, ast.Call)
                  and isinstance(first.value.func, ast.Attribute)
                  and first.value.func.attr == "_stop_udp_server")
            assert ok, "_start_udp_server 必须先无条件 _stop_udp_server()（幂等）"
            return
    pytest.fail("_start_udp_server 不在了")


# --------------------------------------------------------------------------
# A-F5：_entity_area 全守卫
# --------------------------------------------------------------------------
def test_v1052_f5_entity_area_access_all_guarded():
    src = _module_source(ROOT / "core" / "nlu" / "query.py")
    bad = []
    for i, line in enumerate(src.splitlines()):
        if "_entity_area.get(" in line:
            ctx = "\n".join(src.splitlines()[max(0, i - 3):i + 3])
            if "hasattr" not in ctx:
                bad.append(i + 1)
    assert not bad, f"query.py 裸取 _entity_area 未守卫（行 {bad}）→ 异常被级联折叠"


# --------------------------------------------------------------------------
# A-F1 补充：__slots__ 钉同步更新（test_v1051 的 marker 改口径见该文件）
# --------------------------------------------------------------------------
def test_v1052_slots_marker_updated():
    """marker 钉不得再要求已删属性在 __slots__（与本修自洽）。"""
    marker = (Path(__file__).parent / "test_v1051_manager_slots.py").read_text(
        encoding="utf-8")
    assert 'assert "_satellite_selfheal_at" in slots' not in marker, (
        "属性已删除，旧 marker 会让钉与实现打架")
