"""runtime_data 缺席守卫钉桩（2026-09-08 台架实发回归）。

背景：HA core 的删除条目流程是 async_unload（成功后 core 执行
object.__delattr__(entry, "runtime_data")）→ component.async_remove_entry。
本 fork 的 assist 分支裸访问 entry.runtime_data（huijian/__init__.py
get_entry_data）与 diagnostics.py，在"删除 assist 条目"时必抛
AttributeError: 'ConfigEntry' object has no attribute 'runtime_data'
（台架 logbook 实发 "Error calling entry remove callback"）。
setup 失败的条目同样从未 set 过该属性（类级 annotation 无缺省值）。

本测试用 sys.modules stub 真 import 被测模块，构造无 runtime_data 的
entry 对象直调——即实发故障的最小复现；另附源码级钉桩防回退。
"""

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

CC = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"


def _load_huijian_pkg():
    """把 custom_components/huijian_ai/huijian/__init__.py 以 stub 依赖载入。"""
    if "huijian_under_test" in sys.modules:
        return sys.modules["huijian_under_test"]

    # stub homeassistant 依赖面（本机无 homeassistant 包，见 CLAUDE.md 测试环境）
    ha = types.ModuleType("homeassistant")
    exc = types.ModuleType("homeassistant.exceptions")

    class ConfigEntryAuthFailed(Exception):
        pass

    exc.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    helpers = types.ModuleType("homeassistant.helpers")
    er = types.ModuleType("homeassistant.helpers.entity_registry")
    iid = types.ModuleType("homeassistant.helpers.instance_id")
    er.async_entries_for_config_entry = lambda *a, **k: []
    er.async_get = lambda *a, **k: None
    iid.async_get = lambda *a, **k: "stub-haid"
    helpers.entity_registry = er
    helpers.instance_id = iid
    ha.exceptions = exc
    ha.helpers = helpers
    for name, mod in {
        "homeassistant": ha,
        "homeassistant.exceptions": exc,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.entity_registry": er,
        "homeassistant.helpers.instance_id": iid,
    }.items():
        sys.modules.setdefault(name, mod)

    # 伪父包 + const 子模块（供 `from ..const import DOMAIN` 相对导入）
    parent = types.ModuleType("cut_parent")
    parent.__path__ = [str(CC)]
    const = types.ModuleType("cut_parent.const")
    const.DOMAIN = "huijian_ai"
    parent.const = const
    sys.modules.setdefault("cut_parent", parent)
    sys.modules.setdefault("cut_parent.const", const)

    pkginit = CC / "huijian" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "cut_parent.huijian", pkginit, submodule_search_locations=[str(pkginit.parent)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cut_parent.huijian"] = mod
    spec.loader.exec_module(mod)
    sys.modules["huijian_under_test"] = mod
    return mod


def test_get_entry_data_assist_survives_missing_runtime_data():
    """最小复现：删除 assist 条目时 core 已 del runtime_data，不得再抛。"""
    m = _load_huijian_pkg()
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(data={"config_type": "assist"})  # 无 runtime_data 属性
    assert m.get_entry_data(hass, entry, "mcp_transport") is None
    assert m.get_entry_data(hass, entry) == {}
    assert m.get_entry_data(hass, entry, "x", pop=True) is None
    assert m.get_entry_data(hass, entry, "x", set_default=7) == 7


def test_get_entry_data_assist_with_runtime_data_roundtrips():
    m = _load_huijian_pkg()
    runtime = {"mcp_transport": "sentinel"}
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(data={"config_type": "assist"}, runtime_data=runtime)
    assert m.get_entry_data(hass, entry, "mcp_transport") == "sentinel"
    assert m.get_entry_data(hass, entry, "mcp_transport", pop=True) == "sentinel"
    assert "mcp_transport" not in runtime


def test_get_entry_data_device_uses_hass_data_unchanged():
    m = _load_huijian_pkg()
    runtime = SimpleNamespace()
    hass = SimpleNamespace(data={})
    entry = SimpleNamespace(entry_id="e1", data={"config_type": "device"},
                            runtime_data=runtime)
    d = m.get_entry_data(hass, entry)
    d["mcp_transport"] = "dev"
    assert hass.data["huijian_ai"]["e1"]["mcp_transport"] == "dev"
    assert m.get_entry_data(hass, entry, "mcp_transport") == "dev"


def _src(rel):
    return (CC / rel).read_text(encoding="utf-8")


def test_source_pins_no_bare_runtime_data_in_remove_family():
    """防回退：remove/diagnostics 路径禁止裸 entry.runtime_data 访问。"""
    hj = _src("huijian/__init__.py")
    assert 'getattr(entry, "runtime_data", None)' in hj, (
        "get_entry_data assist 分支必须保留缺席守卫（2026-09-08 实发回归）"
    )
    diag = _src("diagnostics.py")
    assert 'getattr(config_entry, "runtime_data", None)' in diag
    assert "entry not loaded" in diag, "未加载条目诊断需给降级说明而非抛异常"


def test_source_pin_mcp_closed_at_unload():
    """mcp_transport 关闭时点必须在 unload（assist 的 runtime_data 尚存活），
    remove 回调时 core 已删数据、清理不可达。"""
    init = _src("__init__.py")
    i = init.index("async def async_unload_entry")
    j = init.index("async def async_remove_entry")
    seg = init[i:j]
    assert '"mcp_transport"' in seg, "async_unload_entry 必须一并关闭 mcp_transport"


def test_source_pin_satellite_validation_error_hint():
    """validation-error + 域内无 assist 条目 → HA 日志必须给修复指引
    （定案：不静默补建被用户删除的条目，但两端都要看得见原因）。"""
    sat = _src("assist_satellite.py")
    assert '"validation-error"' in sat and "语音助手" in sat
