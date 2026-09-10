"""v1.0.34 回归钉（2026-09-10 真机实发三连）。

①AdjustDeviceAttribute 返回无顶层 success → _normalize_result 把整个载荷折进
  raw，话术层拿空 → 执行成功却只剩裸「好的」；
②属性调节族不回 control_targets，载荷即使到位也只拼"已处理"（丢数值）；
③集成 TTS 实体无条件转 mp3 + 卫星管道 preferred wav → 双 ffmpeg 固定开销
 （'办公室 10%' detect→下发实测 1.56s）；
④registry DeferredMapping 以映射用被 HA 现网点名（2027.9 硬失效）。
每个修复各钉一条，改坏即红。
"""
import ast
import json
import pathlib

from core.ha_client import HAClient
from core.executor import Executor
from core.nlu.fast_path import Plan

# 发布链集成副本（CI 构建加载项时打包的就是这一份；yyjicheng/ 为 gitignored
# 的商店仓工作副本，不作为测试对象）
CC = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"


def _norm(obj, status=200):
    return HAClient._normalize_result(status, json.dumps(obj), "AdjustDeviceAttribute")


# ── ① 归一防御：无 success 键的 success_count/states 载荷 ────────────

def test_normalize_success_count_only_shape():
    r = _norm({"success_count": 1, "states": [{"name": "射灯", "success": True}]})
    assert r["success"] is True
    assert r["states"][0]["name"] == "射灯"
    assert r["success_count"] == 1


def test_normalize_all_failed_maps_failure():
    """全实体失败不得借道成真成功——成败按 states 折算。"""
    r = _norm({"success_count": 0, "states": [{"name": "射灯", "success": False}]})
    assert r["success"] is False


def test_normalize_states_only_partial():
    r = _norm({"states": [{"name": "A", "success": False},
                          {"name": "B", "success": True}]})
    assert r["success"] is True


def test_normalize_bare_success_key_still_priority():
    r = _norm({"success": True, "control_targets": [{"name": "窗户"}]})
    assert r["control_targets"] == [{"name": "窗户"}]


# ── ② 话术：调节族全句；邻族不得被劫持 ──────────────────────────────

def test_adjust_speech_full_sentence():
    ex = Executor(ha=None)
    plan = Plan(intent="AdjustDeviceAttribute",
                args={"attribute": "brightness", "delta": "10"},
                source="t0_prefix_strip")
    result = {"success": True, "success_count": 1,
              "states": [{"name": "射灯", "success": True}]}
    s = ex.speech(plan, result)
    assert s != "好的"
    assert "好的" in s and "射灯" in s and "亮度" in s and "10%" in s


def test_unlock_speech_not_hijacked():
    ex = Executor(ha=None)
    plan = Plan(intent="HassUnlock", args={}, source="t0")
    result = {"success": True, "states": [{"name": "门锁", "success": True}]}
    assert "已解锁" in ex.speech(plan, result)


def test_turn_targets_speech_not_hijacked():
    ex = Executor(ha=None)
    plan = Plan(intent="TurnDeviceOn", args={}, source="t0")
    result = {"success": True,
              "control_targets": [{"name": "窗", "area": "办公室"}]}
    s = ex.speech(plan, result)
    assert "窗" in s and "亮度" not in s


def test_states_family_generic_speech_kept():
    """非调节族的 states 兜底话术保持原样（"已处理"）。"""
    ex = Executor(ha=None)
    plan = Plan(intent="HassTurnOn", args={}, source="t0")
    result = {"success": True, "states": [{"name": "灯", "success": True}]}
    assert ex.speech(plan, result) == "好的，灯已处理"


# ── ③④ 集成侧源码钉（真行为钉见 yyjicheng/tests/test_tts_wav_direct.py）──

def test_integration_adjust_returns_success_key():
    src = (CC / "intent_adjust_attribute.py").read_text(encoding="utf-8")
    assert '"success": success_count > 0,' in src, \
        "AdjustDeviceAttribute 返回形态须对齐 turn 族（顶层 success）"


def test_tts_wav_passthrough_wired():
    src = (CC / "tts.py").read_text(encoding="utf-8")
    assert "def _wav_passthrough(" in src
    assert 'return "wav", buf.getvalue()' in src
    assert "async_convert_audio(" in src, "非 wav 客户（media_player 等）的转码路径必须保留"


def test_registry_devices_fixed_entities_untouched():
    """devices 视图已改迭代形；entities 仍是裸 dict（迭代只得 id 键串）——
    core@dev 源码实证无 entities 版 deprecated 视图，迭代 entities 是 P0 炸点，
    此钉防后人"顺手对齐"。"""
    helper = (CC / "intent_helper.py").read_text(encoding="utf-8")
    scene = (CC / "intent_voice_scene.py").read_text(encoding="utf-8")
    assert ".devices.values()" not in helper, "devices 映射用 2027.9 硬失效"
    assert "for device_entry in dev_reg.devices:" in helper
    assert "ent_reg.entities.values()" in helper, "entities 是裸 dict，须 .values()"
    assert "ent_registry.entities.values()" in scene


def test_wav_passthrough_pure_function_matrix():
    """AST 抽出 canonical _wav_passthrough 真执行（全链行为钉，不依赖 HA 栈）。"""
    tree = ast.parse((CC / "tts.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_wav_passthrough")
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<pin>", "exec"), ns)
    f = ns["_wav_passthrough"]
    assert f({"preferred_format": "wav", "preferred_sample_rate": 16000,
              "preferred_sample_channels": 1}, 16000, 1)          # 卫星三件套直通
    assert not f({"audio_format": "mp3", "preferred_format": "wav"}, 16000, 1)
    assert not f({"preferred_format": "wav", "preferred_sample_rate": 22050}, 16000, 1)
    assert not f({"preferred_format": "wav", "preferred_sample_channels": 2}, 16000, 1)
    assert not f({}, 16000, 1)
    assert not f(None, 16000, 1)
    assert not f({"preferred_format": "wav", "preferred_sample_rate": "x"}, 16000, 1)
    assert f({"preferred_format": "wav", "preferred_sample_channels": None}, 16000, 1)
    assert f({"preferred_format": "WAV"}, 16000, 1)
