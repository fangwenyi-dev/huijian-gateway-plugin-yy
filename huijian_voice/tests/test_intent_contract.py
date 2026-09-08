# -*- coding: utf-8 -*-
"""三端意图契约钉（v1.0.20 实锤教训固化）。

背景：v1.0.20 给 fast_path 开了 HassUnlock/HassLock 车道，但 HA core **没有**
这两个内置意图（intent_builtin 文档 + core 全树 intent.py 清点实查），集成端
也没注册——真机上解锁令必 Unknown intent；而单测/E2E 用的 intent 替身恒成功，
整条验收线全绿放行了坏功能。本文件把「发射名 ⊆ 可执行名」变成可执行契约：
以后任何人给 NLU 加新车道，若执行面无人接，这里当场红。
"""
import re
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
CC = HERE / "custom_components" / "huijian_ai"

# HA core 确实内置的意图（2026-09-12 按 developers.home-assistant intent_builtin
# + core components/*/intent.py 全树清点核定；只列本仓会用到的）
HA_CORE_INTENTS = {
    "HassTurnOn", "HassTurnOff", "HassToggle",
    "HassClimateSetTemperature", "HassClimateGetTemperature",
    "HassOpenCover", "HassCloseCover",
    "HassHumidifierMode", "HassHumidifierSetpoint",
    "HassGetCurrentTime", "HassGetCurrentDate", "HassGetCurrentWeather",
    "HassShoppingListAddItem", "HassShoppingListLastItems",
}


def _registered_intents() -> set[str]:
    """集成端注册表：所有 handler 文件的 intent_type 字面量。"""
    names: set[str] = set()
    for py in CC.glob("*.py"):
        names |= set(re.findall(r'intent_type\s*=\s*"([^"]+)"',
                                py.read_text(encoding="utf-8")))
    return names


def _emitted_intents() -> set[str]:
    """加载项可能发射的意图名：fast_path 动作车道 + intent 改道字面量。"""
    src = (HERE / "core" / "nlu" / "fast_path.py").read_text(encoding="utf-8")
    names: set[str] = set()
    # _ACTION_PATTERNS 第二列 与 Plan(intent=...)/intent 改道字面量
    names |= set(re.findall(r're\.compile\([^)]*\)\s*,\s*"([A-Z]\w+)"', src))
    names |= set(re.findall(r'intent\s*=\s*"(Hass[A-Z]\w+|[A-Z]\w+)"', src))
    names |= set(re.findall(r'"(Hass[A-Z]\w+|TurnDevice\w+|ControlWindow|'
                            r'AdjustDeviceAttribute|SetDeviceMode)"', src))
    return names


def test_every_emitted_intent_has_a_handler():
    registered = _registered_intents()
    emitted = _emitted_intents()
    assert emitted, "扫描失效：fast_path 未提取到任何意图名"
    unhandled = {n for n in emitted if n not in registered
                 and n not in HA_CORE_INTENTS}
    assert not unhandled, (
        f"新车道发射名无执行面：{sorted(unhandled)}——"
        "须在 custom_components/huijian_ai/intent*.py 注册 handler"
        "（或确认 HA core 内置后加入 HA_CORE_INTENTS）")


def test_lock_family_registered_in_integration():
    """v1.0.20 缺口回归钉：解锁/上锁执行面必须在集成端。"""
    reg = _registered_intents()
    assert {"HassUnlock", "HassLock"} <= reg, reg
    lock_src = (CC / "intent_lock.py").read_text(encoding="utf-8")
    assert "except Exception" in lock_src, "锁 handler 失守永不抛纪律"
    intent_py = (CC / "intent.py").read_text(encoding="utf-8")
    assert "HassUnlockIntent()" in intent_py and "HassLockIntent()" in intent_py


def test_executor_unlock_speech():
    """话术名实相符：解锁播报走专支（不许裸"好的"糊弄）。"""
    ex = (HERE / "core" / "executor.py").read_text(encoding="utf-8")
    assert 'if intent in ("HassUnlock", "HassLock")' in ex
    assert "已解锁" in ex and "已上锁" in ex
