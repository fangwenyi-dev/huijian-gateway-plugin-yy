# -*- coding: utf-8 -*-
"""HA API 契约钉（v1.0.21 集成端两雷固化，2026-09-12 三端深挖实锤）。

按 core 2025.1.0 源码签名实查，抓到两个**只在真 HA 才炸**的缺陷——
它们都能轻松穿过既有的字符串钉（注册名/话术/文件存在性）：
①`ServiceRegistry.async_call` 形参只有 domain / service / service_data /
  blocking / context / target / return_response——**没有 timeout**。
  传 `timeout=` 必 TypeError（被 except 折叠成"抱歉"，功能静默死）。
②`homeassistant.components.lock.const` 只导出 DOMAIN / LockState，
  SERVICE_LOCK/SERVICE_UNLOCK 在 `homeassistant.const`。从 lock.const 导入
  → ImportError；而集成 `__init__.py` 急切导入 `.intent` → **整个集成加载失败**。
故此处用 AST 做结构化守卫，而非再补一条字符串断言。
"""
import ast
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CC = HERE / "custom_components" / "huijian_ai"

# core 2025.1.0 实查签名（homeassistant/core.py ServiceRegistry.async_call）
ASYNC_CALL_KWARGS = {
    "domain", "service", "service_data", "blocking", "context", "target",
    "return_response",
}

# components.<X>.const 允许导入的符号：DOMAIN 通吃；其余须已核验存在。
# button.SERVICE_PRESS="press"、homeassistant.DATA_EXPOSED_ENTITIES 均已核。
CONST_ALLOW = {
    "button": {"DOMAIN", "SERVICE_PRESS"},
    "homeassistant": {"DOMAIN", "DATA_EXPOSED_ENTITIES"},
}
CONST_ALLOW_DEFAULT = {"DOMAIN"}


def _files():
    return sorted(CC.glob("*.py"))


def _attr_chain(node):
    """展开 Attribute 链的属性名，如 hass.services.async_call → ['async_call','services']。"""
    names, cur = [], node
    while isinstance(cur, ast.Attribute):
        names.append(cur.attr)
        cur = cur.value
    return names


def test_services_async_call_kwargs_are_real():
    """hass.services.async_call 只准用 HA 真实存在的关键字形参。"""
    bad = []
    for py in _files():
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            chain = _attr_chain(node.func)
            # 仅盯 ...services.async_call(...)（llm.ToolInput.async_call 等不在此列）
            if not chain or chain[0] != "async_call" or "services" not in chain:
                continue
            for kw in node.keywords:
                if kw.arg and kw.arg not in ASYNC_CALL_KWARGS:
                    bad.append(f"{py.name}:{node.lineno} 传了未知形参 {kw.arg}=")
    assert not bad, (
        "HA ServiceRegistry.async_call 无此形参，真机必 TypeError："
        + "; ".join(bad))


def test_component_const_imports_are_verified_symbols():
    """components.<X>.const 只准导入已核验符号（防 ImportError 炸整个集成）。"""
    bad = []
    for py in _files():
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = node.module or ""
            if not (mod.startswith("homeassistant.components.")
                    and mod.endswith(".const")):
                continue
            comp = mod.split(".")[2]
            allowed = CONST_ALLOW.get(comp, CONST_ALLOW_DEFAULT)
            for alias in node.names:
                if alias.name not in allowed:
                    bad.append(f"{py.name}:{node.lineno} {mod} 导入 {alias.name}")
    assert not bad, (
        "未核验的 components.*.const 符号（真机 ImportError 会让集成加载失败）："
        + "; ".join(bad))


def test_lock_family_uses_homeassistant_const_services():
    """锁族服务名必须来自 homeassistant.const（实查 lock.const 无 SERVICE_*）。"""
    src = (CC / "intent_lock.py").read_text(encoding="utf-8")
    assert "from homeassistant.const import" in src, "锁服务名未从 homeassistant.const 取"
    assert "SERVICE_LOCK" in src and "SERVICE_UNLOCK" in src
    assert "components.lock.const import" in src, "DOMAIN 应仍取自 lock.const"
    # 反面钉：不得再从 lock.const 取服务名
    for line in src.splitlines():
        if "components.lock.const import" in line:
            assert "SERVICE_" not in line, f"lock.const 无 SERVICE_*：{line.strip()}"
    # 服务调用不得带 timeout=（真机 TypeError 的那条）
    assert "timeout=self.service_timeout" not in src
    assert "_run_then_background" in src, "超时应走框架 _run_then_background"


# ──  注册表枚举：helper 模块级函数，不是注册表对象的方法 ──────────────────
# 真 HA 2026.9.2 现场实炸：huijian/http.py 写成
#   reg.async_entries_for_config_entry(entry.entry_id)
# → AttributeError: 'EntityRegistry' object has no attribute
#   'async_entries_for_config_entry'（HA 自己的提示是 "Did you mean:
#   'async_clear_config_entry'?"）→ /api/huijian-ai/satellites 500、面板降级。
# 正确形式（本仓 manager.py:1738、huijian/__init__.py:74 一直用对了）：
#   er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
REGISTRY_MODULE_FUNCS = {"async_entries_for_config_entry"}
REGISTRY_MODULE_NAMES = {"er", "dr", "entity_registry", "device_registry"}


def _files_all():
    """递归全量。既有 _files() 只 glob 顶层 *.py，子目录（huijian/ 等）是守卫
    盲区——本次生产 500 正落在盲区里，故新规则一律走递归。"""
    return sorted(CC.rglob("*.py"))


def test_registry_enumeration_uses_module_level_call():
    """`async_entries_for_config_entry` 只准以模块函数形式调用。

    为什么不用字符串断言：当时 tests 里的假注册表照着**错误形状**定义了
    `def async_entries_for_config_entry(self, _entry_id)`，替身恒成功 → 测试全绿、
    真机必炸。所以这里按调用形状（AST）钉，且桩已改为与真 HA 表面同形。
    """
    bad = []
    for py in _files_all():
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in REGISTRY_MODULE_FUNCS:
                continue
            recv = node.func.value
            if isinstance(recv, ast.Name) and recv.id in REGISTRY_MODULE_NAMES:
                continue  # er./dr. 模块函数形式，合法
            bad.append(f"{py.relative_to(HERE)}:{node.lineno} 把 "
                       f"{node.func.attr}() 当注册表对象方法调用")
    assert not bad, (
        "这些名字是 homeassistant.helpers.{entity,device}_registry 的模块级函数，"
        "注册表对象上没有，真机必 AttributeError 并折叠成接口 500：" + "; ".join(bad))
