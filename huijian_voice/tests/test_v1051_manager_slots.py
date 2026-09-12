"""v1.0.51 热修钉：`__slots__` 类里新增实例属性必须同步登记。

现场实锤（2026-09-21 12:28:42，v1.0.49/1.0.50 实发）：
    Error setting up entry HUIJIAN-0BD0 for huijian_ai
    File ".../huijian_ai/manager.py", line 193, in __init__
        self._satellite_selfheal_at = 0.0
    AttributeError: 'ESPHomeManager' object has no attribute
        '_satellite_selfheal_at' and no __dict__ for setting new attributes
`ESPHomeManager` 定义了 `__slots__`（无 `__dict__`），新加的实例属性没登记进
`__slots__` → `__init__` 赋值即抛 AttributeError → **整个 config entry setup 失败**
（卫星、全部实体、语音链路一起下线，比它要修的那个缺陷更严重）。

为什么既有 768 钉没拦住：本仓测试一律用 AST 从源码摘函数执行（不 import
homeassistant、不实例化 manager），而这类缺陷只在**实例化**时才炸 —— 静态形态钉
是唯一能覆盖它的手段。本钉遍历类体内所有 `self.X = `（含 AnnAssign/AugAssign），
逐个核对是否在 `__slots__` 中；两份副本（发布链 + 商店工作副本）同钉。
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "custom_components" / "huijian_ai" / "manager.py"
STORE = ROOT.parent / "yyjicheng" / "custom_components" / "huijian_ai" / "manager.py"

# 商店工作副本（yyjicheng/）gitignored、CI 检出树没有——在位才参与双钉
_COPIES = [pytest.param(MAIN, id="main")]
if STORE.exists():
    _COPIES.append(pytest.param(STORE, id="store"))

#: 有 __slots__ 的类 → 需纳入静态核对（新加 __slots__ 类时补这里）
SLOTTED_CLASSES = ("ESPHomeManager",)


def _slots_and_self_assignments(path: Path, class_name: str):
    """返回 (slots, 类体内所有 self.X 赋值名集合)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        slots: set[str] = set()
        for stmt in node.body:
            if (
                isinstance(stmt, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id == "__slots__" for t in stmt.targets
                )
                and isinstance(stmt.value, (ast.Tuple, ast.List))
            ):
                slots = {
                    e.value for e in stmt.value.elts if isinstance(e, ast.Constant)
                }
        assigned: set[str] = set()
        for sub in ast.walk(node):
            targets = []
            if isinstance(sub, ast.Assign):
                targets = sub.targets
            elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)):
                targets = [sub.target]
            for t in targets:
                if (
                    isinstance(t, ast.Attribute)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "self"
                ):
                    assigned.add(t.attr)
        return slots, assigned
    raise AssertionError(f"{path} 中未找到类 {class_name}")


@pytest.mark.parametrize("path", _COPIES)
@pytest.mark.parametrize("class_name", SLOTTED_CLASSES)
class TestSlotsCoverSelfAssignments:
    def test_class_really_has_slots(self, path, class_name):
        """前提钉：该类必须仍是有 __slots__ 的类（否则本钉失效，须改用别的机制）。"""
        slots, _ = _slots_and_self_assignments(path, class_name)
        assert slots, f"{class_name} 的 __slots__ 丢失/被改成动态属性——钉桩前提失效"

    def test_every_self_assignment_is_declared(self, path, class_name):
        """真炸点：任何 self.X = 都必须能在 __slots__ 里找到 X。"""
        slots, assigned = _slots_and_self_assignments(path, class_name)
        missing = sorted(assigned - slots)
        assert not missing, (
            f"{class_name} 有 __slots__（无 __dict__），以下实例属性未登记 → "
            f"__init__/方法赋值即 AttributeError → 整个 config entry setup 失败："
            f"{missing}"
        )

    def test_selfheal_cooldown_is_process_level(self, path, class_name):
        """v1.0.52 不变量：卫星自愈的限频必须**进程级**，且自愈路径不得新增实例属性。

        为什么这条比"某个 slot 名在不在"更重要：自愈动作本身就是
        `async_reload(entry)`，而重载会重建 ESPHomeManager——实例级冷却时戳随旧
        实例清零，600s 限频在"重载 → 重连 → 再自愈"回路里形同虚设（重载风暴）。
        v1.0.51 的实例级写法正是踩了这个语义坑（并顺带把整个条目打死）。
        """
        import ast as _ast

        src = path.read_text(encoding="utf-8")
        assert "_SATELLITE_SELFHEAL_LAST" in src, "自愈冷却表丢失（或又退回实例级）"
        tree = _ast.parse(src)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef) and node.name == "_async_selfheal_missing_satellite":
                assigned = {
                    t.attr
                    for sub in _ast.walk(node)
                    if isinstance(sub, (_ast.Assign, _ast.AnnAssign))
                    for t in (sub.targets if isinstance(sub, _ast.Assign) else [sub.target])
                    if isinstance(t, _ast.Attribute)
                    and isinstance(t.value, _ast.Name)
                    and t.value.id == "self"
                }
                assert not assigned, (
                    f"自愈路径新增了实例属性 {sorted(assigned)}——实例级状态跨 reload 失效，"
                    "且新属性必须同步 __slots__（历史事故：整个条目 setup 失败）"
                )
                break
        else:
            pytest.fail("_async_selfheal_missing_satellite 未找到（自愈能力被删？）")
