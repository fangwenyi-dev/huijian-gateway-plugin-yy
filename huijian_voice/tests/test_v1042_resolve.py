"""v1.0.42 语音自动化传感器解析修复钉（生产缺陷：「当办公室的温度大于三十度
就打开办公室的空调」永远创建失败 + 跨区错绑）。

根因三缺陷（全部实证复现，见 ~/rev_auto/probe.py 底稿）：
  R1 中文描述不切分：单 token 整串子串匹配，「办公室的温度」带「的」必失；
  R2 从不读区域注册表：现代 HA 传感器名就叫「温度」、房间靠 area 绑定；
  R3 无区域感知的 class-unique 兜底：说办公室、全屋唯一温度在卧室→静默绑卧室。

修复：custom_components/huijian_ai/entity_resolve_cn.py 纯函数收束（零 HA 依赖，
本套直接 load 文件钉），集成侧 _resolve_entity_id 只做注册表取数 + 话术。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_P = _ROOT / "custom_components" / "huijian_ai" / "entity_resolve_cn.py"

_spec = importlib.util.spec_from_file_location("entity_resolve_cn", _P)
erc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(erc)


def _c(eid, name, dc="", area=""):
    return {"entity_id": eid, "name": name, "dc": dc, "area": area}


OFFICE = {"办公室": "办公室", "办公": "办公室", "卧室": "卧室", "书房": "卧室"}
# 用户现场：办公室传感器名就叫「温度」（挂在办公室区域），卧室另有温度传感器
SCENE_A = [
    _c("sensor.office_temp", "温度", "temperature", "办公室"),
    _c("sensor.bed_temp", "卧室温度", "temperature", "卧室"),
    _c("sensor.office_hum", "湿度", "humidity", "办公室"),
]


# ---------------------------------------------------------------- R1/R2：切分 + 区域
def test_r1_de_word_not_poison_match():
    """「办公室的温度」带「的」，修复前整串子串必失配。"""
    r = erc.pick("办公室的温度", SCENE_A, OFFICE)
    assert r["best"] == "sensor.office_temp", r


def test_r1_no_de_form_also_binds():
    r = erc.pick("办公室温度", SCENE_A, OFFICE)
    assert r["best"] == "sensor.office_temp", r


def test_r2_area_registry_drives_binding():
    """传感器名不含区域词、仅靠区域注册表也能绑（旧解析的结构性盲区）。"""
    cands = [_c("sensor.aq1", "温度", "temperature", "书房")]
    r = erc.pick("书房的温度", cands, {"书房": "书房"})
    assert r["best"] == "sensor.aq1", r


def test_r2_class_and_area_humidity():
    r = erc.pick("办公室的湿度", SCENE_A, OFFICE)
    assert r["best"] == "sensor.office_hum", r


def test_r2_area_alias_resolves_to_canon():
    """别名「办公」命中后必须按规范名比对候选区域，不能拿别名当名字比。"""
    r = erc.pick("办公温度", SCENE_A, OFFICE)
    assert r["best"] == "sensor.office_temp", r


# ---------------------------------------------------------------- R3：跨区错绑守卫
def test_r3_no_cross_area_silent_bind():
    """办公室无温度传感器、卧室有：修复前 matched_by_class==1 直接绑卧室
    （错绑比失败危险）；现在必须诚实失败且点名区域。"""
    cands = [
        _c("sensor.bed_temp", "卧室温度", "temperature", "卧室"),
        _c("sensor.office_hum", "办公室湿度", "humidity", "办公室"),
    ]
    r = erc.pick("办公室的温度", cands, OFFICE)
    assert r["best"] is None
    assert r["no_in_area"] is True
    assert r["area_hit"] == "办公室"
    assert r["others"] == ["sensor.bed_temp"]


def test_r3_single_house_sensor_without_area_hint_still_binds():
    """描述没提区域、全屋唯一同类 → 保留历史「自动修正」语义不倒退。"""
    cands = [_c("sensor.living_temp", "客厅温度", "temperature", "客厅")]
    r = erc.pick("温度", cands, {})
    assert r["best"] == "sensor.living_temp", r


# ---------------------------------------------------------------- 兼容与消歧
def test_legacy_name_embedded_area_untouched():
    """老数据把区域写进 friendly_name（无区域注册表信息）：整串名字命中。"""
    cands = [_c("sensor.z1", "办公室温度", "temperature", "")]
    r = erc.pick("办公室温度", cands, OFFICE)  # 候选无区域→落入整串名字回退
    assert r["best"] == "sensor.z1", r


def test_area_plus_residual_name_disambiguates():
    """区域内两个温度传感器，靠残串「空调」消歧（残串必须先剥区域词）。"""
    cands = [
        _c("sensor.o1", "空调温度", "temperature", "办公室"),
        _c("sensor.o2", "桌上温度计", "temperature", "办公室"),
    ]
    r = erc.pick("办公室空调温度", cands, OFFICE)
    assert r["best"] == "sensor.o1", r


def test_area_ambiguous_enumerates_for_speech():
    cands = [
        _c("sensor.o1", "空调温度", "temperature", "办公室"),
        _c("sensor.o2", "桌上温度计", "temperature", "办公室"),
    ]
    r = erc.pick("办公室温度", cands, OFFICE)
    assert r["best"] is None
    assert set(r["ambiguous"]) == {"sensor.o1", "sensor.o2"}


def test_classless_state_trigger_needs_name_evidence():
    """无类别词（state 触发描述「办公室空调」）不得绑区域里任意传感器。"""
    cands = [_c("sensor.office_hum", "湿度", "humidity", "办公室")]
    r = erc.pick("办公室空调", cands, OFFICE)
    assert r["best"] is None


def test_multi_class_word_wen_shi_du():
    """「温湿度」复合类别：区域内温度+湿度并存时不得静默二选一，必须列出让用户指定。"""
    cands = [
        _c("sensor.t", "温度", "temperature", "客厅"),
        _c("sensor.h", "湿度", "humidity", "客厅"),
    ]
    r = erc.pick("客厅温湿度", cands, {"客厅": "客厅"})
    assert r["best"] is None
    assert set(r["ambiguous"]) == {"sensor.t", "sensor.h"}


def test_detect_classes_token_economy():
    cls, residue = erc.detect_classes("卧室的温湿度")
    assert cls == ["temperature", "humidity"]
    assert residue == "卧室"
    cls, residue = erc.detect_classes("大门的门被打开")
    assert "door" in cls


def test_english_resolves_via_entityid_substring():
    """英文描述若整串能中 entity_id/名字，新路径直接收束（比 legacy 更早命中）。"""
    cands = [_c("sensor.office_temperature", "Office Temperature", "temperature", "")]
    r = erc.pick("office temperature", cands, {})
    assert r["best"] == "sensor.office_temperature", r


def test_english_unmatched_leaves_to_legacy_path():
    """新路径无法落结论时必须「不抢答」（best=None、非 no_in_area、无 ambiguous），
    由集成侧继续走英文历史路径（kw_map/guessed_classes）。"""
    cands = [_c("sensor.kitchen_temp", "Temperature", "temperature", "")]
    r = erc.pick("office temperature", cands, {})
    assert r["best"] is None and not r["no_in_area"] and not r["ambiguous"]


# ---------------------------------------------------------------- 集成侧接线钉
_IA = (_ROOT / "custom_components" / "huijian_ai" / "intent_automation.py").read_text(
    encoding="utf-8"
)


def test_integration_wired():
    assert "from .entity_resolve_cn import" in _IA
    assert "_collect_area_index" in _IA
    assert "_cn_pick(entity_id, cn_cands, areas)" in _IA
    # 注册表取数必须整段容错（fake hass 环境退回空，不炸主流程）
    assert "except Exception:\n        return {}, {}" in _IA


def test_no_in_area_message_is_honest():
    """失败话术带区域与类别证据（生产排查关键）。"""
    assert "未在「" in _IA and "」区域找到" in _IA and "其他区域有" in _IA
