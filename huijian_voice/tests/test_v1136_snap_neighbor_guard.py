# -*- coding: utf-8 -*-
"""步进"够不到一档"的两难：本文件是**留账钉**（钉住现状口径，不是宣布它正确）。

2026-10-01 我先把方向守卫从吸附支提到两条路之后，独立复核当场抓出副作用：
守卫上提把 cur=50「调高0.5」step=25 从**零变化**改成**整档跳 75**——步进 25/33 的
风速/开合器上，这是用户没要的大幅动作。已回退（见 intent_adjust_attribute.py 内注）。

剩下的真实缺口：`abs(user_target - valid) < 1` 的邻近档循环对 INCREASE 不 break，
于是"非网格当前值 + 不足半步的增量"会**逆向落回左档**（50.5 调高0.3 → 50）。
三种收口各有代价（A 现状/B 跳一档/C 不动并如实说"够不到一档"，C 需要新话术），
**等产品签字**，本文件先把 A 的两个形状钉死：谁悄悄改成 B 而没同步回执口径，这里红。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_audit4_p2 import _calc_target                                 # noqa: E402


def test_neighbor_band_characterizes_current_behavior():
    """留账：邻近档支现状（逆向半步），改动必须先解掉这条并写明选了哪种收口。"""
    AdjustType, call = _calc_target()
    assert call(AdjustType.INCREASE, 0.3, 25, cur=50.5) == 50, \
        "邻近档支行为变了——若这是有意的收口（跳档/不动+话术），请连注释与话术一起改"
    assert call(AdjustType.DECREASE, -0.4, 25, cur=99.5) == 100, "同上（对称形）"


def test_snapping_branch_direction_guard_intact():
    """既有四条吸附钉逐条照抄：守卫不许被整段删掉（v1.1.33 的对抗复核成果）。"""
    AdjustType, call = _calc_target()
    assert call(AdjustType.INCREASE, 10, 25, cur=51) == 75
    assert call(AdjustType.DECREASE, -1, 25, cur=99) == 75
    assert call(AdjustType.INCREASE, 10, 25, cur=50) == 75
    assert call(AdjustType.DECREASE, -10, 25, cur=100) == 75


def test_guard_still_lives_inside_snapping_branch():
    """接线钉：守卫位置=吸附支内（我上提过、已被复核实证副作用并回退）。"""
    import ast
    src = (ROOT / "custom_components" / "huijian_ai"
           / "intent_adjust_attribute.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "calc_target")
    snap_if = next((st for st in ast.walk(fn) if isinstance(st, ast.If)
                    and ast.unparse(st.test) == "target_value is None"), None)
    assert snap_if is not None, "吸附支结构被改动，本钉需同步"
    guards = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)
              and any(getattr(t, "id", "") == "current_value" for t in n.comparators)]
    assert guards, "方向守卫整段失踪（吸附支也不守了）"
    assert all(snap_if.lineno <= g.lineno <= snap_if.end_lineno for g in guards), \
    "守卫又被提到两条路之外（会跳档，见本文件头注）"
