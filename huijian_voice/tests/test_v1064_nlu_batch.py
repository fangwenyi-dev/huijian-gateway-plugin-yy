"""v1.0.64 深审批1 NLU 语义正确性钉（H1/M2/M3，报告 2026-09-23）。

H1 「温度调高一点」相对档被改道闸 `_to_int("+1")=1` 静默变绝对值——全屋空调
   设到 1°C 且谎报「已调到1度」。改道只对无符号绝对值合法。
M2 「关灯和窗/关闭灯和插座」单字通用设备词在 coord_refuse ≥2 字判据盲区，
   T0 单发吃左片丢右片谎报成功。
M3 「开灯亮度50/关灯亮度百分之三十」T0 动词头吞目标后属性尾巴静默丢，
   且违约 CHANGELOG v1.0.37「触发词是开灯时，说开灯亮度50依然是调亮度」。
   能折算落属性通道；位置裸短数字歧义（50% vs 第1档）如实拒不猜。
"""
import asyncio
from pathlib import Path

import pytest

from core.nlu.fast_path import FastPath
from core.nlu.targets import coord_refuse
from core.nlu.textcnn import TextCNN


class S:
    def get(self, k, d=None):
        return {"nlu.textcnn_enabled": True}.get(k, d)


class FS:
    async def refresh(self, force=False):
        pass

    def check(self, t):
        return None

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def verify_or_refresh(self, p):
        return None


@pytest.fixture(scope="module")
def fp():
    tc = TextCNN(Path(__file__).resolve().parents[1] / "nlu_data")
    tc._ensure()
    return FastPath(FS(), tc, S())


# ── H1 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["温度调高一点", "温度调低一点"])
def test_h1_bare_relative_temperature_not_absolute(fp, text):
    p = asyncio.run(fp.match(text))
    assert p is not None, f"{text!r} 落空"
    assert p.intent != "HassClimateSetTemperature", \
        f"{text!r} 又改道绝对档——'+1'被 _to_int 吃成 1°C（v1.0.63 前实发形态）"
    assert p.intent == "AdjustDeviceAttribute"
    assert p.args.get("attribute") == "temperature"
    assert str(p.args.get("delta")).startswith(("+", "-")), "相对档必须带符号"


def test_h1_absolute_temperature_reroute_kept(fp):
    p = asyncio.run(fp.match("把温度调到26度"))
    assert p and p.intent == "HassClimateSetTemperature"
    assert p.args.get("temperature") == 26, "无符号绝对值改道是既有正确语义，不许误伤"


# ── M2 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "关灯和窗", "开灯和窗", "关灯和门",           # 裸单字动词头+单字通用词形
    "关闭灯和插座", "打开灯和窗",                  # 双字头+单字片（len≥2 盲区形）
])
def test_m2_single_char_coord_refused(text):
    assert coord_refuse(text), f"{text!r} 并列句未拒——单发吃左丢右"


@pytest.mark.parametrize("text", ["关灯", "打开灯", "关门"])
def test_m2_single_device_not_refused(fp, text):
    assert not coord_refuse(text)
    p = asyncio.run(fp.match(text))
    assert p is not None and p.intent in ("TurnDeviceOn", "TurnDeviceOff"), \
        f"{text!r} 正常单设备句被误拒"


# ── M3 ──────────────────────────────────────────────────────────────
def test_m3_light_brightness_tail(fp):
    p = asyncio.run(fp.match("开灯亮度50"))
    assert p and p.intent == "AdjustDeviceAttribute", \
        "「开灯亮度50」又落开关谎报——尾巴被吞（v1.0.37 承诺违约形）"
    assert p.args.get("attribute") == "brightness"
    assert str(p.args.get("delta")) == "50"


def test_m3_cn_percent_tail(fp):
    p = asyncio.run(fp.match("关灯亮度百分之三十"))
    assert p and p.intent == "AdjustDeviceAttribute"
    assert p.args.get("attribute") == "brightness"
    assert str(p.args.get("delta")) in ("cn:三十", "30")


def test_m3_area_color_temp_tail(fp):
    p = asyncio.run(fp.match("打开卧室色温4000"))
    assert p and p.intent == "AdjustDeviceAttribute"
    assert p.args.get("attribute") == "color_temperature"
    tgt = str(p.args.get("target"))
    assert "卧室" in tgt, "区域前缀必须进 target，不许丢成全屋"


def test_m3_position_bare_number_refused(fp):
    p = asyncio.run(fp.match("打开窗户位置1"))
    assert p is None or p.intent == "AdjustDeviceAttribute" and str(p.args.get("delta")) != "1", \
        "位置裸数字歧义（50% vs 第1档按压）不许猜成执行"


def test_m3_position_half_kept(fp):
    p = asyncio.run(fp.match("开窗帘位置一半"))
    assert p and p.intent == "AdjustDeviceAttribute"
    assert str(p.args.get("delta")) == "50"
    assert p.args.get("attribute") == "position"


def test_m3_no_regression_plain(fp):
    p = asyncio.run(fp.match("开灯"))
    assert p and p.intent == "TurnDeviceOn", "M3 守卫不得吞普通句"
