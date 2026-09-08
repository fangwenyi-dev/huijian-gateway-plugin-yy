# -*- coding: utf-8 -*-
"""零改动音乐过渡带钉测（用户定向 2026-09-12）。

覆盖：点歌/播控解析表、保守守卫（设备词/假点歌不抢活）、级联 ⑤b 的
HA core 标准服务派发形态、未配置端点引导、失败话术「抱歉」前缀纪律、
LLM 不吞点歌令、上下文零污染。
"""
import asyncio
import time
from collections import OrderedDict

from core.nlu.music import parse_music
from core.nlu.fast_path import Plan
from core.pipeline import Pipeline


def arun(coro):
    return asyncio.run(coro)


# ── fakes（与 test_experience_batch 同款手工装配）────────────────
class Lane:
    def __init__(self, table=None, single=None):
        self.table, self.single = table or {}, single

    async def match(self, text):
        return self.table.get(text, self.single)


class NullQuery:
    async def answer(self, text):
        return None


class RecHa:
    def __init__(self, ok=True):
        self.calls = []
        self.ok = ok

    async def call_service(self, domain, service, data, timeout=10.0):
        self.calls.append((domain, service, data))
        return {"success": self.ok, "message": ""}

    async def fire_event(self, name, data):
        pass


class NoExec:
    async def run(self, plan):
        raise AssertionError("音乐令不应到达 executor")


class ST:
    def __init__(self, d=None):
        self.d = {"dialog.dedup_window_s": 2.0, "dialog.context_enabled": True,
                  "dialog.chain_enabled": False, "dialog.confirm_risky": True,
                  "spatial.satellite_areas": {}, "llm.history_rounds": 10,
                  "dialog.fallback_text": "兜底", "music.player_entity": ""}
        self.d.update(d or {})

    def get(self, k, default=None):
        return self.d.get(k, default)


class ChatAgent:
    enabled = True

    async def answer(self, text, history):
        yield "好的，我陪你聊聊音乐～"


def _pipe(settings=None, ha=None, agent=None):
    p = Pipeline.__new__(Pipeline)
    p.settings = settings or ST()
    p.ha = ha or RecHa()
    p.executor = NoExec()
    p.agent = agent
    p.query = NullQuery()
    p.fast_path = Lane()
    p.klar = Lane()
    p.scenes = None
    p._last = OrderedDict()
    p._turns, p._last_target, p._origin_ts, p._confirm = {}, {}, {}, {}
    p._pending = set()
    p._vocab_ts = time.time()
    return p


# ── 解析表 ─────────────────────────────────────────────────────
def test_parse_play_forms():
    for text, q in [("播放周杰伦", "周杰伦"), ("我想听稻香", "稻香"),
                    ("来点纯音乐", "纯音乐"), ("放一首晴天", "晴天"),
                    ("播放稻香这首歌", "稻香"), ("帮我播一下青花瓷", "青花瓷"),
                    ("听一首海阔天空", "海阔天空")]:
        got = parse_music(text)
        assert got == {"action": "play", "query": q}, (text, got)


def test_parse_generic_play_needs_no_title():
    for text in ("来点音乐", "放首歌", "播音乐", "播放歌曲"):
        assert parse_music(text) == {"action": "play", "query": ""}, text


def test_parse_ctrl_forms():
    for text, act in [("停止播放", "stop"), ("关掉音乐", "stop"), ("把音乐关了", "stop"),
                      ("暂停播放", "pause"), ("音乐暂停", "pause"), ("暂停一下", "pause"),
                      ("继续播放", "resume"), ("接着放", "resume"),
                      ("下一首", "next"), ("切歌", "next"), ("换一首", "next"),
                      ("上一首", "prev"), ("退回上一首", "prev")]:
        assert parse_music(text)["action"] == act, text


def test_parse_guards_refuse_non_music():
    for text in ("打开客厅的灯", "关灯", "播放客厅的灯", "放轻松", "放假",
                 "把它关掉", "现在几点了", "停止", "空调调到26度", "听我的"):
        assert parse_music(text) is None, text


def test_parse_punct_and_polite_tail():
    assert parse_music("停止播放。")["action"] == "stop"
    assert parse_music("播放周杰伦")["query"] == parse_music("播放周杰伦！")["query"]


# ── 级联派发 ───────────────────────────────────────────────────
def test_music_play_dispatches_ma_smart_search():
    ha = RecHa()
    p = _pipe(settings=ST({"music.player_entity": "media_player.书房音箱"}), ha=ha)
    r = arun(p.handle("播放周杰伦", origin="o"))
    assert r.source == "music" and r.ok and "周杰伦" in r.text
    assert ha.calls == [("media_player", "play_media",
                         {"entity_id": "media_player.书房音箱",
                          "media_content_type": "music",
                          "media_content_id": "周杰伦"})]


def test_music_ctrl_service_mapping():
    ha = RecHa()
    p = _pipe(settings=ST({"music.player_entity": "media_player.x"}), ha=ha)
    arun(p.handle("暂停播放"))
    arun(p.handle("继续播放"))
    arun(p.handle("下一首"))
    arun(p.handle("停止播放"))
    got = [c[1] for c in ha.calls]
    assert got == ["media_pause", "media_play", "media_next_track", "media_stop"]
    assert all(c[0] == "media_player" for c in ha.calls)


def test_music_generic_play_guides_without_swallowing():
    ha = RecHa()
    p = _pipe(settings=ST({"music.player_entity": "media_player.x"}), ha=ha)
    r = arun(p.handle("来点音乐"))
    assert r.ok and "歌名" in r.text and ha.calls == []


def test_music_no_endpoint_guides_config():
    ha = RecHa()
    p = _pipe(ha=ha)                        # music.player_entity 空
    r = arun(p.handle("播放周杰伦"))
    assert r.source == "music" and not r.ok
    assert "设置" in r.text and ha.calls == []


def test_music_failure_keeps_apology_prefix():
    p = _pipe(settings=ST({"music.player_entity": "media_player.x"}),
              ha=RecHa(ok=False))
    r = arun(p.handle("下一首"))
    assert not r.ok and r.text.startswith("抱歉")


def test_music_wins_over_llm_and_keeps_context_clean():
    p = _pipe(settings=ST({"music.player_entity": "media_player.x"}),
              agent=ChatAgent())
    r = arun(p.handle("播放晴天", origin="o"))
    assert r.source == "music"              # LLM 没吞点歌令
    assert "o" not in p._last_target        # 音乐令不污染设备上下文
    assert p._turns.get("o")                # 但进对话历史


# ── fast_path 让位（真件在 test_fast_path.py 钉，这里钉级联闭环）──
def test_cascade_reaches_music_band_when_fp_misses():
    """fp 有计划时音乐带不插队（明示目标句零影响纪律）。"""
    ha = RecHa()
    p = _pipe(settings=ST({"music.player_entity": "media_player.x"}), ha=ha)
    p.fast_path = Lane(table={"关掉音响": Plan(intent="TurnDeviceOff",
                                               args={"target": [{"area": "客厅"}]},
                                               source="t0", trace=[])})

    class OkExec:
        def __init__(self): self.plans = []

        async def run(self, plan):
            self.plans.append(plan)
            return True, "好的"

    p.executor = OkExec()
    r = arun(p.handle("关掉音响"))          # 非泛词，fp 正常执行
    assert r.source == "t0" and ha.calls == []
