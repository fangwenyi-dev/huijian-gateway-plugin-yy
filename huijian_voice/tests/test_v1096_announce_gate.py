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
    # v1.0.99：gate 与 if skip 之间多了 preannounce 救援块（窗口 900→1400）；
    # 救援自身语义由 tests/test_v1099_preannounce_rescue.py 钉，本钉只守分因必 WARN。
    seg = src[i:i + 1400]
    assert "if skip:" in seg, "分因必须进 WARN——静默跳过回潮"
    assert '"[Announce] 播报未走文本自合成推流' in seg
    assert "elif taken:" in seg, "命中门→引擎块"


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
