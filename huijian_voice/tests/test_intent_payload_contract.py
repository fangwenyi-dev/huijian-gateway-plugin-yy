# -*- coding: utf-8 -*-
"""意图载荷契约钉（2026-09-12 三端深挖：加载项发射形态 × 集成端消费形态）。

真 HA 语义（core 2025.1.0 源码实查）：
- /api/intent/handle 把 data 每键包成 {"value": v}；
- 集成 intent_helper._match_with_constraints 遍历 target["devices"]
  → area-only 目标（无 devices 键）必 KeyError；
- HA async_match_targets 先 `states = hass.states.async_all(constraints.domains)`
  → **空列表返回空表** → 立刻 MatchFailedReason.DOMAIN
  （实测「通道/插座/开关/大门/音箱」等 14 个常见设备名 domain_hint 为空，
   真机表现为"没找到设备"）。

本文件用真 Pipeline 跑出实际 payload，再逐条对照上述语义断言。
"""
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", "/tmp/hv_payload_contract")
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

CC = HERE / "custom_components" / "huijian_ai"

STATES = {
    "lock.大门": {"entity_id": "lock.大门", "state": "locked",
                  "attributes": {"friendly_name": "大门"}},
    "light.客厅射灯": {"entity_id": "light.客厅射灯", "state": "off",
                       "attributes": {"friendly_name": "客厅射灯"}},
    "cover.客厅窗帘": {"entity_id": "cover.客厅窗帘", "state": "closed",
                       "attributes": {"friendly_name": "客厅窗帘"}},
    "switch.客厅通道": {"entity_id": "switch.客厅通道", "state": "off",
                        "attributes": {"friendly_name": "客厅通道"}},
}
ENTITY_AREA = {"light.客厅射灯": "客厅", "cover.客厅窗帘": "客厅",
               "switch.客厅通道": "客厅", "lock.大门": "客厅"}


class RecHA:
    def __init__(self):
        self.calls = []
        self.ok = True
        self.reachable = True

    async def handle_intent(self, name, data, timeout=10.0):
        self.calls.append((name, data))
        return {"success": True}

    async def call_service(self, domain, service, data=None):
        self.calls.append((f"service:{domain}.{service}", data))
        return {"success": True}

    async def states(self):
        return dict(STATES)

    async def area_names(self):
        return sorted(set(ENTITY_AREA.values()))

    async def get_config(self):
        return {"time_zone": "Asia/Shanghai"}

    async def find_entities(self, area="", domains=(), name_contains=""):
        out = []
        for eid, ent in STATES.items():
            if domains and eid.split(".", 1)[0] not in domains:
                continue
            if area and ENTITY_AREA.get(eid) != area:
                continue
            out.append(ent)
        return out

    async def rest_get(self, path, timeout=6.0):
        return None

    async def fire_event(self, t, d):
        pass


def _build():
    from core.settings import Settings
    from core.nlu.scenes import SceneCache
    from core.nlu.textcnn import TextCNN
    from core.executor import Executor
    from core.pipeline import Pipeline

    st = Settings(Path("/tmp/hv_payload_contract/settings.json"))
    st.update({"spatial": {"satellite_areas": {"10.0.0.9": "客厅"}},
               "llm": {"enabled": False}})
    ha = RecHA()
    pipe = Pipeline(st, ha, SceneCache(ha), TextCNN(HERE / "nlu_data"),
                    Executor(ha, st))
    return pipe, ha


def _turn(pipe, ha, text, origin="10.0.0.9"):
    ha.calls.clear()
    r = asyncio.run(pipe.handle(text, origin))
    intents = [(n, d) for n, d in ha.calls if not n.startswith("service:")]
    return r, intents


def _targets(intents):
    out = []
    for name, data in intents:
        for t in (data or {}).get("target") or []:
            out.append((name, t))
    return out


def test_no_area_only_target_ever_emitted():
    """任何 target 项都必须带 devices（否则集成端 KeyError）。"""
    pipe, ha = _build()
    for text in ("开灯", "关灯", "打开客厅的灯", "打开客厅通道", "把客厅窗帘关上",
                 "解锁大门"):
        _, intents = _turn(pipe, ha, text)
        if text == "解锁大门":
            _, intents = _turn(pipe, ha, "确认")
        for name, t in _targets(intents):
            assert t.get("devices"), f"{text!r} → {name} 发出 area-only 目标：{t}"


def test_lock_target_is_named_and_domain_narrowed_by_integration():
    """解锁令 payload：具名目标；域收窄由集成端强制（源码钉）。"""
    pipe, ha = _build()
    _, intents = _turn(pipe, ha, "解锁大门")
    _, intents = _turn(pipe, ha, "确认")
    names = [n for n, _ in intents]
    assert "HassUnlock" in names, names
    target = [t for n, t in _targets(intents) if n == "HassUnlock"][0]
    # 名字可能被 NLU 归一（"大门"→"门"），只要求具名且非空
    assert target["devices"][0].get("name"), target
    src = (CC / "intent_lock.py").read_text(encoding="utf-8")
    assert 'domains=[LOCK_DOMAIN]' in src, "集成端未把锁目标收窄到锁域"
    assert 'domains=expanded_domains or None' in (
        CC / "intent_helper.py").read_text(encoding="utf-8"), \
        "集成端未把空 domains 归一为 None（空列表在真机恒 DOMAIN 失败）"


def test_generic_word_target_scoped_to_satellite_area():
    """泛类词（灯/射灯）落本卫星区域；指名道姓设备句零影响。"""
    pipe, ha = _build()
    _, intents = _turn(pipe, ha, "开灯")
    t = _targets(intents)[0][1]
    assert t.get("area") == "客厅", t

    _, intents = _turn(pipe, ha, "打开投影")     # 具体设备名、句内无区域词
    t = _targets(intents)[0][1]
    assert not t.get("area"), t   # 具体设备名不缩区域


def test_empty_domains_allowed_but_only_for_unknown_words():
    """domains=[] 只应出现在 domain_hint 未知的词上，且集成端必须兜住。"""
    pipe, ha = _build()
    _, intents = _turn(pipe, ha, "打开客厅通道")
    t = _targets(intents)[0][1]
    assert t["devices"][0]["domains"] == [], t
    # 已知词仍带域（避免跨域误匹配）
    _, intents = _turn(pipe, ha, "开灯")
    assert _targets(intents)[0][1]["devices"][0]["domains"] == ["light"]
