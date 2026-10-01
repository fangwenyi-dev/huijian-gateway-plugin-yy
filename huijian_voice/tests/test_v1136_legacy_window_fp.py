# -*- coding: utf-8 -*-
"""修⑤：C1 存量「自动补窗」回放闸的两处收口——键形判据 + 恢复口。

现场（2026-10-01，本机把 `legacy_auto_window_area` 真函数喂进构造库实得）：

    用户明说的匿名区级开窗（intent+params 形, created_at=2026-09-01）-> '客厅'  ⇒ 永久跳过
    同形带 created_at=2026-10-01                                      -> ''      ⇒ 执行

三件指纹（匿名窗 + 同区开关 + ≥2 非 button 域同向）**分不开两种来源**：
`intent_automation.py` 的 schema 示例逐字就是
`actions=[{intent:'ControlWindow', params:{action:'open', target:[{area:'客厅',
devices:[{domains:['button']}]}]}}]`——现行模型仍在这么写（v1.1.34 复核批自己也承认）。
而旧启发式的唯一写入口 `_auto_supplement_windows`（v1.1.32 及以前）逐字写的是
`{"name": "ControlWindow", "parameters": {...}}`。⇒ 键形是可分的，且已经够了：
`name` 路（语音链 split 产物）里明说的窗**必带设备名**（`intent_device_shared`
按 `is_window_device` 把 devices 拆开保留 name），匿名 ⇒ 只可能是启发式。

第二处：`update_scene` 旧形不重盖 `created_at` ⇒ 用户照播报去面板"删掉这条动作再存"
仍然被跳（恢复口断）。整体替换动作即用户的当下确认，必须重新计时。
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from test_v1127_scene_flow import vs, _fresh_scene_store               # noqa: E402

OLD = "2026-09-01T02:00:00+00:00"
NEW = "2026-10-01T02:00:00+00:00"

# 语音链（启发式同源）形状：name + parameters，匿名区级窗
LEGIT_HEURISTIC = {
    "name": "TurnDeviceOn",
    "parameters": {"target": [{"area": "客厅", "devices": [
        {"name": "吸顶灯", "domains": ["light"]},
        {"name": "空调", "domains": ["climate"]}]}]},
}
WIN_FROM_HEURISTIC = {
    "name": "ControlWindow",
    "parameters": {"action": "open",
                   "target": [{"area": "客厅", "devices": [{"domains": ["button"]}]}]},
}
# 模型/工具通道形状：intent + params（同内容）
WIN_FROM_MODEL = {
    "intent": "ControlWindow",
    "params": {"action": "open",
               "target": [{"area": "客厅", "devices": [{"domains": ["button"]}]}]},
}


def _lw(action, siblings, created):
    return vs.legacy_auto_window_area(action, siblings, created)


# ── ① 真启发式产物照旧被跳（不许因为收窄而漏放）────────────────────
def test_heuristic_product_still_skipped():
    assert _lw(WIN_FROM_HEURISTIC, [LEGIT_HEURISTIC, WIN_FROM_HEURISTIC],
               OLD) == "客厅"


def test_non_tail_anonymous_window_not_skipped():
    """位置判据：旧启发式只会 `new_actions.append(...)`（git 8b47609 逐字取证），
    永远落在动作表**末尾**。不在末尾的匿名区级窗不可能是它的产物 ⇒ 照常执行。
    这条只减少误跳、不放松防御（真产物必在末尾）。"""
    mid = dict(WIN_FROM_HEURISTIC)
    sibs = [mid, LEGIT_HEURISTIC, {"name": "TurnDeviceOn",
                                   "parameters": {"target": []}}]
    assert _lw(mid, sibs, OLD) == "", "非末尾的匿名窗被误跳（启发式append不到这里）"


def test_new_scene_never_skipped():
    assert _lw(WIN_FROM_HEURISTIC, [LEGIT_HEURISTIC, WIN_FROM_HEURISTIC],
               NEW) == ""


# ── ② 模型正当写入不再被跳（撤掉键形判据即红）──────────────────────
def test_model_written_area_window_is_not_skipped():
    sibs = [{**LEGIT_HEURISTIC, "intent": LEGIT_HEURISTIC["name"],
             "params": LEGIT_HEURISTIC["parameters"]}, WIN_FROM_MODEL]
    assert _lw(WIN_FROM_MODEL, sibs, OLD) == "", \
        "模型写的匿名区级窗被当存量启发式永久跳过"


def test_voice_chain_named_window_window_still_not_skipped():
    """语音链明说的窗必带设备名（split 保留 name）——①件判据本就把它排除。"""
    named = {"name": "ControlWindow",
             "parameters": {"action": "open", "target": [
                 {"area": "客厅", "devices": [
                     {"name": "推拉窗", "domains": ["button"]}]}]}}
    assert _lw(named, [LEGIT_HEURISTIC, named], OLD) == ""


# ── ③ 恢复口：改一次动作即重盖 created_at，之后不再被跳 ────────────
def test_update_scene_restamps_only_when_actions_actually_change():
    store = _fresh_scene_store()
    ok, sid = asyncio.run(store.create_scene("回家", [LEGIT_HEURISTIC,
                                                      WIN_FROM_HEURISTIC]))
    assert ok, sid

    def _stamp_and_gate():
        data = asyncio.run(store._load_data())
        sc = data["scenes"][sid]
        return sc.get("created_at"), _lw(sc["actions"][1], sc["actions"],
                                         sc.get("created_at"))

    asyncio.run(store._save_data(_as_old(_load(store), sid)))
    assert _stamp_and_gate()[1] == "客厅", "存量形状本该被跳（前提）"

    # ① 读-改-写回但动作一字未动 ⇒ 不许重盖（复核实证：旧形会重盖，等于把
    #    退场的匿名区级窗动作**放回执行面**，按区压所有窗钮）
    same = _load(store)["scenes"][sid]["actions"]
    asyncio.run(store.update_scene(sid, actions=same))
    stamp, gate = _stamp_and_gate()
    assert stamp == OLD, f"原样重存却重盖了时间戳（{stamp}）"
    assert gate == "客厅", "旧启发式补窗被放回执行面"

    # ② 动作真的变了（用户删掉那条补窗）⇒ 重盖，且闸不再跳
    kept = [a for a in _load(store)["scenes"][sid]["actions"]][:1]
    asyncio.run(store.update_scene(sid, actions=kept))
    data3 = _load(store)["scenes"][sid]
    assert data3.get("created_at") != OLD, "改了动作没重盖 ⇒ 恢复口仍是断的"
    assert len(data3["actions"]) == 1, "删掉补窗动作后没写进去"


def _load(store):
    return asyncio.run(store._load_data())


def _as_old(data, sid):
    data["scenes"][sid]["created_at"] = OLD
    return data


def test_no_ui_writer_and_no_false_guidance():
    """留账（复核实证）：面板**没有**动作编辑口，所以别把用户往那儿指。

    manage.html 两处 PUT 只发 `{trigger_phrase}`/`{trigger}`，`core/admin_api.py`
    代理也只转发 trigger_phrase；`GET /voice-scenes` 不回 raw actions ⇒ 页面改不出
    动作。因此 `update_scene` 的重盖只在 store API 级可达（上面两条钉证的就是这级）。
    本钉守两件事：①文案不许再写"请在面板编辑删除"（假引导）；②一旦真做出动作编辑
    口，①那条断言会红，提醒同步把恢复口的用户路径钉补上。
    """
    root = Path(__file__).resolve().parents[1]
    html = (root / "custom_components/huijian_ai/templates/manage.html").read_text(
        encoding="utf-8")
    api = (root / "custom_components/huijian_ai/api.py").read_text(encoding="utf-8")
    import re
    # 面板任何一处 PUT 的 body 里都不得出现 actions 字段（现网确实没有）
    bodies = re.findall(r"JSON\.stringify\(\{[^{}]*\}\)", html)
    assert bodies, "摘不到 PUT body，本钉需随页面结构同步"
    assert not [b for b in bodies if "actions" in b], \
        "面板出现了动作写入——需把 store 级恢复口升级为用户路径并补钉"
    assert "请在面板编辑删除" not in api, "假引导回潮（面板没有动作编辑口）"


def test_trigger_phrase_only_edit_keeps_stamp():
    """只改触发词不动动作 ⇒ 不许重盖（否则时间闸被白送）。"""
    store = _fresh_scene_store()
    ok, sid = asyncio.run(store.create_scene("回家", [WIN_FROM_HEURISTIC]))
    data = asyncio.run(store._load_data())
    data["scenes"][sid]["created_at"] = OLD
    asyncio.run(store._save_data(data))
    asyncio.run(store.update_scene(sid, trigger_phrase="回来了"))
    data2 = asyncio.run(store._load_data())
    assert data2["scenes"][sid]["created_at"] == OLD, "没动动作却重盖了时间戳"
