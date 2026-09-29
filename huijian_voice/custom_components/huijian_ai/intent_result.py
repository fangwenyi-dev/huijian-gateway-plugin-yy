# -*- coding: utf-8 -*-
"""动作结果折算（集成侧单点，v1.1.29 复核 A5）。

病灶（本仓同族第 4 处漏网）：`intent_set_mode` 族只回 `{"results":[...]}`（无顶层
success 键），而消费侧一律 `result.get("success") is not False` —— 无键 = None is
not False = 恒真 —— 逐台全失败仍被判成功，场景回放/自动化动作链就会播「已执行场景：X」
（红线：绝不谎报成功）。custom_llm_api.py 的 v1.1.27 批 7 收口了同一形态；本模块把判据
收成一处供两条消费链共用（教训：改返回形态必须 grep 全消费点）。
"""
from __future__ import annotations


def fold_action_ok(result) -> tuple[bool, str]:
    """动作结果 → (ok, err)。判不了=True（不误伤未知但合法的形态）。

    · success 键在场即权威；
    · 只回 results 列表 ⇒ 逐台折算（任一台成功=该步可用；空表=失败）；
    · 其余带 error 的 dict ⇒ 采信失败证据（无 error 才放行）；
    · 对象形态沿用 .success / response_type 语义（真 HA IntentResponse 失败走 ERROR）。
    """
    if isinstance(result, dict):
        if "success" in result:
            return (result.get("success") is not False, str(result.get("error") or ""))
        if isinstance(result.get("results"), list):
            rows = [r for r in result["results"] if isinstance(r, dict)]
            ok = any(r.get("success") for r in rows)
            err = next((str(r.get("error")) for r in rows
                        if not r.get("success") and r.get("error")), "")
            return (ok, err or ("" if ok else "执行面未返回任何结果"))
        err = str(result.get("error") or "")
        return (not err, err)
    succ = getattr(result, "success", None)
    if succ is None:
        rtype = getattr(result, "response_type", None)
        err = str(getattr(result, "error_code", "") or "")
        if rtype is None:
            return (True, err)          # 非标准对象：不误伤（同 custom_llm_api 留痕口径）
        return (str(getattr(rtype, "name", "") or "").upper() != "ERROR", err)
    return (succ is not False, str(getattr(result, "error", "") or ""))
