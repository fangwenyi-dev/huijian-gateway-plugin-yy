"""v1.0.39 修复钉：来自 1.0.31→1.0.38 跨版本回归 sweep 的三处实锤。

F1 场景契约优先于显式全屋闸 → tests/test_fast_path.py::test_scene_trigger_beats_wholehouse_plan
F2 场景清单播报带编号（与「删第N条」同源） → tests/test_pipeline_creation.py
F3 逐实体 results 折算不掩盖失败（本文件）；三族替身真形态 → tests/e2e/sim_full.py S12
"""
import json

from core.ha_client import HAClient


def _fold(payload, name="SetDeviceMode"):
    return HAClient._normalize_result(200, json.dumps(payload, ensure_ascii=False), name)


def test_results_payload_folds_success_from_entities():
    """intent_set_mode 只回 {"results":[逐实体 success]}，**没有**顶层 success。
    旧折算认不出这个形态 → 整体折进 raw 并按 HTTP 200 判成功：设备全部失败也
    会回「好的」。与 v1.0.34 修掉的 AdjustDeviceAttribute 属同一形态盲区。"""
    all_bad = {"results": [{"success": False, "name": "客厅射灯",
                            "error": "light 不支持模式 sleep"}]}
    assert _fold(all_bad)["success"] is False, all_bad
    one_ok = {"results": [{"success": False, "name": "a"}, {"success": True, "name": "b"}]}
    assert _fold(one_ok)["success"] is True
    assert _fold({"results": []})["success"] is False          # 空结果不算成功


def test_folding_keeps_existing_shapes_intact():
    """加宽折算不能改判既有形态：顶层 success 权威、states 族照旧、错误体照旧。"""
    assert _fold({"success": False, "error": "boom"})["success"] is False
    assert _fold({"success": True, "control_targets": [{"name": "射灯"}]})["success"] is True
    adj = {"success_count": 0, "states": [{"name": "射灯", "success": False}]}
    assert _fold(adj, "AdjustDeviceAttribute")["success"] is False
    assert _fold({"states": [{"name": "射灯", "success": True}]},
                 "AdjustDeviceAttribute")["success"] is True


def test_sim_ha_three_intents_are_not_always_success():
    """替身纪律：跨端桩不得恒成功（本仓第 4 次同型）。真形态见
    intent_adjust_attribute:690 / intent_set_mode:207 / intent_live_context:209。"""
    import asyncio
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent / "e2e"))
    import sim_full as S
    ha = S.SimHA()
    tgt = [{"area": "客厅", "devices": [{"name": "射灯", "domains": ["light"]}]}]
    bad = asyncio.run(ha.handle_intent("SetDeviceMode", {"target": tgt, "mode": "sleep"}))
    assert bad["success"] is False, bad                      # 灯没有睡眠模式
    ok = asyncio.run(ha.handle_intent(
        "SetDeviceMode", {"target": [{"area": "客厅",
                                      "devices": [{"name": "空调", "domains": ["climate"]}]}],
                          "mode": "sleep"}))
    assert ok["success"] is True and ok.get("results"), ok
    miss = asyncio.run(ha.handle_intent("AdjustDeviceAttribute",
                                        {"target": [{"area": "无此区域"}], "attribute": "brightness"}))
    assert miss["success"] is False, miss
