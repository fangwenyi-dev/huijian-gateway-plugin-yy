# -*- coding: utf-8 -*-
"""v1.1.36 复核①＝P0：动作链"免确认解锁"闸被今天改盲。

现场（我自己跑的复现矩阵，`git show 58815ab` 与 HEAD 同料双跑）：

    actions=[{{intent:TurnDeviceOff, params:{{target:[{{devices:[{{name:"大门"}}]}}]}}}}]
      昨天判=拦   今天判=放行   ←← 免确认解锁后门重开

三段证据链都在盘上：
  1. `custom_llm_api.py:165` 今天把动作链判据从 `_args_targets_lock`（含中文锁词）
     换成 `_params_target_lock_domains`（**只看域**，:175-196 无任何名字判据）；
  2. 它依赖的 `_enrich_target_domains`（:553-556）**只读 `arguments["target"]` 顶层**，
     从不进 `actions[].params.target`——而本闸管的正是后者；
  3. 同文件 **:626 我自己写的注释**：「LLM **常不写 domains**、只给中文设备名」
     ⇒ "无 domains" 是常态不是边角；工具 schema 的 actions 是自由 dict
     （:497/:524 `vol.All(cv.ensure_list, [dict])`），连字段提示都没给模型。

后果：`intent_turn.py:326` `# off = unlock`，TurnDeviceOff×锁在场景/自动化回放
白名单内 ⇒ 一句「回家就关大门」建出来的东西，触发时**免确认解锁**。

今天的正面成果必须保住（不能退回昨天那把误杀）：「大门灯」是 light、「卷帘门」是
cover——它们该由**真实域**判定，而不是被中文子串一枪打死。
"""
import ast
import asyncio
from pathlib import Path

import pytest

SRC = (Path(__file__).resolve().parent.parent
       / "custom_components" / "huijian_ai" / "custom_llm_api.py").read_text(
    encoding="utf-8")

_WANT = {"scene_actions_hit_risk", "_args_targets_lock", "_risky_domain_closure",
         "_params_target_lock_domains", "_enrich_target_list",
         "_RISKY_LOCK_OFF_INTENTS", "_LOCK_NAME_WORDS", "_ALARM_NAME_WORDS",
         "_ALARM_DOMAINS", "_CHAIN_RISKY_INTENTS", "_RISKY_ACTION_CHAIN_INTENTS",
         "_RISKY_SCENE_INTENTS", "_enrich_one_target_list", "_enrich_deep",
         # 抽取器必须把它一起带上：漏了就 NameError 被 _risky_domain_closure 自家
         # except 吞掉 ⇒ door 不闭包到 lock ⇒ 钉假红（我第一版就栽在这，
         # 与 _goldtest 探针同一形态的坑）
         "_RISKY_DOMAIN_ALIASES"}


def _load():
    """按名字抽**自包含**函数/常量进空命名空间（本模块注释即为此设计：
    "闸须自包含，测试用空命名空间抽函数跑"）。HA 不在测试环境里，
    所以只能这样跑真函数——但断言全是真输入真输出，不是查字符串。"""
    ns = {"_LOGGER": type("L", (), {
        "info": staticmethod(lambda *a, **k: None),
        "warning": staticmethod(lambda *a, **k: None)})()}
    for node in ast.parse(SRC).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.Assign, ast.AnnAssign)):
            nm = (node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  else getattr(getattr(node, "target", None) or
                               node.targets[0], "id", ""))
            if nm in _WANT:
                code = ast.get_source_segment(SRC, node)
                exec(compile(code, "<ext>", "exec"), ns)
    return ns


NS = _load()


class _State:
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


def _acts(name, domains=None):
    dev = {"name": name}
    if domains is not None:
        dev["domains"] = domains
    return [{"intent": "TurnDeviceOff", "params": {"target": [{"devices": [dev]}]}}]


def _enrich(arguments):
    """真函数真跑：把 actions[].params.target 里的中文设备名按 HA 真状态回填域。"""
    hass = _Hass(_State("lock", "大门"), _State("light", "大门灯"),
                 _State("cover", "卷帘门"), _State("light", "筒灯"),
                 _State("alarm_control_panel", "安防主机"))
    return asyncio.run(NS["_enrich_target_list"](hass, arguments))


# ── 机制钉：域回填必须递归进 actions ────────────────────────────
def test_enrich_reaches_action_level_targets():
    """今天之前 actions 里的 name 永远拿不到域 ⇒ 只看域的判据必然失明。
    回填到位是"判据只看域"这个选择成立的前提。"""
    args = {"actions": _acts("大门")}
    out = _enrich(args)
    devs = out["actions"][0]["params"]["target"][0]["devices"]
    assert devs[0].get("domains") == ["lock"], f"actions 里的目标没被回填：{devs}"


def test_enrich_does_not_clobber_given_domains():
    """模型已写 domains 时不得覆盖（那是它明确表达的目标域）。"""
    args = {"actions": _acts("大门", domains=["cover"])}
    out = _enrich(args)
    assert out["actions"][0]["params"]["target"][0]["devices"][0]["domains"] == ["cover"]


# ── 行为钉：闸在真实输入形状下必须拦 ────────────────────────────
def test_name_only_lock_action_is_risky():
    """P0 回归主钉：只给中文名「大门」（:626 自称的常态）， enrich 后必须判险。"""
    args = {"actions": _acts("大门")}
    assert NS["scene_actions_hit_risk"](_enrich(args)["actions"]) is True, (
        "无域证据的锁名动作被放行＝免确认解锁后门")


def test_unresolvable_lock_name_fails_closed():
    """HA 里查不到那台（没域可回填）时**不得静默放行**：名字命中锁词就按险处理。
    fail-open 在这里的代价是解锁，不是多问一句。"""
    assert NS["scene_actions_hit_risk"](
        [{"intent": "TurnDeviceOff",
          "params": {"target": [{"devices": [{"name": "大门"}]}]}}]) is True


def test_alarm_disarm_action_is_risky():
    assert NS["scene_actions_hit_risk"](
        [{"intent": "TurnDeviceOff",
          "params": {"target": [{"devices": [{"name": "安防主机"}]}]}}]) is True


# ── 反向不变量：今天救掉的误杀不许回潮 ──────────────────────────
@pytest.mark.parametrize("name,dom", [("大门灯", "light"), ("卷帘门", "cover"),
                                      ("筒灯", "light")])
def test_non_lock_named_action_not_risky(name, dom):
    """真域证据说它不是锁 ⇒ 中文名再像也不拦（这是本批要保住的成果）。"""
    args = {"actions": _acts(name)}
    acts = _enrich(args)["actions"]
    got = acts[0]["params"]["target"][0]["devices"][0].get("domains")
    assert got == [dom], f"回填本身跑歪：{name} -> {got}"
    assert NS["scene_actions_hit_risk"](acts) is False, f"{name} 被误杀（回潮）"


def test_lock_domain_action_still_risky():
    """模型直接写 domains=["lock"] 的老形状不得因这次改动漏网。"""
    assert NS["scene_actions_hit_risk"](
        [{"intent": "TurnDeviceOff",
          "params": {"target": [{"devices": [{"name": "大门",
                                              "domains": ["lock"]}]}]}}]) is True


def test_door_alias_domain_is_risky():
    """door 经 _RISKY_DOMAIN_ALIASES 闭包扩进 lock（v1.0.64 H2/M6 定案）。"""
    assert NS["scene_actions_hit_risk"](
        [{"intent": "TurnDeviceOff",
          "params": {"target": [{"devices": [{"name": "大门",
                                              "domains": ["door"]}]}]}}]) is True
