# -*- coding: utf-8 -*-
"""v1.1.36 ①＝P0：动作链"免确认解锁"闸在**常态输入**下不能瞎。

复现矩阵（同一份料，新旧判据各跑一遍；旧函数取自 `git show 58815ab`）：

    actions=[{intent:TurnDeviceOff, params:{target:[{devices:[{name:"大门"}]}]}}]
      昨天判=拦   今天判=放行        ←← 免确认解锁后门重开

三段证据链都在盘上：
  · `custom_llm_api.py:165` 今天把动作链判据换成"只看域"（`_params_target_lock_domains`）；
  · 配套的 `_enrich_target_domains`(:553-556) **只读顶层 `arguments["target"]`**，
    从不进 `actions[].params.target`——而本闸管的正是后者；
  · 同文件 **:626 我自己写的注释**：「LLM **常不写 domains**、只给中文设备名」
    ⇒ "无 domains" 是常态而非边角；工具 schema 的 actions 是自由 dict（:497/:524）。
后果：`intent_turn.py:326` `# off = unlock`，TurnDeviceOff×锁 在场景/自动化回放
白名单内 ⇒ 一句「回家就关大门」建出来的自动化，触发时免确认解锁
（v1.1.27 批 7 P0-2 当初立闸要堵的就是这条）。

收口=两件事一起做（只做其一都不完备）：
  A. enrich **递归进 actions**：拿真实域判（真锁必带 lock/door/alarm 域）；
  B. 名字**在 HA 里查无此设备**（拿不到任何域证据）时保守判险——放行的代价是
     门开了，误拦的代价只是多问一句。
反面对手是 `test_p1_llm_risk_gate_no_chinese_name_false_positive`（域非锁不得误拒），
所以 B 只在"无域证据"时才允许动中文名。
"""
import asyncio

import pytest

import test_v1127_scene_flow as sc


@pytest.fixture()
def claw():
    sc._install_stubs()
    return sc.claw


class _State:
    """HA 状态替身：判据只读 .name 与 .domain。"""
    def __init__(self, domain, name):
        self.domain = domain
        self.name = name


class _States:
    def __init__(self, rows):
        self._rows = rows

    def async_all(self):
        return list(self._rows)


class _Hass:
    def __init__(self, *rows):
        self.states = _States(rows)


OFFICE = _Hass(_State("lock", "大门"), _State("light", "大门灯"),
               _State("cover", "卷帘门"), _State("light", "办公室射灯"))


def _api(claw):
    return claw.HuijianControlAPI.__new__(claw.HuijianControlAPI)


def _acts(name, domains=None):
    dev = {"name": name}
    if domains is not None:
        dev["domains"] = domains
    return [{"intent": "TurnDeviceOff", "params": {"target": [{"devices": [dev]}]}}]


# ── A：enrich 必须递归进 actions ───────────────────────────────
def test_enrich_fills_domains_inside_actions(claw):
    """LLM 只给中文名的常态料，回填后动作里必须带真实域。"""
    api = _api(claw)
    args = asyncio.run(api._enrich_target_domains(OFFICE, {"actions": _acts("大门")}))
    dev = args["actions"][0]["params"]["target"][0]["devices"][0]
    assert dev.get("domains") == ["lock"], f"actions 里的目标没被回填：{dev}"


def test_enrich_keeps_top_level_behaviour(claw):
    """反向：顶层 target 的既有回填语义不得因递归而退化。"""
    api = _api(claw)
    args = asyncio.run(api._enrich_target_domains(OFFICE, {"target": [
        {"devices": [{"name": "大门"}]}]}))
    assert args["target"][0]["devices"][0].get("domains") == ["lock"]


def test_enrich_does_not_overwrite_explicit_domains(claw):
    api = _api(claw)
    args = asyncio.run(api._enrich_target_domains(
        OFFICE, {"actions": _acts("大门", domains=["light"])}))
    dev = args["actions"][0]["params"]["target"][0]["devices"][0]
    assert dev["domains"] == ["light"], "已声明的域被回填覆盖（会掩盖真实意图）"


# ── 事故料必须被拦（走真入口 _scene_chain_hits_risk）────────────
@pytest.mark.parametrize("intent_type", ["HassCreateAutomation", "HassUpdateAutomation",
                                         "HassCreateVoiceScene"])
def test_name_only_lock_action_is_refused(claw, intent_type):
    """回归主钉：常态料（无 domains）+ HA 里那台是**真锁** ⇒ 必须判险。"""
    api = _api(claw)
    hit = asyncio.run(api._scene_chain_hits_risk(
        OFFICE, intent_type, {"actions": _acts("大门")}))
    assert hit is True, f"{intent_type}：无域证据的锁动作被放行（免确认解锁后门）"


def test_name_only_lock_refused_even_without_hass(claw):
    """B：拿不到任何域证据（HA 不可用/库里存量动作）时**保守判险**。

    旧形在这里静默放行；误拦的代价是多问一句，漏拦的代价是门开了。
    hass=None 是既有钉的真实形态（test_audit4_fixes 就这么调）。
    """
    api = _api(claw)
    hit = asyncio.run(api._scene_chain_hits_risk(
        None, "HassCreateAutomation", {"actions": _acts("大门锁")}))
    assert hit is True


def test_unknown_name_not_in_ha_is_refused(claw):
    """HA 里查无此名 ⇒ enrich 补不出域 ⇒ 只能按名字保守：不得静默放行。"""
    api = _api(claw)
    hit = asyncio.run(api._scene_chain_hits_risk(
        OFFICE, "HassCreateVoiceScene", {"actions": _acts("大门保险")}))
    assert hit is True, "查无此设备且名字含锁词被放行"


# ── 反面对手：不得把中文名启发重新变成误杀 ──────────────────────
@pytest.mark.parametrize("name,dom", [("大门灯", "light"), ("卷帘门", "cover"),
                                      ("办公室射灯", "light")])
def test_real_non_lock_names_still_allowed(claw, name, dom):
    """有**真域证据**且非锁 ⇒ 判据必须只看域（A2 误拒成果不许回潮）。"""
    api = _api(claw)
    hit = asyncio.run(api._scene_chain_hits_risk(
        OFFICE, "HassCreateAutomation", {"actions": _acts(name)}))
    assert hit is False, f"{name}（HA 真域={dom}）被中文名误杀，自动化建不了"


def test_explicit_domains_unchanged(claw):
    """既有钉的两条形形态不得漂移（域闭包/别名/entity_id）。"""
    claw.scene_actions_hit_risk(_acts("大门锁", domains=["lock"])) is True
    assert claw.scene_actions_hit_risk(_acts("大门", domains=["door"])) is True
    assert claw.scene_actions_hit_risk(
        [{"intent": "HassUnlock", "params": {"entity_id": "lock.da_men"}}]) is True
    assert claw.scene_actions_hit_risk(_acts("台灯", domains=["light"])) is False
