# -*- coding: utf-8 -*-
"""修③：动作折算"该步可用"不得把"有台没动"洗成零问题（场景/面板两条链）。

病灶（本机 2026-10-01 实跑，`intent_result.fold_action_ok` 本体）：

    mixed(1成1败) : (True, '卡住')      ← 第二格有真因，消费侧 `ok, _err` 丢了它
    empty dict {} : (True, '')          ← 一个字都没回 = 判不了，却进成功面

`fold_action_ok` 的"任一台成功=该步可用"是**故意**的（docstring 写明不误伤），
所以修的不是折算方向，是**消费侧有义务把第二格播出去**：
· 场景回放（语音，会回执）⇒「已执行场景：X，有设备没动：TurnDeviceOn：卡住」；
· 面板试运行（test-scene / test-automation）⇒ 原来自己 `get("success") is not False`
  自判一行，`{"results":[…]}` 顶层无 success 键 ⇒ 恒真 ⇒ 测试口报全绿；现改走同一个
  `fold_action_ok`（v1.1.29 立的"折算单点"纪律，这条是第 4 处漏网的消费链）。
"""
import ast
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_v1064_honesty_batch import _scene_ns                         # noqa: E402

CC = ROOT / "custom_components" / "huijian_ai"


# ── ① 折算本体：三种形态的真因都要给出来 ───────────────────────────
def _fold():
    ns = {}
    exec(compile((CC / "intent_result.py").read_text(encoding="utf-8"),  # noqa: S102
                 "<intent_result.py>", "exec"), ns)
    return ns["fold_action_ok"]


def test_fold_returns_reason_for_partial_and_unverifiable():
    fold = _fold()
    ok, err = fold({"results": [{"success": True}, {"success": False, "error": "卡住"}]})
    assert ok is True and err == "卡住"
    assert fold({"results": [{"success": False, "error": "离线"}]})[0] is False
    ok2, err2 = fold({})
    assert ok2 is True, "判不了不得改方向（既有钉 fold({})[0] is True）"
    assert err2, "空 dict 必须留痕，不许静默进成功面"


# ── ①-b 三族逐台键名都要读（复核实证：只读 results 时另两族静默全绿）──
def test_fold_reads_all_three_per_device_key_families():
    fold = _fold()
    # states 族＝AdjustDeviceAttribute / Lock 族（intent_adjust_attribute.py:784 顶层
    # success=True 加行内失败台）
    ok, err = fold({"success": True,
                    "states": [{"entity_id": "a", "success": True},
                               {"entity_id": "b", "success": False, "error": "离线"}]})
    assert ok is True and err == "离线", f"states 族部分失败被折成静默：{(ok, err)}"
    # 行外 partial_error＝turn 族窗侧真因（core/executor.py:960-969 早已消费）
    ok2, err2 = fold({"success": True, "partial_error": "2 台没动"})
    assert ok2 is True and err2 == "2 台没动", f"partial_error 被丢：{(ok2, err2)}"
    # 无顶层 success 的纯 states 形
    assert fold({"states": [{"success": False, "error": "不支持"}]})[0] is False
    # 空表算"有这一族但零台生效"＝失败（改判据时踩过：`{"results": []}` 曾被折成
    # 成功，撞响既有钉 test_audit3_fixes::test_a5_fold_results_shape）
    assert fold({"results": []})[0] is False
    assert fold({"states": []})[0] is False


def test_fold_object_error_never_leaks_repr():
    """对象分支的 error 若是方法/函数，绝不许把 repr 播进回执。"""
    fold = _fold()
    ok, err = fold(types.SimpleNamespace(success=True, error=lambda: "真因"))
    assert ok is True and err == "真因", (ok, err)
    ok2, err2 = fold(types.SimpleNamespace(success=True,
                                           error=lambda: None))
    assert ok2 is True and err2 == "", (ok2, err2)


def test_scene_receipt_names_states_family_partial_failure():
    out = _scene_ns(lambda name: {"success": True,
                                  "states": [{"entity_id": "a", "success": True},
                                             {"entity_id": "b", "success": False,
                                              "error": "离线"}]})
    assert out["success"] is True
    assert "离线" in (out.get("message") or ""), out.get("message")


# ── ② 场景回执：部分失败必须点名（撤掉 partial_notes 即红）──────────
def test_scene_receipt_names_the_device_that_did_not_move():
    out = _scene_ns(lambda name: {"results": [{"success": True},
                                              {"success": False, "error": "卡住"}]})
    assert out["success"] is True, "该步确实动了东西，方向不翻成失败"
    msg = out.get("message") or ""
    assert "有设备没动" in msg and "卡住" in msg, msg
    # 不留无读者的出口字段（本仓被 `_receipt` 死码咬过）：部分失败只走 message。
    assert "partial" not in out, out


def test_scene_receipt_clean_when_every_device_moved():
    """反向不变量：不许给每条成功回执都挂个尾巴。"""
    out = _scene_ns(lambda name: {"success": True})
    msg = out.get("message") or ""
    assert out["success"] is True and "有设备没动" not in msg, msg
    assert "已执行场景" in msg, msg


def test_scene_receipt_all_failure_stays_failure():
    out = _scene_ns(lambda name: {"results": [{"success": False, "error": "离线"}]})
    assert out["success"] is False
    assert "有设备没动" not in (out.get("message") or "")


def test_scene_receipt_unverifiable_shape_is_flagged():
    out = _scene_ns(lambda name: {})
    assert out["success"] is True
    assert "有设备没动" in (out.get("message") or ""), "一个字都没回却被播成全绿"


# ── ③ 面板测试口：折算走单点，不许留自判（AST 限定在 TestSceneView.post 内）
def _test_scene_view_post_src():
    src = (CC / "api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "TestSceneView":
            for f in cls.body:
                if isinstance(f, ast.AsyncFunctionDef) and f.name == "post":
                    return ast.get_source_segment(src, f) or ""
    raise AssertionError("TestSceneView.post 不存在（接线点被挪走）")


def test_panel_test_scene_uses_single_source_fold():
    body = _test_scene_view_post_src()
    # 只在**代码行**上判（注释里写的"旧形态原文"会被负向断言误命中——本钉第一轮就
    # 撞上了：判据被自己的说明文字满足＝假红，反过来也会假绿）。
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "fold_action_ok(response)" in code, "面板测试口没走折算单点"
    assert 'response.get("success") is not False' not in code, \
        "自判残留在测试口（无 success 键恒真＝部分失败报全绿）"
    assert "partial_notes" in code and '"note"' in code, "部分失败没出口"


def test_panel_test_automation_carries_partial_note():
    src = (CC / "api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    body = ""
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "TestAutomationView":
            for f in cls.body:
                if isinstance(f, ast.AsyncFunctionDef) and f.name == "post":
                    body = ast.get_source_segment(src, f) or ""
    assert body, "TestAutomationView.post 不存在"
    assert '"note"' in body, "自动化试运行响应没带部分失败出口"
    # 数据出口在 _execute_actions 的成功位第三格
    acts = (CC / "intent_automation.py").read_text(encoding="utf-8")
    assert "results.append((str(intent_name), True, _err))" in acts, \
        "成功位仍写死空串（部分真因在源头就丢了）"
