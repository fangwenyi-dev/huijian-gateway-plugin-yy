"""LLM 档（默认关闭；开启需用户在 Web UI 填 OpenAI 兼容端点）。

v4.1 定案：4C8G 无可用本地 LLM → llm.enabled 默认 False；云端填 base_url/api_key/model
（百炼/火山方舟[慧尖 SFT 模板]/LAN ollama 皆可）。工具面 = huijian_ai 14 意图 +
HA 内置快捷意图的 function-calling 表（与 custom_llm_api 的 15 tools 同构，schema 按
《语音集成源码盘点》§2 意图注册表逐条构造）。工具调用经 Executor 真实执行，
每轮把执行结果回喂模型；最多 max_tool_rounds 轮。
LLM 输出仅进 TTS/屏显文本，不再生成音频（协议 §1.4：LLM 通道禁 binary）。
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

import aiohttp

from . import const

logger = logging.getLogger("huijian.agent")

_TARGET_SCHEMA = {
    "type": "array",
    "description": "目标列表，每项 {area: 区域名, devices: [{name: 设备名(不带区域), domains: [light/cover/climate/fan/...]}]}；全屋则该项省略 devices",
    "items": {"type": "object", "properties": {
        "area": {"type": "string"},
        "devices": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "domains": {"type": "array", "items": {"type": "string"}}}}}}},
}

TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "TurnDeviceOn", "description": "打开设备", "parameters": {
            "type": "object", "properties": {"target": _TARGET_SCHEMA}, "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "TurnDeviceOff", "description": "关闭设备", "parameters": {
            "type": "object", "properties": {"target": _TARGET_SCHEMA}, "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "ControlWindow", "description": "控制窗户：open 开 / close 关 / pause 暂停 / a 内倒",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["open", "close", "pause", "a"]},
            "target": _TARGET_SCHEMA}, "required": ["action", "target"]}}},
    {"type": "function", "function": {
        "name": "AdjustDeviceAttribute", "description": "调节设备属性", "parameters": {
            "type": "object", "properties": {
                "attribute": {"type": "string", "enum": ["brightness", "colour_temperature", "temperature", "fan_speed", "position"]},
                "delta": {"type": "string", "description": "绝对值 '50' / 相对 '+10','-20' / max / min"},
                "target": _TARGET_SCHEMA}, "required": ["attribute", "delta"]}}},
    {"type": "function", "function": {
        "name": "SetDeviceMode", "description": "设置设备模式", "parameters": {
            "type": "object", "properties": {
                "mode": {"type": "string"}, "target": _TARGET_SCHEMA}, "required": ["mode"]}}},
    {"type": "function", "function": {
        "name": "huijianGetLiveContext", "description": "查询所有设备实时状态（回答状态类问题的第一工具）",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "HassTriggerVoiceScene", "description": "触发已创建的语音场景", "parameters": {
            "type": "object", "properties": {"trigger_phrase": {"type": "string"}}, "required": ["trigger_phrase"]}}},
    {"type": "function", "function": {
        "name": "HassCreateVoiceScene", "description": "创建语音场景（trigger_phrase + actions 列表）", "parameters": {
            "type": "object", "properties": {
                "trigger_phrase": {"type": "string"},
                "actions": {"type": "array", "items": {"type": "object"},
                            "description": "每动作 {domain,service,data}"}},
            "required": ["trigger_phrase", "actions"]}}},
    {"type": "function", "function": {
        "name": "HassDeleteVoiceScene", "description": "删除语音场景", "parameters": {
            "type": "object", "properties": {"trigger_phrase": {"type": "string"}}, "required": ["trigger_phrase"]}}},
    {"type": "function", "function": {
        "name": "HassListVoiceScenes", "description": "列出语音场景", "parameters": {"type": "object", "properties": {}}}},
]

SYSTEM_PROMPT = (
    "你是慧尖智能家居语音助手。规则：1) 控制设备必须调用工具，不要凭空声称已完成；"
    "2) 设备状态问题先调用 huijianGetLiveContext 再回答；3) 最终回答是口播短句，"
    "不超过两句话，不要用列表和 Markdown；4) 没有对应设备或工具失败时如实告知。"
    "5) 用户报出的房间/设备若不在设备清单内，先反问确认，不要臆测执行。"
)


class Agent:
    def __init__(self, settings, ha, executor):
        self.settings = settings
        self.ha = ha
        self.executor = executor
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("llm.enabled") and self.settings.get("llm.base_url"))

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45, connect=8))
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _device_brief(self, limit: int = 60) -> str:
        """把 HA 实体清单压成一行一设备的简报（喂 system 尾部，控制 token 量）。"""
        states = await self.ha.states()
        lines = []
        for eid, ent in list(states.items()):
            dom = eid.split(".", 1)[0]
            if dom not in ("light", "cover", "climate", "fan", "switch", "media_player",
                           "humidifier", "lock", "vacuum"):
                continue
            attrs = ent.get("attributes") or {}
            area = self.ha._entity_area.get(eid, "")
            lines.append(f"{attrs.get('friendly_name', eid)}[{dom}]{('@' + area) if area else ''}={ent.get('state')}")
            if len(lines) >= limit:
                break
        return "\n".join(lines)

    async def _chat(self, messages: list, tools: bool) -> dict:
        base = str(self.settings.get("llm.base_url", "")).rstrip("/")
        body = {
            "model": self.settings.get("llm.model", ""),
            "messages": messages,
            "temperature": float(self.settings.get("llm.temperature", 0.3)),
        }
        if tools:
            body["tools"] = TOOLS
            body["tool_choice"] = "auto"
        sess = await self._sess()
        headers = {"Content-Type": "application/json"}
        if key := self.settings.get("llm.api_key"):
            headers["Authorization"] = f"Bearer {key}"
        async with sess.post(f"{base}/chat/completions", json=body, headers=headers) as r:
            if r.status != 200:
                text = (await r.text())[:300]
                raise RuntimeError(f"LLM {r.status}: {text}")
            return await r.json()

    async def answer(self, text: str, history: list[dict]) -> AsyncIterator[str]:
        """流式产出最终口播文本（按句 yield）。失败抛异常由 pipeline 兜底。"""
        messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n当前设备清单：\n" + await self._device_brief()}]
        messages += history[-int(self.settings.get("llm.history_rounds", 10)) * 2:]
        messages.append({"role": "user", "content": text})
        max_rounds = int(self.settings.get("llm.max_tool_rounds", 3))
        for _ in range(max_rounds + 1):
            resp = await self._chat(messages, tools=max_rounds > 0)
            choice = (resp.get("choices") or [{}])[0].get("message", {})
            calls = choice.get("tool_calls") or []
            if not calls:
                final = (choice.get("content") or "").strip() or const.FALLBACK_TEXT
                for sent in _sentences(final):
                    yield sent
                return
            messages.append(choice)
            for call in calls:
                fn = (call.get("function") or {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                ok, speech = await self._tool(name, args)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": json.dumps({"success": ok, "speech": speech}, ensure_ascii=False)})
            # 工具回喂后继续（末轮不再给 tools → 强制出文本）
            max_rounds -= 1
        yield const.FALLBACK_TEXT

    async def _tool(self, name: str, args: dict) -> tuple[bool, str]:
        from .nlu.fast_path import Plan
        if name == "huijianGetLiveContext":
            result = await self.ha.handle_intent(name, {})
            raw = json.dumps(result.get("raw", result), ensure_ascii=False)[:2000]
            return bool(result.get("success")), raw
        if name == "HassCreateVoiceScene" and not self.settings.get("llm.allow_scene_write", True):
            return False, "场景创建未开启"
        plan = Plan(intent=name, args=args, source="llm")
        return await self.executor.run(plan)


def _sentences(text: str) -> list[str]:
    parts, buf = [], ""
    for ch in text:
        buf += ch
        if ch in "。！？；!?;\n":
            if buf.strip():
                parts.append(buf.strip())
            buf = ""
    if buf.strip():
        parts.append(buf.strip())
    return parts or ([text.strip()] if text.strip() else [])
