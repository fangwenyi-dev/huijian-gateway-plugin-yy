# -*- coding: utf-8 -*-
"""v1.0.96 播报门控真值表钉（tests/test_v1096_announce_gate.py）

存在理由：VM 台架案「API 音频播报 0 字节」三天查不动——旧门控是一条布尔链，
任一条件不满足就**静默**走旧 URL 形态（对 API 音频设备＝必然 90s 首包超时拆流），
日志里连"卡在哪道门"都没有。v1.0.96 把门控收进纯函数 `_announce_gate`：
不走自合成的每一条路必须带回具名分因，由调用方 WARN 落盘。

本文件用 exec 提真身跑**真值表**（不是源码字符串复读）：谁把哪道门改回
静默/反转语义，这里当场红；另钉接线三件套（分因 WARN、device_info 竞态回退、
真喇叭设备零噪音）。
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "custom_components" / "huijian_ai" / "assist_satellite.py"


def _load_gate():
    src = SRC.read_text(encoding="utf-8")
    m = re.search(r"^def _announce_gate\(.*?(?=^def |\Z)", src, re.M | re.S)
    assert m, "_announce_gate 纯函数被删/改名——门控若回到内联布尔链=静默案回潮"
    ns: dict = {}
    exec(compile(m.group(0), "<_announce_gate>", "exec"), ns)  # noqa: S102 提纯函数
    return ns["_announce_gate"]


gate = _load_gate()


# ── 真值表 ─────────────────────────────────────────────────────────────
def test_speaker_device_silent_passthrough():
    """真喇叭设备（api_audio=False）：不走自合成、**不产分因**（旧路一字不动，无日志噪音）。"""
    take, skip = gate(api_audio=False, has_message=True, preannounce=False,
                      pipeline_busy=False)
    assert (take, skip) == (False, "")


def test_api_audio_no_message_named():
    take, skip = gate(api_audio=True, has_message=False, preannounce=False,
                      pipeline_busy=False)
    assert take is False
    assert skip and "无message" in skip, "API 音频设备无 media 自取能力，必须具名"


def test_api_audio_preannounce_gate_named():
    take, skip = gate(api_audio=True, has_message=True, preannounce=True,
                      pipeline_busy=False)
    assert take is False and skip and "preannounce" in skip


def test_api_audio_pipeline_busy_guard_kept():
    """护栏②（v1.0.93 活跃轮不抢下行）语义不变——但从此**有名有姓**。"""
    take, skip = gate(api_audio=True, has_message=True, preannounce=False,
                      pipeline_busy=True)
    assert take is False and skip and "活跃轮" in skip


def test_api_audio_idle_takes_selfsynth():
    take, skip = gate(api_audio=True, has_message=True, preannounce=False,
                      pipeline_busy=False)
    assert (take, skip) == (True, "")


def test_keyword_only_signature_pinned():
    """参数必须具名——布尔串位=门控语义漂移的最便宜温床。"""
    with pytest.raises(TypeError):
        gate(True, True, False, False)


# ── 接线钉（门控之外的三件必须活着的事）───────────────────────────────
def test_caller_warns_every_skip():
    src = SRC.read_text(encoding="utf-8")
    i = src.index("taken, skip = _announce_gate(")
    # 窗口改**锚到锚**（旧版是 +1400 定长，v1.0.99 加救援块时已从 900 撑到 1400，
    # v1.1.14 请求先行块一加就爆窗）：定长窗口的失败方式是"实现没错、钉先红"，
    # 而那种红会被当成改动有问题。锚点=门控起、本函数收口点（_revoke_announce_stream）
    # 止——用它而非本次新加的变量名，免得两条钉互相绑死、改名即误红。
    seg = src[i:src.index("_revoke_announce_stream(", i)]
    assert "if skip:" in seg, "分因必须进 WARN——静默跳过回潮"
    assert '"[Announce] 播报未走文本自合成推流' in seg
    assert "elif taken:" in seg, "命中门→引擎块"


def test_announce_request_goes_on_wire_before_stream():
    """A 修（2026-09-26 台架定位）：播报请求必须**先于**推流上线路。

    旧次序"先起推流任务→再 await 请求"下，HA 会先把 socket 灌满 ~50,176B
    （=1.568s @16k/16bit）再等请求落位；设备从第一帧就开始出声，请求一旦晚于
    1.57s 到达，头部缓冲已被放干 → 播报开头约 1.5s 处一顿。同句连播 8 次实测：
    唯一 delay=1820ms 的那次卡，其余 580~860ms 全顺（gapmax 与卡不卡无关）。
    判据取**次序**而非注释/常量，并把"不许退回直接 await"钉成反向。
    """
    src = SRC.read_text(encoding="utf-8")
    i_req = src.index("req_task = self.config_entry.async_create_background_task(")
    i_stream = src.index("self._stream_tts_audio(tts_stream", i_req)
    assert i_req < i_stream, "请求任务必须先于推流任务创建（否则头部有 1.57s 真空窗）"
    blk = src[i_req:src.index("\n        )", i_req)]
    assert "send_voice_assistant_announcement_await_response" in blk, "请求任务本体必须就是它"
    assert "preannounce_media_id=preannounce_media_id or \"\"" in blk, \
        "请求必须读**救援后**的 preannounce（弃前置音的裁决不得被旧值覆盖）"
    # 反向钉：旧的"就地 await 直调"形态不得回来（那等于把顺序又排回推流之后）
    assert "await self.cli.send_voice_assistant_announcement_await_response" not in src, \
        "退回就地 await = 请求又排在推流之后，本缺陷原样复发"
    assert "await req_task" in src, "请求任务必须仍被 await（收口点与异常传播不变）"


def test_device_info_race_fallback_wired():
    """VM 案形态：reload/重连窗 device_info 竞态缺失曾一票否决→静默哑播。
    现以「无 UDP 通道 + API 版本已协商」次级判据接管（慧尖客户群唯一形态）。"""
    src = SRC.read_text(encoding="utf-8")
    seg = src[src.index("v1.0.96：device_info 竞态缺失"):]
    seg = seg[:seg.index("taken, skip =")]
    assert "self._entry_data.device_info is None" in seg
    assert "self._udp_server is None" in seg
    assert "self._entry_data.api_version" in seg
    assert "api_audio_only = True" in seg


def test_guard2_old_semantics_not_regressed():
    """v1.0.93 护栏②原句不回退：活跃 pipeline 轮绝不抢下行——语义搬进真值表，
    源码里若有人重新内联 `and not self._entry_data.assist_pipeline_state` 到
    if 链上（绕开分因入账），本钉不红但真值表钉仍红——双保险缺一不可。"""
    src = SRC.read_text(encoding="utf-8")
    assert re.search(
        r"pipeline_busy=bool\(\s*self\._entry_data\.assist_pipeline_state\)\)", src
    ), "pipeline_busy 参数断线"


if __name__ == "__main__":
    sys_fail = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  [✓] {name}")
            except Exception as e:  # noqa: BLE001
                sys_fail += 1
                print(f"  [✗] {name}: {e}")
    print("v1096 门控真值表：", "全绿" if sys_fail == 0 else f"{sys_fail} 红")
    raise SystemExit(1 if sys_fail else 0)
