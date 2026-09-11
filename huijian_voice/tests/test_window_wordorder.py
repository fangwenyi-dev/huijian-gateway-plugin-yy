"""窗型词尾置动词语序回归（2026-09-16 现场：'内岛展厅内导窗' 后接 clarify 事故）。

根因：②③ 前缀剥离只认 KNOWN_DEVICES_PREFIX，窗型词（平开窗/内开内倒窗…）全部
缺席——「区域+窗型+尾动词」句落空给 klar，歧义目标被过宽闸拦成 clarify，用户
感知即"内倒不识别"。修复=窗型词入 PREFIX/TAIL，与 fast_path._WINDOW_TYPES、
集成 valid names 同集合，本文件钉死三方一致防漂移。
"""
import asyncio

from core.nlu import targets as T
from core.nlu.fast_path import _WINDOW_TYPES
from tests.test_fast_path import FakeScenes  # noqa: F401  复用装置


def test_window_types_all_prefix_strippable():
    """_WINDOW_TYPES（窗型纠正表）必须全部在 ②③ 剥离表内——两表失同步即本钉红。"""
    missing = {w for w in _WINDOW_TYPES if w not in set(T.KNOWN_DEVICES_PREFIX)}
    assert not missing, f"以下窗型词未进 KNOWN_DEVICES_PREFIX: {missing}"


def test_known_devices_tail_has_window_family():
    tail = set(T._KNOWN_DEVICES_TAIL)
    for w in ("内开窗", "外开窗", "推拉门", "智能窗", "内开内倒窗", "单内倒窗", "外装平开窗"):
        assert w in tail, f"表二缺窗型 {w}（⑥子串扫描依赖）"


import os
import pytest
from core.nlu.fast_path import FastPath
from core.nlu.textcnn import TextCNN


@pytest.fixture(scope="module")
def fp(request):
    import copy
    from core.settings import DEFAULTS

    class _S:
        def get(self, dotted, default=None):
            cur = copy.deepcopy(DEFAULTS)
            for k in dotted.split("."):
                if not isinstance(cur, dict) or k not in cur:
                    return default
                cur = cur[k]
            return cur
    tc = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    tc._ensure()
    return FastPath(FakeScenes(), tc, _S())


def _run(fp, text):
    return asyncio.run(fp.match(text))


@pytest.mark.parametrize("text,action", [
    # 现场原话（纠错后形）
    ("内倒展厅内倒窗", "a"),
    # 尾置动词全谱系
    ("展厅平开窗内倒", "a"),
    ("把展厅平开窗内倒", "a"),
    ("展厅内开内倒窗内倒", "a"),
    ("办公室平开窗关闭", "close"),
    ("办公室平开窗关上", "close"),
    # 前置动词原形态不回退
    ("内倒展厅窗户", "a"),
    ("关闭办公室平开窗", "close"),
    ("内倒一下平开窗", "a"),
])
def test_window_wordorder_matrix(fp, text, action):
    p = _run(fp, text)
    assert p is not None, f"{text!r} 仍落空"
    assert p.intent == "ControlWindow"
    assert p.args.get("action") == action


def test_raw_field_utterance_end_to_end(fp):
    """现场 ASR 原句（含误识别）：纠错+语序两级联后必须出 ControlWindow a。"""
    p = _run(fp, "内岛展厅内导窗")
    assert p is not None and p.intent == "ControlWindow"
    assert p.args.get("action") == "a"
    tgt = p.args["target"][0]
    assert tgt.get("area") == "展厅"


def test_cover_family_untouched(fp):
    """窗帘/百叶窗不走窗型剥离（cover 域语义，误入即事故）。"""
    p = _run(fp, "拉上客厅窗帘")
    assert p is not None and p.intent == "TurnDeviceOff"
    assert "cover" in p.args["target"][0]["devices"][0]["domains"]
    assert _run(fp, "卧室百叶窗放下") is None
