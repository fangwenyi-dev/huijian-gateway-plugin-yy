"""TextCNN 15 类钉桩：softmax 语义（收编修复三）、阈值、OOS 拒判。"""
import os

import pytest

from core.nlu.textcnn import TextCNN


@pytest.fixture(scope="module")
def tc():
    t = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    assert t._ensure(), "nlu_data 资产缺失（应随仓 vendored）"
    return t


def test_classes_are_15_with_oos(tc):
    assert len(tc.classes) == 15
    assert "OOS" in tc.classes


def test_threshold_table(tc):
    assert tc.thresholds.get("OOS") == 0.85
    assert tc.thresholds.get("SceneTrigger") == 0.5
    assert tc._threshold("UnknownLabel") == 0.7


def test_high_confidence_hit(tc):
    hit = tc.predict("打开灯")
    assert hit and hit[0] == "TurnDeviceOn"
    assert 0 < hit[1] <= 1.0          # softmax 概率域（raw logits 直比=恒放行的原缺陷钉桩）


def test_oos_rejected(tc):
    assert tc.predict("今天天气怎么样") is None


def test_small_talk_below_threshold_rejected(tc):
    # "你好啊" top1=OOS 或低置信——两路都必须拒判
    assert tc.predict("你好啊") is None


def test_truncation_max_len(tc):
    long_text = "打开灯" * 20
    r = tc.predict(long_text)          # 不崩即可（[:30] 截断）
    assert r is None or r[1] > 0
