"""v1.0.69 根因②钉桩：开关族能力闸（防 core area 扇出错误风暴 + 假成功）。

现场（2026-09-14 11:27:12）：「展厅推拉(窗)」漏进通用 HassTurnOn，core 把
展厅全部 exposed 实体（3×开窗器的 sensor/number/button + 展厅 media_player/
remote/favorite button）逐个 turn_on → 12 条 "does not support entity" 错误
风暴；窗没动、播报却谎称「展厅推拉开了」。闸规则（用户定案：不坐实句形态，
只做保守通用防线）：开关族意图 × ①grounded 实体含非可开关域 / ②无实体且
原话带窗族词（剔除 窗帘/纱窗 后）→ 当场如实失败，两通道均不发。
"""
import asyncio

from conftest import FakeHAClient
from core.executor import Executor
from core.nlu.fast_path import Plan


class Ha(FakeHAClient):
    """FakeHAClient 补 call_service 留痕（services 直调通道）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.svc_calls = []

    async def call_service(self, domain, service, data, timeout=10.0):
        self.svc_calls.append((domain, service, data))
        return {"success": True}


def _run(ha, plan):
    return asyncio.run(Executor(ha, None).run(plan))


def _klar(intent, args, utterance):
    return Plan(intent=intent, args=args, source="klar", utterance=utterance)


# ── 拦：现场两形态 ────────────────────────────────────────────
def test_bare_area_window_utterance_refused_no_ha_call():
    """现场形态：klar 把「展厅推拉」判成 HassTurnOn{area} 无实体。"""
    ha = Ha()
    ok, msg = _run(ha, _klar("HassTurnOn", {"area": "展厅"}, "打开展厅推拉窗"))
    assert ok is False
    assert msg.startswith("抱歉") and "不敢把整屋设备冒按" in msg
    assert ha.calls == [] and ha.svc_calls == [], "拦下即两通道均不得外发"
    # 谎报防线：结果绝不含"开了/打开了"式成功字尾
    assert "开了" not in msg and "已打开" not in msg


def test_grounded_non_toggleable_entities_refused():
    """现场形态②：grounded 混进 button/sensor/number（开窗器设备内部实体）。"""
    ha = Ha()
    plan = _klar("HassTurnOn", {"entity_id": [
        "button.kai_chuang_qi_123f_0001_01_1_kai_qi",
        "sensor.kai_chuang_qi_123f_0001_01_dian_chi_dian_ya",
    ]}, "打开展厅开窗器")
    ok, msg = _run(ha, plan)
    assert ok is False and "不支持直接开关" in msg
    assert ha.calls == [] and ha.svc_calls == []


def test_off_and_toggle_variants_gated():
    for intent, ut in (("HassTurnOff", "关闭办公室推拉门"),
                       ("HassToggle", "内倒一下客厅窗")):
        ha = Ha()
        ok, msg = _run(ha, _klar(intent, {"area": "客厅"}, ut))
        assert ok is False and msg.startswith("抱歉"), f"{intent} 未被同闸罩住"
        assert ha.calls == [] and ha.svc_calls == []


def test_bare_string_entity_id_still_gated():
    """entity_id 为裸字符串（klar 常见形态）也要能钉住 button 域。"""
    ha = Ha()
    ok, _ = _run(ha, _klar("HassTurnOn",
                           {"entity_id": "number.kai_chuang_qi_li_du"}, "开窗"))
    assert ok is False and ha.svc_calls == []


# ── 放：既有合法路不许误伤 ────────────────────────────────────
def test_grounded_light_passes_direct_channel():
    ha = Ha()
    ok, _ = _run(ha, _klar("HassTurnOn", {"entity_id": "light.zhan_ting"},
                           "打开展厅射灯"))
    assert ok is True
    assert ha.svc_calls == [("homeassistant", "turn_on",
                             {"entity_id": "light.zhan_ting"})]
    assert ha.calls == []


def test_curtain_utterance_passes():
    """窗帘/纱窗=合法 cover，裸区域句照常走 intent 通道。"""
    ha = Ha()
    ok, _ = _run(ha, _klar("HassTurnOff", {"area": "卧室"}, "关闭窗帘"))
    assert ok is True
    assert ha.calls == [("HassTurnOff", {"area": "卧室"})]
    assert ha.svc_calls == []


def test_no_window_word_bare_area_passes():
    """非窗句维持现状（core area 解析照旧）：本闸只护窗族病灶。"""
    ha = Ha()
    ok, _ = _run(ha, _klar("HassTurnOn", {"area": "办公室"}, "打开办公室射灯"))
    assert ok is True and ha.calls and ha.svc_calls == []


def test_non_turn_family_intents_untouched():
    ha = Ha(results={"HassLightSet": {"success": True}})
    ok, _ = _run(ha, _klar("HassLightSet", {"entity_id": "sensor.batt",
                                            "brightness": 30}, "亮度调三成"))
    assert ok is True  # LightSet 有自己的双闸（pipeline v1.0.55），本闸不扩权


def test_lock_semantic_passthrough_intact():
    """锁走 turn_on 意图但 _klar_direct 改 lock/unlock——域是 lock，放行。"""
    ha = Ha()
    ok, _ = _run(ha, _klar("HassTurnOn", {"entity_id": "lock.front"}, "锁上大门"))
    assert ok is True
    assert ha.svc_calls and ha.svc_calls[0][:2] == ("lock", "lock")


def test_gate_never_raises_on_garbage():
    ha = Ha()
    ok, _ = _run(ha, Plan(intent="HassTurnOn",
                          args={"entity_id": [None, 42, "no_dot"]},
                          source="klar", utterance=None))
    assert ok is True  # 无合法 eid → 裸 intent 通道；utterance 缺省不误拦
