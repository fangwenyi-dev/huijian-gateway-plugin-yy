"""T0/T1 级联收编回归（用例=迁移期实测矩阵，23 案 + 守卫）。"""
import asyncio

import pytest

from core.nlu.fast_path import FastPath, _is_complex_query
from core.nlu.textcnn import TextCNN


class FakeScenes:
    def __init__(self, triggers=("观影模式",)):
        self.triggers = set(triggers)

    async def refresh(self, force=False):
        pass

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    def check(self, text):
        for t in self.triggers:
            if text == t or text.startswith(t):
                return t
        return None

    async def verify_or_refresh(self, phrase):
        return phrase in self.triggers


@pytest.fixture(scope="module")
def tc(request):
    import os
    from pathlib import Path
    t = TextCNN(os.environ["HUIJIAN_NLU_DATA"])
    t._ensure()
    return t


@pytest.fixture()
def fp(tc, settings):
    return FastPath(FakeScenes(), tc, settings)


MATRIX = {
    # 中文基础指令
    "打开客厅的灯": ("TurnDeviceOn", {"area": "客厅"}),
    "关闭书房筒灯": ("TurnDeviceOff", {"area": "书房"}),
    "开灯": ("TurnDeviceOn", None),
    "关灯": ("TurnDeviceOff", None),
    "把风扇关掉": ("TurnDeviceOff", None),
    "关一下客厅窗帘": ("TurnDeviceOff", {"area": "客厅"}),
    # 窗户族（内倒=A→归一小写 a；集成侧 find_action_in_text 实测兼容）
    "内倒展厅窗户": ("ControlWindow", None),
    "暂停窗户动作": ("ControlWindow", None),
    "窗户内倒一下": ("ControlWindow", None),
    "内倒一下平推车": ("ControlWindow", None),      # 纠错表→平推窗
    # 调节族
    "亮度调到60%": ("AdjustDeviceAttribute", None),
    "色温调到4000k": ("AdjustDeviceAttribute", None),
    "风扇风量小一点": ("AdjustDeviceAttribute", None),
    "客厅的窗帘关一半": ("AdjustDeviceAttribute", {"area": "客厅"}),
    "把卧室灯开到50%": ("AdjustDeviceAttribute", {"area": "卧室"}),
    "卧室亮度调高一点": ("AdjustDeviceAttribute", {"area": "卧室"}),   # 属性词快捷（收编修复六）
    "风量大一点": ("AdjustDeviceAttribute", None),
    "亮一点": ("AdjustDeviceAttribute", None),
    # 场景
    "观影模式": ("HassTriggerVoiceScene", None),
    # 英文
    "turn on the living room light": ("TurnDeviceOn", None),
    # 窗型词落在设备名（2026-09-08 实机：曾被错产成 Turn*）
    "打开 办公室平开窗": ("ControlWindow", {"area": "办公室"}),
    "关闭卧室推拉门": ("ControlWindow", {"area": "卧室"}),
}

GUARD_NONE = [
    "空调调到26度",          # 空调缺区域守卫（原 v1.5 语义保留）
    "客厅现在多少度",        # 放行给查询族
    "现在几点了",
    "创建自动化",
    "帮我把所有灯都关掉",
    "客厅灯为什么不开",
]


@pytest.mark.parametrize("text,expect", MATRIX.items())
def test_matrix(fp, text, expect):
    intent, area_must = expect
    plan = asyncio.run(fp.match(text))
    assert plan is not None, f"{text} 未命中"
    assert plan.intent == intent, (text, plan.intent, plan.trace)
    if area_must:
        assert area_must.items() <= plan.args["target"][0].items(), (text, plan.args)


def test_window_type_device_name_rewrites(fp):
    plan = asyncio.run(fp.match("打开 办公室平开窗"))
    assert plan.intent == "ControlWindow" and plan.args["action"] == "open"
    assert plan.args["target"][0]["devices"][0]["name"] == "平开窗"
    assert any("窗型纠正" in t for t in plan.trace), plan.trace
    off = asyncio.run(fp.match("关闭办公室推拉窗"))
    assert off.intent == "ControlWindow" and off.args["action"] == "close"


def test_curtain_stays_turn_device(fp):
    plan = asyncio.run(fp.match("关闭客厅窗帘"))
    assert plan is not None and plan.intent == "TurnDeviceOff"


@pytest.mark.parametrize("text", GUARD_NONE)
def test_guards_return_none(fp, text):
    assert asyncio.run(fp.match(text)) is None


def test_window_action_lowercase(fp):
    plan = asyncio.run(fp.match("内倒展厅窗户"))
    assert plan.args["action"] == "a"          # 收编改造点 5：归一小写


def test_complex_query_guard_semantics():
    assert _is_complex_query("客厅灯状态如何")
    assert not _is_complex_query("客厅多少度")   # 温度查询放行（原 L269 语义）
    assert _is_complex_query("把客厅保存为会客模式")  # M1 场景创建不接管


def test_scene_uncached_miss(fp):
    fp.scenes = FakeScenes(triggers=())
    assert asyncio.run(fp.match("观影模式")) is None


# ── 场景契约恒最高优先（2026-09-10 真机实锤：触发词被 T0 动作正则吃掉）────
def test_scene_trigger_shaped_like_device_command(tc, settings):
    """触发词=设备指令形态（"打开空调"/"打开客厅的灯"）必须触发场景。
    真机日志病灶：创建成功、复述触发词却落兜底；更糟的形态会直接去开关设备。"""
    fp = FastPath(FakeScenes(triggers=("打开空调", "打开客厅的灯")), tc, settings)
    for s in ("打开空调", "打开客厅的灯"):
        plan = asyncio.run(fp.match(s))
        assert plan is not None, s
        assert (plan.source, plan.intent) == ("scene", "HassTriggerVoiceScene"), (s, plan)
        assert plan.args["trigger_phrase"] == s


def test_scene_prefix_fallback_after_guard_miss(tc, settings):
    """构造失败（空调缺区域守卫等）后仍回落到触发词前缀形态，不再落兜底。"""
    fp = FastPath(FakeScenes(triggers=("打开空调",)), tc, settings)
    for s in ("打开空调吧", "打开空调场景"):
        plan = asyncio.run(fp.match(s))
        assert plan is not None, s
        assert plan.intent == "HassTriggerVoiceScene", (s, plan)
        assert plan.args["trigger_phrase"] == "打开空调"


def test_scene_priority_does_not_swallow_long_command(tc, settings):
    """等值优先的回归护栏：短触发词"开灯"不得把「开灯亮度50」整句吞成场景。"""
    fp = FastPath(FakeScenes(triggers=("开灯",)), tc, settings)
    long_cmd = asyncio.run(fp.match("开灯亮度50"))
    assert long_cmd is not None and long_cmd.intent == "TurnDeviceOn", long_cmd
    exact = asyncio.run(fp.match("开灯"))
    assert exact is not None and exact.intent == "HassTriggerVoiceScene", exact


def test_scene_priority_keeps_area_command(tc, settings):
    """带区域的长句不误吞（触发词"打开空调"vs「打开办公室的空调」）。"""
    fp = FastPath(FakeScenes(triggers=("打开空调",)), tc, settings)
    plan = asyncio.run(fp.match("打开办公室的空调"))
    assert plan is not None and plan.intent == "TurnDeviceOn", plan
    assert plan.args["target"][0]["area"] == "办公室"


# ── 连排句禁走单发（2026-09-10 真机：只执行了后一个）──────────────
def test_serial_sentence_never_single_shot(tc, settings):
    """无连接词的动词连排必须由链发逐段执行；单发通路一律拒（交上层）。

    真机病灶：「关闭办公室射灯关闭办公室平开窗」被 T0 当一句——第一子句被吃成
    区域残渣（"办公室射灯关闭办公室"），只有名字匹配上的平开窗动了，还回
    「已经帮你执行了」；反向形态「关闭客厅射灯打开书房灯」则是后半句被静默丢掉。"""
    fp = FastPath(FakeScenes(triggers=()), tc, settings)
    for s in ("关闭办公室射灯关闭办公室平开窗",
              "打开客厅的灯关闭客厅的窗帘",
              "关闭客厅射灯打开书房灯"):
        assert asyncio.run(fp.match(s)) is None, s
    # 对照：单句不得被连排闸误伤（含把字句/单动作）
    for s, intent in (("关闭办公室平开窗", "ControlWindow"),
                      ("打开 办公室射灯", "TurnDeviceOn"),
                      ("打开客厅的灯", "TurnDeviceOn")):
        plan = asyncio.run(fp.match(s))
        assert plan is not None and plan.intent == intent, (s, plan)


def test_serial_clause_hits_alone(tc, settings):
    """链发按段调用单发通路——每段必须自己能命中（否则链发拒绝，宁欠勿过）。"""
    fp = FastPath(FakeScenes(triggers=()), tc, settings)
    a = asyncio.run(fp.match("关闭办公室射灯"))
    b = asyncio.run(fp.match("关闭办公室平开窗"))
    assert a is not None and a.intent == "TurnDeviceOff"
    assert b is not None and b.intent == "ControlWindow"


def test_office_spotlight_target(fp):
    # 修A 端到端锁定（用户实机轨迹：曾产出 {name:灯} 无区域 → HA 等值匹配全空 "没找到这个设备"）
    plan = asyncio.run(fp.match("打开 办公室射灯"))
    assert plan is not None and plan.intent == "TurnDeviceOn"
    assert plan.args["target"] == [{"area": "办公室",
                                    "devices": [{"name": "射灯", "domains": ["light"]}]}], plan.trace


# ── 体验批 P2-16/10/12 端到端（真 FastPath 链路）────────────────
def test_polite_end_to_end(fp):
    plan = asyncio.run(fp.match("麻烦把客厅灯关掉好吗"))
    assert plan and plan.intent == "TurnDeviceOff"
    assert plan.args["target"][0]["area"] == "客厅"
    assert any(t.startswith("礼貌→") for t in plan.trace)


def test_pronoun_is_not_device_name(fp):
    plan = asyncio.run(fp.match("把它打开"))
    assert plan and plan.intent == "TurnDeviceOn"
    assert not plan.args                              # 空目标形态，交上下文注入
    assert any("代词目标" in t for t in plan.trace)
    # 真件演示实锤形态（2026-09-12）：代词+副词+动作，绝不许吃成设备名
    for t, want in [("关掉它", "TurnDeviceOff"), ("把它也关了", "TurnDeviceOff"),
                    ("那个也关掉", "TurnDeviceOff"), ("关掉那个", "TurnDeviceOff")]:
        p = asyncio.run(fp.match(t))
        assert p and p.intent == want and not p.args, (t, p and (p.intent, p.args))
    # 反例："它的灯"含真实目标词，不能被误判成代词空目标
    p = asyncio.run(fp.match("打开它的灯"))
    assert p and p.args.get("target") and not any("代词目标" in x for x in p.trace)


def test_anaphora_adverb_lane(fp):
    plan = asyncio.run(fp.match("再亮一点"))
    assert plan and plan.intent == "AdjustDeviceAttribute"
    assert plan.args.get("attribute") == "brightness"
    assert any(t.startswith("回指→") for t in plan.trace)


def test_lock_unlock_lane(fp):
    # E2E 补洞（2026-09-12）：开解锁令曾全 fallback，确认环与 NLU 脱节
    for t, want in [("解锁大门", "HassUnlock"), ("大门开锁", "HassUnlock"),
                    ("把大门解锁", "HassUnlock"), ("开锁", "HassUnlock"),
                    ("锁上大门", "HassLock"), ("大门锁上了", "HassLock")]:
        p = asyncio.run(fp.match(t))
        assert p and p.intent == want, (t, p and (p.intent, p.trace))
    assert asyncio.run(fp.match("还没上锁")) is None      # 陈述不是命令
    p = asyncio.run(fp.match("开灯"))                      # 不被解锁车道误吃
    assert p and p.intent == "TurnDeviceOn"


def test_compound_residue_refused(fp):
    # 链式层否决后的残句绝不单发错猜（实测曾把「客厅的灯，然后再关闭窗帘」
    # 整体错配成客厅窗帘）
    assert asyncio.run(fp.match("打开客厅的灯，然后再关闭窗帘")) is None


def test_music_generic_word_not_device(fp):
    # 音乐泛词守卫：「关掉音乐」不当设备名（交音乐带），但「关掉音乐开关」
    # 与带区域的「关掉客厅的音乐」仍可正常解析
    assert asyncio.run(fp.match("关掉音乐")) is None
    assert asyncio.run(fp.match("关音乐")) is None
    p = asyncio.run(fp.match("关掉音乐开关"))
    assert p is not None and p.intent == "TurnDeviceOff"


def test_window_pause_survives_music_lookahead(fp):
    # 负面向：窗帘暂停语义不因音乐带让位而回归（裸"暂停"/"暂停窗帘"仍 ControlWindow）
    p1 = asyncio.run(fp.match("暂停"))
    assert p1 is not None and p1.intent == "ControlWindow"
    p2 = asyncio.run(fp.match("暂停窗帘"))
    assert p2 is not None and p2.intent == "ControlWindow"
    # 让位面向：后接音乐补语的播控令不产窗户计划
    assert asyncio.run(fp.match("停止播放")) is None
    assert asyncio.run(fp.match("暂停音乐")) is None
