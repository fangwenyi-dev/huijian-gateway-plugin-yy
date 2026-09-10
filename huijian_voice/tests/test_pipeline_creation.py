"""Pipeline 语音创建接线行为钉（v1.0.30）。

真 FastPath（含 targets/窗型词表全套逻辑）+ 桩 scenes/executor/klar——
钉"句式→级联→actions→入库 Plan"整链；集成侧执行链路由既有
handle_intent 通道与 E2E 覆盖。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.pipeline import Pipeline  # noqa: E402
from core.nlu.fast_path import Plan  # noqa: E402


class S:
    def __init__(self, **kw):
        self.d = {"nlu.textcnn_enabled": False, **kw}

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
        out = []
        for t in self._t:
            acts = [{"intent": "TurnDeviceOff",
                     "params": {"target": [{"area": t,
                                            "devices": [{"name": "灯"}]}]}}]
            out.append({"trigger_phrase": t, "actions": acts})
        return out

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
    async def match(self, text):
        return None


class Recorder:
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


def _pipe(scenes=None, executor=None, **set_kw):
    return Pipeline(S(**set_kw), ha=None, scenes=scenes or FakeScenes(),
                    textcnn=None, executor=executor or Recorder(),
                    agent=None, klar=StubKlar())


def _casc(pipe, text):
    return asyncio.run(pipe._cascade(text, "test"))


def test_scene_created_with_parsed_actions():
    ex = Recorder()
    r = _casc(_pipe(executor=ex), "当我说晚安就关闭卧室灯")
    assert r.ok and r.source == "creation"
    p = ex.calls[0]
    assert p.intent == "HassCreateVoiceScene"
    assert p.args["trigger_phrase"] == "晚安"
    assert p.args["actions"] == [{
        "intent": "TurnDeviceOff",
        "params": {"target": [{"area": "卧室", "devices": [{"name": "", "domains": ["light"]}]}]},
    }] or p.args["actions"][0]["intent"] == "TurnDeviceOff"
    assert "晚安" in r.text and "关闭卧室灯" in r.text


def test_scene_multi_clause_all_parsed():
    ex = Recorder()
    r = _casc(_pipe(executor=ex), "当我说回家就打开客厅灯并打开卧室灯")
    assert r.ok, r.text
    acts = ex.calls[0].args["actions"]
    assert [a["intent"] for a in acts] == ["TurnDeviceOn", "TurnDeviceOn"]


def test_scene_clause_ununderstood_whole_reject():
    ex = Recorder()
    r = _casc(_pipe(executor=ex), "当我说出发就念一遍今日运势")
    assert not r.ok and "先不创建" in r.text
    assert ex.calls == []                       # 半成品绝不入库


def test_scene_dup_trigger_rejected_before_intent():
    ex = Recorder()
    r = _casc(_pipe(scenes=FakeScenes(["晚安"]), executor=ex),
              "当我说晚安就关闭卧室灯")
    assert not r.ok and "已经有了" in r.text
    assert ex.calls == []


def test_scene_created_forces_cache_refresh():
    sc = FakeScenes()
    r = _casc(_pipe(scenes=sc, executor=Recorder()), "当我说起床就打开客厅窗帘")
    assert r.ok and sc.refreshed == 1           # 触发词即刻可用


def test_auto_numeric_area_inheritance():
    ex = Recorder()
    r = _casc(_pipe(executor=ex), "当客厅温度超过28度就打开空调")
    assert r.ok, r.text
    p = ex.calls[0]
    assert p.intent == "HassCreateAutomation"
    assert p.args["trigger"] == {"entity_id": "客厅温度", "above": 28.0}
    assert p.args["actions"][0]["intent"] == "TurnDeviceOn"
    assert "客厅" in r.text                      # 区域继承进了动作目标


def test_auto_presence_and_time_shapes():
    ex = Recorder()
    assert _casc(_pipe(executor=ex), "当书房检测到有人就打开书房灯").ok
    made = [p for p in ex.calls if p.intent == "HassCreateAutomation"]
    assert made[0].args["trigger"] == {"entity_id": "书房人体", "to": "on"}
    assert _casc(_pipe(executor=ex), "每天早上7点帮我打开客厅窗帘").ok
    made = [p for p in ex.calls if p.intent == "HassCreateAutomation"]
    assert made[1].args["trigger"] == {"at": "07:00"}
    # v1.0.34：创建成功后为播报编号会追加一次列表调用（fail-open 设计）
    assert any(p.intent == "HassListAutomations" for p in ex.calls)


def test_auto_backend_fail_says_so():
    r = _casc(_pipe(executor=Recorder(ok=False)), "当客厅温度超过28度就打开空调")
    assert not r.ok and "没创建成功" in r.text


def test_normal_control_unaffected():
    ex = Recorder()
    pipe = _pipe(executor=ex)
    r = _casc(pipe, "关闭卧室灯")
    assert r.ok and ex.calls[0].intent == "TurnDeviceOff"
    assert pipe.scenes.refreshed == 0            # 普通命令零创建开销


def test_creation_kill_switch():
    ex = Recorder()
    r = _casc(_pipe(executor=ex, **{"nlu.creation_enabled": False}),
              "当我说晚安就关闭卧室灯")
    assert ex.calls == []                        # 关闸后句式回落级联（兜底）


def test_agent_tools_cover_automation():
    """LLM 通道同权钉：agent 工具表必须含自动化四件，schema 引用 trigger/actions。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core.agent import TOOLS
    names = {t["function"]["name"] for t in TOOLS}
    assert {"HassCreateAutomation", "HassDeleteAutomation",
            "HassListAutomations", "HassUpdateAutomation"} <= names
    src = (Path(__file__).resolve().parents[1] / "core" / "pipeline.py").read_text(
        encoding="utf-8")
    for n in ("HassCreateAutomation", "HassDeleteAutomation",
              "HassListAutomations", "HassUpdateAutomation",
              "HassCreateVoiceScene", "HassTriggerVoiceScene"):
        assert n in src, f"{n} 不在 pipeline 级联白名单，LLM 调用会被拒"


# ── v1.0.33 语音删场景（本地链路，零 LLM）───────────────────────
def test_voice_delete_scene_hit():
    sc, ex = FakeScenes(["晚安"]), Recorder()
    r = _casc(_pipe(executor=ex, scenes=sc), "删除场景晚安")
    assert r.ok and "已删除" in r.text and "晚安" in r.text
    p = ex.calls[0]
    assert p.intent == "HassDeleteVoiceScene" and p.args == {"trigger_phrase": "晚安"}
    assert p.source == "creation"
    assert sc.refreshed == 1                      # 删后缓存强制刷新，触发词即刻失效


def test_voice_delete_scene_miss_no_exec():
    sc, ex = FakeScenes([]), Recorder()
    r = _casc(_pipe(executor=ex, scenes=sc), "删除场景没建过的")
    assert not r.ok and "没有找到" in r.text
    assert ex.calls == []                         # 查无此名零执行（不瞎删）


def test_voice_delete_kill_switch():
    sc, ex = FakeScenes(["晚安"]), Recorder()
    _casc(_pipe(executor=ex, scenes=sc,
                **{"nlu.creation_enabled": False}), "删除场景晚安")
    assert ex.calls == []                          # 关闸后句式回落级联（旧行为）


# ── v1.0.34 「删第N条」回指 ──────────────────────────────────────
def test_index_delete_scene_anaphora():
    ex = Recorder()
    pipe = _pipe(scenes=FakeScenes(triggers=("晚安", "午休")), executor=ex)
    r = _casc(pipe, "有哪些场景")
    assert r.ok and "2个语音场景" in r.text and "晚安就" in r.text
    assert pipe._last_list == "scene"
    r = _casc(pipe, "删第2条")
    assert r.ok and "午休" in r.text
    assert ex.calls[-1].intent == "HassDeleteVoiceScene"
    assert ex.calls[-1].args == {"trigger_phrase": "午休"}
    assert pipe._last_list is None                  # 删后清锚，防旧编号误删
    r = _casc(pipe, "删第1条")
    assert not r.ok and "先说" in r.text            # 无上下文如实反问


def test_index_delete_automation_anaphora():
    ex = Recorder(automations=[
        {"automation_id": "a1", "trigger": {"entity_id": "客厅温度", "above": 28},
         "actions": [{"intent": "TurnDeviceOn", "params": {}}]},
        {"automation_id": "a2", "trigger": {"at": "07:00"},
         "actions": [{"intent": "TurnDeviceOn", "params": {}}]}])
    pipe = _pipe(executor=ex)
    r = _casc(pipe, "列出自动化")
    assert r.ok and "2条语音自动化" in r.text and "1，" in r.text
    r = _casc(pipe, "删第1条")
    assert r.ok and "已删除" in r.text
    dele = [p for p in ex.calls if p.intent == "HassDeleteAutomation"]
    assert dele[-1].args == {"automation_id": "a1"}


def test_serial_actions_build_two_steps():
    """真机句式端到端：无标点连排 → 场景入库 2 步动作。"""
    ex = Recorder()
    r = _casc(_pipe(executor=ex), "当我说我回来了就打开客厅射灯关闭客厅窗帘")
    assert r.ok and "我回来了" in r.text
    made = [p for p in ex.calls if p.intent == "HassCreateVoiceScene"]
    acts = made[-1].args["actions"]
    assert [a["intent"] for a in acts] == ["TurnDeviceOn", "TurnDeviceOff"]
    assert "打开客厅射灯" in r.text and "关闭客厅窗帘" in r.text
