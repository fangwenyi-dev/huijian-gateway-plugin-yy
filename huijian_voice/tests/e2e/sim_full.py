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
}
MEDIA = "media_player.书房音箱"


class SimHA:
    """HA 替身：契约表 + 真语义匹配 + 服务调用留痕。"""

    def __init__(self):
        self.calls = []
        self.ok = True
        self.reachable = True
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
            return {"success": True, "scenes": []}
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
        await llm_turn(sess, port, "当我说出发就念一遍今日运势")
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
