# -*- coding: utf-8 -*-
"""修②：否定祈使必须**两档都不执行**（v1.1.27 的"全局"此前只覆盖字面表）。

现场（2026-10-01 本机干净 checkout 全链探针 `_pipe(kl=…).handle` 实得）：

    [探针I] 别开台灯 -> 计划数=1 回复='好的，办好了'

`fast_path.py` 的守卫（`is_negation_imperative` 前身）只做"字面表拒接管"，klar 是
**另一条通路**：引擎把「别开台灯」接地成 `HassTurnOn light.ke_ting_tai_deng`，
不经字面表 ⇒ 直接下发并播成功。这与 v1.0.44 那条"疑问句两档都不得执行"
（pipeline:619 注释记的线上实锤）是同一形状的事故，只是漏了否定族。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.nlu.fast_path import (Plan, is_negation_imperative)     # noqa: E402
from core.pipeline import (select_primary_plan,                    # noqa: E402
                           select_fallback_plan)
from test_experience_batch import (Lane, RecExecutor, HA, _pipe)   # noqa: E401

TAI = "light.ke_ting_tai_deng"
AREA = ("办公室",)
NAMES = ("台灯", "平开窗")


def _kl(utt, eid=TAI, intent="HassTurnOn"):
    return Plan(intent=intent, args={"entity_id": eid}, source="klar", utterance=utt)


def _decide(utt, eid=TAI, intent="HassTurnOn"):
    return select_primary_plan(None, _kl(utt, eid, intent), known_areas=AREA,
                               device_names=NAMES, real_areas=AREA)


# ── ① 承重：裁决面弃用否定计划（撤掉 pipeline 那两处同闸即红）───────
def test_negation_forms_all_refused_by_adjudication():
    for utt in ("别开台灯", "不要开台灯", "不开台灯", "别把台灯打开",
                "不要把窗关上", "别关窗"):
        assert _decide(utt, TAI if "台" in utt else "cover.a") is None, \
            f"{utt} 仍被下发（否定句真执行）"


def test_fallback_lane_same_gate():
    """主路失败后的降级支同闸——只闸主裁决=半道闸（v1.0.90 的同型事故）。"""
    primary = Plan(intent="TurnDeviceOn", args={"device": "台灯"}, source="t0",
                   utterance="别开台灯")
    assert select_fallback_plan(primary, None, _kl("别开台灯"), "好的",
                                known_areas=AREA, device_names=NAMES,
                                real_areas=AREA) is None


def test_full_chain_negation_moves_nothing_and_does_not_claim_success():
    utt = "别开台灯"
    kl = Lane({utt: _kl(utt)})
    ex = RecExecutor()
    ha = HA({TAI: {"entity_id": TAI, "state": "off",
                   "attributes": {"friendly_name": "台灯"}}})
    ha._areas = {"o": "办公室"}
    r = asyncio.run(_pipe(kl=kl, ex=ex, ha=ha).handle(utt, origin="o"))
    assert ex.plans == [], f"否定句经 klar 真执行了：{ex.plans}"
    assert "办好了" not in r.text and "都办妥" not in r.text, r.text


# ── ② 反向不变量：定语小句/否定词后置 不是否定祈使，绝不误杀 ─────────
def test_attributive_clauses_are_not_negation_imperatives():
    for utt in ("把没关紧的窗关上", "把没关的灯打开", "打开书房别倒窗",
                "把没拉严的窗帘拉上", "开台灯"):
        assert is_negation_imperative(utt) is False, f"{utt} 被判成否定祈使（误杀）"


def test_statement_of_state_is_not_executed():
    """「还没上锁」是陈述不是命令——判据把它归进否定族，本闸职责内**不执行**即正解。

    这条不是"误杀豁免"，是"陈述句不该下发"：与 is_query_like 对疑问句的口径同向。
    """
    assert is_negation_imperative("还没上锁") is True
    assert _decide("还没上锁", "lock.a", "HassLock") is None


def test_real_command_still_executes_through_chain():
    utt = "把没关紧的窗关上"
    kl = Lane({utt: _kl(utt, "cover.ban_gong_shi_ping_kai_chuang", "HassTurnOff")})
    ex = RecExecutor()
    ha = HA({"cover.ban_gong_shi_ping_kai_chuang": {
        "entity_id": "cover.ban_gong_shi_ping_kai_chuang", "state": "open",
        "attributes": {"friendly_name": "平开窗"}}})
    ha._areas = {"o": "办公室"}
    asyncio.run(_pipe(kl=kl, ex=ex, ha=ha).handle(utt, origin="o"))
    assert len(ex.plans) == 1, f"定语小句被当否定句打死：{ex.plans}"


# ── ③ 接线与单源：判据只有一处定义，两侧都消费它 ────────────────────
def test_single_source_predicate_wired_on_both_sides():
    """判据单源 + 两侧都消费，且裁决面用的是**整句裸否定**那条。"""
    import ast
    fp_src = (ROOT / "core/nlu/fast_path.py").read_text(encoding="utf-8")
    pl_src = (ROOT / "core/pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(fp_src)
    used_in_fast_path = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "is_negation_imperative" for n in ast.walk(tree))
    assert used_in_fast_path, "字面表侧改回裸正则=第二份判据（单源破口）"
    # 判据本体只能有一份：正则定义一次、动词表定义一次（调用次数不是判据——
    # `is_bare_negation_imperative` 也要用同一个已编译对象取 span）。
    assert fp_src.count("_NEGATION_CMD = re.compile") == 1, "否定正则被复制成多份"
    assert fp_src.count("_NEG_PREP_FLOW = re.compile") == 1, "同上（把字形）"
    assert fp_src.count("_NEG_VERB_ALT = ") == 1, "动作动词表被另抄一份"
    assert pl_src.count("is_bare_negation_imperative(kl.utterance") == 2, \
        "裁决面/降级支两处同闸少了一处（半道闸复活）"


def test_compound_and_attributive_sentences_still_execute():
    """反向不变量（独立复核 2026-10-01 实证过的四类误杀，一条都不许再犯）。

    整句判据会把"否定只管半句"的句子打死——那些句子另一条腿本来该落地：
        关灯，不要拉窗帘        → 关灯该执行
        不用开灯，把窗帘拉上就行 → 拉窗帘该执行
        把没关严窗户拉上        → "没关严"是状态定语，不是拒绝
    """
    cases = [("关灯，不要拉窗帘", "light.ban_gong_shi_shede", "HassTurnOff"),
             ("不用开灯，把窗帘拉上就行", "cover.ban_gong_shi_chuanglian",
              "HassTurnOn"),
             ("把没关严窗户拉上", "cover.ban_gong_shi_chuanglian", "HassTurnOn")]
    states = {eid: {"entity_id": eid, "state": "off",
                    "attributes": {"friendly_name": n}}
              for eid, n in (("light.ban_gong_shi_shede", "射灯"),
                             ("cover.ban_gong_shi_chuanglian", "窗帘"))}
    for utt, eid, intent in cases:
        kl = Lane({utt: Plan(intent=intent, args={"entity_id": eid},
                             source="klar", utterance=utt)})
        ex = RecExecutor()
        ha = HA(states)
        ha._areas = {"o": "办公室"}
        asyncio.run(_pipe(kl=kl, ex=ex, ha=ha).handle(utt, origin="o"))
        assert len(ex.plans) == 1, f"{utt} 被整句否决打死（连排/定语句误杀）"
