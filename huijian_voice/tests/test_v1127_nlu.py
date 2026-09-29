# -*- coding: utf-8 -*-
"""v1.1.27 NLU 面批次钉（审计存活批 10 项，全部 import 真实实现直调）。

钉面 = 输入原话 → 应有意图/参数（或"拒绝执行"），逐项对应：
  ① 否定句全局守卫（别开灯/不要关灯不得产执行类 plan，含反向）；
  ② @absolute 哨兵不得从属性词快捷支漏上 wire；
  ③ _DELTA_SCANNERS 缺「调亮到/调暗到」⇒ 绝对值被吃成 ±20 相对档；
  ④ 色温裸数值（不带 K）三表都不接 ⇒ MISS（亮度族却可裸数）；
  ⑤ 纠错表纯 str.replace 无词界 ⇒「宿舍灯」被切成「宿射灯」（动错设备）；
  ⑥ 场景先命中即返回 ⇒ 短触发词吞长触发词（等值闸落空、前缀兜底错场景）；
  ⑦ 「中午一点」置 12 而非 +12（应 13 点）；
  ⑧ 传感器取数越区旁路先到先得 + vacuum 状态念英文；
  ⑨ targets 死支（④ 前缀候选恒败 / fan 支被截 / sync_vocab 死参）；
  ⑩ klar min_confidence 非法值在 try 外抛 ⇒ 引擎永久停用且降级不可观测。
"""
import asyncio
import copy
import inspect
import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v1127_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core.nlu import corrector, creation                          # noqa: E402
from core.nlu import targets as T                                 # noqa: E402
from core.nlu.fast_path import FastPath                           # noqa: E402
from core.nlu.klar_client import KlarClient                       # noqa: E402
from core.nlu.query import QueryZone                              # noqa: E402
from core.nlu.scenes import SceneCache                            # noqa: E402
from core.nlu.textcnn import TextCNN                              # noqa: E402
from core.settings import DEFAULTS, Settings                      # noqa: E402


class FakeScenes:
    """无触发词替身（场景面由 ⑥ 单钉，不经此替身）。"""

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    def check(self, text):
        return None

    async def verify_or_refresh(self, phrase):
        return False


class _S:
    def get(self, dotted, default=None):
        cur = copy.deepcopy(DEFAULTS)
        for k in dotted.split("."):
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


@pytest.fixture(scope="module")
def fp():
    tc = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    tc._ensure()
    return FastPath(FakeScenes(), tc, _S())


def _m(fp, t):
    return asyncio.run(fp.match(t))


def _domains(plan):
    return {d for t in (plan.args.get("target") or [])
            for d in ((t.get("devices") or [{}])[0].get("domains") or [])}


# ── ① 否定句不得产执行类 plan ────────────────────────────────────
NEG_SENTENCES = [
    "别开灯", "不要关灯", "不要开灯", "别关灯", "把灯别打开",
]


@pytest.mark.parametrize("sentence", NEG_SENTENCES)
def test_negative_command_never_executes(fp, sentence):
    """「别开灯」旧行为=TurnDeviceOn(灯)（真机上就是**把灯开了**）；含反向形态。"""
    p = _m(fp, sentence)
    assert p is None, (sentence, p.intent, p.args)


def test_negation_guard_keeps_v_neg_v_imperatives(fp):
    """词界护栏：V没V 是中段修饰（"关没关紧的窗"），不是否定祈使，不得误杀。"""
    p = _m(fp, "把那个关没关紧的窗关上")
    assert p is not None and p.intent == "ControlWindow", (p and (p.intent, p.args))
    p = _m(fp, "开没开过的灯都打开")
    assert p is not None and p.intent == "TurnDeviceOn", (p and (p.intent, p.args))
    p = _m(fp, "把没关的灯打开")
    assert p is not None and p.intent == "TurnDeviceOn", (p and (p.intent, p.args))


# ── ② @absolute 哨兵不得出属性词快捷支 ───────────────────────────
def test_half_brightness_resolves_not_sentinel(fp):
    """「打开一半的亮度」旧行为 args={'attribute': '@absolute',...} ⇒ 集成 unsupported。"""
    p = _m(fp, "打开一半的亮度")
    assert p is not None, "整句被丢（值蒸发）"
    assert p.intent == "AdjustDeviceAttribute", (p.intent, p.args)
    assert p.args.get("attribute") == "brightness", p.args
    assert str(p.args.get("delta")) == "50", p.args
    assert _domains(p) == {"light"}, p.args


def test_sentinel_never_ships_on_the_new_lane(fp):
    """同 v1.1.1 哨兵总闸口径：任何出站 attribute 都不许以 @ 开头。"""
    for s in ("打开一半的亮度", "开一半的亮度", "打开一半的灯"):
        p = _m(fp, s)
        if p is not None:
            assert not str(p.args.get("attribute", "")).startswith("@"), (s, p.args)


# ── ③ 调亮到/调暗到 = 绝对值，不是相对档 ─────────────────────────
@pytest.mark.parametrize(("sentence", "delta"), [
    ("把客厅射灯亮度调亮到80%", "80"),
    ("把客厅射灯亮度调暗到30%", "30"),
    ("客厅射灯亮度调亮到80%", "80"),
])
def test_brighten_darken_to_is_absolute(fp, sentence, delta):
    """旧行为 delta='+20'/'-20'——用户要 80% 得到原值±20（绝对值静默丢）。"""
    p = _m(fp, sentence)
    assert p is not None, sentence
    assert p.intent == "AdjustDeviceAttribute", (sentence, p.intent)
    assert p.args.get("attribute") == "brightness", (sentence, p.args)
    assert str(p.args.get("delta")) == delta, (sentence, p.args)


def test_relative_brightness_words_still_relative(fp):
    """反向：不带数值的「调亮一点」仍是相对档（不许被绝对值档吞成 MISS）。"""
    p = _m(fp, "客厅射灯调亮一点")
    assert p is not None and str(p.args.get("delta")) == "+20", (p and p.args)


# ── ④ 色温裸数值（不带 K）── 仅在「色温」在场时收 ──────────────
@pytest.mark.parametrize("sentence", [
    "色温调到5000", "色温5000", "把客厅灯色温调到5000", "客厅灯色温调到5000",
])
def test_bare_color_temperature_number(fp, sentence):
    p = _m(fp, sentence)
    assert p is not None, f"{sentence} → MISS（亮度族可裸数，色温族却必须带 K）"
    assert p.args.get("attribute") == "color_temperature", (sentence, p.args)
    assert str(p.args.get("delta")) == "5000", (sentence, p.args)


def test_bare_number_not_widened_to_temperature(fp):
    """反向钉：不得为了收色温裸数把「温度」也放宽（会误吃空调设定值）。"""
    for s in ("温度调到5000", "把温度调到5000", "空调温度调到5000"):
        p = _m(fp, s)
        assert p is None or p.args.get("attribute") != "color_temperature", (s, p and p.args)


def test_kelvin_form_unchanged(fp):
    for s in ("色温调到4000k", "色温调到5000K"):
        p = _m(fp, s)
        assert p is not None and p.args.get("attribute") == "color_temperature", (s, p and p.args)
        assert str(p.args.get("delta")) in ("4000", "5000"), (s, p and p.args)


# ── ⑤ 纠错表必须按词界替换 ───────────────────────────────────────
def test_corrector_does_not_corrupt_dorm_light():
    """旧行为 apply("关掉宿舍灯")="关掉宿**射灯**"——纠错后去动另一台设备。"""
    assert corrector.apply("关掉宿舍灯") == "关掉宿舍灯"
    assert corrector.apply("打开宿舍的灯") == "打开宿舍的灯"


def test_corrector_word_boundary_keeps_real_fixes():
    """反向：词界替换不得把既有必纠形态（含 test_corrector 钉的）打回原形。"""
    assert corrector.apply("打开客厅的舍灯") == "打开客厅的射灯"
    assert corrector.apply("关闭办公室平台商打开办公室射灯") == "关闭办公室平开窗打开办公室射灯"
    assert corrector.apply("内倒一下平推车") == "内倒一下平推窗"
    assert corrector.apply("把站厅的统灯打开") == "把展厅的筒灯打开"
    assert corrector.apply("打开办公系统的灯") == "打开办公室的灯"


def test_dorm_light_never_turns_into_spotlight(fp):
    """端到端：真链路不得把「宿舍灯」写成「射灯」（动错设备=最危险形态）。"""
    p = _m(fp, "关掉宿舍灯")
    if p is not None:
        name = ((p.args.get("target") or [{}])[0].get("devices") or [{}])[0].get("name", "")
        assert name != "射灯", (p.intent, p.args)


# ── ⑥ 场景匹配：全等优先 → 最长优先 ─────────────────────────────
def test_scene_cache_prefers_exact_and_longest():
    sc = SceneCache(None)
    sc._triggers = ["关灯", "关灯睡觉"]          # 短词先建 ⇒ 旧实现先命中即返回
    assert sc.check("关灯睡觉") == "关灯睡觉", "长触发词被短触发词吞（走错场景）"
    assert sc.check("关灯") == "关灯"
    assert sc.check("关灯睡觉吧") == "关灯睡觉"


def test_scene_trigger_must_be_two_chars():
    """单字触发词是吞并源头（"灯"/"门"前缀命中一切）；创建侧直接拒建。"""
    p = creation.parse("我说灯就打开客厅灯")
    assert p is None or p.get("trigger_phrase") != "灯", p
    ok = creation.parse("我说关灯睡觉就打开书房灯")
    assert ok and ok.get("trigger_phrase") == "关灯睡觉", ok


# ── ⑦ 中午 N 点 = 13~15 点 ──────────────────────────────────────
@pytest.mark.parametrize(("sentence", "at"), [
    ("每天中午一点打开书房灯", "13:00"),
    ("每天中午两点打开书房灯", "14:00"),
    ("每天中午三点打开书房灯", "15:00"),
])
def test_noon_after_noon_hours_shift(sentence, at):
    p = creation.parse(sentence)
    assert p and p.get("trigger", {}).get("at") == at, (sentence, p)


@pytest.mark.parametrize(("sentence", "at"), [
    ("每天中午十二点打开书房灯", "12:00"),
    ("每天中午11点打开书房灯", "11:00"),
    ("每天下午一点打开书房灯", "13:00"),
])
def test_noon_other_hours_unchanged(sentence, at):
    p = creation.parse(sentence)
    assert p and p.get("trigger", {}).get("at") == at, (sentence, p)


# ── ⑧ 查询族：绑区优先 + vacuum 中文话术 ────────────────────────
class _QueryHa:
    """办公室绑区温感在**后**、客厅绑区的旁路温感（名字带"办公室"）在前。"""

    _areas = {"a1": "办公室", "a2": "客厅"}
    _entity_area = {"sensor.bypass": "客厅", "sensor.bound": "办公室"}

    ENTS = [
        {"entity_id": "sensor.bypass", "state": "30.0",
         "attributes": {"friendly_name": "办公室温度", "device_class": "temperature",
                        "unit_of_measurement": "°C"}},
        {"entity_id": "sensor.bound", "state": "26.0",
         "attributes": {"friendly_name": "温湿度传感器 温度", "device_class": "temperature",
                        "unit_of_measurement": "°C"}},
        {"entity_id": "vacuum.robot", "state": "cleaning",
         "attributes": {"friendly_name": "扫地机器人"}},
    ]

    async def states(self):
        return {e["entity_id"]: e for e in self.ENTS}

    async def find_entities(self, area="", domains=()):
        out = []
        for e in self.ENTS:
            dom = e["entity_id"].split(".")[0]
            if domains and dom not in domains:
                continue
            nm = (e.get("attributes") or {}).get("friendly_name") or ""
            if area and area not in nm:
                continue
            out.append(e)
        return out

    async def get_config(self):
        return {"time_zone": "Asia/Shanghai"}


class _VacHa(_QueryHa):
    ENTS = [
        {"entity_id": "vacuum.robot", "state": "cleaning",
         "attributes": {"friendly_name": "扫地机器人"}},
    ]


@pytest.fixture()
def qz():
    return QueryZone(_QueryHa(), Settings(Path(os.environ["HUIJIAN_DATA"]) / "v1127q.json"))


@pytest.fixture()
def qz_vac():
    return QueryZone(_VacHa(), Settings(Path(os.environ["HUIJIAN_DATA"]) / "v1127v.json"))


def _q(qz, t):
    return asyncio.run(qz.answer(t))


def test_sensor_answer_prefers_area_bound_entity(qz):
    """旧行为答 30 度（名字带"办公室"但绑在客厅的旁路传感器先到先得）。"""
    ans = _q(qz, "办公室温度多少")
    assert ans and "26" in ans and "30" not in ans, ans


@pytest.mark.parametrize(("state", "cn"), [
    ("cleaning", "清扫中"), ("returning", "回充中"), ("paused", "已暂停"),
])
def test_vacuum_state_speaks_chinese(state, cn):
    """旧行为把 raw state 念进播报（"扫地机器人处于 cleaning"）。"""
    ha = _VacHa()
    ha.ENTS = [{"entity_id": "vacuum.robot", "state": state,
                "attributes": {"friendly_name": "扫地机器人"}}]
    q = QueryZone(ha, Settings(Path(os.environ["HUIJIAN_DATA"]) / f"v1127v_{state}.json"))
    ans = _q(q, "扫地机器人现在什么状态")
    assert ans and cn in ans and state not in ans, (state, ans)


def test_count_answer_counts_cleaning_vacuum(qz_vac):
    """旧行为「1台扫地机器人都关着呢」（cleaning 不在 on_words 表）。"""
    ans = _q(qz_vac, "有几台扫地机器人开着")
    assert ans and "开着" in ans and "都关着" not in ans, ans


# ── ⑨ targets 死支/死参（不留装样子）────────────────────────────
def test_sync_vocab_has_no_dead_device_class_param():
    assert "device_class" not in inspect.signature(T.sync_vocab).parameters


def test_parse_target_has_no_dead_action_match_param():
    assert "action_match" not in inspect.signature(T.parse_target).parameters


def test_dead_branch_removal_keeps_targets_behavior():
    """删死支前后行为等值：设备前缀候选（旧 ④）与扇族域提示（旧 674-675）。"""
    assert T.parse_target("空调风量大一点")[:2] == (None, "空调")
    assert T.domain_hint("换气扇") == ["fan"]
    assert T.domain_hint("风扇") == ["fan"]


# ── ⑩ klar 配置非法：按缺省处理 + 留痕（不再静默永久停用）───────
class _KlarS:
    def __init__(self, **over):
        self.d = {"klar.enabled": True, "klar.url": "http://klar.test:10520",
                  "klar.language": "zh-CN", "klar.timeout_s": 2.0,
                  "klar.min_confidence": 0.80, "klar.token": ""}
        self.d.update(over)

    def get(self, key, default=None):
        return self.d.get(key, default)


def _klar_payload(conf=0.9):
    return {"decision": {"type": "execute"},
            "plan": {"steps": [{"intent": {"name": "HassTurnOn",
                                           "slots": [{"name": "domain", "value": "light"}]}}],
                     "confidence": conf},
            "speech": "好的"}


class _KlarResp:
    def __init__(self, status, payload):
        self.status, self._p = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._p


class _KlarSession:
    def __init__(self, status=200, payload=None):
        self.calls = []
        self.status, self.payload = status, payload

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(url)
        return _KlarResp(self.status, self.payload)

    async def close(self):
        pass


def test_klar_bad_min_confidence_keeps_engine_usable_and_visible():
    """旧行为：ValueError 在 try 外抛 → 每句折叠 None、fails=0、last_error=''。"""
    s = _KlarSession(payload=_klar_payload())
    c = KlarClient(_KlarS(**{"klar.min_confidence": "abc"}), session=s)
    p = asyncio.run(c.match("开灯"))
    assert p is not None and p.intent == "HassTurnOn", "合法响应(0.9)按缺省门就该接管"
    st = c.state()
    assert st["fails"] >= 1 or st.get("config_fails"), st
    assert st["last_error"], st
    assert "min_confidence" in st["last_error"], st


def test_klar_bad_min_confidence_bool_and_none_forms():
    """非法值一律**按缺省门**处理（引擎继续服务）；bool/容器同判，None=缺省不算错。"""
    for bad in (True, "", [1], {}, "abc"):
        s = _KlarSession(payload=_klar_payload())
        c = KlarClient(_KlarS(**{"klar.min_confidence": bad}), session=s)
        p = asyncio.run(c.match("开灯"))
        assert p is not None, (bad, "非法配置不该让引擎停摆")
        assert c.state().get("config_fails"), (bad, c.state())
    s = _KlarSession(payload=_klar_payload())
    c = KlarClient(_KlarS(**{"klar.min_confidence": None}), session=s)
    assert asyncio.run(c.match("开灯")) is not None
    assert not c.state().get("config_fails"), c.state()


def test_klar_good_min_confidence_untouched():
    s = _KlarSession(payload=_klar_payload(conf=0.55))
    c = KlarClient(_KlarS(), session=s)
    assert asyncio.run(c.match("开灯")) is None          # 0.55 < 0.80：原判据不变
    assert c.state()["last_error"] == ""
    assert not c.state().get("config_fails")
