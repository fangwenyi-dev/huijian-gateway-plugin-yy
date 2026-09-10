"""NLU × LLM 边界钉（用户令 2026-09-15：没有 LLM 时场景/自动化必须完美运行，
加了 LLM 只是多一层兜底——不能有冲突）。

三组契约：
  A. 无 LLM：场景/自动化全生命周期（建/列/删/删第N条/改场景/改自动化）本地闭环，
     全程零 LLM 调用；
  B. 有 LLM 不改路径：本地命中的句子 LLM 一次都不碰；本地执行失败只在
     "确定没生效" 时才复议（部分执行/结果不确定一律不复议，防重复执行）；
  C. 开关说到做到：nlu.enabled=false 本地全线让位；LLM 写自动化默认关。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.agent import Agent  # noqa: E402
from core.executor import Executor  # noqa: E402
from core.nlu.fast_path import Plan  # noqa: E402
from core.pipeline import Pipeline, _NLU_OFF_TEXT  # noqa: E402


# ── 桩件 ──────────────────────────────────────────────────────
class S:
    def __init__(self, **kw):
        self.d = {"nlu.textcnn_enabled": False, "llm.enabled": False, **kw}

    def get(self, k, default=None):
        return self.d.get(k, default)


class FakeScenes:
    def __init__(self, triggers=()):
        self._t = list(triggers)
        self.refreshed = 0

    @property
    def triggers(self):
        return self._t

    def all(self):
        return [{"trigger_phrase": t,
                 "actions": [{"intent": "TurnDeviceOff",
                              "params": {"target": [{"area": t,
                                                     "devices": [{"name": ""}]}]}}]}
                for t in self._t]

    def check(self, text):
        return text if text in self._t else None

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        self.refreshed += 1

    async def verify_or_refresh(self, phrase):
        return True


class StubKlar:
    def __init__(self, plan=None):
        self.plan = plan

    async def match(self, text):
        return self.plan


class Recorder:
    """执行桩：记录 Plan，恒定成功。"""

    def __init__(self, ok=True, automations=None):
        self.calls = []
        self.ok = ok
        self._autos = automations if automations is not None else []

    async def run(self, plan):
        self.calls.append(plan)
        return self.ok, "好"

    async def run_raw(self, plan):
        self.calls.append(plan)
        if plan.intent == "HassListAutomations":
            return True, {"success": True, "automations": list(self._autos)}
        return self.ok, {"success": self.ok}


class FakeAgent:
    """LLM 桩：任何 answer 调用都计数。"""

    def __init__(self, speech="这是大模型的回答"):
        self.enabled = True
        self.calls = 0
        self._speech = speech

    async def answer(self, text, history):
        self.calls += 1
        yield self._speech


class FakeHA:
    """真 Executor 用的 HA 桩：按序吐预设结果（验证 run 的执行状态回传）。"""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def handle_intent(self, name, args):
        self.calls.append((name, args))
        return self.outcomes.pop(0) if self.outcomes else {"success": True}

    async def call_service(self, domain, service, data):
        self.calls.append((f"{domain}.{service}", data))
        return {"success": True}


AUTOS = [
    {"automation_id": "a1", "trigger": {"entity_id": "客厅温度", "above": 28},
     "actions": [{"intent": "TurnDeviceOn", "params": {}}]},
    {"automation_id": "a2", "trigger": {"at": "07:00"},
     "actions": [{"intent": "TurnDeviceOn", "params": {}}]},
]


def _pipe(scenes=None, executor=None, agent=None, klar=None, ha=None, **set_kw):
    return Pipeline(S(**set_kw), ha=ha, scenes=scenes or FakeScenes(),
                    textcnn=None, executor=executor or Recorder(),
                    agent=agent, klar=klar or StubKlar())


def _casc(pipe, text):
    return asyncio.run(pipe._cascade(text, "test"))


# ── A. 无 LLM：场景/自动化全生命周期本地闭环 ────────────────────
def test_bare_scene_delete_lists_and_never_deletes():
    """「删除场景」不带名字：本地列清单+编号引导，零删除调用。"""
    sc, ex = FakeScenes(["晚安", "午休"]), Recorder()
    pipe = _pipe(scenes=sc, executor=ex)
    r = _casc(pipe, "删除场景")
    assert r.ok and "要删哪个场景" in r.text and "晚安" in r.text
    assert ex.calls == []                        # 引导 ≠ 执行（绝不瞎删）
    assert pipe._last_list == "scene"            # 编号锚点已立
    r2 = _casc(pipe, "删第1条")                   # 紧接着按编号删，本地可用
    assert r2.ok and "晚安" in r2.text
    assert ex.calls[-1].intent == "HassDeleteVoiceScene"
    assert ex.calls[-1].args == {"trigger_phrase": "晚安"}


def test_bare_automation_delete_sets_anchor():
    """裸删自动化的引导清单同时立编号锚点——否则「删第N条」够不着（本次修复）。"""
    ex = Recorder(automations=AUTOS)
    pipe = _pipe(executor=ex)
    r = _casc(pipe, "删除自动化")
    assert r.ok and "要删哪一条" in r.text
    assert pipe._last_list == "automation"
    r2 = _casc(pipe, "删第2条")
    assert r2.ok and "已删除" in r2.text
    dele = [p for p in ex.calls if p.intent == "HassDeleteAutomation"]
    assert dele[-1].args == {"automation_id": "a2"}


def test_modify_automation_full_sentence_replaces_trigger_and_actions():
    ex = Recorder(automations=AUTOS)
    r = _casc(_pipe(executor=ex), "把自动化1改成每天早上8点打开客厅窗帘")
    assert r.ok and "自动化1" in r.text
    p = [x for x in ex.calls if x.intent == "HassUpdateAutomation"][-1]
    assert p.args["automation_id"] == "a1"
    assert p.args["trigger"] == {"at": "08:00"}
    assert p.args["actions"][0]["intent"] == "TurnDeviceOn"


def test_modify_automation_actions_only_keeps_trigger():
    ex = Recorder(automations=AUTOS)
    r = _casc(_pipe(executor=ex), "把自动化1的动作改成打开客厅灯")
    assert r.ok and "动作已改成" in r.text
    p = [x for x in ex.calls if x.intent == "HassUpdateAutomation"][-1]
    assert "trigger" not in p.args               # 触发条件不动（集成侧部分更新）
    assert p.args["actions"][0]["intent"] == "TurnDeviceOn"


def test_modify_automation_trigger_only_keeps_actions():
    ex = Recorder(automations=AUTOS)
    r = _casc(_pipe(executor=ex), "把自动化1的触发条件改成每天晚上8点")
    assert r.ok and "触发条件已改成" in r.text
    p = [x for x in ex.calls if x.intent == "HassUpdateAutomation"][-1]
    assert p.args["trigger"] == {"at": "20:00"}
    assert "actions" not in p.args               # 动作沿用原样


def test_modify_automation_actions_only_inherits_area_from_trigger():
    """仅改动作且动作句没带区域 → 用旧触发条件里的区域名继承（"客厅温度"→客厅）。"""
    ex = Recorder(automations=AUTOS)
    r = _casc(_pipe(executor=ex), "把自动化1的动作改成打开空调")
    assert r.ok, r.text
    p = [x for x in ex.calls if x.intent == "HassUpdateAutomation"][-1]
    tgt = p.args["actions"][0]["params"]["target"][0]
    assert tgt["area"] == "客厅" and tgt["devices"][0]["domains"] == ["climate"]


def test_modify_automation_unparsable_action_rejects_whole():
    """新动作听不懂 → 整单拒绝，旧自动化零改动（同创建纪律）。"""
    ex = Recorder(automations=AUTOS)
    r = _casc(_pipe(executor=ex), "把自动化1的动作改成念一遍今日运势")
    assert not r.ok and "先不创建" in r.text
    assert [x for x in ex.calls if x.intent == "HassUpdateAutomation"] == []


def test_modify_automation_miss_no_exec():
    ex = Recorder(automations=AUTOS)
    r = _casc(_pipe(executor=ex), "把自动化9改成每天早上8点打开客厅灯")
    assert not r.ok and "没有找到" in r.text
    assert [x for x in ex.calls if x.intent == "HassUpdateAutomation"] == []


def test_local_lifecycle_needs_no_llm_at_all():
    """同一回合串起建→列→改→删：agent=None（无 LLM）全程可用。"""
    ex, sc = Recorder(automations=AUTOS), FakeScenes(["晚安"])
    pipe = _pipe(scenes=sc, executor=ex)
    assert _casc(pipe, "当我说回家就打开客厅灯").ok
    assert _casc(pipe, "有哪些自动化").ok
    assert _casc(pipe, "把自动化1改成每天早上8点打开客厅灯").ok
    assert _casc(pipe, "把场景晚安改成关闭客厅灯").ok
    assert _casc(pipe, "删除场景晚安").ok
    kinds = [p.intent for p in ex.calls]
    for want in ("HassCreateVoiceScene", "HassListAutomations",
                 "HassUpdateAutomation", "HassDeleteVoiceScene"):
        assert want in kinds, kinds


# ── B. 有 LLM：只做兜底，不改路径、不重复执行 ────────────────────
def test_local_hit_never_touches_llm():
    """本地命中的句子（含场景/自动化全能链路）LLM 一次都不碰。"""
    ag, ex = FakeAgent(), Recorder(automations=AUTOS)
    pipe = _pipe(executor=ex, agent=ag, **{"llm.enabled": True,
                                           "llm.base_url": "http://x/v1"})
    for text in ("当我说晚安就关闭卧室灯", "把自动化1改成每天早上8点打开客厅灯",
                 "删除场景晚安", "有哪些自动化"):
        assert _casc(pipe, text).source == "creation", text
    assert ag.calls == 0


def test_partial_execution_is_not_replayed_by_llm():
    """多步链第 2 步失败但第 1 步已落地 → 绝不复议（相对量动作会被叠加第二遍）。"""
    ag = FakeAgent()
    klar = StubKlar(Plan(intent="TurnDeviceOn", args={"target": []}, source="klar",
                         extra_steps=[{"name": "TurnDeviceOff", "args": {}}]))
    ha = FakeHA([{"success": True},
                 {"success": False, "error": "no match for entity"}])
    pipe = _pipe(executor=Executor(ha), agent=ag, klar=klar,
                 **{"llm.enabled": True, "llm.base_url": "http://x/v1"})
    r = _casc(pipe, "顺口溜一句")                 # fast_path 不中，klar 桩命中
    assert not r.ok and "前面 1 步已完成" in r.text
    assert ag.calls == 0                          # 复议被安全闸拦下
    assert any("复议跳过" in t for t in r.trace)


def test_indeterminate_failure_is_not_replayed_by_llm():
    """超时/连接类失败=结果不确定（HA 可能已执行）→ 不复议。"""
    ag = FakeAgent()
    ha = FakeHA([{"success": False, "error": "timeout"}])
    pipe = _pipe(executor=Executor(ha), agent=ag,
                 **{"llm.enabled": True, "llm.base_url": "http://x/v1"})
    r = _casc(pipe, "打开客厅灯")
    assert not r.ok and ag.calls == 0
    assert any("复议跳过" in t for t in r.trace)


def test_definite_failure_still_consults_llm():
    """确定没生效（未知意图）→ 仍然复议：LLM 只做兜底，这条通道保留。"""
    ag = FakeAgent()
    ha = FakeHA([{"success": False, "error": "unknown intent"}])
    pipe = _pipe(executor=Executor(ha), agent=ag,
                 **{"llm.enabled": True, "llm.base_url": "http://x/v1"})
    r = _casc(pipe, "打开客厅灯")
    assert r.source == "llm" and ag.calls == 1


# ── C. 开关说到做到 ───────────────────────────────────────────
def test_nlu_disabled_gives_way_to_llm():
    """nlu.enabled=false：本地承接/快速通道全线让位，句子交给 LLM。"""
    ag, ex = FakeAgent(), Recorder(automations=AUTOS)
    pipe = _pipe(executor=ex, agent=ag, **{"nlu.enabled": False,
                                           "llm.enabled": True,
                                           "llm.base_url": "http://x/v1"})
    r = _casc(pipe, "当我说晚安就关闭卧室灯")
    assert r.source == "llm" and ag.calls == 1
    assert ex.calls == []                        # 本地零执行（开关不是摆设）


def test_nlu_disabled_without_llm_says_why():
    ex = Recorder()
    pipe = _pipe(executor=ex, **{"nlu.enabled": False})
    r = _casc(pipe, "当我说晚安就关闭卧室灯")
    assert not r.ok and r.text == _NLU_OFF_TEXT
    assert ex.calls == []


def test_dry_run_reports_nlu_switch():
    pipe = _pipe(**{"nlu.enabled": False})
    out = asyncio.run(pipe.dry_run("当我说晚安就关闭卧室灯"))
    assert out["nlu_enabled"] is False and out["plan"] is None
    assert out["final"] == _NLU_OFF_TEXT
    on = asyncio.run(_pipe().dry_run("关闭卧室灯"))
    assert on["nlu_enabled"] is True and on["plan"] is not None


def test_llm_automation_write_defaults_off():
    """llm.allow_automation_write 默认 False：LLM 不得建/改/删自动化（本地句可建）。"""
    ex = Recorder()
    ag = Agent(S(**{"llm.enabled": True, "llm.base_url": "http://x/v1"}), None, ex)
    for name in ("HassCreateAutomation", "HassUpdateAutomation",
                 "HassDeleteAutomation"):
        ok, say = asyncio.run(ag._tool(name, {"automation_id": "a1"}))
        assert ok is False and "没开启" in say, name
    assert ex.calls == []


def test_llm_automation_write_when_enabled():
    ex = Recorder()
    ag = Agent(S(**{"llm.enabled": True, "llm.base_url": "http://x/v1",
                    "llm.allow_automation_write": True}), None, ex)
    ok, _ = asyncio.run(ag._tool("HassCreateAutomation",
                                 {"trigger": {"at": "07:00"}, "actions": []}))
    assert ok is True and ex.calls[-1].intent == "HassCreateAutomation"


# ── D. 本地理解的两处安全修复（2026-09-15 实证）──────────────────
class FakeHAWithAreas:
    """只有区域注册表的 HA 桩（过宽目标闸的判据来源）。"""
    _areas = {"a1": "客厅", "a2": "书房"}


def test_ac_sentence_with_area_prefixed_device_name_still_parses():
    """动态词表把区域吸进设备名（HA 实体常叫「客厅空调」）时，空调指令不能被
    "缺区域"守卫整类拒掉——否则无 LLM 场景下空调就是不可用设备。"""
    from core.nlu import targets as T
    from core.nlu.fast_path import FastPath
    try:
        T.sync_vocab({"climate.客厅空调": {"attributes": {"friendly_name": "客厅空调"}}})
        fp = FastPath(FakeScenes(), None, S())
        p = asyncio.run(fp.match("打开客厅空调"))
        assert p is not None and p.intent == "TurnDeviceOn"
        item = p.args["target"][0]["devices"][0]
        assert item["name"] == "客厅空调" and item["domains"] == ["climate"]  # 窄目标
        m = asyncio.run(fp.match("客厅空调设为睡眠模式"))
        assert m is not None and m.intent == "SetDeviceMode" and m.args["mode"] == "sleep"
        assert asyncio.run(fp.match("打开空调")) is None      # 裸"空调"仍要求区域
    finally:
        T.clear_vocab()


def test_overbroad_area_target_blocked_on_direct_command():
    """"客厅开灯"会被解析成 name=客厅/domains 空 → 执行命中客厅全部设备
    （含开关/门锁）。这类过宽目标必须拦下并引导，零执行。"""
    ex = Recorder()
    r = _casc(_pipe(executor=ex, ha=FakeHAWithAreas()), "客厅开灯")
    assert not r.ok and r.source == "clarify" and "不确定你要哪一台" in r.text
    assert ex.calls == []


def test_overbroad_area_target_blocked_in_creation():
    """同一形态的动作子句绝不许写进场景/自动化（否则日后整片区域一起动）。"""
    ex = Recorder()
    r = _casc(_pipe(executor=ex, ha=FakeHAWithAreas()), "当我说回家就客厅开灯")
    assert not r.ok and "不确定你要哪一台" in r.text
    assert ex.calls == []


def test_overbroad_gate_keeps_unambiguous_commands_working():
    """拦的是歧义不是功能：说清设备照常执行。"""
    ex = Recorder()
    r = _casc(_pipe(executor=ex, ha=FakeHAWithAreas()), "打开客厅灯")
    assert r.ok and ex.calls[-1].intent == "TurnDeviceOn"


# ── E. 开关护栏：状态可见 + 关掉要留痕 + 页面接线 ─────────────────
def test_status_snapshot_exposes_nlu_switch():
    """写盘的状态快照（首页数据源）必须带 nlu_enabled。"""
    src = (Path(__file__).resolve().parents[1] / "core" / "main.py").read_text(
        encoding="utf-8")
    assert '"nlu_enabled"' in src, "core/main.py 状态快照缺 nlu_enabled"


def test_turning_off_local_nlu_logs_warning(caplog):
    """关掉本地理解必须在日志留痕（现场支持第一眼能看到根因），恢复时也有记录。"""
    import logging
    from core import main as core_main

    class Dummy:
        pass

    d = Dummy()
    with caplog.at_level(logging.WARNING, logger="huijian.main"):
        core_main.Service._warn_local_nlu(d, {"nlu": {"enabled": False},
                                              "llm": {"enabled": True,
                                                      "base_url": "http://x/v1"}})
    assert any("本地理解已关闭" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="huijian.main"):
        # 两端都关：话术要点明"只会回固定兜底"
        core_main.Service._warn_local_nlu(Dummy(), {"nlu": {"enabled": False},
                                                    "llm": {"enabled": False}})
    assert any("固定兜底" in r.message for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="huijian.main"):
        d2 = Dummy()
        d2._nlu_warn_off = True
        core_main.Service._warn_local_nlu(d2, {"nlu": {"enabled": True}})
    assert any("已恢复" in r.message for r in caplog.records)


def test_settings_page_wires_the_three_switches():
    """页面必须真把三个开关接上（写进保存体），并带"手滑护栏"确认：
    历史上 nlu.enabled 就是"页面能勾、后端不读"的死开关。"""
    www = (Path(__file__).resolve().parents[1] / "www" / "index.html").read_text(
        encoding="utf-8")
    for el in ('id="nlu_creation"', 'id="llm_scene_write"', 'id="llm_auto_write"'):
        assert el in www, f"页面缺 {el}"
    for key in ("creation_enabled:", "allow_scene_write:", "allow_automation_write:"):
        assert key in www, f"保存体缺 {key}"
    assert "syncNluWarn" in www and "确定保存？" in www, "缺关掉本地理解的显式确认"
    assert "本地理解" in www and '"nlu_enabled"' in www, "首页状态位未接 nlu_enabled"


def test_overbroad_gate_catches_area_as_name_inside_multi_device_target():
    """klar 多目标里混进一个"区域当设备名"同样会把整片区域带开 → 逐台检查并拦下。"""
    klar = StubKlar(Plan(intent="HassTurnOn", args={"target": [
        {"area": "书房", "devices": [{"name": "灯", "domains": ["light"]}]},
        {"devices": [{"name": "客厅", "domains": []}]}]}, source="klar"))
    ex = Recorder()
    r = _casc(_pipe(executor=ex, klar=klar, ha=FakeHAWithAreas()), "玄关一句")
    assert not r.ok and r.source == "clarify" and ex.calls == []


def test_duplicate_automations_numbered_by_identity():
    """两条内容完全相同的自动化：删除/改写的播报编号必须按对象身份取
    （list.index 用 == 比较会指到先出现那条，话术就骗人了）。"""
    dup = [{"automation_id": "a1", "trigger": {"at": "07:00"},
            "actions": [{"intent": "TurnDeviceOn", "params": {}}]},
           {"automation_id": "a2", "trigger": {"at": "07:00"},
            "actions": [{"intent": "TurnDeviceOn", "params": {}}]}]
    ex = Recorder(automations=dup)
    r = _casc(_pipe(executor=ex), "删除自动化2")
    assert r.ok and "2，" in r.text, r.text
    dele = [p for p in ex.calls if p.intent == "HassDeleteAutomation"]
    assert dele[-1].args == {"automation_id": "a2"}


def test_llm_scene_write_gate_still_applies():
    ex = Recorder()
    ag_on = Agent(S(**{"llm.enabled": True, "llm.base_url": "http://x/v1"}), None, ex)
    ok, _ = asyncio.run(ag_on._tool("HassCreateVoiceScene",
                                    {"trigger_phrase": "晚安", "actions": []}))
    assert ok is True                            # 场景默认放行（本地句零 LLM 已覆盖）
    ag_off = Agent(S(**{"llm.enabled": True, "llm.base_url": "http://x/v1",
                        "llm.allow_scene_write": False}), None, ex)
    for name in ("HassCreateVoiceScene", "HassDeleteVoiceScene"):
        ok2, say2 = asyncio.run(ag_off._tool(name, {}))
        assert ok2 is False and "没开启" in say2, name
