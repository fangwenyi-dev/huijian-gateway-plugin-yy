# -*- coding: utf-8 -*-
"""v1.0.99 前置音救援钉：preannounce 不得拖死 API 音频板播报正文。

部署实锤（2026-09-19 晚，v1.0.98 装至 VM 后 v1.0.96 分因 WARN 首战点名）：
[Announce] 播报未走文本自合成推流：**preannounce前置音**（message=19字,
preannounce=True…）——core 2026.9 assist_satellite/services.py 的 announce
schema `preannounce` **默认 True**，凡 message 必注入 PREANNOUNCE_URL 提示音；
慧尖 API 音频板放不出这声「叮~」（无 URL 自取能力），护栏③旧语义=整单回退
=正文也陪葬 90s 0 字节。修复=救援纯函数：api_audio+preannounce 分因+有正文 ⇒
弃前置音仍接管；其余分因与 SPEAKER-only 形态不救（v1.0.96 语义原样）。

钉两层：① _preannounce_rescued 真值表（AST 提真身 exec）；② 接线与源头
（救援点在 gate 之后 WARN 之前、清 preannounce_media_id 的 send 落点、
text.py 派发显式 preannounce=False 源头关断）。
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
AS = ROOT / "custom_components" / "huijian_ai" / "assist_satellite.py"
TXT = ROOT / "custom_components" / "huijian_ai" / "text.py"


def _load_rescuer():
    src = AS.read_text(encoding="utf-8")
    m = re.search(r"^def _preannounce_rescued\(.*?(?=^def |^#: |\Z)", src, re.M | re.S)
    assert m, ("_preannounce_rescued 被删/改名——core 默认 preannounce=True 下"
               "API 音频板播报将回到 90s 0 字节（v1.0.99 案复发）")
    ns: dict = {}
    exec(compile(m.group(0), "<_preannounce_rescued>", "exec"), ns)  # noqa: S102
    return ns["_preannounce_rescued"]


rescued = _load_rescuer()

_PREANN = "preannounce前置音"


# ── ① 真值表 ────────────────────────────────────────────────────────────
def test_rescue_api_audio_preannounce_with_message():
    """案核组合：必须救。"""
    assert rescued(api_audio=True, has_message=True, skip=_PREANN) is True


def test_no_rescue_without_api_audio():
    """SPEAKER-only 真喇叭：护栏③照旧拒（URL 自取形态一字不动，也不产噪音）。"""
    assert rescued(api_audio=False, has_message=True, skip=_PREANN) is False


def test_no_rescue_for_other_skips():
    """无 message/撞活跃轮等分因不救——护栏②语义原样（固件 busy refuse 时
    抢代次反杀在播应答）。"""
    assert rescued(api_audio=True, has_message=True, skip="撞活跃轮(护栏②不抢下行)") is False
    assert rescued(api_audio=True, has_message=True, skip="无message(API音频设备无自取media URL能力)") is False
    assert rescued(api_audio=True, has_message=True, skip="") is False


def test_no_rescue_without_message():
    """纯 media announce：正文都无，谈不上弃前置音。"""
    assert rescued(api_audio=True, has_message=False, skip=_PREANN) is False


def test_keyword_only_signature():
    import pytest
    with pytest.raises(TypeError):
        rescued(True, True, _PREANN)


# ── ② 接线与源头 ────────────────────────────────────────────────────────
def test_caller_rescues_between_gate_and_warn():
    src = AS.read_text(encoding="utf-8")
    i_gate = src.index("taken, skip = _announce_gate(")
    i_resc = src.index("if _preannounce_rescued(api_audio=api_audio_only,")
    i_warn = src.index('if skip:\n            _LOGGER.warning(\n                "[Announce] 播报未走文本自合成推流')
    assert i_gate < i_resc < i_warn, "救援必须夹在 gate 与分因 WARN 之间"
    seg = src[i_resc:i_resc + 600]
    assert 'taken, skip = True, ""' in seg
    assert 'preannounce_media_id = ""' in seg, "救援必须清掉发给设备的前置音 URL"


def test_send_still_uses_cleared_preannounce():
    src = AS.read_text(encoding="utf-8")
    i = src.index("send_voice_assistant_announcement_await_response")
    seg = src[i:i + 400]
    assert 'preannounce_media_id=preannounce_media_id or ""' in seg, \
        "Announce 请求必须走被救援清空的同一个变量"


def test_text_route_disables_preannounce_at_source():
    """主通道源头就关（自动化直调 core announce 的形态由 ②救援兜底）。"""
    src = TXT.read_text(encoding="utf-8")
    i = src.index('"assist_satellite",')
    seg = src[i:i + 500]
    assert '"preannounce": False' in seg


def test_core_default_preannounce_still_true_note():
    """本钉不复读 core 源码——只钉住我方认知前提：schema 默认 True 属 core
    2026.9 行为。若未来 core 把默认改掉，救援函数变成 no-op（skip 不再是
    preannounce 开头），真值表①组仍全绿，无回潮风险，故此处不做字符串钉。"""
    assert rescued(api_audio=True, has_message=True, skip=_PREANN) is True


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
    print("v1099 前置音救援钉：", "全绿" if fails == 0 else f"{fails} 红")
    raise SystemExit(1 if fails else 0)
