# -*- coding: utf-8 -*-
"""2026-09-30 现场双修回归钉（用户日志三条）：

① 「展厅催拉窗开到百分之三十」——「开到百分之三十」结构本身一直是认的
  （POSITION_CASES 已钉），真凶是 STT 近音「催拉窗」：⑥ 单字泛称档把整词
  折成「窗」→ 区域+泛称=整区窗扇出，被过宽闸 clarify 拦成「说具体点」
  （用户其实说具体了，是识别歪了字）。根治=targets._generic_rescue 音节级
  近音救援（一切 cui 系变体一网打尽），催拉窗字面表作快路双保险。
② 英文语音——SenseVoice 转写正确（日志可证），t0 也拆得开，但客户 HA 全
  中文命名 → 必「没找到符合条件的设备」。路线=英文目标桥：bilingual_targets
  并集追加中文等价目标（英文命名 HA 零回归）；英文窗词换中文形顶前
  （ControlWindow 集成端只读 targets[0]）。
③ _parse_position 谎报闸：cn2num 对非纯数词静默返 0（'最大'→position 0=
  完全关窗还播成功）——裸中文 token 必须逐字全是数词才可信。

另钉 stt.local_model 引擎选择键路（Web 下拉 → settings → AsrEngine.model_key）。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_en_win_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core.nlu import corrector, targets as T  # noqa: E402
from core.nlu.fast_path import FastPath, _parse_position  # noqa: E402


class FakeScenes:
    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass

    async def refresh(self, force=False):
        pass

    def check(self, text):
        return None

    async def verify_or_refresh(self, phrase):
        return False


@pytest.fixture(autouse=True)
def _static_vocab():
    T.clear_vocab()
    yield
    T.clear_vocab()


@pytest.fixture()
def settings():
    from core.settings import Settings
    return Settings(Path(os.environ["HUIJIAN_DATA"]) / f"enwin-{os.getpid()}.json")


@pytest.fixture()
def fp(settings):
    return FastPath(FakeScenes(), None, settings)


def _match(fp, text):
    return asyncio.run(fp.match(text))


# ── ① 泛称近音折叠救援 ────────────────────────────────────────────
RESCUE_CASES = [
    ("催拉窗", None, "推拉窗"),
    ("展厅翠拉窗", "展厅", "推拉窗"),      # 表外变体——只有救援能救（cui 同音族）
    ("提生窗", None, "提升窗"),            # 全同音 d=0
    ("书房别倒窗", "书房", "内倒窗"),      # 区域剥离后残余修饰段bie/nei一音节之差
]


@pytest.mark.parametrize(("raw", "area", "name"), RESCUE_CASES)
def test_generic_rescue_replaces_collapse(raw, area, name):
    a, n, _score = T.parse_target(raw)
    assert n == name, (raw, a, n)
    if area:
        assert a == area, (raw, a)
    else:
        assert not a, (raw, a)


NORESCUE_CASES = [
    ("宁开窗", "窗"),      # 对 内开窗/平开窗 各差一音节成平局 → 不猜
    ("拉窗", "窗"),        # 两字噪音窗不入（拉窗→天窗 形似实错）
    ("催拉窗帘", "窗帘"),  # 帘族先行命中，不进救援
    ("3号窗", "窗"),       # 含数字非纯汉字修饰段
]


@pytest.mark.parametrize(("raw", "name"), NORESCUE_CASES)
def test_generic_rescue_stays_conservative(raw, name):
    _a, n, _s = T.parse_target(raw)
    assert n == name, (raw, n)


def test_cuilachuang_position_sentence_end_to_end(fp):
    plan = _match(fp, "展厅催拉窗开到百分之三十")
    assert plan is not None, plan
    assert plan.intent == "ControlWindow" and plan.args.get("position") == 30
    tgt = plan.args["target"][0]
    assert tgt["area"] == "展厅" and tgt["devices"][0]["name"] == "推拉窗", tgt


def test_cuilachuang_variant_rescues_without_table(fp):
    # 「翠拉窗」不在纠错表——纯靠音节救援
    assert "翠拉窗" not in corrector.BASE_CORRECTIONS
    plan = _match(fp, "展厅翠拉窗开到百分之三十")
    assert plan is not None and plan.intent == "ControlWindow"
    assert plan.args.get("position") == 30
    assert plan.args["target"][0]["devices"][0]["name"] == "推拉窗", plan.args


def test_corrector_pins_field_log_variant():
    assert "推拉窗" in corrector.apply("展厅催拉窗开到百分之三十")


# ── ③ _parse_position 谎报闸 ─────────────────────────────────────
@pytest.mark.parametrize("token", ["最大", "全部", "顶", "三成", "十分之三"])
def test_parse_position_rejects_nonnumeral_tokens(token):
    assert _parse_position(token, True) is None


def test_parse_position_keeps_numeral_tokens():
    assert _parse_position("三十", True) == 30
    assert _parse_position("十", True) == 10
    assert _parse_position("二十五", True) == 25
    assert _parse_position("一半", False) == 50


# ── ② 英文目标桥 ─────────────────────────────────────────────────
def _clone(targets, area, name):
    for t in targets:
        if (t.get("area") or "") == area:
            for d in t.get("devices") or []:
                if d.get("name") == name:
                    return d
    return None


def test_en_light_bilingual(fp):
    plan = _match(fp, "turn on the office light")
    assert plan is not None and plan.intent == "TurnDeviceOn"
    tgt = plan.args["target"]
    assert tgt[0] == {"area": "office",
                      "devices": [{"name": "light", "domains": []}]}, tgt
    d = _clone(tgt, "办公室", "灯")
    assert d is not None and d["domains"] == ["light"], tgt


def test_en_plural_and_in_suffix(fp):
    for sent in ("turn on the office lights", "turn on the lights in the office"):
        plan = _match(fp, sent)
        assert plan is not None, sent
        assert _clone(plan.args["target"], "办公室", "灯") is not None, (sent, plan.args)


def test_en_bare_device_no_empty_area_clone(fp):
    # 无区域的英文全屋句：中文克隆不得带 area:''（集成端空串=unset_area_constraint
    # 特殊语义，会把"有区域的实体"全排除掉）
    plan = _match(fp, "turn on the lights")
    assert plan is not None and plan.intent == "TurnDeviceOn"
    tgt = plan.args["target"]
    zh = [t for t in tgt if any((d.get("name") == "灯") for d in t.get("devices") or [])]
    assert zh and all("area" not in t for t in zh), tgt


def test_en_multiword_device_and_area(fp):
    plan = _match(fp, "turn on the bedroom air conditioner")
    assert plan is not None and plan.intent == "TurnDeviceOn"
    d = _clone(plan.args["target"], "卧室", "空调")
    assert d is not None and d["domains"] == ["climate"], plan.args


def test_en_window_flips_controlwindow_head_first(fp):
    # ControlWindow 集成端只读 targets[0]——中文形必须顶位，且不再追加英文尾
    plan = _match(fp, "close the bedroom window")
    assert plan is not None and plan.intent == "ControlWindow"
    assert plan.args.get("action") == "close"
    tgt = plan.args["target"]
    assert tgt[0]["area"] == "卧室" and tgt[0]["devices"][0]["name"] == "窗", tgt


def test_en_ac_bare_still_area_guarded(fp):
    # 裸 ac 与裸"空调"同权：缺区域拒执行（不全屋扇出）
    plan = _match(fp, "turn on the ac")
    assert plan is None or plan.intent != "TurnDeviceOn", getattr(plan, "args", None)


def test_chinese_targets_get_no_bilingual_tail(fp):
    plan = _match(fp, "打开客厅的灯")
    assert plan is not None
    assert plan.args["target"] == [
        {"area": "客厅", "devices": [{"name": "灯", "domains": ["light"]}]}], plan.args


def test_bilingual_targets_idempotent_on_pure_cn():
    entries = [{"area": "展厅", "devices": [{"name": "推拉窗", "domains": []}]}]
    assert T.bilingual_targets(entries) is entries


# ── ③ stt.local_model 引擎选择键路（后端键位 + Web 面板钉）───────
def test_asr_model_key_follows_local_model(settings):
    from core.asr import AsrEngine
    eng = AsrEngine(settings, None)
    assert eng.model_key == "asr_sensevoice_small"          # 默认档
    settings.update({"stt": {"local_model": "paraformer"}})  # Web 保存的写入形
    assert eng.model_key == "asr_paraformer_bilingual"
    settings.update({"stt": {"local_model": "garbage"}})
    assert eng.model_key == "asr_sensevoice_small"          # 未知值回落默认


def test_www_local_model_selector_wired():
    html = (HERE / "www" / "index.html").read_text(encoding="utf-8")
    assert 'id="stt_local_model"' in html, "本地模型下拉未挂线"
    assert 'id="sttLocalBox"' in html, "云档隐藏盒未挂线"
    assert 'local_model:$("#stt_local_model").value' in html, "保存体未带 local_model"
    assert 'S.stt.local_model==="paraformer"' in html, "loadSettings 未回填选择"
    assert '"paraformer"' in html and "stt_kind" in html, "状态卡未显示在载引擎"


# ── ④ 2026-09-30 数据集对账批：电动窗六表同步 + 帘字防吞 + 动态区域表 ──
CC = HERE / "custom_components" / "huijian_ai"


def test_dongdianchuang_six_table_sync_source_pins():
    from core.nlu.fast_path import _WINDOW_TYPES as W
    assert "电动窗" in W
    assert "电动窗" in T.KNOWN_DEVICES_PREFIX and "电动窗帘" in T.KNOWN_DEVICES_PREFIX
    assert "电动窗" in T.KNOWN_DEVICES and "电动窗帘" in T.KNOWN_DEVICES
    const = (CC / "intent_window_const.py").read_text(encoding="utf-8")
    mapping = const[const.index("WINDOW_NAME_MAPPING = {"):const.index("WINDOW_ALL_NAMES")]
    assert '"电动窗": "电动窗",' in const or '"电动窗": "电动窗"' in mapping
    assert mapping.index('"电动窗"') < mapping.index('"窗":'), "泛称 窗 键截胡具名电动窗"
    shared = (CC / "intent_device_shared.py").read_text(encoding="utf-8")
    assert '"电动窗"' in shared
    ctl = (CC / "intent_window_control.py").read_text(encoding="utf-8")
    assert "电动窗" in ctl, "intent 描述 valid names 未同步（LLM 槽位引导面）"
    # 帘族短路闸（本批同修存量缺陷：'智能窗帘' 曾被 '智能窗' 键截胡）
    assert 'if any(k in name_lower for k in ("帘", "纱窗", "百叶")):' in const, \
        "extract_window_name 帘族顶层闸丢失"


def test_window_type_curtain_shortcircuit():
    from core.nlu.fast_path import _window_type
    assert _window_type("电动窗") == "电动窗"
    assert _window_type("电动窗帘") is None       # 帘=cover，绝不是窗
    assert _window_type("智能窗帘") is None
    assert _window_type("百叶窗") is None         # 百叶=帘族既有纪律


def test_dongdianchuang_recognized(fp):
    plan = _match(fp, "打开展厅电动窗")
    assert plan is not None and plan.intent == "ControlWindow"
    assert plan.args["action"] == "open"
    assert plan.args["target"][0]["devices"][0]["name"] == "电动窗"
    assert plan.args["target"][0]["area"] == "展厅"


def test_dongdianchuang_position(fp):
    plan = _match(fp, "电动窗开到百分之三十")
    assert plan is not None and plan.intent == "ControlWindow"
    assert plan.args.get("position") == 30
    assert plan.args["target"][0]["devices"][0]["name"] == "电动窗"


def test_dongdian_chuanglian_stays_cover(fp):
    plan = _match(fp, "关闭电动窗帘")
    assert plan is not None and plan.intent == "TurnDeviceOff"
    d = plan.args["target"][0]["devices"][0]
    assert d["name"] == "电动窗帘" and d["domains"] == ["cover"], plan.args
    plan2 = _match(fp, "打开展厅电动窗帘")
    assert plan2.intent == "TurnDeviceOn"
    assert plan2.args["target"][0]["devices"][0]["name"] == "电动窗帘"
    assert plan2.args["target"][0]["area"] == "展厅"


def test_zhinengchuanglian_no_window_hijack(fp):
    # 存量缺陷回归钉：'智能窗帘' 曾被 '智能窗' 词根截胡→窗型纠正→按窗钮
    plan = _match(fp, "关闭智能窗帘")
    assert plan is not None and plan.intent == "TurnDeviceOff", plan.args
    assert "电动窗" not in str(plan.args) and "智能窗" not in str(plan.args), plan.args


def test_dynamic_area_table_rescues_non_suffix_areas():
    T.sync_areas(["主卧", "次卧", "阳台", "玄关"])
    a, n, _s = T.parse_target("主卧灯")
    assert (a, n) == ("主卧", "灯"), (a, n)
    a, n, _s = T.parse_target("阳台窗")
    assert (a, n) == ("阳台", "窗"), (a, n)
    a, n, _s = T.parse_target("次卧窗")
    assert (a, n) == ("次卧", "窗"), (a, n)


def test_base_areas_static_cold_start_no_registry():
    # autouse fixture 已 clear_vocab（连带清空 _dyn_areas）——本钉验冷启动
    # （HA 区域注册表未同步）时，数据集高频通用区名仍作静态基准可析出。
    assert not T._dyn_areas, "前置：动态区表须为空才是真冷启动"
    for area in ("主卧", "次卧", "阳台", "玄关", "车库", "露台", "走廊"):
        assert area in T.BASE_AREAS and T._area_like(area), area
    # 不带 AREA_SUFFIX 尾字的区名（卧/台/关/库/廊 均非尾字）——旧逻辑全丢，
    # 静态基准兜住：区域+裸设备词/区域+泛窗 均正确拆分。
    for raw, area, name in [
        ("主卧灯", "主卧", "灯"), ("次卧窗", "次卧", "窗"),
        ("阳台灯", "阳台", "灯"), ("车库门", "车库", "门"),
        ("玄关窗", "玄关", "窗"), ("走廊灯", "走廊", "灯"),
    ]:
        a, n, _s = T.parse_target(raw)
        assert (a, n) == (area, name), (raw, a, n)


def test_base_areas_do_not_swallow_device_words():
    # 护栏：BASE_AREAS 只做区域前缀，绝不吞成设备词（「阳台」不是设备）；
    # 且带设备尾字的纯设备词不受影响（「筒灯」原样析出、不误挂区域）。
    for area_word in sorted(T.BASE_AREAS):
        assert T._area_like(area_word), area_word       # 区域判据只认它做区域（前缀车道）
        _a, n, _s = T.parse_target(area_word)
        # 裸区词单独成目标 → 不得虚构设备：设备名应为空/None，绝不等于区域词本身
        assert not n, (area_word, _a, n)
    # 「阳台」只以「区域前缀」身份生效：区域+修饰段形里正确析出区段
    a, n, _s = T.parse_target("阳台北面")
    assert (a, n) == ("阳台", "北面"), (a, n)
    # 纯设备词零扰动：不因 BASE_AREAS 接入被误判出区域
    a, n, _s = T.parse_target("筒灯")
    assert a is None and n == "筒灯", (a, n)


def test_known_areas_gate_includes_base_without_registry():
    # 过宽拦截/klar 裁决的 _known_areas 即使注册表全空，也应认得通用区名
    # （否则"打开主卧"冷启动不拦=整区冒按）。
    import core.pipeline as PP

    class _Ha:
        _areas = {}

    class _Cfg:
        def get(self, k, d=None):
            return d if d is not None else {}

    p = PP.Pipeline.__new__(PP.Pipeline)   # 不跑 __init__（零依赖探针）
    p.ha = _Ha()
    p.settings = _Cfg()
    assert T.BASE_AREAS <= p._known_areas()   # 静态基准恒在，注册表空也不哑


def test_dynamic_area_position_lane(fp):
    T.sync_areas(["玄关"])
    plan = _match(fp, "玄关窗开到百分之三十")
    assert plan is not None and plan.intent == "ControlWindow"
    assert plan.args.get("position") == 30
    assert plan.args["target"][0]["area"] == "玄关", plan.args


def test_en_area_table_dataset_additions(fp):
    plan = _match(fp, "turn on the entrance light")
    assert plan is not None
    assert _clone(plan.args["target"], "玄关", "灯") is not None, plan.args
    plan = _match(fp, "turn on the workshop lights")
    assert _clone(plan.args["target"], "车间", "灯") is not None, plan.args


# ── ⑤ 2026-10-01 复核补口：区域尾字×设备词首字跨词根截胡 ──────────
# 病灶（本批 ④ 段冷启动钉揪出的遗留红，HEAD 既有）：⑥ 设备词子串扫描是
# 「先命中先得」，而「阳台灯/露台灯」内含设备词 台灯（起点 idx=1 落在区名
# 「阳台」内部）→ 「台」被吞进设备名、区域整段丢失：用户喊阳台灯，HA 去找
# 台灯实体（假动作/误设备）。动态区表同型（「南阳台灯」）一并复现。
# 判据 _area_split_wins：命中点之后仍存在更长的区域名前缀（partial overlap）
# → 该命中是拼词假象，跳过让单字泛称车道以「整区名+设备字」承接。
SPLIT_HIJACK_CASES = [
    ("阳台灯", "阳台", "灯"),        # 数据集 area x20，本批冷启动钉原红项
    ("露台灯", "露台", "灯"),        # 同型（台 也是 露台 尾字）
    ("走廊灯", "走廊", "灯"),        # 无拼词冲突的对照组
    ("主卧灯", "主卧", "灯"),
]


@pytest.mark.parametrize(("raw", "area", "name"), SPLIT_HIJACK_CASES)
def test_area_tail_does_not_merge_into_device_word(raw, area, name):
    a, n, _s = T.parse_target(raw)
    assert (a, n) == (area, name), (raw, a, n)


def test_area_split_boundary_no_over_trigger():
    # 零重叠（设备词起点==区名长）不得误伤：区+具名设备原样保留。
    a, n, _s = T.parse_target("阳台台灯")
    assert (a, n) == ("阳台", "台灯"), (a, n)
    a, n, _s = T.parse_target("阳台落地灯")
    assert (a, n) == ("阳台", "落地灯"), (a, n)
    # 纯设备词零扰动（区名判据不参与）
    assert T.parse_target("台灯")[:2] == (None, "台灯")
    assert T.parse_target("床头台灯")[:2] == (None, "台灯")


def test_area_split_with_dynamic_registry():
    # 客户自定义区名同类（注册表通道）：南阳台灯 不得塌成台灯。
    T.sync_areas(["南阳台", "生活阳台"])
    assert T.parse_target("南阳台灯")[:2] == ("南阳台", "灯")
    assert T.parse_target("生活阳台灯")[:2] == ("生活阳台", "灯")
    assert T.parse_target("南阳台台灯")[:2] == ("南阳台", "台灯")


def test_area_split_keeps_registry_self_qualified_name():
    # idx==0 例外（与本闸同批收口）：HA 实体全名自带区域——「客厅空调」经
    # sync_vocab 入表后 ⑥ 长词优先整段命中，若被拆成 area=客厅+裸空调，
    # 未挂 area 的现场必 miss（test_nlu_llm_boundary 同钉，此处锁判据）。
    T.sync_vocab({"climate.客厅空调": {"attributes": {"friendly_name": "客厅空调"}}})
    assert T.parse_target("客厅空调")[:2] == (None, "客厅空调")
    # 反向对照：真截胡形态（命中起点落在区名内部）仍须裁掉。
    T.clear_vocab()
    assert T.parse_target("阳台灯")[:2] == ("阳台", "灯")


def test_balcony_light_end_to_end_keeps_area(fp):
    plan = _match(fp, "打开阳台灯")
    assert plan is not None and plan.intent == "TurnDeviceOn", plan
    t = plan.args["target"][0]
    assert t["area"] == "阳台", plan.args
    assert t["devices"][0]["name"] == "灯", plan.args
