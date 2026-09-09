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
