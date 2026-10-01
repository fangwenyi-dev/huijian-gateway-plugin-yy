# -*- coding: utf-8 -*-
"""动作结果折算（集成侧单点，v1.1.29 复核 A5）。

病灶（本仓同族第 4 处漏网）：`intent_set_mode` 族只回 `{"results":[...]}`（无顶层
success 键），而消费侧一律 `result.get("success") is not False` —— 无键 = None is
not False = 恒真 —— 逐台全失败仍被判成功，场景回放/自动化动作链就会播「已执行场景：X」
（红线：绝不谎报成功）。custom_llm_api.py 的 v1.1.27 批 7 收口了同一形态；本模块把判据
收成一处供两条消费链共用（教训：改返回形态必须 grep 全消费点）。
"""
from __future__ import annotations

# 「判不了」的具名留痕（修③）：判不了**不等于**成功——方向仍按不误伤放行（既有钉），
# 但必须带一句可播的原因，否则「已执行场景」会把"执行面一个字都没回"洗成零问题。
UNVERIFIED_NOTE = "执行面未返回可判结果"


def _err_text(value) -> str:
    """把 error 证据收成**可播文本**。callable 不得 str() 出去（对抗复核实证：
    `str(bound method)` 会变成「有设备没动：<function error at 0x…>」直达播报）。"""
    try:
        if value is None or isinstance(value, bool):
            return ""
        if callable(value):
            value = value()
        if value is None or isinstance(value, bool):
            return ""
        text = str(value).strip()
        return "" if ("bound method" in text or "<function" in text) else text
    except Exception:  # noqa: BLE01 —— 折算永不抛
        return ""


def _row_err(rows) -> str:
    return next((_err_text(r.get("error")) for r in rows
                 if isinstance(r, dict) and not r.get("success")
                 and _err_text(r.get("error"))), "")


def fold_action_ok(result) -> tuple[bool, str]:
    """动作结果 → (ok, err)。判不了=True（不误伤未知但合法的形态）。

    · success 键在场即权威；
    · 逐台结果**三族键名都要读**（`states`＝AdjustDeviceAttribute/Lock 族、
      `results`＝SetDeviceMode 族、行外 `partial_error`＝turn 族窗侧真因）——
      口径与 `core/executor.py:1078-1084`、`:960-969` 严格一致，不另起一套。
      此前只读 `results` ⇒ 前两类部分失败永远静默（对抗复核实证：
      `{'success':True,'states':[{…,'error':'离线'}]}` 折成 `(True,'')`）；
    · 任一台成功=该步可用（方向不改），**但没动的那台必须点名**：ok=True 时
      第二个返回值非空即"部分未生效"的原因，消费侧有义务播出去（修③）；
    · 其余带 error 的 dict ⇒ 采信失败证据（无 error 才放行）；
    · 空 dict ⇒ 判不了，方向不误伤，但带 UNVERIFIED_NOTE 留痕。
    · 对象形态沿用 .success / response_type 语义（真 HA IntentResponse 失败走 ERROR）。
    """
    if isinstance(result, dict):
        raw = result.get("states")
        if not isinstance(raw, list):
            raw = result.get("results")
        has_rows = isinstance(raw, list)          # **空表也算"有这一族"**＝失败
        rows = [r for r in raw if isinstance(r, dict)] if has_rows else []
        pe = _err_text(result.get("partial_error"))
        if "success" in result:
            return (result.get("success") is not False,
                    _err_text(result.get("error")) or pe or _row_err(rows))
        if has_rows:
            ok = any(r.get("success") for r in rows)
            err = pe or _row_err(rows)
            if ok and not err and len(rows) < len(raw):
                err = UNVERIFIED_NOTE        # 行里混着判不了的东西：留痕，别当全绿
            return (ok, err or ("" if ok else "执行面未返回任何结果"))
        if pe:
            return (True, pe)                # 无 success/无行，只有部分失败证据
        err = _err_text(result.get("error"))
        if not result:
            return (True, UNVERIFIED_NOTE)
        return (not err, err)
    succ = getattr(result, "success", None)
    if succ is None:
        rtype = getattr(result, "response_type", None)
        err = _err_text(getattr(result, "error_code", ""))
        if rtype is None:
            return (True, err)          # 非标准对象：不误伤（同 custom_llm_api 留痕口径）
        return (str(getattr(rtype, "name", "") or "").upper() != "ERROR", err)
    return (succ is not False, _err_text(getattr(result, "error")))
