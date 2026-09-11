"""61 条 ASR 热词纠错表钉桩（口径裁定：061701 代码版=严格超集，58 条为移植基线；
2026-09-16 现场增补「平×窗」插音族 3 条，计数以本钉为准）。"""
from core.nlu import corrector


def test_table_has_61_entries():
    assert len(corrector.BASE_CORRECTIONS) == 61


def test_core_pairs():
    for wrong, right in [("内导", "内倒"), ("站厅", "展厅"), ("统灯", "筒灯"),
                         ("办公系统", "办公室"), ("办公事", "办公室"), ("平推车", "平推窗"),
                         ("放量大一点", "风量大一点"), ("汇江", "慧尖"),
                         ("平台窗", "平开窗"), ("平抬窗", "平开窗"), ("平胎窗", "平开窗")]:
        assert corrector.BASE_CORRECTIONS[wrong] == right


def test_pingchuang_insertion_realworld():
    """现场日志实锤句（2026-09-16）：复合句里的「平台窗」必须纠回「平开窗」。"""
    raw = "关闭办公室空调打开办公室平台窗"
    out = corrector.apply(raw)
    assert out == "关闭办公室空调打开办公室平开窗"
    # 不伤及邻词：真·平推窗句不被新键误触
    assert corrector.apply("把平推窗关上") == "把平推窗关上"
    assert corrector.apply("羊台的窗") == "阳台的窗"


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
