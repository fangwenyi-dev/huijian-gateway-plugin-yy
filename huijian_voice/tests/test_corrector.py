"""58 条 ASR 热词纠错表钉桩（口径裁定：061701 代码版=严格超集，条目数=58）。"""
from core.nlu import corrector


def test_table_has_58_entries():
    assert len(corrector.BASE_CORRECTIONS) == 58


def test_core_pairs():
    for wrong, right in [("内导", "内倒"), ("站厅", "展厅"), ("统灯", "筒灯"),
                         ("办公系统", "办公室"), ("办公事", "办公室"), ("平推车", "平推窗"),
                         ("放量大一点", "风量大一点"), ("汇江", "慧尖")]:
        assert corrector.BASE_CORRECTIONS[wrong] == right


def test_apply_longest_first():
    # 「办公系统」必须先于短键生效（原 dict 序隐患的钉桩）
    assert corrector.apply("打开办公系统的灯") == "打开办公室的灯"
    assert corrector.apply("把站厅的统灯打开") == "把展厅的筒灯打开"
    # 二次套用不回退
    assert corrector.apply(corrector.apply("内导展厅窗")) == "内倒展厅窗"


def test_apply_extra_merges_without_override():
    out = corrector.apply("把智能面板打开", extra={"智能面板": "场景面板"})
    assert "场景面板" in out
    # 用户表同名键不覆盖基础表（基础表经回归验证）
    out2 = corrector.apply("内导", extra={"内导": "歪斜"})
    assert out2 == "内倒"
