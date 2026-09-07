"""目标提取/清洗单元钉桩（收编修复五/六与拼音择优的行为锁定）。"""
import pytest

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


# ── 修A（2026-09 用户实机轨迹："打开 办公室射灯"→target 退化 {name:灯} 且区域丢失）──
# 根因：射灯/灯带/吸顶灯/台灯/落地灯/床头灯/夜灯 原只在 KNOWN_DEVICES_PREFIX，
# 候选⑥子串扫描与加分只认 KNOWN_DEVICES → 单字"灯"(4+1=5) 压过区域候选。
# 以下钉桩锁定 7 个灯类词全表 + 属格剥离 + 未知复合词的区域二次回捞。

LAMP_FAMILY = [
    ("办公室射灯", ("办公室", "射灯")),
    ("办公室的射灯", ("办公室", "射灯")),      # 「的」属格中段也须保区域
    ("办公室 射灯", ("办公室", "射灯")),       # ASR 空格形态
    ("卧室吸顶灯", ("卧室", "吸顶灯")),
    ("客厅落地灯", ("客厅", "落地灯")),
    ("书房台灯", ("书房", "台灯")),
    ("儿童房夜灯", ("儿童房", "夜灯")),
    ("卧室床头灯", ("卧室", "床头灯")),
    ("办公室灯带", ("办公室", "灯带")),
    ("办公室吊灯", ("办公室", "灯")),          # 未知复合词：回捞区域、不臆造设备名
]


@pytest.mark.parametrize("phrase,expect", LAMP_FAMILY)
def test_lamp_family_keeps_area(phrase, expect):
    area, name, score = T.parse_target(phrase)
    assert (area, name) == expect, (phrase, area, name, score)


def test_lamp_words_precise_when_bare():
    # 裸说灯类词必须给精确名（HA 侧 name 是等值匹配，"灯" 匹配不上 "射灯"）
    for word in ("射灯", "灯带", "吸顶灯", "台灯", "落地灯", "床头灯", "夜灯"):
        assert T.parse_target(word)[1] == word, word


def test_bare_light_still_generic():
    # 泛称"开灯/把灯打开"行为锁定不变（不被灯类词表并集误伤）
    assert T.parse_target("灯")[1] == "灯"
    assert T.parse_target("灯打开")[1] == "灯"
    assert T.parse_target("把灯打开")[1] == "灯"
