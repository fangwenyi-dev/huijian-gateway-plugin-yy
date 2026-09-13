"""v1.0.63 开向位置缺陷钉（P0，golden 建表实锤）。

病灶（v1.0.62 golden 副产品）：「窗帘开一半」被泛用 ^(开|打开) 吃成
TurnDeviceOn，"一半"当残渣剥丢——用户要半开得到**全开**。错误结果比
拒答危险（宁缺勿错铁律）。旧表只有 ^关一半（50）。

定案语义：cover position 是绝对开合度（0 闭 100 开），「开一半」与
「关一半」目标位同为 50——executor 既有绝对落地不动，仅补 NLU 入口
（_ACTION_PATTERNS 头位 + _DELTA_SCANNERS["position"] 残扫车道）。
"""
import asyncio
from pathlib import Path

import pytest

from core.nlu.fast_path import FastPath
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


@pytest.mark.parametrize("text", [
    "窗帘开一半", "开一半窗帘", "窗帘打开一半", "窗帘开到一半",   # 修复目标形
    "关一半窗帘", "窗帘关一半",                                    # 旧形不回退
])
def test_half_is_position_not_toggle(fp, text):
    p = asyncio.run(fp.match(text))
    assert p is not None, f"{text!r} 不该落空"
    assert p.intent == "AdjustDeviceAttribute", \
        f"{text!r} 又回到泛用开关档（{p.intent}）——一半语义被吞"
    assert p.args.get("attribute") == "position"
    assert str(p.args.get("delta")) == "50"


@pytest.mark.parametrize("text,want", [
    ("窗帘开到30%", "30"),      # 数值绝对位不回退
    ("窗帘开到一半", "50"),
])
def test_numeric_position_intact(fp, text, want):
    p = asyncio.run(fp.match(text))
    assert p and p.intent == "AdjustDeviceAttribute"
    assert str(p.args.get("delta")) == want


@pytest.mark.parametrize("text,want", [
    ("打开窗帘", "TurnDeviceOn"),     # 泛用开关句不被新表劫持
    ("关闭窗帘", "TurnDeviceOff"),
    ("开窗帘", "TurnDeviceOn"),
])
def test_plain_cover_toggle_untouched(fp, text, want):
    p = asyncio.run(fp.match(text))
    assert p and p.intent == want, f"{text!r} 被位置新表误劫（{p.intent}）"
