# -*- coding: utf-8 -*-
"""v1.1.2 查询族批次钉（2026-09-21 真机对账 + 查询覆盖面探针实锤）。

探针口径：49 句问句跑本地两棵引擎（命令档 FastPath + 查询档 QueryZone），
真机实体表只读拉取。结果 28 句未答，并揪出两类比"漏答"更坏的缺陷：

① **安全级·问一句动一次设备**（与「内倒→雷达」同 severity 同纪律）：
   「客厅射灯关了吗」→ ^关了 命中字面表 → TurnDeviceOff 真的把灯关掉；
   「射灯开了吗/射灯开着吗」→ TurnDeviceOn；「平开窗关了吗/窗户关了吗」→
   ControlWindow close（按窗钮）。根因两处叠加：_is_complex_query 的疑问判据
   只收 为什么|怎么|如何|是不是|有没有|能否|可以.*吗，不收**状态疑问尾**
   （V+吗/V+没有/是开着还是关着）；查询族的状态分支尾巴表只有 8 个固定写法。
   祈使侧反向钉死：「帮我把灯打开好吗」这类礼貌请求的 吗 已被
   normalize_polite/_ECHO_TONE 剥掉，必须照旧执行——新闸不许把请求判成问句。

② **答错比不答坏**：聚合计数忽略设备类别——「哪些窗开着」答"开着12个设备，
   比如HUIJIAN-BB28 麦克风开关…"（把 switch 全算进去），「还有几盏灯亮着」同
   样；「客厅有多少灯开着」答"客厅的个设备都关着呢"（量词丢失+类别丢失）。
   根因：_count_answer 的设备词取自一个"可选组 + {0,4} 任意字"的 search，
   最左匹配下组经常是空 → 退化成全域计数。

③ 漏答族：状态疑问尾巴不全、日期问句（星期几/几号/什么时候）、设备关着时问
   属性（"射灯亮度多少"→ 该答"射灯是关着的"而不是"这句话我还不会"）、
   人感传感器不存在时给通用兜底而不是如实说明。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v1112_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core.nlu.fast_path import FastPath                      # noqa: E402
from core.nlu.query import QueryZone                         # noqa: E402
from core.settings import Settings                           # noqa: E402


class FakeScenes:
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


class Ha:
    """固定实体表：一间办公室 + 一盏关着的射灯 + 两扇窗 + 一颗人感。"""

    _areas = {}
    _entity_area = {}

    ENTS = [
        {"entity_id": "light.she_deng", "state": "off",
         "attributes": {"friendly_name": "办公室射灯"}},
        {"entity_id": "cover.ping_kai_chuang", "state": "open",
         "attributes": {"friendly_name": "办公室平开窗 开窗器",
                        "current_position": 40}},
        {"entity_id": "cover.tui_lachuang", "state": "closed",
         "attributes": {"friendly_name": "书房推拉窗 开窗器", "current_position": 0}},
        {"entity_id": "switch.bb28_mic", "state": "on",
         "attributes": {"friendly_name": "HUIJIAN-BB28 麦克风开关"}},
        {"entity_id": "switch.bangongshi_chazuo", "state": "on",
         "attributes": {"friendly_name": "办公室插座"}},
        {"entity_id": "binary_sensor.office_pir", "state": "on",
         "attributes": {"friendly_name": "办公室人感", "device_class": "occupancy"}},
        {"entity_id": "sensor.t_ws", "state": "30.0",
         "attributes": {"friendly_name": "办公室温湿度传感器 温度",
                        "device_class": "temperature", "unit_of_measurement": "°C"}},
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


@pytest.fixture()
def fp():
    return FastPath(FakeScenes(), None, Settings(Path(os.environ["HUIJIAN_DATA"]) / "v112.json"))


@pytest.fixture()
def qz():
    return QueryZone(Ha(), Settings(Path(os.environ["HUIJIAN_DATA"]) / "v112q.json"))


def _m(fp, t):
    return asyncio.run(fp.match(t))


def _q(qz, t):
    return asyncio.run(qz.answer(t))


# ── ① 安全闸：状态疑问句绝不进命令档 ────────────────────────────
QUESTION_NOT_COMMAND = [
    "客厅射灯关了吗", "办公室射灯开了吗", "射灯开着吗", "办公室射灯现在开着吗",
    "射灯关了没有", "平开窗关了吗", "窗户关了吗", "灯还亮着吗",
    "办公室平开窗现在是开着的吗", "书房窗是不是开着", "办公室射灯是开着还是关着",
    "窗帘拉上了吗", "办公室射灯现在什么状态", "查询办公室平开窗状态",
    "空调是不是关着的",
]


@pytest.mark.parametrize("sentence", QUESTION_NOT_COMMAND)
def test_state_questions_never_become_commands(fp, sentence):
    """问一句关一次设备＝本批最高优先缺陷；命令档必须一律不接管。"""
    p = _m(fp, sentence)
    assert p is None, f"{sentence} 被命令档接成 {p.intent} {p.args}"


# 反向：祈使/请求句不得被新闸误杀（"吗"作为礼貌尾已被剥掉）
IMPERATIVE_STILL_COMMAND = [
    ("把办公室射灯关了", "TurnDeviceOff"),
    ("关闭办公室射灯", "TurnDeviceOff"),
    ("办公室射灯关掉", "TurnDeviceOff"),
    ("帮我把办公室射灯打开好吗", "TurnDeviceOn"),
    ("打开办公室平开窗", "ControlWindow"),
    ("关闭办公室平开窗", "ControlWindow"),
    ("所有灯都关啦", "TurnDeviceOff"),
    ("把窗帘拉上", None),
]


@pytest.mark.parametrize(("sentence", "intent"), IMPERATIVE_STILL_COMMAND)
def test_commands_still_execute_after_the_gate(fp, sentence, intent):
    p = _m(fp, sentence)
    assert p is not None, f"{sentence} 被疑问闸误杀（命令档不再接管）"
    if intent:
        assert p.intent == intent, (sentence, p.intent, p.args)


@pytest.mark.parametrize(("sentence", "want"), [
    ("办公室平开窗现在是开着的吗", "开着"),
    ("办公室射灯是开着还是关着", "关着"),
    ("办公室射灯现在什么状态", "关着"),
    ("查询办公室平开窗状态", "开着"),
    ("射灯开着吗", "关着"),
    ("平开窗关了吗", "开着"),
])
def test_state_questions_get_answers(qz, sentence, want):
    """拦下来只是第一步：这些句子必须真的被答出来。"""
    ans = _q(qz, sentence)
    assert ans and want in ans, (sentence, ans)


# ── ② 聚合计数：类别过滤必须生效（答错比不答坏）─────────────────
@pytest.mark.parametrize(("sentence", "must", "must_not"), [
    ("哪些窗开着", "扇", ["麦克风", "BB28", "插座"]),
    ("还有几盏灯亮着", "灯", ["麦克风", "BB28"]),
    ("办公室有多少灯开着", "盏灯", ["个设备都关着呢"]),
    ("家里有几个设备没关", "设备", []),
])
def test_count_answer_respects_device_class(qz, sentence, must, must_not):
    ans = _q(qz, sentence)
    assert ans, sentence
    assert must in ans, (sentence, ans)
    for bad in must_not:
        assert bad not in ans, (sentence, ans, bad)


def test_count_answer_grammar_has_no_dangling_measure_word(qz):
    """「客厅的个设备都关着呢」这类漏量词句子不能出现在带类别的问句里。"""
    for s in ("办公室有多少灯开着", "办公室几盏灯亮着"):
        ans = _q(qz, s)
        assert ans and "的个设备" not in ans and "个设备都关着" not in ans, (s, ans)


# ── ③ 日期时间问句 ─────────────────────────────────────────────
@pytest.mark.parametrize(("sentence", "must"), [
    ("今天星期几", "星期"),
    ("现在什么时候", "点"),
    ("今天几号", "号"),
    ("现在几点了", "点"),
])
def test_date_and_time_questions(qz, sentence, must):
    ans = _q(qz, sentence)
    assert ans and must in ans, (sentence, ans)


# ── ④ 设备关着时问属性：如实说"关着的"，不是"这句话我还不会"─────
@pytest.mark.parametrize("sentence", ["办公室射灯亮度多少", "射灯现在多亮", "办公室射灯色温多少"])
def test_attribute_query_on_off_device_states_it_is_off(qz, sentence):
    ans = _q(qz, sentence)
    assert ans and ("关着" in ans or "没开" in ans or "开着" in ans), (sentence, ans)


# ── ⑤ 人感传感器不存在时如实说明（存在时正常答）─────────────────
def test_presence_answer_works_with_sensor(qz):
    ans = _q(qz, "办公室有没有人")
    assert ans and "人" in ans, ans


def test_presence_without_hardware_says_so(qz):
    qz.ha.ENTS = [e for e in Ha.ENTS
                  if (e.get("attributes") or {}).get("device_class") != "occupancy"]
    try:
        ans = _q(qz, "卧室有没有人")
    finally:
        qz.ha.ENTS = Ha.ENTS
    assert ans and ("人感" in ans or "传感器" in ans or "没" in ans), ans


# ── 变异靶：新闸必须"能红"（关掉判据即本文件当场红）───────────────
def test_gate_is_anchorable(fp, monkeypatch):
    from core.nlu import fast_path as F
    assert getattr(F, "STATE_QUESTION_TAIL", None) is not None, "疑问尾表被删=闸失效"
    assert F.STATE_QUESTION_TAIL.search("射灯关了吗")
