"""T0/T1 级联收编回归（用例=迁移期实测矩阵，23 案 + 守卫）。"""
import asyncio

import pytest

from core.nlu.fast_path import FastPath, _is_complex_query
from core.nlu.textcnn import TextCNN


class FakeScenes:
    def __init__(self, triggers=("观影模式",)):
        self.triggers = set(triggers)

    async def refresh(self, force=False):
        pass

    def check(self, text):
        for t in self.triggers:
            if text == t or text.startswith(t):
                return t
        return None

    async def verify_or_refresh(self, phrase):
        return phrase in self.triggers


@pytest.fixture(scope="module")
def tc(request):
    import os
    from pathlib import Path
    t = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    t._ensure()
    return t


@pytest.fixture()
def fp(tc, settings):
    return FastPath(FakeScenes(), tc, settings)


MATRIX = {
    # 中文基础指令
    "打开客厅的灯": ("TurnDeviceOn", {"area": "客厅"}),
    "关闭书房筒灯": ("TurnDeviceOff", {"area": "书房"}),
    "开灯": ("TurnDeviceOn", None),
    "关灯": ("TurnDeviceOff", None),
    "把风扇关掉": ("TurnDeviceOff", None),
    "关一下客厅窗帘": ("TurnDeviceOff", {"area": "客厅"}),
    # 窗户族（内倒=A→归一小写 a；集成侧 find_action_in_text 实测兼容）
    "内倒展厅窗户": ("ControlWindow", None),
    "暂停窗户动作": ("ControlWindow", None),
    "窗户内倒一下": ("ControlWindow", None),
    "内倒一下平推车": ("ControlWindow", None),      # 纠错表→平推窗
    # 调节族
    "亮度调到60%": ("AdjustDeviceAttribute", None),
    "色温调到4000k": ("AdjustDeviceAttribute", None),
    "风扇风量小一点": ("AdjustDeviceAttribute", None),
    "客厅的窗帘关一半": ("AdjustDeviceAttribute", {"area": "客厅"}),
    "把卧室灯开到50%": ("AdjustDeviceAttribute", {"area": "卧室"}),
    "卧室亮度调高一点": ("AdjustDeviceAttribute", {"area": "卧室"}),   # 属性词快捷（收编修复六）
    "风量大一点": ("AdjustDeviceAttribute", None),
    "亮一点": ("AdjustDeviceAttribute", None),
    # 场景
    "观影模式": ("HassTriggerVoiceScene", None),
    # 英文
    "turn on the living room light": ("TurnDeviceOn", None),
    # 窗型词落在设备名（2026-09-08 实机：曾被错产成 Turn*）
    "打开 办公室平开窗": ("ControlWindow", {"area": "办公室"}),
    "关闭卧室推拉门": ("ControlWindow", {"area": "卧室"}),
}

GUARD_NONE = [
    "空调调到26度",          # 空调缺区域守卫（原 v1.5 语义保留）
    "客厅现在多少度",        # 放行给查询族
    "现在几点了",
    "创建自动化",
    "帮我把所有灯都关掉",
    "客厅灯为什么不开",
]


@pytest.mark.parametrize("text,expect", MATRIX.items())
def test_matrix(fp, text, expect):
    intent, area_must = expect
    plan = asyncio.run(fp.match(text))
    assert plan is not None, f"{text} 未命中"
    assert plan.intent == intent, (text, plan.intent, plan.trace)
    if area_must:
        assert area_must.items() <= plan.args["target"][0].items(), (text, plan.args)


def test_window_type_device_name_rewrites(fp):
    plan = asyncio.run(fp.match("打开 办公室平开窗"))
    assert plan.intent == "ControlWindow" and plan.args["action"] == "open"
    assert plan.args["target"][0]["devices"][0]["name"] == "平开窗"
    assert any("窗型纠正" in t for t in plan.trace), plan.trace
    off = asyncio.run(fp.match("关闭办公室推拉窗"))
    assert off.intent == "ControlWindow" and off.args["action"] == "close"


def test_curtain_stays_turn_device(fp):
    plan = asyncio.run(fp.match("关闭客厅窗帘"))
    assert plan is not None and plan.intent == "TurnDeviceOff"


@pytest.mark.parametrize("text", GUARD_NONE)
def test_guards_return_none(fp, text):
    assert asyncio.run(fp.match(text)) is None


def test_window_action_lowercase(fp):
    plan = asyncio.run(fp.match("内倒展厅窗户"))
    assert plan.args["action"] == "a"          # 收编改造点 5：归一小写


def test_complex_query_guard_semantics():
    assert _is_complex_query("客厅灯状态如何")
    assert not _is_complex_query("客厅多少度")   # 温度查询放行（原 L269 语义）
    assert _is_complex_query("把客厅保存为会客模式")  # M1 场景创建不接管


def test_scene_uncached_miss(fp):
    fp.scenes = FakeScenes(triggers=())
    assert asyncio.run(fp.match("观影模式")) is None


def test_office_spotlight_target(fp):
    # 修A 端到端锁定（用户实机轨迹：曾产出 {name:灯} 无区域 → HA 等值匹配全空 "没找到这个设备"）
    plan = asyncio.run(fp.match("打开 办公室射灯"))
    assert plan is not None and plan.intent == "TurnDeviceOn"
    assert plan.args["target"] == [{"area": "办公室",
                                    "devices": [{"name": "射灯", "domains": ["light"]}]}], plan.trace
