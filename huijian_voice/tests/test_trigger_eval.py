"""trigger_eval 纯判定单测（零 HA 依赖，直 load 集成侧模块文件）。

v1.0.30：集成侧 custom_components/huijian_ai/trigger_eval.py 是无依赖纯函数，
加载项测试直接 importlib 装载——CI Windows python 不装 HA 也能跑。
"""
import importlib.util
from datetime import datetime
from pathlib import Path

_P = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai" / "trigger_eval.py"
_spec = importlib.util.spec_from_file_location("trigger_eval", _P)
te = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(te)

met, due = te.state_trigger_met, te.time_trigger_due


def test_numeric_above_strict():
    assert met({"entity_id": "sensor.t", "above": 28}, "28.5")
    assert not met({"above": 28}, "28")        # 等于不触发（进沿语义）
    assert not met({"above": 28}, "27")


def test_numeric_below():
    assert met({"below": 16}, "15")
    assert not met({"below": 16}, "16")


def test_both_bounds_is_band():
    """above+below 同给=区间内触发（HA numeric_state 与 060401 旧引擎一致语义）。"""
    t = {"above": 10, "below": 30}
    assert met(t, "20") and not met(t, "31") and not met(t, "9")


def test_binary_state_to_fixed():
    """060401 旧缺陷钉：'有人' 自动化 float('on') 失败恒不触发——修复后必须能中。"""
    assert met({"entity_id": "binary_sensor.p", "to": "on"}, "on")
    assert not met({"entity_id": "binary_sensor.p", "to": "on"}, "off")
    assert met({"to": "off"}, "off")


def test_pure_monitor_numeric_any_change():
    assert met({"entity_id": "sensor.x"}, "42")       # 无数值条件：任意数值变化即中
    assert not met({"entity_id": "sensor.x"}, "on")  # 非数值无 to → 旧语义恒不中
    assert not met({}, None)                   # 脏数据不外抛


def test_non_numeric_without_to_false():
    assert not met({"above": 28}, "on")        # 旧语义保持


def test_time_due_basic():
    t = {"at": "07:00"}
    assert due(t, datetime(2026, 9, 13, 7, 0))
    assert not due(t, datetime(2026, 9, 13, 7, 1))
    assert not due({}, datetime(2026, 9, 13, 7, 0))


def test_time_due_days():
    sun = datetime(2026, 9, 13, 7, 0)          # 周天 isoweekday=7
    assert due({"at": "07:00", "days": [1, 2]}, sun) is False
    assert due({"at": "07:00", "days": [7]}, sun) is True


def test_time_same_minute_dedupe():
    now = datetime(2026, 9, 13, 7, 0)
    assert due({"at": "07:00"}, now, now.isoformat()) is False
    yesterday = datetime(2026, 9, 12, 7, 0).isoformat()
    assert due({"at": "07:00"}, now, yesterday) is True


def test_time_bad_iso_never_raises():
    assert due({"at": "07:00"}, datetime(2026, 9, 13, 7, 0), "垃圾串") is True
