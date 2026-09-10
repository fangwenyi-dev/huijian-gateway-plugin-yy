#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ESP 全链路模拟台（持久化版；2026-09-12 重建入库，原 /tmp 脚本被环境清理丢失）。

与旧版的本质区别：**SimHA 不再恒成功**——
- 意图名按集成源码派生的契约表校验（未知意图即失败，与真 HA 一致）；
- 目标匹配按 HA 真语义建模：空 domains=不过滤、area-only 目标=无设备、
  名字按 friendly_name 子串匹配；匹配不到即 "No available devices found"。
这正是 v1.0.21 HassUnlock 假绿（替身恒成功）与空 domains 真机失败两类问题
的现场化验证。

跑法：cd huijian_voice && python3 tests/e2e/sim_full.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]          # huijian_voice/
sys.path.insert(0, str(HERE))
os.environ["HUIJIAN_DATA"] = tempfile.mkdtemp(prefix="hv_sim_")
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

import aiohttp                                                    # noqa: E402
from aiohttp import web                                           # noqa: E402

from core.settings import Settings                                # noqa: E402
from core import ws_server                                        # noqa: E402
from core.nlu.scenes import SceneCache                            # noqa: E402
from core.nlu.textcnn import TextCNN                              # noqa: E402
from core.executor import Executor                                # noqa: E402
from core.pipeline import Pipeline                                # noqa: E402

# ── 意图契约表：集成源码派生 ∪ HA core 内置（2025.1.0 实查）─────────
CC = HERE / "custom_components" / "huijian_ai"
KNOWN_INTENTS = {"HassTurnOn", "HassTurnOff", "HassToggle",
                 "HassClimateSetTemperature", "HassGetCurrentTime",
                 "HassOpenCover", "HassCloseCover", "HassShoppingListLastItems"}
for _py in CC.glob("*.py"):
    KNOWN_INTENTS |= set(re.findall(r'intent_type\s*=\s*"([^"]+)"',
                                    _py.read_text(encoding="utf-8")))
LOCK_INTENTS = {"HassUnlock", "HassLock"}
TURN_INTENTS = {"TurnDeviceOn": "turn_on", "TurnDeviceOff": "turn_off",
                "ControlWindow": "open_cover"}

STATES = {
    "lock.大门": ("locked", "大门", "客厅"),
    "light.客厅射灯": ("off", "客厅射灯", "客厅"),
    "light.书房灯": ("off", "书房灯", "书房"),
    "cover.客厅窗帘": ("closed", "客厅窗帘", "客厅"),
    "switch.客厅通道": ("off", "客厅通道", "客厅"),
    # 空调单列：区域继承的目标必须收窄到它，"客厅"整片设备不能被带开
    "climate.客厅空调": ("off", "客厅空调", "客厅"),
}
MEDIA = "media_player.书房音箱"


class SimHA:
    """HA 替身：契约表 + 真语义匹配 + 服务调用留痕。"""

    def __init__(self):
        self.calls = []
        self.ok = True
        self.reachable = True
        self.vscenes = []           # 语音场景状态库（create/delete 真实增删）
        self.vautomations = []      # 语音自动化状态库（create/list/delete 真增删）
        self._states = {eid: {"entity_id": eid, "state": s,
                              "attributes": {"friendly_name": fn}}
                        for eid, (s, fn, _a) in STATES.items()}
        self._states[MEDIA] = {"entity_id": MEDIA, "state": "idle",
                               "attributes": {"friendly_name": "书房音箱"}}
        self._entity_area = {eid: a for eid, (_s, _f, a) in STATES.items()}
        self._entity_area[MEDIA] = "书房"
        self._areas = {a: a for a in set(self._entity_area.values())}

    def _friendly(self, eid):
        return self._states[eid]["attributes"]["friendly_name"]

    def _match(self, target):
        """HA 真语义：空 domains=不过滤；无 devices 键=空；名字子串匹配。"""
        out = set()
        for t in target or []:
            devices = t.get("devices") or []
            area = t.get("area")
            for d in devices:
                doms = d.get("domains") or None      # 集成端已归一空→None
                name = d.get("name") or ""
                for eid in self._states:
                    if doms and eid.split(".", 1)[0] not in doms:
                        continue
                    if area and self._entity_area.get(eid) != area:
                        continue
                    if name and name not in self._friendly(eid):
                        continue
                    out.add(eid)
        return sorted(out)

    async def handle_intent(self, name, data, timeout=10.0):
        self.calls.append(("intent", name, data))
        if name not in KNOWN_INTENTS:
            return {"success": False, "message": f"Unknown intent {name}"}
        if name == "HassListVoiceScenes":
            return {"success": True, "scenes": [dict(s) for s in self.vscenes]}
        if name == "HassCreateVoiceScene":
            # 真集成语义：白名单（core.nlu.creation 派生，非手抄）+ 状态增删
            from core.nlu import creation as _cr
            tp = str(data.get("trigger_phrase") or "").strip()
            acts = data.get("actions")
            if not tp or not isinstance(acts, list) or not acts:
                return {"success": False, "error": "参数缺失"}
            bad = [a for a in acts if not isinstance(a, dict)
                   or a.get("intent") not in _cr.ACTIONABLE_INTENTS]
            if bad:
                return {"success": False, "error": f"动作不可执行:{bad}"}
            if any(s["trigger_phrase"] == tp for s in self.vscenes):
                return {"success": False, "error": f"场景「{tp}」已存在"}
            self.vscenes.append({"scene_id": f"vs_{len(self.vscenes)+1}",
                                 "trigger_phrase": tp, "actions": acts})
            return {"success": True, "scene_id": f"vs_{len(self.vscenes)}"}
        if name == "HassDeleteVoiceScene":
            tp = str(data.get("trigger_phrase") or "").strip()
            hit = [s for s in self.vscenes if s["trigger_phrase"] == tp]
            if not hit:
                return {"success": False, "error": f"未找到场景:{tp}"}
            self.vscenes.remove(hit[0])
            return {"success": True, "message": "deleted"}
        if name == "HassCreateAutomation":
            from core.nlu import creation as _cr
            trig = data.get("trigger") or {}
            acts = data.get("actions")
            if not isinstance(trig, dict) or not (
                    str(trig.get("entity_id") or "").strip()
                    or str(trig.get("at") or "").strip()):
                return {"success": False, "error": "trigger 缺 entity_id/at"}
            if not isinstance(acts, list) or not acts or [
                    a for a in acts if not isinstance(a, dict)
                    or a.get("intent") not in _cr.ACTIONABLE_INTENTS]:
                return {"success": False, "error": "动作不可执行"}
            aid = f"automation_{len(self.vautomations)+1:04d}"
            self.vautomations.append({
                "automation_id": aid, "trigger": trig, "actions": acts,
                "created_at": "2026-09-14T00:00:00", "last_triggered": None})
            return {"success": True, "automation_id": aid}
        if name == "HassListAutomations":
            return {"success": True,
                    "automations": [dict(a) for a in self.vautomations]}
        if name == "HassDeleteAutomation":
            aid = str(data.get("automation_id") or "")
            hit = [a for a in self.vautomations if a["automation_id"] == aid]
            if not hit:
                return {"success": False, "error": f"未找到自动化:{aid}"}
            self.vautomations.remove(hit[0])
            return {"success": True, "message": "deleted"}
        if name == "HassUpdateAutomation":
            # 与集成 HaasUpdateAutomationIntent 同语义（trigger/actions 可给其一，
            # 未找到 ID / 两者都没给 / 动作不可执行 一律失败——替身绝不恒成功）
            aid = str(data.get("automation_id") or "")
            trig, acts = data.get("trigger"), data.get("actions")
            hit = [a for a in self.vautomations if a["automation_id"] == aid]
            if not hit:
                return {"success": False, "error": f"未找到自动化ID'{aid}'"}
            if not trig and not acts:
                return {"success": False, "error": "请提供要修改的trigger或actions"}
            if trig is not None and not (
                    str(trig.get("entity_id") or "").strip()
                    or str(trig.get("at") or "").strip()):
                return {"success": False,
                        "error": "trigger.entity_id 或 trigger.at 至少给一个"}
            from core.nlu import creation as _cr2
            if acts is not None and (not isinstance(acts, list) or not acts or [
                    a for a in acts if not isinstance(a, dict)
                    or a.get("intent") not in _cr2.ACTIONABLE_INTENTS]):
                return {"success": False, "error": "动作不可执行"}
            if trig is not None:
                hit[0]["trigger"] = dict(trig)
            if acts is not None:
                hit[0]["actions"] = [dict(a) for a in acts]
            return {"success": True, "message": f"已更新自动化：{aid}"}
        if name in LOCK_INTENTS:
            ents = [e for e in self._match(data.get("target"))
                    if e.split(".", 1)[0] == "lock"]
            if not ents:
                return {"success": False, "error": "未找到可用的门锁设备"}
            await self.call_service("lock",
                                    "lock" if name == "HassLock" else "unlock",
                                    {"entity_id": ents})
            return {"success": True,
                    "states": [{"name": self._friendly(e), "success": True}
                               for e in ents]}
        if name in TURN_INTENTS:
            ents = self._match(data.get("target"))
            if not ents:
                return {"success": False, "error": "No available devices found"}
            svc = TURN_INTENTS[name]
            for dom in {e.split(".", 1)[0] for e in ents}:
                await self.call_service(
                    dom, svc if name != "ControlWindow" else "open_cover",
                    {"entity_id": [e for e in ents
                                   if e.split(".", 1)[0] == dom]})
            return {"success": True, "control_targets": [
                {"name": self._friendly(e), "area": self._entity_area.get(e, "")}
                for e in ents]}
        return {"success": True}

    async def call_service(self, domain, service, data=None):
        self.calls.append(("service", domain, service, data))
        return {"success": True}

    async def states(self):
        return dict(self._states)

    async def area_names(self):
        return sorted(set(self._areas.values()))

    async def get_config(self):
        return {"time_zone": "Asia/Shanghai"}

    async def find_entities(self, area="", domains=(), name_contains=""):
        out = []
        for eid, ent in self._states.items():
            if domains and eid.split(".", 1)[0] not in domains:
                continue
            if area and self._entity_area.get(eid) != area:
                continue
            out.append(ent)
        return out

    async def rest_get(self, path, timeout=6.0):
        return None

    async def rest_write(self, method, path, body=None, timeout=8.0):
        """页内操作写通道：test/rename/edit 真语义（动作经 handle_intent 真执行）。"""
        body = body or {}
        if (method, path) == ("POST", "/api/huijian-ai/test-scene"):
            tp = str(body.get("trigger_phrase") or "")
            hit = [s for s in self.vscenes if s["trigger_phrase"] == tp]
            if not hit:
                return {"success": False, "error": f"未找到场景:{tp}"}
            for a in hit[0]["actions"]:
                r = await self.handle_intent(a["intent"], a.get("params") or {})
                if not r.get("success"):
                    return {"success": False, "error": "动作执行失败"}
            return {"success": True}
        if (method, path) == ("POST", "/api/huijian-ai/test-automation"):
            aid = str(body.get("automation_id") or "")
            hit = [a for a in self.vautomations if a["automation_id"] == aid]
            if not hit:
                return {"success": False, "error": f"未找到自动化:{aid}"}
            for a in hit[0]["actions"]:
                r = await self.handle_intent(a["intent"], a.get("params") or {})
                if not r.get("success"):
                    return {"success": False, "error": "动作执行失败"}
            hit[0]["last_triggered"] = "2026-09-14T12:00:00"
            return {"success": True}
        if method == "PUT" and path.startswith("/api/huijian-ai/voice-scenes/"):
            sid = path.rsplit("/", 1)[-1]
            new = str(body.get("trigger_phrase") or "").strip()
            hit = [s for s in self.vscenes if s["scene_id"] == sid]
            if not hit or not new:
                return {"success": False, "error": "场景不存在或新名空"}
            if any(s["trigger_phrase"] == new for s in self.vscenes):
                return {"success": False, "error": f"触发词「{new}」已占用"}
            hit[0]["trigger_phrase"] = new
            return {"success": True}
        if method == "PUT" and path.startswith("/api/huijian-ai/automations/"):
            aid = path.rsplit("/", 1)[-1]
            hit = [a for a in self.vautomations if a["automation_id"] == aid]
            trig = body.get("trigger")
            if not hit or not isinstance(trig, dict):
                return {"success": False, "error": "自动化不存在或 trigger 缺失"}
            hit[0]["trigger"] = trig
            return {"success": True}
        return {"success": False, "error": f"无此写路由 {method} {path}"}

    async def fire_event(self, t, d):
        pass


class SimAsr:
    """按音频身份取脚本（pcm 前两字节=帧标记），真实 ASR 语义。"""

    def __init__(self):
        self.script = {}          # {帧标记: (延迟, 文本)}

    async def transcribe_pcm(self, pcm):
        delay, text = self.script.get(pcm[:2], (0.0, ""))
        await asyncio.sleep(delay)
        return text

    def ready(self):
        return True


class FakeDecoder:
    """协议测试解码注入点：任何 opus 帧都当 PCM 收下（旧台架同款）。"""

    def decode(self, data):
        return data                 # 原样保留帧标记，供 SimAsr 按音频取脚本


class SimTts:
    last_used = 0
    synth_calls = 0


PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{detail}]" if detail and not cond else ""))


async def ws_open(sess, port, channel):
    return await sess.ws_connect(f"http://127.0.0.1:{port}/xiaozhi/v1/{channel}")


async def collect(ws, pred, timeout=8.0):
    frames, deadline = [], time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = await ws.receive(timeout=max(0.05, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            break
        if msg.type == aiohttp.WSMsgType.TEXT:
            frames.append(("json", json.loads(msg.data)))
        elif msg.type == aiohttp.WSMsgType.BINARY:
            frames.append(("bin", msg.data))
        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            break
        if pred(frames):
            break
    return frames


async def llm_turn(sess, port, text, timeout=10.0):
    async with await ws_open(sess, port, "llm") as ws:
        await ws.send_str(json.dumps({"type": "listen", "state": "detect",
                                      "text": text}))
        return await collect(ws, lambda f: any(
            k == "json" and x.get("type") == "text" and x.get("state") == "end"
            for k, x in f), timeout=timeout)


def text_of(frames):
    return "".join(x.get("data", "") for k, x in frames
                   if k == "json" and x.get("type") == "text"
                   and x.get("state") == "sentence_end")


async def main():
    ha = SimHA()
    asr = SimAsr()
    settings = Settings(Path(os.environ["HUIJIAN_DATA"]) / "settings.json")
    settings.update({"llm": {"enabled": False},
                     "spatial": {"satellite_areas": {"127.0.0.1": "客厅"}}})
    scenes = SceneCache(ha)
    textcnn = TextCNN(HERE / "nlu_data")
    executor = Executor(ha, settings)
    pipeline = Pipeline(settings, ha, scenes, textcnn, executor, klar=None)

    ctx = ws_server.AppContext(settings=settings, ha=ha, asr=asr, tts=SimTts(),
                               pipeline=pipeline, scenes=scenes,
                               textcnn=textcnn, store=None,
                               started_at=time.time(), host="")
    ctx.decoder_factory = FakeDecoder      # session.py 的协议测试注入点
    runner = web.AppRunner(ws_server.make_ws_app(ctx), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    print(f"═══ ESP 全链路模拟台（端口 {port}，意图契约表 {len(KNOWN_INTENTS)} 项）═══")

    async with aiohttp.ClientSession() as sess:
        # S1 握手/心跳
        print("\n─ S1 握手与心跳 ─")
        async with await ws_open(sess, port, "stt") as ws:
            await ws.send_str('{"type":"hello","device":{"mac":"aa:bb"}}')
            await ws.send_str('{"type":"ping"}')
            f = await collect(ws, lambda fr: any(
                k == "json" and x.get("type") == "pong" for k, x in fr), 5)
            check("S1.1 收到 pong", any(k == "json" and x.get("type") == "pong"
                                        for k, x in f))
            check("S1.2 无 error 键", not any(k == "json" and "error" in x
                                              for k, x in f))

        # S2 单 stop 单回执
        print("\n─ S2 STT 单 stop ─")
        asr.script = {b"\xfc\x01": (0.0, "打开客厅的射灯")}
        async with await ws_open(sess, port, "stt") as ws:
            await ws.send_str('{"type":"listen","state":"start"}')
            await ws.send_bytes(b"\xfc\x01" * 4)
            await ws.send_str('{"type":"listen","state":"stop"}')
            f = await collect(ws, lambda fr: sum(
                1 for k, x in fr if k == "json" and x.get("type") == "stt") >= 1, 6)
            stts = [x for k, x in f if k == "json" and x.get("type") == "stt"]
            check("S2.1 恰回一条 stt", len(stts) == 1, str(stts))
            check("S2.2 文本正确", stts and stts[0].get("text") == "打开客厅的射灯",
                  str(stts))

        # S3 双 stop 抢占（2.0s 裕度，WSL 抖动下稳定）
        print("\n─ S3 双 stop 抢占 ─")
        asr.script = {b"\xfc\x02": (2.0, "打开书房的灯"),
                      b"\xfc\x03": (0.05, "关掉书房的灯")}
        async with await ws_open(sess, port, "stt") as ws:
            await ws.send_str('{"type":"listen","state":"start"}')
            await ws.send_bytes(b"\xfc\x02" * 4)
            await ws.send_str('{"type":"listen","state":"stop"}')
            await ws.send_str('{"type":"listen","state":"start"}')
            await ws.send_bytes(b"\xfc\x03" * 4)
            await ws.send_str('{"type":"listen","state":"stop"}')
            f = await collect(ws, lambda fr: sum(
                1 for k, x in fr if k == "json" and x.get("type") == "stt") >= 2, 15)
            texts = sorted(x.get("text", "") for k, x in f
                           if k == "json" and x.get("type") == "stt")
            check("S3.1 两个 stop 各回一条", len(texts) == 2, str(texts))
            check("S3.2 被抢占者空帧收束（或极端时序各回真文本）",
                  texts in (sorted(["", "关掉书房的灯"]),
                            sorted(["打开书房的灯", "关掉书房的灯"])), str(texts))

        # S4 上下文继承 + 空 domains 真匹配（集成端归一后应命中 switch.客厅通道）
        print("\n─ S4 上下文 / 空 domains 匹配 ─")
        n = len(ha.calls)
        await llm_turn(sess, port, "打开客厅的射灯")
        hit = [c for c in ha.calls[n:] if c[0] == "service"]
        check("S4.1 开射灯真调用服务", any("客厅射灯" in json.dumps(c, ensure_ascii=False)
                                            for c in hit), str(hit)[:160])
        n = len(ha.calls)
        r = await llm_turn(sess, port, "关掉它")
        hit = [c for c in ha.calls[n:] if c[0] == "service"]
        check("S4.2 关掉它继承目标（射灯）",
              any("客厅射灯" in json.dumps(c, ensure_ascii=False) for c in hit),
              str(hit)[:160])
        n = len(ha.calls)
        await llm_turn(sess, port, "打开客厅通道")
        hit = [c for c in ha.calls[n:] if c[0] == "service"]
        check("S4.3 空 domains 名（客厅通道）真匹配到设备",
              any("客厅通道" in json.dumps(c, ensure_ascii=False) for c in hit),
              str(hit)[:200])

        # S5 空间化：泛类词落本区域
        print("\n─ S5 空间化 ─")
        n = len(ha.calls)
        await llm_turn(sess, port, "开灯")
        intents = [c for c in ha.calls[n:] if c[0] == "intent"]
        tgt = (intents[-1][2].get("target") or [{}])[0] if intents else {}
        check("S5.1 泛类词目标补本区域", tgt.get("area") == "客厅", str(tgt))

        # S6 解锁：确认环 → 真执行（HA 语义匹配 + 锁域收窄）
        print("\n─ S6 解锁确认环 ─")
        n = len(ha.calls)
        r = await llm_turn(sess, port, "解锁大门")
        check("S6.1 首句只问不办", not [c for c in ha.calls[n:] if c[0] == "service"],
              str(ha.calls[n:])[:120])
        check("S6.2 问句含确认语义", "确认" in text_of(r), text_of(r))
        n = len(ha.calls)
        r = await llm_turn(sess, port, "确认")
        svc = [c for c in ha.calls[n:] if c[0] == "service"]
        check("S6.3 确认后真解锁 lock.大门",
              any(c[1] == "lock" and c[2] == "unlock"
                  and "lock.大门" in (c[3] or {}).get("entity_id", []) for c in svc),
              str(svc)[:200])
        n = len(ha.calls)
        await llm_turn(sess, port, "解锁大门")
        await llm_turn(sess, port, "取消")
        check("S6.4 取消后不执行",
              not [c for c in ha.calls[n:] if c[0] == "service"],
              str(ha.calls[n:])[:160])

        # S7 音乐带：引导 → 配置 → 点歌 → 播控
        print("\n─ S7 音乐带 ─")
        r = await llm_turn(sess, port, "放首歌")
        check("S7.1 未配置端点回配置指引", "配置" in text_of(r) or "设置" in text_of(r),
              text_of(r))
        settings.update({"music": {"player_entity": MEDIA}})
        n = len(ha.calls)
        await llm_turn(sess, port, "播放王心凌")
        pm = [c for c in ha.calls[n:] if c[0] == "service"
              and c[1] == "media_player" and c[2] == "play_media"]
        check("S7.2 点歌走 play_media 智能检索",
              bool(pm) and pm[0][3].get("media_content_type") == "music"
              and pm[0][3].get("media_content_id") == "王心凌", str(pm)[:200])
        n = len(ha.calls)
        await llm_turn(sess, port, "下一首")
        check("S7.3 下一首 → media_next_track",
              any(c[0] == "service" and c[2] == "media_next_track"
                  for c in ha.calls[n:]), str(ha.calls[n:])[:160])
        n = len(ha.calls)
        await llm_turn(sess, port, "停止播放")
        check("S7.4 停止播放 → media_stop 且不撞设备意图",
              any(c[0] == "service" and c[2] == "media_stop"
                  for c in ha.calls[n:])
              and not [c for c in ha.calls[n:] if c[0] == "intent"],
              str(ha.calls[n:])[:160])

        # S8 兜底不崩 + 契约表零违规
        print("\n─ S8 兜底与契约 ─")
        r = await llm_turn(sess, port, "帮我订张去北京的机票")
        check("S8.1 未命中句走兜底不崩", bool(text_of(r)), text_of(r))
        names = {c[1] for c in ha.calls if c[0] == "intent"}
        check("S8.2 全部意图名在契约表内", names <= KNOWN_INTENTS,
              str(sorted(names - KNOWN_INTENTS)))

        # S9 语音创建（v1.0.30 零 LLM；契约表从集成源码派生 HassCreate* 自动放行）
        print("\n─ S9 语音创建 ─")
        n = len(ha.calls)
        r = await llm_turn(sess, port, "当我说晚安就关闭客厅射灯")
        cr = [c for c in ha.calls[n:] if c[0] == "intent"
              and c[1] == "HassCreateVoiceScene"]
        check("S9.1 场景入库结构正确", bool(cr) and
              cr[0][2].get("trigger_phrase") == "晚安" and
              cr[0][2]["actions"][0]["intent"] == "TurnDeviceOff", str(cr)[:200])
        check("S9.2 回显确认含触发词", "晚安" in text_of(r), text_of(r))
        if cr:
            a = cr[0][2]["actions"][0]
            ex = await ha.handle_intent(a["intent"], a["params"])
            check("S9.3 入库动作按真集成语义可直接执行", ex.get("success") is True,
                  str(ex)[:160])
        else:
            check("S9.3 入库动作按真集成语义可直接执行", False, "无入库动作")
        n = len(ha.calls)
        r = await llm_turn(sess, port, "当客厅温度超过28度就打开客厅窗帘")
        au = [c for c in ha.calls[n:] if c[0] == "intent"
              and c[1] == "HassCreateAutomation"]
        check("S9.4 数值自动化入库 trigger 完整", bool(au) and
              au[0][2]["trigger"].get("above") == 28.0 and
              au[0][2]["trigger"].get("entity_id") == "客厅温度", str(au)[:200])
        n = len(ha.calls)
        r = await llm_turn(sess, port, "把自动化1的动作改成打开空调")
        upd = [c for c in ha.calls[n:] if c[0] == "intent"
               and c[1] == "HassUpdateAutomation"]
        tgt3 = ((((upd[0][2].get("actions") or [{}])[0].get("params") or {})
                 .get("target")) if upd else None)
        hit3 = ha._match(tgt3) if tgt3 else None
        # 旧形态 {name:"客厅", domains:[]} 会按名字子串命中客厅全部设备——
        # 区域继承必须收窄到空调本身（SimHA 按集成真语义复算）
        legacy = ha._match([{"devices": [{"name": "客厅", "domains": []}]}])
        check("S9.4b 区域继承收窄到空调（不误开客厅全屋）",
              bool(upd) and hit3 == ["climate.客厅空调"], f"{tgt3} → {hit3}")
        check("S9.4c 对照：旧形态确属过宽（区域当名字命中多台）",
              len(legacy) >= 3, str(legacy))
        n = len(ha.calls)
        r = await llm_turn(sess, port, "当我说出发就念一遍今日运势")
        check("S9.5 听不懂子句整单拒绝（零半成品入库）",
              not [c for c in ha.calls[n:]
                   if c[0] == "intent" and c[1].startswith("HassCreate")],
              str(ha.calls[n:])[:160])
        n = len(ha.calls)
        r = await llm_turn(sess, port, "每天早上7点帮我打开客厅窗帘")
        au2 = [c for c in ha.calls[n:] if c[0] == "intent"
               and c[1] == "HassCreateAutomation"]
        check("S9.6 时间自动化 at 归一", bool(au2) and
              au2[0][2]["trigger"] == {"at": "07:00"}, str(au2)[:160])
        check("S9.7 时间回显说人话", "早上7点" in text_of(r), text_of(r))
        n = len(ha.calls)
        r = await llm_turn(sess, port, "删除场景晚安")
        dl = [c for c in ha.calls[n:] if c[0] == "intent"
              and c[1] == "HassDeleteVoiceScene"]
        check("S9.8 语音删场景按名精准删除", bool(dl) and
              dl[0][2] == {"trigger_phrase": "晚安"} and
              not any(s["trigger_phrase"] == "晚安" for s in ha.vscenes)
              and "已删除" in text_of(r), str(dl)[:160] + " | " + text_of(r))
        n = len(ha.calls)
        r = await llm_turn(sess, port, "删除场景没创建过的词")
        check("S9.9 查无此名不瞎删（如实报+零调用）",
              not [c for c in ha.calls[n:] if c[0] == "intent"]
              and "没有找到" in text_of(r), text_of(r))

        # S9b v1.0.34 生命周期语音句（列出/编号删/关键词删/裸删引导/改场景）
        print("\n─ S9b 生命周期句式 ─")
        r = await llm_turn(sess, port, "我有哪些自动化")
        t = text_of(r)
        check("S9.10 列自动化带编号与条数", "2条" in t and "1，" in t and "2，" in t, t)
        n = len(ha.calls)
        r = await llm_turn(sess, port, "删除自动化温度")
        check("S9.11 关键词删自动化命中", len(ha.vautomations) == 1 and
              [c for c in ha.calls[n:] if c[1] == "HassDeleteAutomation"]
              and "已删除" in text_of(r), text_of(r))
        n = len(ha.calls)
        r = await llm_turn(sess, port, "删除自动化")
        check("S9.12 裸删列编号引导（零执行）", "要删哪一条" in text_of(r) and
              not [c for c in ha.calls[n:] if c[1] == "HassDeleteAutomation"],
              text_of(r))
        r = await llm_turn(sess, port, "删除自动化1")
        check("S9.13 序号删自动化", len(ha.vautomations) == 0 and
              "已删除" in text_of(r), text_of(r))
        r = await llm_turn(sess, port, "有哪些场景")
        check("S9.14 空场景如实报+句式示范", "还没有" in text_of(r), text_of(r))
        n = len(ha.calls)
        r = await llm_turn(sess, port, "当我说午休就打开客厅射灯")
        check("S9.15 建场景「午休」", len(ha.vscenes) == 1 and "已创建" in text_of(r),
              text_of(r))
        n = len(ha.calls)
        r = await llm_turn(sess, port, "把场景午休改成关闭客厅射灯")
        cur = ha.vscenes[0] if ha.vscenes else {}
        check("S9.16 改场景整句替换动作（触发词不变）",
              "已改成" in text_of(r) and len(ha.vscenes) == 1 and
              cur.get("trigger_phrase") == "午休" and
              cur.get("actions", [{}])[0].get("intent") == "TurnDeviceOff",
              text_of(r) + " | " + str(cur)[:120])
        n = len(ha.calls)
        r = await llm_turn(sess, port, "把场景不存在改成开灯")
        check("S9.17 改无此名如实报（零调用）",
              "没有找到" in text_of(r) and
              not [c for c in ha.calls[n:] if c[0] == "intent"], text_of(r))

        # 「删第N条」回指链（CHANGELOG 承诺句的实证位）
        r = await llm_turn(sess, port, "每天早上8点关闭客厅窗帘")
        check("S9.18a 时间自动化入库（回指前提）", len(ha.vautomations) == 1,
              text_of(r))
        r = await llm_turn(sess, port, "有哪些自动化")
        check("S9.18 清单播报建立回指锚", "1条" in text_of(r), text_of(r))
        r = await llm_turn(sess, port, "删第1条")
        check("S9.19 删第N条命中上次清单", len(ha.vautomations) == 0 and
              "已删除" in text_of(r), text_of(r))
        n = len(ha.calls)
        r = await llm_turn(sess, port, "删除第1条")   # 异文绕开重放缓存（防重复句设计）
        check("S9.20 删后清锚防旧编号误删（反问零执行）",
              "先说" in text_of(r) and
              not [c for c in ha.calls[n:] if c[0] == "intent"], text_of(r))
        r = await llm_turn(sess, port, "列出场景")   # 异文（S9.14 用过"有哪些场景"，防重放）
        check("S9.21a 场景清单播报", "1个语音场景" in text_of(r)
              and "午休就关闭客厅射灯" in text_of(r), text_of(r))
        r = await llm_turn(sess, port, "删掉第1个")
        check("S9.21 场景清单回指删除", len(ha.vscenes) == 0 and
              "已删除" in text_of(r), text_of(r))
        # 真机实证句式：多动作无标点连排（v1.0.34 补丁锚）
        r = await llm_turn(sess, port,
                           "当我说我回来了就帮我同时打开客厅射灯关闭客厅窗帘")
        sc = ha.vscenes[-1] if ha.vscenes else {}
        check("S9.22 多动作连排句入库两步（真机原句式）",
              len(sc.get("actions", [])) == 2 and "已创建" in text_of(r),
              text_of(r)[:60])

        # S9c 本地闭环补全（无 LLM 全生命周期：改自动化 + 裸删场景引导）
        print("\n─ S9c 本地闭环补全（改自动化/裸删场景） ─")
        r = await llm_turn(sess, port, "每天早上6点半打开客厅射灯")
        check("S9.23a 自动化入库（改的前提）", len(ha.vautomations) == 1,
              text_of(r))
        r = await llm_turn(sess, port, "把自动化1改成每天早上8点关闭客厅射灯")
        cur = ha.vautomations[0] if ha.vautomations else {}
        check("S9.23 改自动化整句替换（触发条件+动作都换）",
              len(ha.vautomations) == 1 and cur.get("trigger") == {"at": "08:00"}
              and cur.get("actions", [{}])[0].get("intent") == "TurnDeviceOff"
              and "已改成" in text_of(r),
              text_of(r) + " | " + str(cur)[:120])
        r = await llm_turn(sess, port, "把自动化1的动作改成打开客厅射灯")
        cur = ha.vautomations[0] if ha.vautomations else {}
        check("S9.24 改自动化仅动作（触发条件不动）",
              cur.get("trigger") == {"at": "08:00"}
              and cur.get("actions", [{}])[0].get("intent") == "TurnDeviceOn"
              and "动作已改成" in text_of(r),
              text_of(r) + " | " + str(cur)[:120])
        r = await llm_turn(sess, port, "把自动化1的触发条件改成每天晚上9点")
        cur = ha.vautomations[0] if ha.vautomations else {}
        check("S9.25 改自动化仅触发条件（动作不动）",
              cur.get("trigger") == {"at": "21:00"}
              and cur.get("actions", [{}])[0].get("intent") == "TurnDeviceOn"
              and "触发条件已改成" in text_of(r),
              text_of(r) + " | " + str(cur)[:120])
        n = len(ha.calls)
        r = await llm_turn(sess, port, "把自动化1的动作改成念一遍新闻")
        check("S9.26 新动作听不懂整单拒绝（零改动）",
              "先不创建" in text_of(r)
              and not [c for c in ha.calls[n:] if c[1] == "HassUpdateAutomation"],
              text_of(r))
        n = len(ha.calls)
        r = await llm_turn(sess, port, "把自动化9改成每天早上8点打开客厅射灯")
        check("S9.27 改不存在的自动化如实报（零调用）",
              "没有找到" in text_of(r)
              and not [c for c in ha.calls[n:] if c[1] == "HassUpdateAutomation"],
              text_of(r))
        n = len(ha.calls)
        r = await llm_turn(sess, port, "删除场景")
        check("S9.28 裸删场景本地引导（零执行）", "要删哪个场景" in text_of(r)
              and not [c for c in ha.calls[n:] if c[1] == "HassDeleteVoiceScene"],
              text_of(r))
        r = await llm_turn(sess, port, "把第1个删掉")
        check("S9.29 引导后编号删除可用（无 LLM 闭环）",
              len(ha.vscenes) == 0 and "已删除" in text_of(r), text_of(r))
        n = len(ha.calls)
        r = await llm_turn(sess, port, "客厅开灯")
        check("S9.30 区域当设备名的过宽目标被拦（零执行+引导）",
              "不确定你要哪一台" in text_of(r)
              and not [c for c in ha.calls[n:] if c[0] == "service"],
              text_of(r))
        n = len(ha.calls)
        r = await llm_turn(sess, port, "打开客厅灯")
        check("S9.31 说清设备后照常执行（拦的是歧义不是功能）",
              bool([c for c in ha.calls[n:] if c[0] == "service"]), text_of(r))

        # S9d 「LLM 只做兜底」实证：真 Agent + 桩端点计数——本地命中的句子
        # （含场景/自动化生命周期）一次都不许打到 LLM；只有全未命中才允许。
        print("\n─ S9d LLM 只做兜底（真 Agent + 桩端点计数） ─")
        llm_hits: list = []

        async def _chat(request):
            llm_hits.append(await request.json())
            return web.json_response({"choices": [{"message": {
                "role": "assistant", "content": "这是大模型的兜底回答。"}}]})

        llm_app = web.Application()
        llm_app.router.add_post("/v1/chat/completions", _chat)
        llm_runner = web.AppRunner(llm_app, access_log=None)
        await llm_runner.setup()
        llm_site = web.TCPSite(llm_runner, "127.0.0.1", 0)
        await llm_site.start()
        llm_port = llm_runner.addresses[0][1]
        from core.agent import Agent                               # noqa: E402
        settings.update({"llm": {"enabled": True, "stream": False,
                                 "model": "stub", "temperature": 0.3,
                                 "base_url": f"http://127.0.0.1:{llm_port}/v1"}})
        pipeline.agent = Agent(settings, ha, executor)
        try:
            n0 = len(llm_hits)
            r = await llm_turn(sess, port, "当我说睡觉就关闭客厅射灯")
            check("S9d.1 本地建场景零 LLM 调用",
                  len(ha.vscenes) == 1 and len(llm_hits) == n0, text_of(r))
            r = await llm_turn(sess, port, "把自动化1改成每天早上7点关闭客厅射灯")
            cur = ha.vautomations[0] if ha.vautomations else {}
            check("S9d.2 本地改自动化零 LLM 调用",
                  len(llm_hits) == n0 and cur.get("trigger") == {"at": "07:00"}
                  and "已改成" in text_of(r), text_of(r))
            r = await llm_turn(sess, port, "删除场景")
            check("S9d.3 本地裸删引导零 LLM 调用",
                  len(llm_hits) == n0 and "要删哪个场景" in text_of(r), text_of(r))
            r = await llm_turn(sess, port, "帮我推荐一部科幻电影")
            check("S9d.4 全未命中才交 LLM（兜底通道仍在）",
                  len(llm_hits) == n0 + 1 and "大模型" in text_of(r),
                  text_of(r) + f" | hits={len(llm_hits)}")
        finally:
            settings.update({"llm": {"enabled": False}})
            pipeline.agent = None
            await llm_runner.cleanup()

        # S10 场景模式（v1.0.30 SetMode 语料吸收：preset 英文规范名直发）
        print("\n─ S10 场景模式 ─")
        n = len(ha.calls)
        await llm_turn(sess, port, "客厅空调设为睡眠模式")
        sm = [c for c in ha.calls[n:] if c[0] == "intent"
              and c[1] == "SetDeviceMode"]
        check("S10.1 睡眠模式归一 sleep 送集成", bool(sm) and
              sm[0][2].get("mode") == "sleep", str(sm)[:200])
        n = len(ha.calls)
        await llm_turn(sess, port, "客厅射灯调亮一点")
        check("S10.2 泛化模式行不截胡亮度句",
              not [c for c in ha.calls[n:]
                   if c[0] == "intent" and c[1] == "SetDeviceMode"],
              str(ha.calls[n:])[:160])
        n = len(ha.calls)
        await llm_turn(sess, port, "客厅空调设为浪漫模式")
        check("S10.3 词表外模式不误发（宁缺勿错）",
              not [c for c in ha.calls[n:]
                   if c[0] == "intent" and c[1] == "SetDeviceMode"],
              str(ha.calls[n:])[:160])

    await runner.cleanup()
    total = len(PASS) + len(FAIL)
    print(f"\n═══ 完成测试：{len(PASS)}/{total} 通过 ═══")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
