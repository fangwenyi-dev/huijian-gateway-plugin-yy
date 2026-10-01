# -*- coding: utf-8 -*-
"""v1.1.36 复核批二：对抗复核代理抓出的**我自己这批新引入的误拦**（逐条自己复现过）。

代理原报告的判读我全部自己跑过一遍，以下四条复现成立（另两条见 §后）：

①证据闸补齐 6 族之后，锁/扫地机域的"类别词"本身就兼做动词，于是**日常动词短语**
  被当成"点名的设备名"：实测（清单含「大门」，真区域=办公室）
      「锁上门」    → 拦：没有找到对应的设备「锁上门」
      「给门上锁」  → 拦
      「关好门」    → 拦
      「反锁上门」  → 拦
      「暂停扫地机」→ 拦
  （「把门锁上/门锁上/让扫地机回桩/风扇设成睡眠」正常。）
②位置句的孤字下限停在 2 字之后，**数量词短语**也不再被剥空：
      「关掉一个灯」「关掉一盏灯」「关掉两个灯」「关掉一只灯」→ 全部变成"点名查无"
  旧形把这些剥成空串 ⇒ 无判据 ⇒ 放行。
③第 4 支（星期几）的排除集与豁免判据不对称：`_answer_inner` 的星期几分支**没有**
  `定时|预约` 排除，而我的豁免全局先排除 ⇒ 「预约的会议是星期几」这类时钟答案仍被贴
  "数字可能不是最新"。
④`_enrich_target_list` 只走 `actions` **一层**，而判据 `scene_actions_hit_risk` 是
  栈式任意嵌套 ⇒ 二层里的 `{name:"大门灯"}` 回填不到域 ⇒ 走保守判险 ⇒ 误杀；
  同一形状放一层则正常。

修向（都是"该放行回去"，不牺牲 v1.1.35 战果）：
  ①②给"修饰段"加两道**形状**否定：锁/扫地机域要求修饰段是**描述性**的（含「的」
    或以方位尾结束）；数量词整段（一/两/几 + 个只盏台部根）不作名字。
  ③豁免判据按分支各用自己的排除集，与 `_answer_inner` 逐支对应。
  ④回填改成真的递归，并补上代理抓到的一条**假钉**（M3：删掉"判前自回填"仍 118 全绿）。
"""
import asyncio
from pathlib import Path

import pytest
from core.nlu.fast_path import Plan
from core.pipeline import (_category_nouns, _unknown_spoken_device_name,
                           select_primary_plan)
from core.nlu import query as Q
from test_experience_batch import HA, Lane, RecExecutor, _pipe

OFFICE = ("办公室",)
LOCK_NAMES = ("大门", "书房门锁")
LIGHT_NAMES = ("办公室射灯", "台灯")


def _kl(intent, eid, utt):
    return Plan(intent=intent, args={"entity_id": eid}, source="klar", utterance=utt)


# ── ① 锁/扫地机域：动词短语不是"点名的设备" ─────────────────────
@pytest.mark.parametrize("utt", ["锁上门", "给门上锁", "关好门", "反锁上门",
                                 "把门上锁", "暂停扫地机"])
def test_lock_domain_verb_phrases_are_not_names(utt):
    """本批补齐 6 族时把日常锁句变成了"没有找到对应的设备「锁上门」"——必须回放行。"""
    dom = "vacuum" if "扫地" in utt else "lock"
    got = _unknown_spoken_device_name(
        utt, _category_nouns(dom), LOCK_NAMES,
        known_areas=OFFICE, real_areas=OFFICE)
    assert got == "", f"动词短语被当设备名拦下：{utt} -> {got!r}"


@pytest.mark.parametrize("utt", ["把会飞的门锁上", "启动会飞的扫地机"])
def test_lock_domain_real_named_absent_still_refused(utt):
    """**正向不变量**：描述性修饰段（带「的」）仍要拦——v1.1.35 那条战果不许被
    这次"放回去"顺带弄丢。
    """
    dom = "vacuum" if "扫地" in utt else "lock"
    got = _unknown_spoken_device_name(
        utt, _category_nouns(dom), LOCK_NAMES,
        known_areas=OFFICE, real_areas=OFFICE)
    assert got, f"点了家里没有的名字却放行：{utt}"


@pytest.mark.parametrize("utt", ["关掉一个灯", "关掉一盏灯", "关掉两个灯",
                                 "关掉一只灯", "关掉那盏灯", "把灯关了"])
def test_numeral_quantifier_phrases_not_names(utt):
    """数量词整段不作设备名（旧形剥成空串=放行，这次不许更严）。"""
    got = _unknown_spoken_device_name(utt, _category_nouns("light"),
                                       LIGHT_NAMES, known_areas=OFFICE,
                                       real_areas=OFFICE)
    assert got == "", f"数量词被当点名设备：{utt} -> {got!r}"


def test_light_domain_locative_and_flight_modifiers_still_refused():
    """反向：位置/描述性修饰段照旧拦（本批改的是动词与数量词，不是收回闸）。"""
    for utt in ("关掉阳台的灯", "关掉书桌的灯", "关掉会飞的灯"):
        got = _unknown_spoken_device_name(utt, _category_nouns("light"),
                                          LIGHT_NAMES, known_areas=OFFICE,
                                          real_areas=OFFICE)
        assert got, f"{utt} 本该点名查无却放行"


# ── ③ 星期几分支的排除集必须与豁免一致 ─────────────────────────
@pytest.mark.parametrize("utt", ["预约的会议是星期几", "定时任务周几跑",
                                 "明天预约看周几"])
def test_weekday_branch_has_no_timing_exclusion_asymmetry(utt):
    """`_answer_inner` 的星期几分支本来就没有 `定时|预约` 排除（它确实能答这类句），
    豁免判据却全局先排除 ⇒ 这些时钟答案仍被贴"数字可能不是最新"。
    两支必须一致：这条句**是**时钟支 ⇒ 免注。
    """
    assert Q._answer_from_local_clock(utt) is True, (
        f"{utt} 由星期几分支作答（无排除闸），却不被豁免 ⇒ 仍会被贴假注")


def test_date_branch_still_excludes_timing_words():
    """反向不变量：`几号` 支**有** `定时|预约` 排除，那两句就不算时钟支（否则我把
    排除关系抹平，快照类答案会被免注）。
    """
    for utt in ("预约几号提醒", "定时几号开始"):
        assert Q._answer_from_local_clock(utt) is False, f"{utt} 不该免注"


# ── ④ actions 递归：二层嵌套也得回填到域 ───────────────────────
def _stub_claw():
    import test_v1127_scene_flow as sc
    sc._install_stubs()
    return sc.claw


class _State:
    def __init__(self, domain, name):
        self.domain, self.name = domain, name


class _H:
    class states:
        @staticmethod
        def async_all():
            return [_State("light", "大门灯"), _State("lock", "大门"),
                    _State("cover", "卷帘门")]


def test_nested_actions_get_enriched():
    """判据是栈式任意嵌套，回填却只走一层 ⇒ 二层里的「大门灯」拿不到域 ⇒ 保守判险
    误杀普通自动化。两层必须一致对待。
    """
    claw = _stub_claw()
    leaf = {"intent": "TurnDeviceOff",
            "params": {"target": [{"devices": [{"name": "大门灯"}]}]}}
    nested = [{"intent": "HassBroadcast", "params": {}, "actions": [leaf]}]
    out = asyncio.run(claw._enrich_target_list(_H(), {"actions": nested}))
    dev = out["actions"][0]["actions"][0]["params"]["target"][0]["devices"][0]
    assert dev.get("domains") == ["light"], f"二层 actions 没被回填：{dev}"
    assert claw.scene_actions_hit_risk(out["actions"]) is False, \
        "二层嵌套的灯被误判成解锁风险（一层却正常）"


def test_top_level_and_one_level_unchanged():
    """**反向不变量**：递归不许把一层的既有形状改坏。"""
    claw = _stub_claw()
    acts = [{"intent": "TurnDeviceOff",
             "params": {"target": [{"devices": [{"name": "大门"}]}]}}]
    out = asyncio.run(claw._enrich_target_list(_H(), {"actions": acts}))
    dev = out["actions"][0]["params"]["target"][0]["devices"][0]
    assert dev.get("domains") == ["lock"], dev
    assert claw.scene_actions_hit_risk(out["actions"]) is True


# ── ⑤ 场景库存量动作"判前自回填"：对抗复核抓到这是一条**假钉** ────
def test_store_actions_enriched_before_judging():
    """库里存的动作是**建场景当时**的原始 args，当年回填只碰顶层 target ⇒ 存量永远
    没有域证据。代理把 `await _enriched_store_actions(...)` 整行删掉跑 6 个相关文件
    ⇒ 118 全绿（我自己复现同形）：那行改动没被任何钉守住。

    判别料刻意选**名字里不含锁词、但 HA 里是锁**的设备（「入户」）：
    不回填 ⇒ 无域证据也无线索 ⇒ 放行；回填 ⇒ lock ⇒ 拦。
    """
    claw = _stub_claw()

    class _H2:
        class states:
            @staticmethod
            def async_all():
                return [_State("lock", "入户"), _State("light", "大门灯")]

    stored = [{"intent": "TurnDeviceOff",
               "params": {"target": [{"devices": [{"name": "入户"}]}]}}]
    judged = asyncio.run(claw._enriched_store_actions(_H2(), stored))
    assert judged[0]["params"]["target"][0]["devices"][0].get("domains") == ["lock"]
    assert claw.scene_actions_hit_risk(judged) is True, \
        "存量动作没回填就判 ⇒ 免确认解锁照样能从触发链过去"
    assert "domains" not in stored[0]["params"]["target"][0]["devices"][0], \
        "回填就地改了库存对象（污染场景库）"


def test_scene_chain_gate_calls_the_enricher():
    """接线：`_scene_chain_hits_risk` 必须**真的**在判前调回填（只加工具函数不算修）。

    从源文件按 AST 取那个方法体（不用 inspect.getsource：桩加载的模块里取到的片段
    带装饰器缩进，parse 会炸），只认 `Call` 节点里的名字——注释里写到同一个词不算数
    （本仓记过的"描述性注释满足文本钉"同型坑）。
    """
    import ast

    path = (Path(__file__).resolve()).parent.parent / "custom_components" /         "huijian_ai" / "custom_llm_api.py"
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    m = [n for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef)
         for n in cls.body
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == "_scene_chain_hits_risk"]
    assert m, "没找到 _scene_chain_hits_risk（改名即断，别让它躲过本钉）"
    names = {n.func.id for n in ast.walk(m[0])
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_enriched_store_actions" in names, (
        "风险扫描没在判前回填域（M3 型假绿：删掉那行 118 个测试仍全绿），"
        f"实际调用={sorted(names)}")


# ── ⑥ 复核代理抓的注释失真：数字与"从没生效过"的半句 ─────────────
def test_gate_comments_match_reality():
    """`pipeline.py` 里那句"白名单有 12 个"与 `http.py` 那句"此前一直没生效过"
    都得与事实一致——宣称失真是本批反复出现的病，注释也算宣称。
    """
    import re

    from core.nlu.klar_client import KLAR_CONTROL_INTENTS
    src = (Path(__file__).resolve().parent.parent / "core" / "pipeline.py"
           ).read_text(encoding="utf-8")
    i = src.index("_KLAR_WRITE_INTENTS = frozenset")
    note = src[max(0, i - 1500):i]
    nums = set(re.findall(r"(\d+) 个", note))
    if nums:       # 允许不写数字，写了就必须对
        assert nums == {str(len(KLAR_CONTROL_INTENTS))}, (
            f"注释里的白名单数量 {nums} 与实际 {len(KLAR_CONTROL_INTENTS)} 不符")
    http = (Path(__file__).resolve().parent.parent / "custom_components"
            / "huijian_ai" / "huijian" / "http.py").read_text(encoding="utf-8")
    j = http.index("def _file_cache_disabled")
    doc = http[j:j + 1200]
    # 注释的准确性也算宣称。旧写法只对**带值**生效，裸 `?nocache` 从没禁掉过缓存；
    # 而原注释用的是全称断言"此前一直没生效过"（对 `?nocache=1` 那半是反的）。
    # 两条都钉：全称断言不许回来，两半真值表都要写清。
    assert "一直没生效过" not in doc, "注释又用全称断言（对 ?nocache=1 是假的）"
    assert "一直有效" in doc and "从来没禁掉过缓存" in doc, "注释没写全两半真值表"


# ── ⑦ 第二条 LLM 通道（core/agent.py 工具面）此前整条没进动作链闸 ────
class _SSettings:
    def __init__(self, d=None):
        self.d = {"dialog.confirm_risky": True, "llm.allow_scene_write": True,
                  "llm.allow_automation_write": True}
        self.d.update(d or {})

    def get(self, k, default=None):
        return self.d.get(k, default)


def _agent():
    from core.agent import Agent
    a = Agent.__new__(Agent)
    a.settings = _SSettings()
    a.ha = None
    return a


LOCK_ACTS = {"actions": [{"intent": "TurnDeviceOff",
                          "params": {"target": [{"devices": [{"name": "大门"}]}]}}]}
LIGHT_ACTS = {"actions": [{"intent": "TurnDeviceOn",
                           "params": {"target": [{"devices": [{"name": "台灯"}]}]}}]}


@pytest.mark.parametrize("tool", ["HassCreateVoiceScene", "HassCreateAutomation",
                                   "HassUpdateAutomation"])
def test_agent_channel_refuses_lock_action_chains(tool):
    """同一条料走 custom_llm_api 今天被拦，走本通道此前照样入库——
    触发时 `intent_turn.py:326 # off = unlock` 免确认解锁。"""
    ok, speech = asyncio.run(_agent()._tool(tool, LOCK_ACTS))
    assert ok is False, f"{tool}：带锁动作的场景/自动化被静默放行"
    assert "确认" in speech, speech


def test_agent_channel_allows_plain_light_chain():
    """**反向不变量**：普通灯动作不得被这道闸误拒（否则自动化又建不了了）。"""
    # 过了风险闸之后还要走区域预检/入库，那需要真 ha——本钉只关心"有没有被这道闸
    # 拦下"，下游异常按"已放行"处理（断言只看拒绝文案不许出现）。
    try:
        ok, speech = asyncio.run(_agent()._tool("HassCreateVoiceScene", LIGHT_ACTS))
    except Exception:
        speech = ""
    assert "跳过确认" not in speech, f"普通灯动作被风险闸误杀：{speech!r}"


def test_actions_target_lock_is_recursive():
    """嵌套两层的动作也要看到（与 `_enrich_target_list` 同一条教训）。"""
    from core.agent import _actions_target_lock
    nested = [{"intent": "HassBroadcast", "params": {}, "actions":
               [{"intent": "TurnDeviceOff",
                 "params": {"target": [{"devices": [{"name": "大门"}]}]}}]}]
    assert _actions_target_lock(nested) is True, "二层动作没看到（免确认解锁后门）"
    assert _actions_target_lock(LIGHT_ACTS["actions"]) is False
    assert _actions_target_lock(None) is False
    assert _actions_target_lock([{"intent": "x", "params": None}]) is False


def test_agent_tool_actually_calls_the_chain_gate():
    """接线：`_tool` 里必须真的对写入类工具调了链判据（只加工具函数不算修）。"""
    import ast
    import textwrap
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "core" / "agent.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    m = [n for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef)
         for n in cls.body
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == "_tool"]
    assert m, "没找到 Agent._tool（改名即断）"
    names = {n.func.id for n in ast.walk(m[0])
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_actions_target_lock" in names, f"_tool 没调链判据：{sorted(names)}"
    ids = {ast.unparse(n) for n in ast.walk(m[0]) if isinstance(n, ast.Name)}
    assert "_CHAIN_WRITE_TOOLS" in ids, "链闸没绑到写入类工具集上"
