"""v1.0.64 批3 谎报成功对钉（H3/H4，报告 2026-09-23）。

H3：场景/自动化执行循环的成败判定只认**异常**，而失败被折叠成
    {"success": False} 返回值——零动作生效也回「已执行场景：X」。与
    「绝不吃一扇谎报成功」的自家铁律（intent_window_control._all_window_result
    注释原话）直接冲突。
H4：_press_multi_buttons v1.0.52 改返回 (results, failed_msgs) 后第三消费者
    （intent_turn「所有窗户」分支）漏解包——0/N 全失败也报成功。§三"改返回
    形态必须 grep 全消费点"一族病的计数断言钉。

手法沿用仓内先例：AST 抽真实函数源码 exec（test_v1041_security），拒绝
字符串钉不住行为的假绿；intent 文件依赖 homeassistant 符号，在 exec 命名
空间注入占位。
"""
import ast
import asyncio
import textwrap
import types
from pathlib import Path

CC = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"


def _extract_func_src(path: Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return textwrap.dedent(ast.get_source_segment(src, node))
    raise AssertionError(f"{path.name} 找不到函数 {name}")


class _Log:
    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass  # v1.0.97 validate_slots_safely 失败分因走 _LOGGER.exception


# ── H3 场景侧：折叠失败必须判 success ───────────────────────────────
def _scene_ns(result_factory):
    """执行抽出的 HassTriggerVoiceSceneIntent.async_handle。"""
    import inspect

    ns = {"_LOGGER": _Log(), "asyncio": asyncio, "JsonObjectType": dict}

    async def _fake_timeout(self, intent_obj, intent_name, params):
        r = result_factory(intent_name)
        if inspect.isawaitable(r):
            r = await r
        return r

    class Self:
        service_timeout = 1
        _execute_action_with_timeout = _fake_timeout

        def async_validate_slots(self, slots):
            return {"trigger_phrase": {"value": "观影模式"}}

    # 场景 store 打桩：get_scene_by_trigger 返回两个动作
    class Store:
        async def get_scene_by_trigger(self, phrase):
            return {"scene_id": "s1", "trigger_phrase": phrase,
                    "actions": [{"intent": "TurnDeviceOn", "params": {}},
                                {"intent": "TurnDeviceOff", "params": {}}]}

    src = (CC / "intent_voice_scene.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "HassTriggerVoiceSceneIntent":
            for f in cls.body:
                if isinstance(f, ast.AsyncFunctionDef) and f.name == "async_handle":
                    target = f
    assert target is not None
    code = textwrap.dedent(ast.get_source_segment(src, target))
    ns["intent"] = types.SimpleNamespace(
        Intent=object, IntentHandleError=type("IntentHandleError", (Exception,), {}))
    # v1.0.97：async_handle 体改经 validate_slots_safely 入口——AST 抽真函数
    # 注入（拒绝替身恒成功：helper 语义漂移/被绕回裸调用时此处当场红）。
    exec(compile(_extract_func_src(CC / "intent_helper.py",  # noqa: S102
                                   "validate_slots_safely"),
                 "<intent_helper.extract>", "exec"), ns)
    # 2026-09-30（C1 存量面）：async_handle 还调模块级 legacy_auto_window_area
    # （旧「自动补窗」动作回放闸）——同口径抽真函数注入，不用恒空替身。它内部
    # 还调创建时刻判据 `_scene_predates_auto_window_retirement`，一并抽真身；
    # 该函数用 datetime.fromisoformat ⇒ ns 得给出 datetime/timezone。
    from datetime import datetime as _dt, timezone as _tz
    from typing import Any as _Any
    ns["datetime"], ns["timezone"], ns["Any"] = _dt, _tz, _Any
    for _fn in ("legacy_auto_window_area",
                "_scene_predates_auto_window_retirement"):
        exec(compile(_extract_func_src(CC / "intent_voice_scene.py", _fn),
                     "<scene.legacy_extract>", "exec"), ns)
    assert callable(ns.get("legacy_auto_window_area")), "回放闸未注入"
    assert callable(ns.get("_scene_predates_auto_window_retirement")), "时间戳判据未注入"
    # v1.1.29：成败折算已收口到 intent_result.fold_action_ok——真源注入（漂移即红），
    # 与上面的 validate_slots_safely 同规。
    exec(compile((CC / "intent_result.py").read_text(encoding="utf-8"),  # noqa: S102
                 "<intent_result.py>", "exec"), ns)
    exec(compile(code, "<extract>", "exec"), ns)  # noqa: S102
    ns["get_voice_scene_store"] = lambda hass: Store()
    intent_obj = types.SimpleNamespace(hass=None, slots={}, context=None)
    return asyncio.run(ns["async_handle"](Self(), intent_obj))


def test_h3_scene_folded_failure_not_success():
    out = _scene_ns(lambda name: {"success": False, "error": "设备离线"})
    assert out["success"] is False, "全折叠失败仍报成功——H3 回退"
    assert "已执行场景" not in (out.get("message") or ""), "失败话术混入成功文案"
    assert all(a["result"] != "success" for a in out["executed_actions"])


def test_h3_scene_mixed():
    seq = {"TurnDeviceOn": {"success": True},
           "TurnDeviceOff": {"success": False, "error": "离线"}}
    out = _scene_ns(lambda name: seq[name])
    assert out["success"] is False and any(
        a["result"] == "success" for a in out["executed_actions"]), "部分失败必须整体不判成"


def test_h3_scene_all_good_still_success():
    out = _scene_ns(lambda name: {"success": True})
    assert out["success"] is True and "已执行场景" in out.get("message", "")


# ── H3 自动化侧 ─────────────────────────────────────────────────────
def _auto_results(response):
    ns = {"_LOGGER": _Log(), "DOMAIN": "huijian_ai"}
    exec(compile((CC / "intent_result.py").read_text(encoding="utf-8"),  # noqa: S102
                 "<intent_result.py>", "exec"), ns)
    async def _handle(**kw):
        return response
    ns["ha_intent"] = types.SimpleNamespace(async_handle=_handle)
    src = (CC / "intent_automation.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for f in getattr(cls, "body", []):
                if isinstance(f, ast.AsyncFunctionDef) and f.name == "_execute_actions":
                    target = f
    assert target is not None
    code = textwrap.dedent(ast.get_source_segment(src, target))
    exec(compile(code, "<extract>", "exec"), ns)  # noqa: S102
    self = types.SimpleNamespace(_hass=None)
    return asyncio.run(ns["_execute_actions"](
        self, [{"intent": "TurnDeviceOn", "params": {}}]))


def test_h3_automation_folded_dict_failure():
    r = _auto_results({"success": False, "error": "离线"})
    assert r == [("TurnDeviceOn", False, "离线")], "折叠 dict 失败被记成功——H3 回退"


def test_h3_automation_intent_response_error_type():
    resp = types.SimpleNamespace(success=False, error="no match")
    r = _auto_results(resp)
    assert r and r[0][1] is False and "no match" in r[0][2]


def test_h3_automation_success_kept():
    r = _auto_results(types.SimpleNamespace(success=True, error=None))
    assert r == [("TurnDeviceOn", True, "")]


# ── H4 形态升级消费点计数钉（§三 一族病的机械化防御）───────────────
def test_h4_press_multi_buttons_consumers():
    pat = "_press_multi_buttons("
    # intent_turn 必须解包双元组（旧缺陷形态：单值接 tuple）
    it = (CC / "intent_turn.py").read_text(encoding="utf-8")
    assert "results, failed_msgs = await _press_multi_buttons" in it
    assert "results = await _press_multi_buttons" not in it, \
        "tuple 消费者漏改形态回退（返回值升级必须 grep 全消费点）"
    # 计数：定义 1 + 消费点 3（intent_turn 1 + window_control 2），全部
    # 双元组解包——新增调用者必须同步，否则计数或解包断言变红
    total = sum((CC / f).read_text(encoding="utf-8").count(pat)
                for f in ("intent_turn.py", "intent_window_control.py"))
    assert total == 4, f"消费点数变化（现 {total}）——新增调用者必须同步双元组解包"
    unpacked = sum((CC / f).read_text(encoding="utf-8").count(
        ", failed_msgs = await _press_multi_buttons")
        for f in ("intent_turn.py", "intent_window_control.py"))
    assert unpacked == total - 1, "有消费点未解包 failed_msgs"


# ── v1.1.29 复核 A5：{"results":[…]} 形态的两条消费链（同族第 4 处漏网） ──
def test_a5_scene_replay_results_shape_all_failed():
    """SetDeviceMode 族只回 {"results":[…]}（无 success 键）⇒ 逐台全失败仍播
    「已执行场景：X」。旧判据 `get("success") is not False` 无键即恒真。"""
    out = _scene_ns(lambda name: {"results": [{"success": False,
                                               "error": "does not support set_mode"}]})
    assert out["success"] is False, out
    assert "已执行场景" not in (out.get("message") or ""), out


def test_a5_scene_replay_results_shape_one_ok():
    seq = {"TurnDeviceOn": {"results": [{"success": True}]},
           "TurnDeviceOff": {"results": [{"success": True}]}}
    out = _scene_ns(lambda name: seq[name])
    assert out["success"] is True and "已执行场景" in out.get("message", ""), out


def test_a5_automation_results_shape_all_failed():
    r = _auto_results({"results": [{"name": "x", "success": False, "error": "不支持"}]})
    assert r == [("TurnDeviceOn", False, "不支持")], r


def test_a5_automation_results_shape_any_ok():
    r = _auto_results({"results": [{"success": False}, {"success": True}]})
    assert r and r[0][1] is True, r
