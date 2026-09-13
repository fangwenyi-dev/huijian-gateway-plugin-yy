"""v1.0.55 修复批钉桩：klar 主裁决窗闸 + HassLightSet 双闸 + 云 TTS 失败钉扎。

现场依据（2026-09-12 日志，用户实录）：
①「办公室瓶盖窗速度设为百分之三十五」被引擎**会话回放兜底**（draft.rs：未知
  目标+任意数字 → 硬套上一个可见灯 + HassLightSet+brightness）→ 摄影灯被点亮
  （args 里只有 pinyin entity_id，v1.0.12 的 args 窗词闸失明）；
②「办公室平安商速速度设为百分之三十」——ASR 把「窗速」听残，句内**无窗字**，
  t0 合理拒收，但「速度」语义 × light 目标同样必须拦；
③「还是有两个 tts 音色」——云端持续不可用时逐句"先试云失败再本地" =
  逐句换嗓 + 逐句白等云超时；半途断流还会一内一女混播。
修法与职责见 core/pipeline._klar_window_lamp_conflict、
core/executor._klar_direct（HassLightSet）、core/tts（_CLOUD_PIN_S）。
"""
import asyncio
import logging
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from core.executor import Executor                                   # noqa: E402
from core.nlu.fast_path import Plan                                  # noqa: E402
from core.pipeline import select_primary_plan                        # noqa: E402
from core.tts import TtsEngine, _CLOUD_PIN_S                         # noqa: E402

FRAME = b"OPUSFRAME"


def _kl(intent, args, utterance):
    return Plan(intent=intent, args=args, source="klar", utterance=utterance)


# ── ① 主裁决窗闸 ─────────────────────────────────────────────────
def test_field_case_window_speed_to_light_is_vetoed():
    """案1 原句：窗+速度 → 目标是摄影灯 → 主裁决整条弃用（落 None）。"""
    kl = _kl("HassLightSet",
             {"entity_id": "light.ban_gong_shi_she_deng", "brightness": "30"},
             "办公室瓶盖窗速度设为百分之三十五")
    assert select_primary_plan(None, kl) is None


def test_field_case_garbled_no_window_char_but_speed_still_vetoed():
    """案2：ASR 残句无「窗」字，但「速度」×light（句内无灯词）同样弃用。"""
    kl = _kl("HassLightSet",
             {"entity_id": "light.ban_gong_shi_she_deng", "brightness": "30"},
             "办公室平安商速速度设为百分之三十")
    assert select_primary_plan(None, kl) is None


def test_window_guard_lets_legitimates_through():
    light = {"entity_id": "light.x", "brightness": "80"}
    # 正常灯句：无窗/速度词 → kl 照常当选
    assert select_primary_plan(None, _kl("HassLightSet", light, "办公室射灯亮度调到百分之八十"))
    # 窗句 × cover 目标：合法（klar 干窗帘是判定书分工）
    assert select_primary_plan(None, _kl("HassTurnOn", {"entity_id": "cover.chuang"},
                                         "把办公室平开窗开到一半"))
    # 风扇 × 速度：合法
    assert select_primary_plan(None, _kl("HassTurnOn", {"entity_id": "fan.feng"},
                                         "风扇速度调高"))
    # 窗帘/纱窗先行剔除 → 「把窗帘打开」不触闸
    assert select_primary_plan(None, _kl("HassTurnOn", {"entity_id": "cover.cl"}, "把窗帘打开"))
    # 句内同时点灯（窗户旁边的灯）→ 放行
    assert select_primary_plan(None, _kl("HassTurnOn", light, "把窗户旁边的灯打开"))
    # 守卫意图族之外（空调温度）不拦
    assert select_primary_plan(None, _kl("HassClimateSetTemperature",
                                         {"entity_id": "climate.a", "temperature": 26},
                                         "屋里空调速度不管了调到26度"))
    # 未 grounded 步（无 entity_id）不归本闸
    assert select_primary_plan(None, _kl("HassLightSet", {"brightness": "30"}, "窗开合速度30%"))


def test_window_guard_veto_drops_fp_residue_too():
    """veto 后宁落级联下层：同句字面表残卡也不得顶包（防止 fp 侧同样错开灯）。"""
    fp = Plan(intent="HassTurnOn", args={"entity_id": "light.y"}, source="t1",
              utterance="办公室瓶盖窗速度设为百分之三十五")
    kl = _kl("HassLightSet", {"entity_id": "light.x", "brightness": "35"}, fp.utterance)
    assert select_primary_plan(fp, kl) is None


def test_guard_never_raises_on_odd_inputs():
    for bad in (None,
                _kl("HassLightSet", None, None),
                _kl("HassLightSet", {"entity_id": "点号都没有"}, "窗速度"),
                _kl("HassLightSet", {"entity_id": "light."}, "窗速度")):
        select_primary_plan(None, bad)          # 只求永不抛（返回值不约束）


# ── ② executor HassLightSet 双闸 ────────────────────────────────
def test_light_set_non_light_domain_falls_back_to_intent_channel():
    ex = Executor(None)
    assert ex._klar_direct("HassLightSet",
                           {"entity_id": "switch.chuang", "brightness": "30"}) is None


def test_light_set_percent_brightness_maps_to_pct():
    ex = Executor(None)
    d, s, data = ex._klar_direct("HassLightSet",
                                 {"entity_id": "light.x", "brightness": "80%"})
    assert (d, s) == ("light", "turn_on")
    assert data == {"entity_id": "light.x", "brightness_pct": 80.0}


def test_light_set_out_of_percent_range_passthrough():
    """0–100 之外（可能已是 0–255 形态）不改写，留给 HA schema 如实裁决。"""
    ex = Executor(None)
    _, _, data = ex._klar_direct("HassLightSet", {"entity_id": "light.x", "brightness": 150})
    assert data == {"entity_id": "light.x", "brightness": 150}
    _, _, data2 = ex._klar_direct("HassLightSet", {"entity_id": "light.x", "color": "红"})
    assert data2 == {"entity_id": "light.x", "color_name": "红"}


# ── ③ 云 TTS 失败钉扎 ───────────────────────────────────────────
class S:
    def __init__(self, d=None):
        self.d = {"tts.provider": "cloud_openai_compat"}
        self.d.update(d or {})

    def get(self, k, dv=None):
        return self.d.get(k, dv)


def _engine(**sd):
    eng = TtsEngine(S(sd), type("MS", (), {})())
    eng._tts = object()                       # 视为已加载（离线钉桩口径）
    eng._synth = lambda sent, sid, speed: b"\x00\x00" * 480
    eng._encode = lambda pcm: [FRAME]
    return eng


def _collect(eng, text="好的，灯打开了。"):
    out, eng_out = [], {}
    async def go():
        async for pkt in eng.stream_opus(text, engine_out=eng_out):
            out.append(pkt)
    asyncio.run(go())
    return out, eng_out


def _cloud_failing(eng, fail_first=1):
    """把 _cloud_stream 换成"前 fail_first 次抛错、之后成功"的云替身。"""
    eng._cloud_attempts = 0

    async def fake(text):
        eng._cloud_attempts += 1
        if eng._cloud_attempts <= fail_first:
            raise RuntimeError("connect refused")
        yield b"CLOUD-PKT"
    eng._cloud_stream = fake


def test_cloud_failure_pins_local_for_following_turns():
    eng = _engine()
    _cloud_failing(eng)
    out1, e1 = _collect(eng, "第一句。")
    assert out1 == [FRAME] and "云回落" in e1["engine"]          # 本轮仍出声（回落）
    assert eng._cloud_fail_ts > 0
    out2, e2 = _collect(eng, "第二句。")
    assert out2 == [FRAME] and "云钉扎" in e2["engine"]           # 钉扎窗口：直接本地
    assert eng._cloud_attempts == 1                               # **没有**再试云=不换嗓不白等
    eng._cloud_fail_ts = time.monotonic() - _CLOUD_PIN_S - 1      # 冷却到期
    out3, e3 = _collect(eng, "第三句。")
    assert eng._cloud_attempts == 2                               # 自动放一行试云
    assert out3 == [b"CLOUD-PKT"] and eng._cloud_fail_ts == 0.0   # 云恢复→解除


def test_cloud_midstream_failure_does_not_stitch_local_voice():
    eng = _engine()
    async def half_stream(text):
        yield b"CLOUD-PKT"
        eng._cloud_fail_ts = 0.0
        raise RuntimeError("stream reset")
    eng._cloud_stream = half_stream
    out, e = _collect(eng)
    assert out == [b"CLOUD-PKT"]                                  # 半途断：不再拼本地嗓
    assert eng._cloud_fail_ts > 0                                 # 且开启钉扎


def test_local_provider_unaffected_by_pin():
    eng = _engine(**{"tts.provider": "local_kokoro"})
    eng._cloud_fail_ts = time.monotonic()                          # 即使云窗口开着
    out, e = _collect(eng)
    assert out == [FRAME] and "云" not in e["engine"]              # 本地档无回落叙事


def test_pin_logs_are_warn_level(caplog):
    """现场日志级=INFO：钉扎/断流必须 WARN 可见（诊断日志级别铁律）。"""
    eng = _engine()
    _cloud_failing(eng)
    with caplog.at_level(logging.INFO):
        _collect(eng, "来一句。")
        _collect(eng, "再来一句。")
    assert any(r.levelno == logging.WARNING and "钉扎" in r.message
               for r in caplog.records)
    assert any("云钉扎中" in r.message for r in caplog.records)
