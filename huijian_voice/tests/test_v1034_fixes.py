"""v1.0.34 回归钉（2026-09-10 真机实发三连）。

①AdjustDeviceAttribute 返回无顶层 success → _normalize_result 把整个载荷折进
  raw，话术层拿空 → 执行成功却只剩裸「好的」；
②属性调节族不回 control_targets，载荷即使到位也只拼"已处理"（丢数值）；
③集成 TTS 实体无条件转 mp3 + 卫星管道 preferred wav → 双 ffmpeg 固定开销
 （'办公室 10%' detect→下发实测 1.56s）；
④registry DeferredMapping 以映射用被 HA 现网点名（2027.9 硬失效）。
每个修复各钉一条，改坏即红。
"""
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


# ── ③④ 集成侧源码钉 ──────────────────────────────────────────────

def test_integration_adjust_returns_success_key():
    src = (CC / "intent_adjust_attribute.py").read_text(encoding="utf-8")
    assert '"success": success_count > 0,' in src, \
        "AdjustDeviceAttribute 返回形态须对齐 turn 族（顶层 success）"


def test_tts_empty_pcm_fail_loud_wired():
    """审查 M2：空合成必须在 audio.py 直封支截住——44 字节纯头能骗过出口
    `if not audio` 闸写进 HA 缓存=同句永久静音（2026-09-09 病灶）。"""
    audio = (CC / "huijian" / "audio.py").read_text(encoding="utf-8")
    i = audio.index("if not pcm:")
    j = audio.index("wav = wrap_pcm_as_wav(")
    assert i < j, "空 PCM 闸必须在 wrap 之前"
    assert "return" in audio[i:j]


def test_tts_no_raw_opus_injection():
    """审查 L9：解码失败不得把原始 opus 包当 PCM yield（卫星播噪声）。"""
    src = (CC / "tts.py").read_text(encoding="utf-8")
    assert "frame dropped" in src and "continue" in src
    k = src.index("Decode opus failed")
    assert "continue" in src[k:k + 200], "except 支序必须是 continue"


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


# ── M3/L4：admin 写路由 id/触发词闸门（真函数行为钉）───────────────

def test_admin_id_and_phrase_gates():
    from core.admin_api import _bad_id, _bad_phrase
    assert not _bad_id("voice_scene_20260910143642")
    assert not _bad_id("3f9c2a1e-5b6d-4a11-9c8e-7d2f0a1b4c5d")   # HA uuid
    assert _bad_id("../../states/light.on")                        # yarl 点段穿越
    assert _bad_id("a.b")                                          # 一切点形拒
    assert _bad_id("") and _bad_id(None) and _bad_id("x y")
    assert not _bad_phrase("晚安好梦")
    assert _bad_phrase("晚安 好梦")                                 # 空格→语音删不掉
    assert _bad_phrase("晚安，好梦")                                # 标点同拒
    assert _bad_phrase("一二三四五六七八九十一二三四五")             # >12 字


def test_zh_error_no_empty_parens():
    from core.executor import zh_error
    assert zh_error("") == "抱歉，这一步没有执行成功，可以换个说法再试"
    assert "（unmapped-weird-err）" in zh_error("unmapped-weird-err")


def test_models_readiness_renders_inner_layer():
    """v1.0.35：首页模型就绪度必须迭代 {updated, models} 外壳的**内层**——
    直接 entries(外壳) 把 updated/models 渲染成两行红问号（用户实锤截图）。"""
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / "www" / "index.html").read_text(encoding="utf-8")
    assert "ms.models" in html, "就绪度卡未取内层 models"
    assert "o.state" in html and "o.pct" in html, "内层字段须用 state/pct（写者协议）"
    assert "状态更新于" in html
    w = (root / "core" / "model_store.py").read_text(encoding="utf-8")
    assert '"updated": time.time(), "models": snap' in w, "写者外壳形态变了要同步前端"
