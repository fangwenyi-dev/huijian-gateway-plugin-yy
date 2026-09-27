# -*- coding: utf-8 -*-
"""中文数词的亮度绝对值（2026-09-27 办公 .91 实锤回归）。

现场签名（板 32b8 / fw 2.1.68 / 加载项 1.1.14 / SenseVoice）：说
「办公室射灯亮度调到一百」，加载项把整句交给 klar，日志
`[执行] HassLightSet {'entity_id': 'light.ban_gong_shi_she_deng', 'brightness': '1'} → 成功 | 办公室 1%`
——用户要 100%，拿到 1%（灯几乎灭）。同一人下一秒改说「百分之百」就对了，因为
`百分之` 形在字面表里有规则、裸数词形没有。

根因不在我们话术层，在引擎侧：klar 的 zh_cn 数词表是 `NumberStyle::ListedOnly`
（`nlu/klar-ha-nlu/src/lang/packs/zh_cn/pack.rs:237-268`，只有「一」「百」这类
**列出的**形），多字合并分支只实现了德/英（`src/parse/numbers.rs:26-55`），
「一百」被拆成 [1,100] 后 `first_number()` 取第一个 ⇒ 1。温度族早已有裸 CN 规则，
亮度族漏配 —— 本文件钉补上的那条，并钉住它**不许**抢「百分之」形与「一半」形。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_cnb_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core.nlu import targets as T                               # noqa: E402
from core.nlu.fast_path import FastPath                         # noqa: E402
from core.settings import Settings                              # noqa: E402


@pytest.fixture(autouse=True)
def _static_vocab():
    T.clear_vocab()
    yield
    T.clear_vocab()


class FakeScenes:
    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    def check(self, text):
        return None


@pytest.fixture()
def fp():
    return FastPath(FakeScenes(), None,
                    Settings(Path(os.environ["HUIJIAN_DATA"]) / f"cnb-{os.getpid()}.json"))


def _plan(fp, text):
    return asyncio.run(fp.match(text))


@pytest.mark.parametrize("text, want", [
    ("亮度调到一百", "100"),
    ("亮度调到八十", "80"),
    ("亮度调到五十", "50"),
    ("亮度调到二十", "20"),
    ("亮度调到十", "10"),
    ("亮度设到一百", "100"),
    ("调亮到八十", "80"),
    ("亮度调到百", "100"),      # ASR 常把「一百」的"一"吃掉，裸「百」也必须是 100
])
def test_bare_cn_number_is_absolute_brightness(fp, text, want):
    """裸中文数词必须进字面表并被 cn2num 归一，而不是掉给 klar。"""
    p = _plan(fp, text)
    assert p is not None, "漏配：整句将掉给 klar（实锤形态）"
    assert p.intent == "AdjustDeviceAttribute", (text, p.intent, p.args)
    assert p.args.get("attribute") == "brightness", (text, p.args)
    assert p.args.get("delta") == want, (text, p.args)


@pytest.mark.parametrize("text, want", [
    ("亮度调到百分之五十", "50"),      # 「百分之」形优先级必须仍在本行之上
    ("亮度调到百分之百", "100"),
    ("亮度调到百分之二十", "20"),
    ("亮度调到80", "80"),              # 阿拉伯数字形不受影响
])
def test_existing_forms_not_stolen(fp, text, want):
    p = _plan(fp, text)
    assert p is not None and p.intent == "AdjustDeviceAttribute", (text, p and p.intent)
    assert p.args.get("delta") == want, (text, p.args)


def test_half_never_becomes_one_percent(fp):
    """(?!半) 的专职：「亮度调到一半」绝不允许被本行咬成 一=1%。

    裸句在无设备上下文时本来就不进字面表（None 是既有行为，带上下文的 50% 由
    test_multi_device_and_opener.test_brightness_half_word 钉），这里只钉危险方向。
    """
    p = _plan(fp, "亮度调到一半")
    assert p is None or str(p.args.get("delta")) != "1", p and p.args


def test_klar_misparse_signature_is_gone(fp):
    """反向钉：修好后，任何裸中文数词亮度形都不得产出数字串 '1'。"""
    for text in ("亮度调到一百", "亮度调到百", "调亮到一百", "亮度调到五十"):
        p = _plan(fp, text)
        assert p is not None, text
        assert str(p.args.get("delta")) != "1", (text, p.args)
