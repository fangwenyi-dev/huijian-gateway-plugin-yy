# -*- coding: utf-8 -*-
"""v1.0.98 announce 形态判定钉（_api_audio_form）——播报三日 0 字节案真凶收口。

2026-09-19 VM(2026.9.2 现役 1.0.97)+COM27 七轮实证：core assist_satellite.announce
(message) 送达设备（串口 Announce 行在）、下行 0 bytes、90s 封顶拆流，HA 侧
WARN/ERROR **零条**（system_log/list 与推流事件面双双干净）。定罪链：
固件 v2.1.12 起 get_feature_flags() 并报 FEATURE_SPEAKER|FEATURE_API_AUDIO
（为对话腿 `SPEAKER|API_AUDIO 任一即推流` 放行），而插件 v1.0.93 announce 腿
判据 `API_AUDIO and not SPEAKER` 恰把这一并存形态排除 → _announce_gate(api_audio
=False) 返回 (False, "")=按设计静默 → _do_announce 一字不动作。对话腿（任一即
推流）与播报腿（互斥）自相矛盾——两端注释各说各话，日志面互相甩锅，三日查空。

本文件把形态判定收进纯函数 `_api_audio_form` 并钉死真值表：任何把判据改回
「非 SPEAKER」独占形态的动作当场红；SPEAKER-only 真喇叭必须恒 False（旧 URL
自取路零回退）。
"""
import pathlib
import re
from enum import IntEnum

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "custom_components" / "huijian_ai" / "assist_satellite.py"


class VoiceAssistantFeature(IntEnum):
    """与 aioesphomeapi.api_pb2.VoiceAssistantFeature 同位（1.10+ 申报面）。"""

    NONE = 0
    VOICE_ASSISTANT = 1 << 0
    SPEAKER = 1 << 1
    API_AUDIO = 1 << 2
    TIMER = 1 << 3


def _load_form():
    src = SRC.read_text(encoding="utf-8")
    m = re.search(r"^def _api_audio_form\(.*?(?=^def |^#: |\Z)", src, re.M | re.S)
    assert m, ("_api_audio_form 纯函数被删/改名——announce 形态判定若回到内联"
               "布尔，三日 0 字节静默案（v2.1.12 并存形态被 not SPEAKER 排除）即回潮")
    ns: dict = {"VoiceAssistantFeature": VoiceAssistantFeature}
    exec(compile(m.group(0), "<_api_audio_form>", "exec"), ns)  # noqa: S102 提纯函数
    return ns["_api_audio_form"]


form = _load_form()

# 固件实际形态常量（v2.1.12+：VA|SPEAKER|API_AUDIO（+TIMER…），取前三位即可）
_HUIJIAN_V2112 = (VoiceAssistantFeature.VOICE_ASSISTANT
                  | VoiceAssistantFeature.SPEAKER
                  | VoiceAssistantFeature.API_AUDIO)
_HUIJIAN_V2111 = VoiceAssistantFeature.API_AUDIO
_REAL_SPEAKER = VoiceAssistantFeature.SPEAKER


def test_coexist_flags_takeover():
    """案核真凶钉：慧尖板 2.1.12+ 并存形态必须接管推流。修前此处必红。"""
    assert form(_HUIJIAN_V2112) is True, (
        "SPEAKER+API_AUDIO 并存被排除=v2.1.12 慧尖板 announce 又回 0 字节静默案")


def test_api_audio_only_still_takeover():
    assert form(_HUIJIAN_V2111) is True


def test_speaker_only_never_takeover():
    """真喇叭（SPEAKER-only 无 API_AUDIO）：旧 URL 自取路一字不动。"""
    assert form(_REAL_SPEAKER) is False


def test_voice_assistant_only_no_takeover():
    """只报 VOICE_ASSISTANT 位（无音频下行能力）：不接管（无音频腿可推）。"""
    assert form(VoiceAssistantFeature.VOICE_ASSISTANT) is False


def test_zero_flags_no_takeover():
    assert form(0) is False


def test_old_exclusive_predicate_not_resurrected():
    """禁回潮：源码里不得再出现 `and not (…SPEAKER)` 的 announce 互斥判据。"""
    src = SRC.read_text(encoding="utf-8")
    assert re.search(r"and not \(?_flags & VoiceAssistantFeature\.SPEAKER", src) is None, (
        "v1.0.93 旧独占判据回潮——v2.1.12 并存形态将被再次静默排除，0 字节案复发")


def test_caller_wired_to_form_function():
    src = SRC.read_text(encoding="utf-8")
    i = src.index("api_audio_only = _api_audio_form(")
    seg = src[max(0, i - 400):i + 120]
    assert "voice_assistant_feature_flags_compat" in seg, "判定的输入必须来自 compat flags"
    assert "taken, skip = _announce_gate(" in src[i:i + 900], "判定结果必须进 gate"


def test_gate_composition_end_to_end():
    """形态判定→gate 的合取面：并存+idle+有 message ⇒ taken。"""
    src = SRC.read_text(encoding="utf-8")
    m = re.search(r"^def _announce_gate\(.*?(?=^def |\Z)", src, re.M | re.S)
    gns: dict = {}
    exec(compile(m.group(0), "<_announce_gate>", "exec"), gns)  # noqa: S102
    take, skip = gns["_announce_gate"](api_audio=form(_HUIJIAN_V2112),
                                       has_message=True, preannounce=False,
                                       pipeline_busy=False)
    assert (take, skip) == (True, ""), "现役慧尖板 announce 必须走自合成推流"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  [OK] {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  [FAIL] {name}: {e}")
    print("v1098 形态钉：", "全绿" if fails == 0 else f"{fails} 红")
    raise SystemExit(1 if fails else 0)
