# -*- coding: utf-8 -*-
"""v1.1.24-B 加载项侧钉：开关族动作的目标域必须**收窄到主域**。

现场实锤：动态词表把"同名字命中实体的域"并集写进目标（`打开办公室空调` →
domains=[button,climate,light,number,select,switch]），集成按"名字+域"展开成一串实体，
A 修（跳过不可服务域）之后**全部可 turn_on 的兄弟实体都会被打开**——空调那条并集里有
8 个 switch（含睡眠模式/ECO/干燥/辅热）+ 指示灯；而 v1.0.90 的窄域（['climate']）没有
这个静默副作用。本钉把"开关动作取主域"钉死，并抱住属性句/窗户两条不受影响的边界。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os

import tempfile

os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hj_narrow_"))

from core.nlu import targets as T  # noqa: E402
from core.nlu.fast_path import FastPath  # noqa: E402


def _ent(eid, name):
    return {"entity_id": eid, "state": "off", "attributes": {"friendly_name": name}}


# 复刻办公 .91 的形态：空调族（climate+8 switch+number+select+button+light）
_STATES = {e["entity_id"]: e for e in [
    _ent("climate.xiaomi_mc9_aeaf_air_conditioner", "办公室空调 Air Conditioner"),
    _ent("switch.xiaomi_mc9_aeaf_switch_status", "办公室空调 Air Conditioner 开关"),
    _ent("switch.xiaomi_mc9_aeaf_sleep_mode", "办公室空调 睡眠模式"),
    _ent("switch.xiaomi_mc9_aeaf_dryer", "办公室空调 干燥功能"),
    _ent("number.xiaomi_mc9_aeaf_fan_percent", "办公室空调 风速百分比"),
    _ent("select.xiaomi_mc9_aeaf_fan_level", "办公室空调 风机档位"),
    _ent("button.xiaomi_mc9_aeaf_info", "办公室空调 信息"),
    _ent("light.xiaomi_mc9_aeaf_indicator_light", "办公室空调 Indicator Light"),
    _ent("light.ban_gong_shi_she_deng", "射灯"),
    _ent("select.she_deng_effect", "射灯 Effect"),
    _ent("button.ban_gong_shi_she_deng_que_ren", "射灯 确认"),
    _ent("cover.ban_gong_shi_ping_kai_chuang_kai_chuang_qi", "平开窗 开窗器"),
    _ent("button.ban_gong_shi_ping_kai_chuang_1_kai_qi", "平开窗 ① 开启"),
    _ent("number.ban_gong_shi_ping_kai_chuang_su_du", "平开窗 速度"),
    _ent("switch.xiang_xun_ji", "香薰机"),          # 纯 switch 设备（无主域可收）
]}


class _S:
    def get(self, k, default=None):
        return default


class _Scenes:
    triggers = set()

    def check(self, t):
        return None

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        return None

    async def refresh(self, force=False):
        return None


def _fp():
    T.sync_vocab(_STATES, {})
    return FastPath(_Scenes(), None, _S())


def _doms(plan):
    return [d for t in plan.args["target"] for v in (t.get("devices") or [])
            for d in (v.get("domains") or [])]


def test_turn_action_narrows_to_primary_domain():
    """开关动作取主域：空调→climate、射灯→light（旧版窄域行为；防静默多动兄弟实体）。"""
    fp = _fp()
    p = asyncio.run(fp.match("打开办公室空调"))
    assert p and p.intent == "TurnDeviceOn", p
    assert _doms(p) == ["climate"], p.args
    p2 = asyncio.run(fp.match("打开射灯"))
    assert p2 and _doms(p2) == ["light"], p2.args


def test_turn_without_primary_domain_keeps_switch():
    """无主域的纯 switch 设备照旧可开（收窄只认主域优先级，不做一刀切禁用）。"""
    fp = _fp()
    p = asyncio.run(fp.match("打开香薰机"))
    assert p and _doms(p) == ["switch"], p.args


def test_whole_house_turn_also_narrows():
    """全屋句同样收窄（「打开所有灯」不该把灯的 select/switch 兄弟全带上）。"""
    fp = _fp()
    p = asyncio.run(fp.match("打开所有灯"))
    assert p is not None and _doms(p) == ["light"], p.args


def test_attribute_lane_keeps_full_union():
    """反向钉：属性句**不**收窄——「空调风速调到50%」要用 number/select 域。"""
    fp = _fp()
    p = asyncio.run(fp.match("把办公室空调风速调到50%"))
    assert p is not None, "属性句必须保持可解析"
    doms = _doms(p)
    assert any(d in doms for d in ("number", "select")), doms


def test_window_target_untouched():
    """反向钉：窗户不受影响——ControlWindow 走名字+按钮逻辑，域列表**原样**（夹具下
    确定性等值：cover+button+number 一个不少；收窄若误罩窗户这里当场红）。"""
    fp = _fp()
    p = asyncio.run(fp.match("打开平开窗"))
    assert p and p.intent == "ControlWindow", p
    assert set(_doms(p)) == {"button", "cover", "number"}, p.args
