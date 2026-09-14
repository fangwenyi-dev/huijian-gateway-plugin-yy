"""2026-09-14 现场日志 → 2026-09-27 修复批：「平盖窗」谎报三连修的行为钉。

病灶链：ASR 听错（平开窗→平盖窗）→ parse_target 把未知窗词尾剥折叠成
泛称「窗」→ TurnDeviceOn 车道 → 集成裸「窗」走全窗兜底并**伪造**
「好的，办公室的窗户打开了」（窗没动、办公室所有开合器按钮反被按）。
三层修：①corrector 音似表（平盖窗/平改窗→平开窗）；②fast_path 泛窗
闸（抗折叠保具名整词 + 裸「窗」也归 ControlWindow 如实车道）；
③集成端「未识别窗名不敢按全窗」如实拒收终（test_window_overopen_guard
既有钉，本文件不重复）。
"""
import asyncio
import os

import pytest

from core.nlu import corrector
from core.nlu.fast_path import FastPath
from core.nlu.textcnn import TextCNN


class FakeScenes:
    def __init__(self):
        self.triggers = set()

    async def refresh(self, force=False):
        pass

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    def check(self, text):
        return None

    async def verify_or_refresh(self, phrase):
        return None


@pytest.fixture(scope="module")
def tc():
    t = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    t._ensure()
    return t


@pytest.fixture()
def fp(tc, settings):
    return FastPath(FakeScenes(), tc, settings)


def test_corrector_pingaichuang():
    assert corrector.apply("打开办公室平盖窗") == "打开办公室平开窗"
    assert corrector.apply("关闭平改窗") == "关闭平开窗"


def test_field_utterance_now_real_window(fp):
    """现场原句：修后必须走 ControlWindow+平开窗（真开窗，不再是谎报）。"""
    plan = asyncio.run(fp.match("打开办公室平盖窗"))
    assert plan is not None and plan.intent == "ControlWindow"
    assert plan.args["action"] == "open"
    assert plan.args["target"][0]["area"] == "办公室"
    assert plan.args["target"][0]["devices"][0]["name"] == "平开窗"


def test_unknown_window_token_keeps_whole_name(fp):
    """层②独立成立（不依赖 corrector 恰好收过音）：未知「X窗」保留整词
    → ControlWindow → 集成如实拒收。旧行为=折叠成「窗」按全窗+谎报。"""
    plan = asyncio.run(fp.match("打开办公室钻石窗"))
    assert plan is not None and plan.intent == "ControlWindow"
    nm = plan.args["target"][0]["devices"][0]["name"]
    assert nm == "钻石窗", f"具名窗词被折叠成 {nm!r}=谎报温床回归"
    assert any("抗折叠" in t for t in plan.trace)


def test_bare_window_goes_honest_lane(fp):
    """裸「窗」句改道 ControlWindow：全窗执行走如实收口（空结果=失败），
    不再经 TurnDeviceOn 的 setdefault 伪造「X的窗户打开了」。"""
    for text in ("打开办公室窗", "关闭办公室的窗", "开个窗"):
        plan = asyncio.run(fp.match(text))
        assert plan is not None and plan.intent == "ControlWindow", text
        assert plan.args["action"] in ("open", "close")


def test_curtain_and_opener_lanes_unmoved(fp):
    """回归护栏：帘族仍走 Turn*（cover 设备），开窗器词仍走整名保留车道。"""
    plan = asyncio.run(fp.match("打开办公室窗帘"))
    assert plan.intent == "TurnDeviceOn"
    off = asyncio.run(fp.match("关闭3号开窗器"))
    assert off.intent == "ControlWindow"
    assert "开窗器" in off.args["target"][0]["devices"][0]["name"]
