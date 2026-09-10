"""触发条件判定（纯函数，零 HA 依赖——加载项 tests/test_trigger_eval.py 直测）。

v1.0.30 对 060401 自动化引擎的两处优化收口：
  1. 旧引擎只有数值穿越判定（`float(state)` 失败恒 continue）——人体感应
     这类 on/off 二值状态自动化**永远不触发**（HassCreateAutomation 描述里
     明明写着「检测到有人」，实现跟不上承诺）。补 `to` 状态等值判定。
  2. 完全不支持时间触发（"每天早上7点开窗帘"在旧 SFT 集被折成死场景）。
     补 `at="HH:MM"` 每天形态，可选 days(ISO 1-7) 限定周几。

穿越语义保持旧版：state_changed 事件层已过滤 old==new，本层只判"新值是否
满足条件"；above/below 严格不等号（等于不触发，与 HA automation numeric_state
进沿语义一致）；无 above/below/to 的纯实体监控=任意变化触发（旧语义）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def state_trigger_met(trigger: dict[str, Any], new_state: Any) -> bool:
    """实体状态触发判定。to→等值命中；above/below→数值穿越；皆无→任意变化即中。

    非数值状态且未配 to → False（与旧引擎 float 失败 continue 等义）。
    永不抛。"""
    try:
        to = trigger.get("to")
        if to is not None:
            return str(new_state) == str(to)
        try:
            value = float(new_state)
        except (TypeError, ValueError):
            return False
        above, below = trigger.get("above"), trigger.get("below")
        if above is None and below is None:
            return True                      # 纯实体监控：任意变化即触发（旧语义）
        if above is not None and value <= float(above):
            return False
        if below is not None and value >= float(below):
            return False
        return True
    except Exception:                        # 判定层铁律：任何脏数据不外抛
        return False


def time_trigger_due(trigger: dict[str, Any], now: datetime,
                     last_fired: Optional[str] = None) -> bool:
    """时间触发：at="HH:MM" 当天该分钟命中一次；days=[1..7] 可限定周几。

    last_fired（ISO 串）同分钟去重：HA time_change 每秒 :00 只回调一次，
    但 reload/重启竞态下同分钟可能连进——日期+HH:MM 相等即拒。永不抛。"""
    try:
        at = str(trigger.get("at") or "").strip()
        if not at:
            return False
        if now.strftime("%H:%M") != at:
            return False
        days = trigger.get("days")
        if days and now.isoweekday() not in {int(d) for d in days}:
            return False
        if last_fired:
            try:
                lf = datetime.fromisoformat(str(last_fired))
            except (TypeError, ValueError):
                lf = None                      # 脏时间戳不拦触发（旧引擎无此闸）
            if lf is not None and \
                    lf.strftime("%Y-%m-%d %H:%M") == now.strftime("%Y-%m-%d %H:%M"):
                return False
        return True
    except Exception:
        return False
