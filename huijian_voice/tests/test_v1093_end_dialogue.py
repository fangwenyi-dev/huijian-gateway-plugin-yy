# -*- coding: utf-8 -*-
"""v1.0.93 退下词表 + end_dialogue 旗三端链路钉（2026-09-18 用户批准批）。

现场链（091703 会话真机实锤）：
  「退下」不被识别 → 回「我还不会」→ 照常 re-listen；更底层的是空轮（no-text）
  也以 16s 周期无限续听（D1）。修复=三层各干各的、一根新信号线：
    ① 加载项：fast_path **整句精确**字面表 → 级联短路（零执行/零上下文注入）
       → Reply.end_dialogue → LLM end 帧 end_dialogue:1；
    ② 集成：llm_transport 聚合捕获 → conversation 实体按 chat_log.conversation_id
       记账（core 2026.9.2 ConversationResult.as_dict 固定三键，无私有透传位——
       旁路记账是唯一不碰 core 的通道）→ assist_satellite INTENT_END 弹取转
       kv end_dialogue="1"；
    ③ 固件 v2.1.55：stop_after_tts_ 单轮旗 + 空轮闸 + 20轮/10min 护栏
       （固件面验证走 COM12 台架，不在本文件）。
纪律钉：正向专用旗——end_dialogue 只表达"停"，continue_conversation 语义
零改动（v2.1.47 孤旗教训）；词表**整句精确**，「安静一点」「再见面」负例不得中。
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v1093_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(ROOT / "nlu_data"))

from core import const                                        # noqa: E402
from core.nlu.fast_path import (END_DIALOGUE_INTENT, FastPath, Plan,  # noqa: E402
                                is_end_dialogue)
from core.nlu.textcnn import TextCNN                          # noqa: E402
from core.pipeline import (HUIJIAN_ONLY_INTENTS, Pipeline,     # noqa: E402
                           Reply, select_primary_plan)

# ── 词表语义（正负例都钉死；与用户批准表逐词对账）──────────────────────
POSITIVE = [
    "退下", "退下吧", "好的退下", "好的，退下", "结束对话", "不聊了",
    "不说", "不说了", "再见", "拜拜", "停止聆听", "退出对话", "安静",
    "别念了", "不用了", "退下。", "安静！", "再见~", "结束对话吧",
    "不用了。", "不聊了呀",
]
NEGATIVE = [
    "安静一点", "安静一下", "再见面", "再见了一面", "退下门口那盏灯",
    "结束对话窗口", "停止聆听测试", "不说秘密", "把安静模式打开",
    "退下吧别吹了", "关闭不用了", "你好小智", "开灯", "", "退",
    "别念了也能再念一遍", "停止聆听模式退出",
]


@pytest.mark.parametrize("s", POSITIVE)
def test_end_words_positive(s):
    assert is_end_dialogue(s), f"{s!r} 应在收词表（用户批准 2026-09-18）"


@pytest.mark.parametrize("s", NEGATIVE)
def test_end_words_negative(s):
    assert not is_end_dialogue(s), f"{s!r} 被误杀——词表只认整句精确"


def test_end_word_registered_exclusive():
    """裁决面：退下意图在慧尖独占集——与 klar 并列命中时恒胜（防 draft 回放
    把"退下"句抓去调光，那是 v1.0.92 目标证据闸同族病灶）。"""
    assert END_DIALOGUE_INTENT == "HuijianEndConversation"
    assert END_DIALOGUE_INTENT in HUIJIAN_ONLY_INTENTS
    fp = Plan(intent=END_DIALOGUE_INTENT, args={}, source="t0_end")
    kl = Plan(intent="HassLightSet", args={"entity_id": "light.a"},
              source="klar", utterance="退下")
    assert select_primary_plan(fp, kl) is fp


class FakeScenes:
    def __init__(self, triggers=()):
        self.triggers = set(triggers)

    async def refresh(self, force=False):
        pass

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    def check(self, text):
        return text if text in self.triggers else None

    async def verify_or_refresh(self, phrase):
        return phrase in self.triggers


def _mk_fp(tc, triggers=()):
    return FastPath(FakeScenes(triggers), tc, {})


@pytest.fixture(scope="module")
def tc():
    t = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    t._ensure()
    return t


def test_match_returns_end_plan_before_action_scan(tc):
    fp = _mk_fp(tc)
    p = asyncio.run(fp.match("退下"))
    assert p is not None and p.intent == END_DIALOGUE_INTENT
    assert p.source == "t0_end" and p.args == {}


def test_scene_contract_outranks_end_words(tc):
    """场景契约恒最高优先（模块头裁决①）：触发词写成「退下」的契约句，
    退下字面表必须让位。"""
    fp = _mk_fp(tc, triggers=("退下",))
    p = asyncio.run(fp.match("退下"))
    assert p is not None and p.source == "scene", \
        "用户自定义契约被词表截胡=契约失效（最高优先纪律回退）"


# ── 级联短路：零执行、零上下文注入、旗挂 Reply ──────────────────────────
def _mk_pipe_stub(reply_plan):
    p = object.__new__(Pipeline)
    p.settings = {"nlu.enabled": True, "dialog.dedup_window_s": 0.0,
                  "dialog.chain_enabled": True, "dialog.context_enabled": True}
    p.telemetry = None
    p._last = {}
    p.ha = SimpleNamespace(fire_event=lambda *a: None)

    async def _confirm_answer(text, origin):
        return None

    async def _voice_creation(text, origin):
        return None

    async def _try_compound(text, origin):
        return None

    async def _match_pair(text):
        return reply_plan, None

    p._confirm_answer = _confirm_answer
    p._voice_creation = _voice_creation
    p._try_compound = _try_compound
    p._match_pair = _match_pair
    p._known_areas = lambda: ()
    p.executor = SimpleNamespace(run=_boom)
    return p


async def _boom(*a, **k):
    raise AssertionError("退下轮不得进 executor（纯会话控制零设备面）")


def test_cascade_end_short_circuit():
    end_plan = Plan(intent=END_DIALOGUE_INTENT, args={}, source="t0_end",
                    trace=["退下字面表"])
    p = _mk_pipe_stub(end_plan)
    r = asyncio.run(p._cascade("退下", ""))
    assert r.end_dialogue is True
    assert r.text == const.END_DIALOGUE_SAY
    assert r.ok is True and r.source == "t0_end"


def test_cascade_end_bypasses_context_injection():
    """退出句空 args 绝不被 _apply_context 继承上一轮目标（那会给"退下"配
    设备上下文——纯控制句不吃任何注入）。上面 _boom executor 即证。"""
    test_cascade_end_short_circuit()


def test_dedup_propagates_end_flag():
    """去重复述窗口内二说「退下」：旗必须随复述同发（丢旗=该停不停）。"""
    p = object.__new__(Pipeline)
    p.settings = {"dialog.dedup_window_s": 2.0}
    p._last = {"退下": {"first": __import__("time").time(), "fut": None,
                        "reply": Reply(const.END_DIALOGUE_SAY, "t0_end", True,
                                       end_dialogue=True)}}
    r = asyncio.run(p._dedup_gate("退下"))
    assert r is not None and r.end_dialogue is True and r.source == "dedup"


# ── LlmSession：旗挂 end 帧（True 才加键，False 逐字节旧形）────────────
class _WS:
    def __init__(self):
        self.sent = []
        self.closed = False          # BaseSession._send 先读 ws.closed（缺=AttributeError 被吞）

    async def send_str(self, s):
        self.sent.append(json.loads(s))

    async def send_bytes(self, b):
        self.sent.append(b)


def _mk_llm_ctx(reply):
    async def handle(text, origin="", on_sentence=None):
        return reply
    return SimpleNamespace(pipeline=SimpleNamespace(handle=handle),
                           device_hint="panel")


def _run_llm_turn(reply):
    from core.session import LlmSession
    ws = _WS()
    s = LlmSession(ws, _mk_llm_ctx(reply))

    async def main():
        await s.on_text(json.dumps({"type": "listen", "state": "detect",
                                    "text": "退下"}))
        for _ in range(100):
            if any(isinstance(x, dict) and x.get("state") == "end"
                   for x in ws.sent):
                break
            await asyncio.sleep(0.01)
        if s._task and not s._task.done():
            s._task.cancel()
    asyncio.run(main())
    return ws


def test_llm_end_frame_carries_flag():
    ws = _run_llm_turn(Reply(const.END_DIALOGUE_SAY, "t0_end", True,
                             end_dialogue=True))
    end = [x for x in ws.sent if x.get("state") == "end"]
    assert end and end[-1].get("end_dialogue") == 1
    assert end[-1]["type"] == "text"


def test_llm_end_frame_unchanged_without_flag():
    ws = _run_llm_turn(Reply("客厅开了。", "t0", True))
    end = [x for x in ws.sent if x.get("state") == "end"]
    assert end and set(end[-1]) == {"type", "state"}, \
        "普通轮 end 帧逐字节旧形（旧客户端零暴露纪律，与 rid 批同律）"


# ── 链中退下分句：不进 executor，旗挂链应答 ────────────────────────────
def test_chain_end_clause_filtered_and_flagged():
    p = object.__new__(Pipeline)
    p.settings = {"dialog.chain_enabled": True, "dialog.context_enabled": True}
    p.scenes = None
    p._confirm = {}
    ran = {}

    async def _confirm_answer(text, origin):
        return None

    async def _match_pair(clause):
        if clause == "退下":
            return Plan(intent=END_DIALOGUE_INTENT, args={},
                        source="t0_end"), None
        return Plan(intent="TurnDeviceOff", args={"target": []},
                    source="t0"), None

    async def _run(plan):
        ran["plan"] = plan
        return True, "灯关了"

    p._confirm_answer = _confirm_answer
    p._match_pair = _match_pair
    p._known_areas = lambda: ()
    p._apply_context = lambda plan, text, origin, seed=None: plan
    p._overbroad_area_target = lambda plan: None
    p._risky = lambda plan: False
    p._spec_of = lambda plan: None
    p._note_target = lambda origin, plan: None
    p._remember_turn = lambda *a: None
    p.executor = SimpleNamespace(run=_run)
    from core.nlu.fast_path import split_compound
    if not split_compound("关灯然后退下"):
        pytest.skip("split_compound 形态变了，本钉改走真句（见注释）")
    r = asyncio.run(p._try_compound("关灯然后退下", ""))
    assert r is not None and r.end_dialogue is True
    assert ran["plan"].intent == "TurnDeviceOff", "退下分句绝不得进 executor"
    assert all(st.get("name") != END_DIALOGUE_INTENT
               for st in ran["plan"].extra_steps)
    assert r.text.startswith("灯关了") and const.END_DIALOGUE_SAY in r.text


# ── 集成侧接线钉（behavior 面在集成仓 test_window/契约台架，此处钉 wiring）──
CC = ROOT / "custom_components" / "huijian_ai"


def test_integration_registry_module_pure():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "huijian_end_dialogue", CC / "end_dialogue.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.clear()
    assert m.consume("conv-1") is False
    m.mark("conv-1")
    assert m.consume("conv-1") is True
    assert m.consume("conv-1") is False, "弹取制：INTENT_END 二读不得重复停轮"
    m.mark(None); m.mark("")
    assert m.consume(None) is False and m.consume("") is False
    for i in range(100):                      # 有界 FIFO：残账不无界积累
        m.mark(f"c{i}")
    assert len(m._pending) <= m._MAX
    assert m.consume("c99") is True


def test_integration_llm_transport_capture_wiring():
    src = (CC / "huijian" / "llm_transport.py").read_text(encoding="utf-8")
    assert 'if data.state == "end":' in src
    assert 'bool(data.get("end_dialogue"))' in src
    assert "end_dialogue=1" in src, "聚合 Delta 只在 True 时带键（False 旧形）"


def test_integration_conversation_mark_wiring():
    src = (CC / "conversation.py").read_text(encoding="utf-8")
    assert "end_dialogue.mark(chat_log.conversation_id)" in src
    assert 'k != "end_dialogue"' in src, \
        "自定义键剥除后再交 core delta 消费面（不赌 core 对未知键的宽容）"


def test_integration_satellite_consume_wiring():
    src = (CC / "assist_satellite.py").read_text(encoding="utf-8")
    assert 'data_to_send["end_dialogue"] = "1"' in src
    assert "end_dialogue.consume(" in src
    # 取 on_pipeline_event 的 elif 分支（文件头枚举映射里也有同名常量）
    i = src.index("event_type == VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_END")
    seg = src[i:i + 900]
    assert "continue_conversation" in seg and "end_dialogue" in seg, \
        "kv 必须与 continue_conversation 同批发出（INTENT_END 单事件收口）"
    assert seg.index("continue_conversation") < seg.index("end_dialogue.consume"), \
        "先保旧键形状再加新键（读序钉）"


# ── announce 推流修复接线钉（行为面=真机 COM12 判据）───────────────────
def test_announce_streaming_wiring():
    src = (CC / "assist_satellite.py").read_text(encoding="utf-8")
    assert "def _stream_tts_audio(" in src
    assert "announce: bool = False," in src, "签名默认 False=旧调用零影响"
    assert "if not announce and not self._is_running:" in src, \
        "豁免只此一处，pipeline 路 _is_running 闸不回退"
    assert "announce=True)" in src, "播报调用点必须显式豁免"
    assert "VoiceAssistantFeature.SPEAKER" in src
    # 护栏②：活跃轮不抢下行（v1.0.96 起门控收进纯函数 _announce_gate，
    # pipeline_busy 必须作为参数入账——分因真值表钉在 test_v1096_announce_gate）
    i = src.index("taken, skip = _announce_gate(")
    seg = src[i:i + 400]
    assert "pipeline_busy=bool(" in seg
    assert '"[Announce] 播报未走文本自合成推流' in src, \
        "不走自合成的每个分因必须具名 WARN——静默跳过是 VM 案三日无日志的根因"


def test_tts_d3_constants():
    src = (ROOT / "core" / "tts.py").read_text(encoding="utf-8")
    assert "_GEN_WAIT_S = 30.0" in src, "锁预算对齐 52s 窗（D3）被回改？"
    assert "max_workers=4" in src, "D3 池扩容被回改？"
    from core.tts import TtsEngine
    e = TtsEngine({}, SimpleNamespace())
    assert e._pool()._max_workers == 4
    e._pool().shutdown(wait=False)
