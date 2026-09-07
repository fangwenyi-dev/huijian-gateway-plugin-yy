"""错误话术链回归钉桩（2026-09-11 真机「空括号」事故）。

事故链：客户 HA 内存里跑的是老版 vendor 集成（加载项 boot 落盘了新版但
HA Core 未重启/未 reload）→ 老 handler 无 no-match 封堵，`assert
candidate_entities` 炸未捕获异常 → HTTP 500 纯文本 → ha_client 把 500 洗成
message="" → zh_error("") → 「抱歉，这一步没有执行成功（），可以换个说法再试」
——真栈逐字复现后才定位。本文件钉死三层加固不回退：
① ha_client 5xx 必出结构化中文错误（永不空 message）；
② zh_error 对 No available/内部错误有专属话术（不吐英文、不带空括号）；
③ 落盘集成 handler 无裸 assert（永不抛，失败折叠成 dict）。
"""
import pathlib
import re

from core.ha_client import HAClient
from core.executor import zh_error

_ROOT = pathlib.Path(__file__).resolve().parents[1]


# ── ① ha_client 5xx 结构化 ─────────────────────────────────────
def test_5xx_gets_structured_chinese_message():
    for st in (500, 502, 503):
        r = HAClient._normalize_result(st, "500 Internal Server Error\n\nServer got itself in trouble", "TurnDeviceOn")
        assert r["success"] is False
        assert r["message"] == f"HA 内部错误({st})", f"{st} 又洗成空 message 了"
        assert "（）" not in r["message"] and r["message"].strip()


def test_4xx_message_fallback_untouched():
    r = HAClient._normalize_result(404, "404: Not Found", "X")
    assert r["success"] is False and r["message"]


# ── ② zh_error 专属映射 ────────────────────────────────────────
def test_zh_error_no_available_devices():
    out = zh_error("No available devices found")
    assert "没找到符合条件的设备" in out
    assert "No available" not in out, "英文原文漏进播报"


def test_zh_error_internal_maps_to_restart_hint():
    out = zh_error("HA 内部错误(500)")
    assert "重启" in out and "（）" not in out


def test_zh_error_never_empty_parens_from_pipeline():
    """上游保证 raw 非空；函数层空括号模板仍在（兜底行为不变）。"""
    assert zh_error("") == "抱歉，这一步没有执行成功（），可以换个说法再试"


# ── ③ handler 无裸 assert ──────────────────────────────────────
_HANDLER_FILES = ("intent_turn.py", "intent_adjust_attribute.py", "intent_set_mode.py")


def test_no_bare_assert_in_intent_handlers():
    """assert 是 500 黑盒发生器：加载项侧只能看到空壳错误。防重新引入。"""
    for f in _HANDLER_FILES:
        src = (_ROOT / "custom_components" / "huijian_ai" / f).read_text(encoding="utf-8")
        assert not re.search(r"^\s*assert candidate_entities", src, re.M), \
            f"{f} 复活了裸 assert——未捕获异常=真机空括号话术事故复发路径"
        assert "No available devices found" in src, f"{f} 缺结构化失败返回"
