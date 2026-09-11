"""v1.0.42 两需求回归钉。

① 传感器查询修复（生产日志 2026-09-11：「现在办公室的温度多少」落兜底）：
   _find_area 旧正则 {2,4}? 不剥时间/引导前缀——把「现在办公室」整段当区域名，
   按错区域过滤→全滤掉→None。修：Q1 前缀剥离、Q1b 两字区域（书房/客厅）、
   Q2 registry 未同步时降级唯一命中；多传感器无 registry 走诚实引导不乱报。
② 场景/自动化支持 HA 全产品（扫地机器人/雷达/电视/加湿器等）：
   fast_path 家电族动作层（启动/暂停/回充）、targets 域提示补齐、
   creation 设备状态触发（当电视被打开…）、集成端 to= 触发池放开非传感器域。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))         # tests/ 进路径（conftest 惯例）
from conftest import FakeHAClient                       # noqa: E402

from core.nlu.query import QueryZone                    # noqa: E402
from core.settings import Settings                      # noqa: E402
from core.nlu.fast_path import FastPath                  # noqa: E402
from test_fast_path import FakeScenes                    # noqa: E402


# ── ① 查询族：区域提取与降级 ─────────────────────────────────────
def _q(states, areas, entity_area):
    return QueryZone(FakeHAClient(states=states, areas=areas,
                                  entity_area=entity_area), None)


KLAR_OFFICE = {   # 真机形态：传感器就叫「温度」，靠区域注册表绑「办公室」
    "sensor.klar_temp": {"entity_id": "sensor.klar_temp", "state": "26.5",
                         "attributes": {"friendly_name": "温度",
                                        "device_class": "temperature"}},
}


def test_q1_time_prefix_not_swallowed_as_area():
    # 生产 root cause：区域名不能含「现在」。registry 有办公室时正常答。
    q = _q(KLAR_OFFICE, {"a1": "办公室"}, {"sensor.klar_temp": "办公室"})
    ans = asyncio.run(q.answer("现在办公室的温度多少"))
    assert ans and "26" in ans and "度" in ans, ans


def test_q1b_two_char_area_suffix():
    # 「书房/客厅」两字区域在 registry 未同步时也要抽得出（旧 {2,4}? 至少三字）。
    states = {"sensor.s": {"entity_id": "sensor.s", "state": "23",
                           "attributes": {"friendly_name": "书房温度",
                                          "device_class": "temperature"}}}
    q = _q(states, {}, {})
    ans = asyncio.run(q.answer("书房温度是多少"))
    assert ans and "23" in ans, ans


def test_q2_no_registry_unique_fallback():
    # registry 空但仅一颗温度传感器：降级命中而非 None 落兜底。
    q = _q(KLAR_OFFICE, {}, {})
    ans = asyncio.run(q.answer("办公室多少度"))
    assert ans and "26" in ans, ans


def test_q2_no_registry_multi_is_honest():
    # registry 空且多颗同类：绝不瞎报，给诚实引导。
    states = {
        "sensor.a": {"entity_id": "sensor.a", "state": "21",
                     "attributes": {"friendly_name": "温度", "device_class": "temperature"}},
        "sensor.b": {"entity_id": "sensor.b", "state": "26",
                     "attributes": {"friendly_name": "厨房温度", "device_class": "temperature"}},
    }
    q = _q(states, {}, {})
    ans = asyncio.run(q.answer("办公室的温度多少"))
    assert ans and "同步" in ans and "26" not in ans.split("同步")[0], ans


def test_q_prefix_polite_and_check_variants():
    q = _q(KLAR_OFFICE, {"a1": "办公室"}, {"sensor.klar_temp": "办公室"})
    for s in ("现在办公室的温度多少", "请问办公室温度是多少",
              "办公室的温度怎么样", "办公室多少度"):
        assert asyncio.run(q.answer(s)), s


# ── ② 家电族：fast_path 动作层 ───────────────────────────────────
def _fp():
    return FastPath(FakeScenes(), None, Settings(Path("/tmp/hj_appl_v42")))


def _match(text):
    return asyncio.run(_fp().match(text))


def test_appliance_vacuum_start_pause_return():
    p = _match("启动扫地机器人")
    assert p and p.intent == "TurnDeviceOn", p and p.trace
    d = p.args["target"][0]["devices"][0]
    assert "vacuum" in d["domains"], d

    p = _match("暂停扫地机器人")
    assert p and p.intent == "PauseDevice", p and p.trace

    p = _match("扫地机器人回充")
    assert p and p.intent == "TurnDeviceOff", p and p.trace


def test_appliance_sov_wordorder():
    # 设备词前置语序（SOV），动作表够不到，靠家电层。
    p = _match("让客厅的扫地机器人开始打扫")
    assert p and p.intent == "TurnDeviceOn", p and p.trace
    assert p.args["target"][0].get("area") == "客厅"


def test_appliance_bare_command():
    p = _match("开始扫地")
    assert p and p.intent == "TurnDeviceOn", p and p.trace
    assert "vacuum" in p.args["target"][0]["devices"][0]["domains"]


def test_appliance_standard_onoff_untouched():
    # 反例：打开/关闭扫地机器人交回原表（家电层不劫持），意图不变。
    p = _match("关闭扫地机器人")
    assert p and p.intent == "TurnDeviceOff", p and p.trace
    p = _match("打开扫地机器人")
    assert p and p.intent == "TurnDeviceOn", p and p.trace


def test_appliance_never_on_questions():
    # 疑问/否定绝不冒动设备（_VAC_NOGO 闸）。
    for s in ("扫地机器人现在什么状态", "不要启动扫地机器人", "扫地机器人回充了吗"):
        p = _match(s)
        assert p is None or p.intent not in ("TurnDeviceOn", "TurnDeviceOff", "PauseDevice"), s


def test_appliance_tv_still_turn_family():
    p = _match("打开电视")
    assert p and p.intent == "TurnDeviceOn", p and p.trace
    assert "media_player" in p.args["target"][0]["devices"][0]["domains"]


def test_targets_domain_hints_new_appliances():
    from core.nlu import targets as T
    assert T.domain_hint("加湿器") == ["humidifier"]
    assert "fan" in T.domain_hint("净化器")
    assert T.domain_hint("电视") == ["media_player"]
    assert T.domain_hint("扫地机器人") == ["vacuum"]


def test_targets_static_vocab_cold_start():
    # 冷启动（无 registry 动态词）也能直呼新品类设备名。
    from core.nlu import targets as T
    for w in ("扫地机器人", "吸尘器", "电视", "加湿器"):
        assert any(w in d for d in T.KNOWN_DEVICES) or w in T.KNOWN_DEVICES, w


# ── ② 自动化：设备状态触发 ────────────────────────────────────────
def test_creation_device_state_triggers():
    from core.nlu.creation import parse
    r = parse("当电视被打开就拉上窗帘")
    assert r and r["kind"] == "automation", r
    assert r["trigger"]["to"] == "on" and r["trigger"]["entity_id"] == "电视", r

    r = parse("当扫地机器人开始清扫就关闭电视")
    assert r and r["trigger"]["to"] == "cleaning", r

    r = parse("当扫地机器人回充就打开玄关灯")
    assert r and r["trigger"]["to"] == "returning", r


def test_creation_presence_still_wins_and_no_false_positive():
    from core.nlu.creation import parse
    # 人体感应旧分支不受影响。
    r = parse("当办公室检测到有人就开灯")
    assert r and r["kind"] == "automation" and r["trigger"]["to"] == "on", r
    # 「洗衣机洗完」词表外，不冒然产触发（宁可 None 落上层）。
    assert parse("当洗衣机洗完就晾衣服") is None


def test_creation_scene_not_stolen_by_devstate():
    # 「当我说X就Y」场景分支在设备状态之前，不能被 devstate 抢。
    from core.nlu.creation import parse
    r = parse("当我说看电视就打开电视")
    assert r and r["kind"] == "scene", r


# ── 集成侧接线（HA 未装本地，源码钉）────────────────────────────
_INT = Path(__file__).parent.parent / "custom_components" / "huijian_ai"


def test_integration_pause_intent_registered():
    src = (_INT / "intent.py").read_text(encoding="utf-8")
    assert "PauseDeviceIntent" in src
    assert 'intent.async_register(hass, PauseDeviceIntent())' in src
    assert "from .intent_turn import" in src and "PauseDeviceIntent" in \
        next(l for l in src.splitlines() if "from .intent_turn import" in l)


def test_integration_pause_calls_table():
    src = (_INT / "intent_turn.py").read_text(encoding="utf-8")
    assert '"vacuum": ("vacuum", "pause")' in src
    assert '"media_player": ("media_player", "media_pause")' in src
    assert '"cover": ("cover", "stop_cover")' in src
    # 不可暂停域显式判失败、话术层不谎报成功。
    assert '暂不支持暂停该设备' in src


def test_integration_state_trigger_pool_opened():
    src = (_INT / "intent_automation.py").read_text(encoding="utf-8")
    assert "state_mode" in src and '"automation", "scene", "script"' in src
    # 数值阈值仍限传感器池（不放开 above/below）
    assert "pool = sensor_states" in src


def test_integration_class_words_radar():
    src = (_INT / "entity_resolve_cn.py").read_text(encoding="utf-8")
    assert '("毫米波", ("motion", "presence"))' in src
    assert '("雷达", ("motion", "presence"))' in src
