# -*- coding: utf-8 -*-
"""v1.1.35「点名设备查无」闸（2026-09-30 办公 .91 真机实锤，台账外新缺陷）。

现场（加载项 v1.1.34，注文本走 :8000 llm 通道，逐句前后取 /api/states 对拍）：
    → 「关掉会飞的灯」  回复「会飞的灯关了」
      日志 `[执行] HassTurnOff {'entity_id': 'light.ban_gong_shi_she_deng'} → 成功`
      ＝**屋里唯一那台灯被真关掉**，而"会飞的灯"这台设备根本不存在；
    → 「打开办公室射灯然后关掉会飞的灯」 回复「好的，都办妥了」
      日志 `第 1/2 步 HassTurnOn …射灯 → 成功` + `第 2/2 步 HassTurnOff …射灯 → 成功`
      ＝两腿绑**同一台**，先开后关净零变化，却报全办好了。
    同句在 source=klar（单发）与 chain（链）两路都复现 ⇒ 不是链特例。

机制：v1.0.92 的「控制步目标证据」闸判据是"原话里出现目标域设备词"，而
「会飞的灯」里确实有"灯"字 ⇒ 放行；KLAR（Rust 引擎）把修饰语丢掉顶了同类别唯一
那台。类别词只证明"这句在说这一类"，**不证明用户点的那台存在**。

修法（core/pipeline._unknown_spoken_device_name + _klar_named_absent_target）：
原话里"类别词前面的修饰段"构成一个具体设备名，而这个名字在**这台 HA 当前在装清单**
里查无（既不相等/不包含、也非同长度近音）⇒ ①裁决弃用该计划（主路与降级支同闸），
②两处裁决点如实回「没有找到对应的设备「X」…」，不播成「这句话我还不会」。
三条放行路径都是既有纪律（静态词表/在装清单/同长度近音 ≤1 音节——催拉窗→推拉窗、
社灯→射灯 这类 ASR 听岔必须照旧能执行）。清单**由调用方显式传入**，不读 targets
进程级全局词表（夹具会抑制 sync_vocab，读全局＝随收集顺序时开时关）。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.nlu.fast_path import Plan                                   # noqa: E402
from core.pipeline import (select_primary_plan, select_fallback_plan,  # noqa: E402
                           _klar_write_without_target_evidence)
from test_experience_batch import (Lane, RecExecutor, HA, _pipe)       # noqa: E401


def _arun(coro):
    return asyncio.run(coro)


# 办公室真实地形：两台**同名**「射灯」（一台离线孪生）+ 台灯 + 走廊感应灯
HOME = {
    "light.ban_gong_shi_she_deng": {"attributes": {"friendly_name": "射灯"}},
    "light.she_deng": {"attributes": {"friendly_name": "射灯"}},
    "light.ke_ting_tai_deng": {"attributes": {"friendly_name": "台灯"}},
    "light.zou_lang_gan_ying_deng": {"attributes": {"friendly_name": "走廊感应灯"}},
    "light.chuang_tou_deng": {"attributes": {"friendly_name": "床头灯"}},
}
EID = "light.ban_gong_shi_she_deng"


def _kl(utt, intent="HassTurnOff", eid=EID):
    return Plan(intent=intent, args={"entity_id": eid}, source="klar",
                utterance=utt)


# ── ① 现场事故原句：不执行 + 如实说没找到（含用户自己说的那截名字）────
def test_field_flight_lamp_refused_and_says_no_such_device():
    kl = Lane({"关掉会飞的灯": _kl("关掉会飞的灯")})
    ex = RecExecutor()
    r = _arun(_pipe(kl=kl, ex=ex, ha=HA(HOME)).handle("关掉会飞的灯", origin="o"))
    assert ex.plans == [], f"不存在的设备名被真执行了（同类别唯一那台被顶包）：{ex.plans}"
    assert "没有找到对应的设备" in r.text, r.text
    assert "会飞的灯" in r.text, f"回显要点名用户说的那台: {r.text}"
    assert r.source == "no_such_device" and r.ok is False


def test_field_chain_two_legs_no_longer_bind_same_lamp():
    """链式那条：旧形两腿绑同一台、净零变化却播「都办妥了」。"""
    utterances = ("打开办公室射灯然后关掉会飞的灯", "打开办公室射灯", "关掉会飞的灯")
    kl = Lane({u: _kl(u, intent="HassTurnOn" if u.endswith("射灯") else "HassTurnOff")
               for u in utterances})
    ex = RecExecutor()
    r = _arun(_pipe(kl=kl, ex=ex, ha=HA(HOME))
              .handle("打开办公室射灯然后关掉会飞的灯", origin="o"))
    assert ex.plans == [], f"链内查无腿被放行 ⇒ 只剩一腿静默执行: {ex.plans}"
    assert "没有找到对应的设备" in r.text and "办妥" not in r.text, r.text


# ── ② 双向不变量：家里**有**这名字时，同一说法必须照常执行 ────────────
def test_same_words_execute_when_the_name_does_exist():
    """防"把某个说法写成黑名单"：拦的判据是**查无此名**，不是这几个字。"""
    home = dict(HOME)
    home["light.hui_fei_de_deng"] = {"attributes": {"friendly_name": "会飞的灯"}}
    kl = Lane({"关掉会飞的灯": _kl("关掉会飞的灯", eid="light.hui_fei_de_deng")})
    ex = RecExecutor()
    r = _arun(_pipe(kl=kl, ex=ex, ha=HA(home)).handle("关掉会飞的灯", origin="o"))
    assert [p.utterance for p in ex.plans] == ["关掉会飞的灯"], \
        f"家里真有这台却被拒: {ex.plans} / {r.text}"


# ── ③ 反向不变量：合法说法一律不得被本闸拦掉 ────────────────────────
def test_legitimate_shapes_still_execute():
    ok_cases = [
        "打开射灯",                 # 整名命中
        "把客厅灯关了",             # 区域名+类别词（客厅是位置词，不是修饰语）
        "关掉台灯",                 # 静态词表里的类别词本体
        "把走廊灯打开",             # 半截名字落在「走廊感应灯」内
        "关掉感应灯",               # 反过来只说后半截
        "关灯",                     # 纯泛称：无可判修饰段
        "打开全部的灯",             # 泛称+量词
        "打开社灯",                 # ASR 近音（社灯↔射灯 同长度同音）必须照旧放行
        "关掉床投灯",               # 同音救援路径本体（见下一条，修饰段满 2 字才进判据）
        "办公室的射灯打开",
    ]
    for utt in ok_cases:
        kl = Lane({utt: _kl(utt)})
        ex = RecExecutor()
        _arun(_pipe(kl=kl, ex=ex, ha=HA(HOME)).handle(utt, origin="o"))
        assert ex.plans, f"合法句被本闸误拦: 「{utt}」"


def test_near_homophone_rescue_is_the_load_bearing_path():
    """「社灯」的修饰段只 1 字，压根进不了判据 ⇒ 近音这条路径此前**无人覆盖**。
    用两臂把它钉实：家里装了「床头灯」时「关掉床投灯」照旧执行（ASR 听岔同音同长度，
    与 targets._generic_rescue 的 ≤1 音节纪律同源）；把这台摘掉，同一句就必须被拦。
    只动清单不动句子 ⇒ 变异"撤掉近音放行"必定当场红（互含/静态表两臂都够不着它）。"""
    utt = "关掉床投灯"
    ex = RecExecutor()
    _arun(_pipe(kl=Lane({utt: _kl(utt)}), ex=ex, ha=HA(HOME)).handle(utt, origin="o"))
    assert ex.plans, "装了床头灯，同音的「床投灯」被误拦"

    without = {k: v for k, v in HOME.items() if k != "light.chuang_tou_deng"}
    ex2 = RecExecutor()
    r2 = _arun(_pipe(kl=Lane({utt: _kl(utt)}), ex=ex2,
                     ha=HA(without)).handle(utt, origin="o"))
    assert not ex2.plans, f"没装床头灯却照样执行（近音判据越权）: {ex2.plans}"
    assert "没有找到对应的设备" in r2.text, r2.text


# ── ④ 降级支同闸（v1.0.90 教训：只闸主裁决=半道闸）──────────────────
def test_fallback_lane_carries_the_same_gate():
    kl = _kl("关掉会飞的灯")
    fp = Plan(intent="TurnDeviceOff", args={}, source="t0", utterance="关掉会飞的灯")
    primary = Plan(intent="TurnDeviceOff", args={}, source="t0",
                   utterance="关掉会飞的灯")
    assert select_fallback_plan(primary, fp, kl, "关掉会飞的灯",
                                set(), tuple(v["attributes"]["friendly_name"]
                                             for v in HOME.values())) is None
    # 不传清单 ⇒ 子闸不参与（没清单就没裁决权，且不得读全局词表）
    assert select_fallback_plan(primary, fp, kl, "关掉会飞的灯", set()) is kl


def test_no_registry_means_no_verdict():
    """ha 拿不到在装清单时**绝不拦**——判据必须来自真清单，不来自全局静态印象。"""
    assert not _klar_write_without_target_evidence(_kl("关掉会飞的灯"), set(), ())
    assert _klar_write_without_target_evidence(
        _kl("关掉会飞的灯"), set(), tuple("射灯" for _ in (1,)))
