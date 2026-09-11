"""LLM 档（默认关闭；开启需用户在 Web UI 填 OpenAI 兼容端点）。

v4.1 定案：4C8G 无可用本地 LLM → llm.enabled 默认 False；云端填 base_url/api_key/model
（百炼/火山方舟[慧尖 SFT 模板]/LAN ollama 皆可）。工具面 = huijian_ai 14 意图 +
HA 内置快捷意图的 function-calling 表（与 custom_llm_api 的 15 tools 同构，schema 按
《语音集成源码盘点》§2 意图注册表逐条构造）。工具调用经 Executor 真实执行，
每轮把执行结果回喂模型；最多 max_tool_rounds 轮。
LLM 输出仅进 TTS/屏显文本，不再生成音频（协议 §1.4：LLM 通道禁 binary）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import AsyncIterator, Optional

import aiohttp

from . import const

logger = logging.getLogger("huijian.agent")

# 流式句读（P2-15）：与 _sentences 同一终止符集，增量切句用
_SENT_END = re.compile(r"[。！？；!?;\n]")
_ROUND_END = object()          # 单轮句子队列的收束哨兵

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
        # v1.0.42 家电族：暂停运行中的设备（扫地机器人/电视音响/窗帘停走）。
        "name": "PauseDevice", "description": "暂停正在运行的设备：扫地机器人暂停清扫、电视/音箱暂停播放、窗帘停止移动",
        "parameters": {
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
        "name": "HassCreateVoiceScene", "description": (
            "创建语音场景（「当我说X就Y」句式）。actions 每项固定 {intent, params}，"
            "intent ∈ TurnDeviceOn/TurnDeviceOff/ControlWindow/AdjustDeviceAttribute/"
            "SetDeviceMode，params 与该意图直接下令的槽位一致"), "parameters": {
            "type": "object", "properties": {
                "trigger_phrase": {"type": "string"},
                "actions": {"type": "array", "items": {"type": "object"},
                            "description": "每动作 {intent, params}"}},
            "required": ["trigger_phrase", "actions"]}}},
    {"type": "function", "function": {
        "name": "HassDeleteVoiceScene", "description": "删除语音场景", "parameters": {
            "type": "object", "properties": {"trigger_phrase": {"type": "string"}}, "required": ["trigger_phrase"]}}},
    {"type": "function", "function": {
        "name": "HassListVoiceScenes", "description": "列出语音场景", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "HassCreateAutomation", "description": (
            "创建语音自动化（「当[事件]就[动作]」事件型触发，区别于「当我说X」的语音场景）。"
            "传感器阈值 trigger={entity_id:客厅温度, above:28}（低于用 below）；"
            "人体状态 trigger={entity_id:书房人体, to:'on'或'off'}；"
            "每天定时 trigger={at:'07:30'}（24小时制 HH:MM）。"
            "actions 每项 {intent, params}，intent 同设备控制五类"), "parameters": {
            "type": "object", "properties": {
                "trigger": {"type": "object", "properties": {
                    "entity_id": {"type": "string"},
                    "above": {"type": "number"}, "below": {"type": "number"},
                    "to": {"type": "string"}, "at": {"type": "string"}}},
                "actions": {"type": "array", "items": {"type": "object"},
                            "description": "每动作 {intent, params}"}},
            "required": ["trigger", "actions"]}}},
    {"type": "function", "function": {
        "name": "HassListAutomations", "description": "列出语音自动化（返回 automation_id 供删除/修改）",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "HassDeleteAutomation", "description": "删除语音自动化", "parameters": {
            "type": "object", "properties": {"automation_id": {"type": "string"}},
            "required": ["automation_id"]}}},
    {"type": "function", "function": {
        "name": "HassUpdateAutomation", "description": "修改语音自动化（trigger 或 actions 给其一）", "parameters": {
            "type": "object", "properties": {"automation_id": {"type": "string"},
                "trigger": {"type": "object"}, "actions": {"type": "array", "items": {"type": "object"}}},
            "required": ["automation_id"]}}},
]

# 场景/自动化写入类工具（本地级联已零 LLM 全覆盖，LLM 只是兜底，所以写权限
# 必须显式开关——默认口径见 settings.DEFAULTS：场景放行、自动化关）。
_SCENE_WRITE_TOOLS = frozenset({
    "HassCreateVoiceScene", "HassDeleteVoiceScene",
})
_AUTOMATION_WRITE_TOOLS = frozenset({
    "HassCreateAutomation", "HassUpdateAutomation", "HassDeleteAutomation",
})

SYSTEM_PROMPT = (
    "你是慧尖智能家居语音助手。规则：1) 控制设备必须调用工具，不要凭空声称已完成；"
    "2) 设备状态问题先调用 huijianGetLiveContext 再回答；3) 最终回答是口播短句，"
    "不超过两句话，不要用列表和 Markdown；4) 没有对应设备或工具失败时如实告知。"
    "5) 用户报出的房间/设备若不在设备清单内，先反问确认，不要臆测执行。"
    "6) 「当我说X就Y」用 HassCreateVoiceScene；「当传感器/温度/时间到条件就Y」用"
    "HassCreateAutomation；两者的 actions 一律 {intent, params} 形态。"
)


# v1.0.41 安全（审查 S2 第一层）：LLM 回吐的工具名不可信——模型可被话术/friendly_names
# 诱导吐出含 `../` 的名字，而下游 legacy 回落把名字裸拼进 URL 路径（yarl 归一化
# dot-segment → 携 Supervisor 全权 token 可打任意 HA REST 写端点）。白名单唯一真源=
# 工具 schema 本身（TOOLS 增删自动同步，单点维护）。
_TOOL_NAMES = frozenset(
    t["function"]["name"] for t in TOOLS
    if isinstance(t, dict) and isinstance(t.get("function"), dict)
    and isinstance(t["function"].get("name"), str))


class Agent:
    def __init__(self, settings, ha, executor):
        self.settings = settings
        self.ha = ha
        self.executor = executor
        self._session: Optional[aiohttp.ClientSession] = None
        self._no_stream = False        # 平台不认 SSE 一次即 latch（本实例不再试）

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

    def _req(self, messages: list, tools: bool, stream: bool) -> tuple[str, dict, dict]:
        base = str(self.settings.get("llm.base_url", "")).rstrip("/")
        body = {
            "model": self.settings.get("llm.model", ""),
            "messages": messages,
            "temperature": float(self.settings.get("llm.temperature", 0.3)),
        }
        if tools:
            body["tools"] = TOOLS
            body["tool_choice"] = "auto"
        if stream:
            body["stream"] = True
        headers = {"Content-Type": "application/json"}
        if key := self.settings.get("llm.api_key"):
            headers["Authorization"] = f"Bearer {key}"
        return f"{base}/chat/completions", body, headers

    async def _chat(self, messages: list, tools: bool) -> dict:
        url, body, headers = self._req(messages, tools, stream=False)
        sess = await self._sess()
        async with sess.post(url, json=body, headers=headers) as r:
            if r.status != 200:
                text = (await r.text())[:300]
                raise RuntimeError(f"LLM {r.status}: {text}")
            return await r.json()

    class StreamUnsupported(Exception):
        """平台不认 stream=true（4xx 或返回非 SSE）——answer 内本回合回退整包。"""

    async def _chat_stream(self, messages: list, tools: bool,
                           emit) -> dict:
        """SSE 增量：句读完整即 emit(sentence)（async 回调）；返回组装的 assistant 消息。
        只兼容 OpenAI 式 `data:{choices:[{delta:{content|tool_calls}}]}` 事件流。"""
        url, body, headers = self._req(messages, tools, stream=True)
        sess = await self._sess()
        content = ""
        calls: dict[int, dict] = {}
        buf = ""
        flushed = 0                          # content 中已 emit 的字符数
        done = False
        async with sess.post(url, json=body, headers=headers) as r:
            ctype = r.headers.get("Content-Type", "")
            if r.status != 200:
                raise Agent.StreamUnsupported(f"HTTP {r.status}")
            if "event-stream" not in ctype and "ndjson" not in ctype and "json" in ctype:
                raise Agent.StreamUnsupported(f"非 SSE 响应({ctype[:40]})")
            while not done:
                raw = await r.content.read(4096)
                if not raw:
                    break
                buf += raw.decode("utf-8", "ignore")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        done = True
                        break
                    try:
                        ev = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    ch = (ev.get("choices") or [{}])[0]
                    delta = ch.get("delta") or {}
                    if delta.get("content"):
                        content += delta["content"]
                        # 增量句切：到达终止标点即 emit（半句留在 buffer 等下轮）
                        while True:
                            m = _SENT_END.search(content, flushed)
                            if not m:
                                break
                            seg = content[flushed:m.end()].strip()
                            flushed = m.end()
                            if seg:
                                await emit(seg)
                    for tc in delta.get("tool_calls") or []:
                        idx = int(tc.get("index") or 0)
                        slot = calls.setdefault(idx, {"id": "", "type": "function",
                                                      "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]
        tail = content[flushed:].strip()
        if tail:
            await emit(tail)
        return {"role": "assistant",
                "content": content or None,
                **({"tool_calls": [calls[k] for k in sorted(calls)]} if calls else {})}

    async def _one_round(self, messages: list, tools: bool, q: "asyncio.Queue") -> tuple[dict, bool]:
        """单轮对话（流式优先）。文本句随到随入 q；返回 (assistant 消息, 是否已流式送出)。
        StreamUnsupported 只可能在首事件前抛（q 尚空），整轮安全改走非流式并永久 latch。"""
        streamed = False
        try:
            if not self.settings.get("llm.stream", True) or self._no_stream:
                resp = await self._chat(messages, tools=tools)
                msg = (resp.get("choices") or [{}])[0].get("message", {})
            else:
                async def _emit(s: str) -> None:
                    await q.put(s)
                try:
                    msg = await self._chat_stream(messages, tools, _emit)
                    streamed = True
                except Agent.StreamUnsupported as e:
                    logger.warning("[LLM] 平台不支持流式(%s) → 本实例回退整包", str(e)[:80])
                    self._no_stream = True
                    resp = await self._chat(messages, tools=tools)
                    msg = (resp.get("choices") or [{}])[0].get("message", {})
            return msg, streamed
        finally:
            await q.put(_ROUND_END)

    async def answer(self, text: str, history: list[dict]) -> AsyncIterator[str]:
        """流式产出最终口播文本（按句 yield）。失败抛异常由 pipeline 兜底。
        体验批 P2-15：SSE 真流式——最终答案句读一合成即 yield，长答案感知延迟大降。"""
        messages = [{"role": "system", "content": SYSTEM_PROMPT + "\n\n当前设备清单：\n" + await self._device_brief()}]
        messages += history[-int(self.settings.get("llm.history_rounds", 10)) * 2:]
        messages.append({"role": "user", "content": text})
        max_rounds = int(self.settings.get("llm.max_tool_rounds", 3))
        for _ in range(max_rounds + 1):
            q: asyncio.Queue = asyncio.Queue()
            task = asyncio.create_task(self._one_round(messages, max_rounds > 0, q))
            try:
                while True:
                    item = await q.get()
                    if item is _ROUND_END:
                        break
                    yield item
                msg, streamed = await task
            except BaseException:
                task.cancel()
                raise
            calls = msg.get("tool_calls") or []
            if not calls:
                final = (msg.get("content") or "").strip()
                if streamed:
                    if not final:
                        yield const.FALLBACK_TEXT
                else:
                    for sent in _sentences(final or const.FALLBACK_TEXT):
                        yield sent
                return
            messages.append(msg)
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
        # v1.0.41 安全（审查 S2 第一层）：白名单外的工具名直接拒（含一切路径形态），
        # 永不带入执行/回落链。非 str 名也在此拦下（LLM 可吐任意 JSON）。
        if not isinstance(name, str) or name not in _TOOL_NAMES:
            logger.warning("[Agent] LLM 回吐非法工具名，已拒绝: %r", str(name)[:80])
            return False, "工具名不合法"
        if name == "huijianGetLiveContext":
            result = await self.ha.handle_intent(name, {})
            raw = json.dumps(result.get("raw", result), ensure_ascii=False)[:2000]
            return bool(result.get("success")), raw
        if (name in _AUTOMATION_WRITE_TOOLS
                and not self.settings.get("llm.allow_automation_write", False)):
            return False, ("自动化的创建/修改/删除没开启——直接说「当客厅温度超过28度"
                           "就打开空调」这类句子，本地就能建，不需要大模型")
        if (name in _SCENE_WRITE_TOOLS
                and not self.settings.get("llm.allow_scene_write", True)):
            return False, "语音场景的创建/删除没开启"
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
