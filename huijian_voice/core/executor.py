"""执行层：Plan → POST /api/intent/handle → 中文话术。

话术映射依据《语音集成源码盘点》§3.1/§4 与收编 _friendly_text：
- huijian_ai handler 返回裸 dict：{success, control_targets:[{name,area}]} /
  {success, message} / {success:False, error:"英文"}（窗口抽取失败等）。
- 锁语义反转修复（D7 加载项侧兜底）：控制目标名词含「锁」时，TurnDeviceOn 播报
  「已上锁」、TurnDeviceOff 播报「已解锁」（handler 无 domain 字段，取设备名启发）。
- 英文 error 中文化（D5）：常见失败短语映射，未知失败给可复述的通用短句。
"""
from __future__ import annotations

import logging
from typing import Optional

from .nlu.fast_path import Plan

logger = logging.getLogger("huijian.executor")

ACT_CN = {"open": "打开", "close": "关闭", "pause": "暂停", "a": "内倒", "tilt": "内倒"}
ATTR_CN = {"brightness": "亮度", "colour_temperature": "色温", "color_temperature": "色温",
           "temperature": "温度", "fan_speed": "风量", "position": "开合度"}
MODE_CN = {"heat": "制热", "cool": "制冷", "dry": "除湿", "fan_only": "送风", "auto": "自动",
           "eco": "节能", "sleep": "睡眠", "offline": "关闭"}
_EN_ERR_MAP = [
    ("could not extract window name", "没找到要控制的窗户，试试说「客厅的窗户内倒」"),
    ("window control failed", "窗户控制没成功，可能窗户没在 HA 里配好"),
    ("no.*match", "没找到符合条件的设备"),
    ("not found", "没找到这个设备"),
    ("entity", "设备清单里没匹配到，请换个叫法试试"),
    ("timeout", "执行超时了，请再试一次"),
    ("unauthorized", "HA 令牌权限不足，请在加载项检查集成安装"),
]


def zh_error(raw: str) -> str:
    low = (raw or "").lower()
    for key, zh in _EN_ERR_MAP:
        if key in low:
            return f"抱歉，{zh}"
    return f"抱歉，这一步没有执行成功（{raw[:30]}），可以换个说法再试"


class Executor:
    def __init__(self, ha, settings=None):
        self.ha = ha
        self.settings = settings

    async def run(self, plan: Plan) -> tuple[bool, str]:
        """执行 Plan。返回 (success, 中文播报)。永不抛。"""
        result = await self.ha.handle_intent(plan.intent, plan.args)
        ok = bool(result.get("success"))
        reply = self.speech(plan, result) if ok else zh_error(str(result.get("error") or result.get("message") or ""))
        logger.info("[执行] %s %s → %s | %s", plan.intent, plan.args, "成功" if ok else "失败", reply)
        return ok, reply

    # ── 话术生成 ────────────────────────────────────────────────
    def speech(self, plan: Plan, result: dict) -> str:
        args = plan.args
        intent = plan.intent
        # 场景
        if intent == "HassTriggerVoiceScene":
            msg = result.get("message")
            if msg and any("\u4e00" <= c <= "\u9fff" for c in msg):
                return msg if msg.endswith(("。", "！", "!", "了")) else msg + "了"
            name = getattr(plan, "scene_name", None) or args.get("trigger_phrase", "场景")
            return f"好的，{name}场景已执行"
        if intent == "HassCreateVoiceScene":
            return "好的，场景已创建"
        if intent == "HassDeleteVoiceScene":
            return "好的，场景已删除"
        if intent == "HassListVoiceScenes":
            scenes = result.get("scenes") or []
            if not scenes:
                return "你还没有创建过语音场景"
            names = "、".join((s.get("name") or s.get("trigger_phrase", "")) for s in scenes[:6])
            return f"目前有这些场景：{names}"
        # 空调温度直改（HA 内置意图改道）
        if intent == "HassClimateSetTemperature":
            t = args.get("temperature")
            area = args.get("area") or ""
            return f"好的，{area}空调已调到{t}度"
        if intent == "HassGetCurrentTime":
            return result.get("speech", {}).get("plain", {}).get("output", "") or "好的"
        # control_targets 族（TurnDeviceOn/Off、ControlWindow、AdjustDeviceAttribute、SetDeviceMode）
        targets = result.get("control_targets") or []
        if targets:
            return self._targets_speech(plan, targets)
        if (result.get("message") or "").strip():
            msg = str(result["message"]).strip()
            return msg if any("\u4e00" <= c <= "\u9fff" for c in msg) else zh_error(msg)
        if result.get("states"):
            names = [s.get("name", "") for s in result["states"] if s.get("success")]
            return f"好的，{'、'.join(names) if names else '设备'}已处理"
        if "success_count" in result:
            return "好的，已执行"
        return "好的"

    def _targets_speech(self, plan: Plan, targets: list) -> str:
        names = "、".join([t.get("name", "") for t in targets if t.get("name")]) or "设备"
        areas = [t.get("area", "") for t in targets if t.get("area")]
        area = areas[0] if areas else ""
        head = f"{area}的" if area else ""
        args = plan.args
        intent = plan.intent
        # 锁语义反转（D7 兜底）：目标名含「锁」
        if any("锁" in (t.get("name") or "") for t in targets):
            if intent == "TurnDeviceOn":
                return f"好的，{head}{names}已上锁"
            if intent == "TurnDeviceOff":
                return f"好的，{head}{names}已解锁"
        if intent == "TurnDeviceOn":
            return f"好的，{head}{names}打开了"
        if intent == "TurnDeviceOff":
            return f"好的，{head}{names}关了"
        if intent == "ControlWindow":
            act = ACT_CN.get(str(args.get("action", "")).lower(), "调节")
            return f"好的，{head}{names}已{act}"
        if intent == "AdjustDeviceAttribute":
            attr = ATTR_CN.get(args.get("attribute", ""), args.get("attribute", ""))
            delta = str(args.get("delta", ""))
            if delta.startswith("+") or delta.startswith("-"):
                up = delta.startswith("+")
                verb = {"brightness": ("调亮", "调暗"), "fan_speed": ("调大", "调小"),
                        "temperature": ("调高", "调低"), "position": ("开大", "关小"),
                        "color_temperature": ("调冷", "调暖")}.get(args.get("attribute"), ("调高", "调低"))
                return f"好的，{head}{names}的{attr}{verb[0] if up else verb[1]}了" if attr else f"好的，已调节{names}"
            if args.get("attribute") == "temperature":
                return f"好的，{head}{names}温度调到{delta}度了"
            unit = "%" if args.get("attribute") in ("brightness", "position") else ("K" if args.get("attribute") == "color_temperature" else "档")
            return f"好的，{head}{names}的{attr}已设为{delta}{unit if attr != '色温' else ''}".replace("档档", "档")
        if intent == "SetDeviceMode":
            mode = MODE_CN.get(args.get("mode", ""), args.get("mode", ""))
            return f"好的，{head}{names}已切到{mode}模式"
        # 未知成功
        return f"好的，{head}{names}已处理"
