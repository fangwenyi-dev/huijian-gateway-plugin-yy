"""v1.0.79 钉桩：TTS 实体可用态自愈。

背景（history.csv 实锤）：tts.huijian_speech 的 available 是 v1.0.65 T7 的
transport 跟随 property，但宿主 TextToSpeechEntity should_poll=False（推送实体）
——停机窗口被求成 unavailable 后，transport 恢复没有任何事件再评估 property：
现场播报连日正常而状态卡死"不可用"，历次"恢复"全部只发生在条目 reload 的
实体重建瞬间（unavailable→""→available 的三连形态）。本批在 async_added_to_hass
挂 30s 周期复核（值变才写状态）。行为执行需 HA 类栈，按仓内遥测钉先例走
源级结构钉：接线三要素 + T7 判据原样禁动守卫。
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TTS_PY = ROOT / "custom_components" / "huijian_ai" / "tts.py"


def _method_src(name: str) -> str:
    tree = ast.parse(TTS_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"tts.py 缺方法 {name}")


def test_avail_selfheal_wired():
    body = _method_src("async_added_to_hass")
    assert "async_track_time_interval" in body, "周期复核未接线"
    assert "timedelta(seconds=30)" in body, "复核周期漂移，需显式改钉"
    assert "async_write_ha_state" in body
    assert "async_on_remove" in body, "未随实体摘除=泄漏定时器"
    # 值变才写：必须有 _avail_last 比较，禁止每拍无脑 write_ha_state
    assert "_avail_last" in body
    assert "if avail != self._avail_last" in body


def test_t7_availability_predicate_untouched():
    """恢复自愈≠放松判据：T7 三形态原样（None→False；已连→True；退避<3→True）。"""
    body = _method_src("available")
    assert "is_connected" in body
    assert "reconnect_times" in body
    assert "< 3" in body
    src = TTS_PY.read_text(encoding="utf-8")
    assert "from homeassistant.helpers.event import async_track_time_interval" in src
