"""v1.0.90 钉：开关族能力闸覆盖面 + 窗控话术去重 + 服务调用异常必须被消费。

全部来自真机确定性复现（COM12 + HA 1.0.89，经加载项 llm 通道直灌级联）：
  「关闭展厅未开窗」→ 现场先「能力闸拦下」，随后**降级通道**投
     TurnDeviceOff{target:[{devices:[{name:'展厅'}]}]} 又"成功 | 好的，展厅的展厅关了"
     ⇒ 旧 `_TURN_FAMILY` 只认 Hass* 三名，降级后不再复检（时拦时放行的真因）。
  「… → media_player.zhan_ting does not support media_player.turn_off」
     伴随 `Error doing job: Task exception was never retrieved (task: None)`
     ⇒ `asyncio.wait` 的 done 分支从不取回任务异常。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "core" / "executor.py"
WCTL = ROOT / "custom_components" / "huijian_ai" / "intent_window_control.py"
TURN = ROOT / "custom_components" / "huijian_ai" / "intent_turn.py"


def _extract(path, name, ns=None):
    import re
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            g = {"re": re, "any": any, "isinstance": isinstance}
            if ns:
                g.update(ns)
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), g)
            return g[name]
    raise AssertionError(f"{path} 找不到 {name}")


def _class_const(path, cls, field):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    c = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls)
    for st in c.body:
        tgt = (getattr(st.target, "id", "") if isinstance(st, ast.AnnAssign)
               else (getattr(st.targets[0], "id", "") if isinstance(st, ast.Assign) else ""))
        if tgt != field:
            continue
        expr = ast.unparse(st.value)
        # literal_eval 不认函数调用：`frozenset({...})` 先剥壳再解析集合字面量
        for wrap in ("frozenset(", "set(", "tuple(", "list("):
            if expr.startswith(wrap) and expr.endswith(")"):
                expr = expr[len(wrap):-1]
                break
        return ast.literal_eval(expr)
    raise AssertionError(f"{cls}.{field} 未找到")


_GATE_NS = {
    "_TURN_FAMILY": _class_const(EXE, "Executor", "_TURN_FAMILY"),
    "_UNTOGGLEABLE_DOMAINS": _class_const(EXE, "Executor", "_UNTOGGLEABLE_DOMAINS"),
    "_WINDOW_HINT_WORDS": _class_const(EXE, "Executor", "_WINDOW_HINT_WORDS"),
}


class _Self:
    def __init__(self, ns):
        for k, v in ns.items():
            setattr(self, k, v)


def _gate(ns):
    fn = _extract(EXE, "_turn_gate", ns)

    def call(name, args, utt):
        return fn(_Self(ns), name, args, utt)
    return call


# ── ① 覆盖面：慧尖自有开关意图必须同闸 ──────────────────────────────────

def test_turn_family_covers_huijian_intent_names():
    fam = _GATE_NS["_TURN_FAMILY"]
    for n in ("HassTurnOn", "HassTurnOff", "HassToggle",
              "TurnDeviceOn", "TurnDeviceOff", "ToggleDevice"):
        assert n in fam, f"{n} 不在开关族闸内——降级通道会绕过复检"


def test_gate_blocks_huijian_area_fanout_window_utterance():
    """现场复现句：降级 TurnDeviceOff{target:[{devices:[{name:'展厅'}]}]} + 原句含窗。"""
    g = _gate(_GATE_NS)
    r = g("TurnDeviceOff",
          {"target": [{"devices": [{"name": "展厅", "domains": []}]}]},
          "关闭展厅未开窗")
    assert r, "降级通道面积扇出未被拦＝谎报成功与实体风暴重新出现"


def test_gate_still_allows_normal_light_and_pure_cover_sentences():
    g = _gate(_GATE_NS)
    assert g("TurnDeviceOn",
             {"target": [{"area": "客厅", "devices": [{"name": "灯", "domains": ["light"]}]}]},
             "打开客厅的灯") is None, "误伤普通灯句"
    assert g("HassTurnOn", {"area": "客厅"}, "打开客厅的窗帘") is None, \
        "纯窗帘句（合法 cover）被窗字误杀——本批刻意不扩大闸面"
    assert g("HassTurnOn", {"entity_id": "light.a"}, "打开灯") is None


def test_gate_untoggleable_entity_blocked_on_new_family_too():
    """新增三名走同一条域判据：不可开关域的 entity_id 必须如实失败。"""
    g = _gate(_GATE_NS)
    r = g("TurnDeviceOff", {"entity_id": "sensor.zhan_ting_a"}, "关展厅")
    assert r, "sensor 实体喂 turn_off 必须拦（v1.0.69 判据对慧尖名同样成立）"
    assert g("HassTurnOff", {"entity_id": "media_player.zhan_ting"}, "关展厅") is None, \
        "media_player 可开关，不该被域闸误杀（v1.0.69 判据不变）"


# ── ② 话术去重：不再念「展厅的展厅」──────────────────────────────────────

def test_window_label_dedups_area_named_device():
    lbl = _extract(WCTL, "_window_label")
    assert lbl("展厅", "展厅", None) == "展厅", "设备名回落成区域名时必须去重"
    assert lbl("展厅", "推拉窗", None) == "展厅的推拉窗", "原「区域+设备」语义不变"
    assert lbl(None, "推拉窗", None) == "推拉窗"
    assert lbl("展厅", None, None) == "展厅的所有窗户", "全屋形态不变"
    assert lbl("展厅", "", "内开窗") == "展厅的内开窗"


# ── ③ 服务调用异常必须被消费（不再刷 HA 全局未取回）──────────────────────

def test_run_then_background_consumes_done_task_exception():
    src = TURN.read_text(encoding="utf-8")
    i = src.index("async def _run_then_background")
    body = src[i:src.index("    @staticmethod", i)]
    assert "elif done:" in body and "t.exception()" in body, \
        "asyncio.wait 的 done 分支必须取回异常，否则 ServiceNotSupported 变成 " \
        "`Task exception was never retrieved` 全局错误风暴"
    assert "add_done_callback(_log_exception)" in body, "超时支原有消费不得回退"
