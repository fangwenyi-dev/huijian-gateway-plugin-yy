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

    # P1 音乐批：区域词表与端点态（缺省空=区域定向关闭，老钉测形态不变）
    _areas: dict = {}
    _states: dict = {}

    async def area_names(self):
        return sorted(set(self._areas.values()))

    async def get_state(self, entity_id):
        return dict((self._states or {}).get(entity_id) or {})


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
    p._music_last = OrderedDict()
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


# ── P1 增强（方案 §5.1，2026-09-25）：区域定向 / 正在播放 / 防呆闸 ────
def test_parse_music_area_lead_forms():
    areas = {"客厅", "卧室"}
    assert parse_music("在卧室播放周杰伦", areas) == {
        "action": "play", "query": "周杰伦", "area": "卧室"}
    assert parse_music("用客厅的音箱播放稻香", areas) == {
        "action": "play", "query": "稻香", "area": "客厅"}
    assert parse_music("在客厅放音乐", areas) == {
        "action": "play", "query": "", "area": "客厅"}
    assert parse_music("在卧室暂停播放", areas) == {
        "action": "pause", "query": "", "area": "卧室"}
    # 裸「放+曲名」不在点歌动词表（v1.0.21 起刻意保守，防"放门口"误吞）：
    # 区域剥成半解析也不返出，整句 None 交 LLM 兜底
    assert parse_music("用客厅的音箱放稻香", areas) is None
    # 未知区域不瞎抽：整句放行级联（不返回半解析结果）
    assert parse_music("在书房播放晴天", areas) is None
    # 无词表（老形态）：区域前缀仍在，_PLAY 不匹配 → None（与 v1.0.21 一致）
    assert parse_music("在卧室播放周杰伦") is None


def test_parse_music_area_tail_player_phrase():
    areas = {"客厅"}
    assert parse_music("播放周杰伦，客厅的音箱", areas) == {
        "action": "play", "query": "周杰伦", "area": "客厅"}
    # 粘连形态（曲名可能自带区域字）宁漏不误：设备词收尾守卫让位
    assert parse_music("播放周杰伦的音箱", areas) is None
    # 无词表时音箱族同样让位（本轮把音箱族并入 _DEVICE_TAIL 的收紧钉）
    assert parse_music("播放客厅的音箱") is None


def test_now_playing_intents_parse_first():
    for text in ("现在放的是什么歌", "正在播放的是什么歌", "这首歌谁唱的",
                 "放的啥", "这是什么歌"):
        got = parse_music(text)
        assert got and got["action"] == "now_playing", text
    # 非查询形不误吞
    assert parse_music("现在几点了") is None
    assert parse_music("播放周杰伦")["action"] == "play"


class StateHa(RecHa):
    def __init__(self, states=None, **kw):
        super().__init__(**kw)
        self._states = states or {}


def test_now_playing_reads_entity_attributes():
    ha = StateHa(states={"media_player.x": {
        "state": "playing",
        "attributes": {"media_title": "晴天", "media_artist": "周杰伦"}}})
    p = _pipe(settings=ST({"music.player_entity": "media_player.x"}), ha=ha)
    r = arun(p.handle("现在放的是什么歌"))
    assert r.ok and "晴天" in r.text and "周杰伦" in r.text


def test_now_playing_falls_back_to_play_ledger():
    """卫星实体 playing 但 media_title 空 → 回退加载项点歌记账（缺口②）。"""
    ha = StateHa(states={"media_player.sat": {
        "state": "playing", "attributes": {"friendly_name": "慧尖卫星"}}})
    p = _pipe(settings=ST({"music.player_entity": "media_player.sat"}), ha=ha)
    assert arun(p.handle("播放晴天")).ok
    r = arun(p.handle("现在放的是什么歌"))
    assert "晴天" in r.text and "点歌记录" in r.text


def test_now_playing_idle_says_nothing_playing():
    ha = StateHa(states={"media_player.x": {"state": "idle", "attributes": {}}})
    p = _pipe(settings=ST({"music.player_entity": "media_player.x"}), ha=ha)
    r = arun(p.handle("现在放的是什么歌"))
    assert r.ok and "没有在放歌" in r.text


def test_area_mapped_selects_mapped_entity():
    ha = RecHa()
    p = _pipe(settings=ST({"music.player_entity": "media_player.def",
                           "music.area_entities": {"卧室": "media_player.bed"}}),
              ha=ha)
    r = arun(p.handle("在卧室播放晴天"))
    assert r.ok and ha.calls == [("media_player", "play_media",
                                  {"entity_id": "media_player.bed",
                                   "media_content_type": "music",
                                   "media_content_id": "晴天"})]


def test_area_unmapped_falls_back_with_honest_note():
    """区域 ∈ 词表但未映射 + 有默认端点 → 默认端点执行并如实补一句。"""
    ha = RecHa()
    p = _pipe(settings=ST({"music.player_entity": "media_player.def",
                           "music.area_entities": {}}), ha=ha)
    p.ha._areas = {"media_player.def": "客厅"}
    r = arun(p.handle("在客厅播放晴天"))
    assert r.ok and ha.calls[0][2]["entity_id"] == "media_player.def"
    assert "默认" in r.text


def test_area_unknown_word_falls_out_of_music_band():
    """词表取不到的前缀（书房未注册）→ 不瞎抽区域，级联自然放行。"""
    ha = RecHa()
    p = _pipe(settings=ST({"music.player_entity": "media_player.def",
                           "music.area_entities": {}}), ha=ha)
    p.ha._areas = {"media_player.def": "客厅"}
    r = arun(p.handle("在书房播放晴天"))
    assert r.source == "fallback" and ha.calls == []


def test_area_satellite_map_supplies_vocab():
    """satellite_areas 的值也进区域词表（无 HA 注册表缓存时仍可定向）。"""
    ha = RecHa()
    p = _pipe(settings=ST({"music.player_entity": "media_player.def",
                           "music.area_entities": {"卧室": "media_player.bed"},
                           "spatial.satellite_areas": {"10.0.0.9": "卧室"}}), ha=ha)
    r = arun(p.handle("在卧室播放晴天"))
    assert ha.calls[0][2]["entity_id"] == "media_player.bed"


def test_music_band_precedes_query_family_for_now_playing():
    """「音箱现在放的是什么歌」不得被查询族劫持（音乐带上移的立论用例）。"""
    class GrabQuery:
        async def answer(self, text):
            return "查询族抢答了" if "什么" in text else None
    p = _pipe(settings=ST({"music.player_entity": "media_player.x"}),
              ha=StateHa(states={"media_player.x": {"state": "idle",
                                                    "attributes": {}}}))
    p.query = GrabQuery()
    r = arun(p.handle("音箱现在放的是什么歌"))
    assert r.source == "music" and "没有在放歌" in r.text


def test_play_rejects_virtual_paths_and_urls():
    p = _pipe(settings=ST({"music.player_entity": "media_player.x"}), ha=RecHa())
    for bad in ("播放 https://a.b/c.mp3", "播放 media-source://x/y"):
        r = arun(p.handle(bad))
        assert not r.ok and "歌名" in r.text, bad
    assert p.ha.calls == []                        # 源头拦下，不打服务


def test_play_wait_note_default_on_off_switch():
    p = _pipe(settings=ST({"music.player_entity": "media_player.x"}), ha=RecHa())
    r = arun(p.handle("播放稻香"))
    assert r.ok and "等一小会儿" in r.text         # P2a 前首音预期管理（缺口④）
    p2 = _pipe(settings=ST({"music.player_entity": "media_player.x",
                            "music.expect_wait_note": False}), ha=RecHa())
    r2 = arun(p2.handle("播放稻香"))
    assert r2.ok and "等一小会儿" not in r2.text   # 第三方秒开档可关


def test_ledger_bounded_and_per_entity():
    p = _pipe(settings=ST({"music.player_entity": "media_player.x"}), ha=RecHa())
    for i in range(20):
        p._ledger_record(f"media_player.e{i % 3}", f"歌{i}")
    assert len(p._music_last) <= p._MUSIC_LEDGER_MAX
    assert p._ledger_peek("media_player.e0")       # 未被逐出的仍有记账


def test_music_wins_over_llm_with_area():
    p = _pipe(settings=ST({"music.player_entity": "media_player.def",
                           "music.area_entities": {"卧室": "media_player.bed"}}),
              ha=RecHa(), agent=ChatAgent())
    r = arun(p.handle("在卧室播放周杰伦"))
    assert r.source == "music"                     # LLM 依旧吞不掉点歌令
