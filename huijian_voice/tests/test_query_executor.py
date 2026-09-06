"""查询族（M1 本地读回）与执行话术层测试。"""
import asyncio

from conftest import FakeHAClient
from core.executor import Executor, zh_error
from core.nlu.query import QueryZone
from core.nlu.fast_path import Plan


def _ha():
    states = {
        "sensor.living_temp": {"entity_id": "sensor.living_temp", "state": "26.5",
                               "attributes": {"friendly_name": "客厅温度", "device_class": "temperature"}},
        "sensor.bed_temp": {"entity_id": "sensor.bed_temp", "state": "22.0",
                            "attributes": {"friendly_name": "卧室温度", "device_class": "temperature"}},
        "light.living": {"entity_id": "light.living", "state": "on",
                         "attributes": {"friendly_name": "客厅筒灯"}},
    }
    return FakeHAClient(states=states, areas={"a1": "客厅", "a2": "卧室"},
                        entity_area={"sensor.living_temp": "客厅", "light.living": "客厅"})


def test_temp_query():
    q = QueryZone(_ha(), None)
    ans = asyncio.run(q.answer("客厅现在多少度"))
    assert ans and "27" not in ans and "26" in ans and "度" in ans


def test_state_query():
    q = QueryZone(_ha(), None)
    ans = asyncio.run(q.answer("客厅灯开着吗"))
    assert ans and "开着" in ans


def test_time_query():
    q = QueryZone(_ha(), None)
    ans = asyncio.run(q.answer("现在几点了"))
    assert ans and "点" in ans and "分" in ans


def test_miss_returns_none():
    q = QueryZone(_ha(), None)
    assert asyncio.run(q.answer("打开灯")) is None


# ── 话术层 ─────────────────────────────────────────────────────
def _exec(results=None):
    return Executor(FakeHAClient(results=results or {}), None)


def test_speech_turn_and_lock():
    ex = _exec()
    p = Plan(intent="TurnDeviceOn", args={"target": [{"area": "客厅", "devices": [{"name": "筒灯"}]}]})
    assert ex.speech(p, {"success": True, "control_targets": [{"name": "筒灯", "area": "客厅"}]}) == "好的，客厅的筒灯打开了"
    p2 = Plan(intent="TurnDeviceOn", args={})
    assert "上锁" in ex.speech(p2, {"success": True, "control_targets": [{"name": "门锁", "area": "大门"}]})
    p3 = Plan(intent="TurnDeviceOff", args={})
    assert "解锁" in ex.speech(p3, {"success": True, "control_targets": [{"name": "门锁"}]})


def test_speech_window_and_adjust():
    ex = _exec()
    p = Plan(intent="ControlWindow", args={"action": "a", "target": [{"devices": [{"name": "窗户"}]}]})
    assert "内倒" in ex.speech(p, {"success": True, "control_targets": [{"name": "窗户", "area": "展厅"}]})
    p2 = Plan(intent="AdjustDeviceAttribute", args={"attribute": "temperature", "delta": "26",
                                                    "target": [{"area": "卧室", "devices": [{"name": "空调"}]}]})
    assert "26度" in ex.speech(p2, {"success": True, "control_targets": [{"name": "空调", "area": "卧室"}]})
    p3 = Plan(intent="AdjustDeviceAttribute", args={"attribute": "brightness", "delta": "+20"})
    assert "调亮" in ex.speech(p3, {"success": True, "control_targets": [{"name": "灯"}]})


def test_speech_scene_and_climate_route():
    ex = _exec()
    assert "观影" in ex.speech(Plan(intent="HassTriggerVoiceScene", args={"trigger_phrase": "观影模式"}),
                               {"success": True})
    assert "26度" in ex.speech(Plan(intent="HassClimateSetTemperature", args={"temperature": 26, "area": "卧室"}),
                               {"success": True})


def test_zh_error_mapping():
    assert "没找到" in zh_error("Could not extract window name from 'x'")
    assert "超时" in zh_error("timeout")
    z = zh_error("weird failure")
    assert z.startswith("抱歉")


def test_executor_run_failure_folds():
    ex = _exec({"TurnDeviceOn": {"success": False, "error": "No entities matched"}})
    ok, msg = asyncio.run(ex.run(Plan(intent="TurnDeviceOn", args={})))
    assert ok is False and msg.startswith("抱歉")
