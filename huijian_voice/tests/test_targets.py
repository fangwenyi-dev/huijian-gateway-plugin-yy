"""目标提取/清洗单元钉桩（收编修复五/六与拼音择优的行为锁定）。"""
from core.nlu import targets as T


def test_cn2num():
    assert T.cn2num("二十三") == "23"
    assert T.cn2num("十") == "10"
    assert T.cn2num("一百二十三") == "123"
    assert T.cn2num("七") == "7"


def test_normalize_name():
    assert T.normalize_name("一号测试窗") == "1号测试窗"


def test_parse_basic():
    area, name, score = T.parse_target("客厅的灯")
    assert (area, name) == ("客厅", "灯")
    area, name, _ = T.parse_target("书房筒灯")
    assert (area, name) == ("书房", "筒灯")


def test_parse_window_mid():
    # 中段词（原表缺陷①修正）
    area, name, _ = T.parse_target("窗户动作")
    assert name == "窗户"


def test_parse_ac_exact_beats_phonetic():
    # 拼音择优（原表缺陷②修正）：空调 不被 筒灯(dist=5) 截胡
    area, name, _ = T.parse_target("空调")
    assert name == "空调"


def test_parse_bare_light_not_bulb():
    # "灯打开" 不得被近音 "灯泡" 截胡（收编修复钉桩）
    area, name, _ = T.parse_target("灯打开")
    assert name == "灯"


def test_parse_modal_strip_and_ba():
    area, name, _ = T.parse_target("把灯打开")
    assert name == "灯"
    assert T.strip_modal("关闭吧") == "关闭"


def test_clean_name_verb_tail():
    assert T.clean_name("灯打开") == "灯"
    assert T.clean_name("的筒灯") == "筒灯"


def test_domain_hint():
    assert T.domain_hint("空调") == ["climate"]
    assert T.domain_hint("窗帘") == ["cover"]
    assert T.domain_hint("筒灯") == ["light"]


def test_homophone_correction_path():
    # pypinyin 实装时「ping推车」类噪音由纠错表兜住；这里验证词表含平推窗
    assert "平推窗" in T.KNOWN_DEVICES
