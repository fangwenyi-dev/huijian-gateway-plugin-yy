"""v1.0.41 修复清单（F1–F14）回归钉。

纪律（沿用 test_v1040_fixes）：
· 前端无 DOM 环境：HTML/JS 改动做**源码形态钉**（防"实现被回退"，不防样式）；
· custom_components 侧依赖 homeassistant（测试环境不装）：_js/_validate_trigger
  用 ast 从 api.py 外科摘出单测，其余做源码钉；
· NLU 改动用 FakeTC（真模型矩阵在 test_fast_path），语序归一/守卫/全屋各钉正反例。
"""
import ast
import asyncio
import html as html_mod
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from conftest import FakeHAClient                                  # noqa: E402
from core.admin_api import (_PHRASE_RE, CTX_KEY, _auto_edit,        # noqa: E402
                            _automations, _scene_row, _scenes)
from core.nlu.fast_path import (FastPath, _cover_intent,            # noqa: E402
                                _cover_wordorder, _is_complex_query)
from core.nlu.scenes import SceneCache, TTL_S                       # noqa: E402
from tests.test_fast_path import FakeScenes                         # noqa: E402

API_SRC = (ROOT / "custom_components" / "huijian_ai" / "api.py").read_text(encoding="utf-8")
IA_SRC = (ROOT / "custom_components" / "huijian_ai" / "intent_automation.py").read_text(encoding="utf-8")
MANAGE_SRC = (ROOT / "custom_components" / "huijian_ai" / "templates" /
              "manage.html").read_text(encoding="utf-8")
WWW = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
VCSS = (ROOT / "www" / "css" / "voice.css").read_text(encoding="utf-8")


def _api_ns():
    """ast 摘出 api.py 的 _js / _validate_trigger / _TRIGGER_ENTITY_RE 隔离执行
    （三个都是纯函数/常量，不触 homeassistant 导入）。"""
    tree = ast.parse(API_SRC)
    ns = {"re": re, "html_mod": html_mod}
    for node in tree.body:
        hit_fn = isinstance(node, ast.FunctionDef) and node.name in ("_js", "_validate_trigger")
        hit_as = (isinstance(node, ast.Assign) and
                  any(getattr(t, "id", "") == "_TRIGGER_ENTITY_RE" for t in node.targets))
        if hit_fn or hit_as:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "<api.py>", "exec"), ns)
    return ns


NS = _api_ns()
_js = NS["_js"]
_validate_trigger = NS["_validate_trigger"]


# ── F1：onclick 双层转义（先 JS 单引号层、再 HTML 属性层）────────────
def test_f1_js_escape_two_layer_order():
    assert _js("a'b\\c") == "a\\&#x27;b\\\\c"          # 反斜杠先双写，引号两层各管各
    assert _js("a\nb") == "a\\nb"                      # 换行 → JS 两字符转义
    assert _js('a"b') == "a&quot;b"                    # 双引号在属性语境必须死
    evil = "');alert(document.cookie);('"
    out = _js(evil)
    assert "<" not in out and '"' not in out
    assert "'" not in out                              # 单引号只以 \&#x27; 形态存在
    # 顺序不可反的反证：若先 html 再 JS，' 会变成 &#x27; 被 JS 层原样带着走，
    # 浏览器属性解码后 JS 拿到 `&#x27;` 字面量 → 编辑弹窗回写污染（v1.0.40 病灶）。


def test_f1_api_onclick_values_all_js_escaped():
    lines = [l for l in API_SRC.splitlines() if 'onclick="' in l]
    assert lines, "api.py 行内 onclick 模板消失？"
    assert all("_js(" in l for l in lines), [l for l in lines if "_js(" not in l]
    assert "onclick=\"deleteScene('{_js(scene_id_raw)}', '{_js(trigger_raw)}', event)\"" in API_SRC
    assert "openEditAuto('{_js(auto_id_raw)}', " in API_SRC
    assert 'above_js = _js("" if above is None else above)' in API_SRC   # None→空串
    assert 'below_js = _js("" if below is None else below)' in API_SRC


def test_f1_manage_handlers_take_event():
    assert MANAGE_SRC.count("ev ? ev.target : event.target") == 4
    for sig in ("function deleteScene(sceneId, triggerPhrase, ev)",
                "function deleteAutomation(automationId, triggerText, ev)",
                "function testScene(triggerPhrase, ev)",
                "function testAutomation(autoId, ev)"):
        assert sig in MANAGE_SRC, sig


# ── F2：PUT trigger 形态闸 ──────────────────────────────────────────
def test_f2_validate_trigger_accepts():
    for ok in ({"entity_id": "sensor.a1_b2", "above": 28},
               {"entity_id": "sensor.t", "above": 28.5, "below": 10},
               {"at": "07:00"},                              # 时间形态不带 entity_id
               {"platform": "time", "at": "07:00"},
               {"entity_id": "light.x", "to": "on"},
               {"entity_id": "light.x", "to": True, "attribute": "brightness"},
               {"for": "00:05:00"},                          # 未知键标量放行
               {}):
        assert _validate_trigger(ok) == "", ok


def test_f2_validate_trigger_rejects():
    assert _validate_trigger("not a dict") == "trigger"
    assert _validate_trigger(None) == "trigger"
    assert _validate_trigger({"entity_id": "a',alert"}) == "entity_id"
    assert _validate_trigger({"entity_id": "sensor.A"}) == "entity_id"      # 大写
    assert _validate_trigger({"entity_id": "sensor.a.b"}) == "entity_id"    # 双点
    assert _validate_trigger({"entity_id": "sensor" * 20}) == "entity_id"   # 无点长串
    assert _validate_trigger({"above": True}) == "above"                    # bool 非阈值
    assert _validate_trigger({"below": "28"}) == "below"
    assert _validate_trigger({"to": {"a": 1}}) == "to"
    assert _validate_trigger({"attribute": ["x"]}) == "attribute"
    assert _validate_trigger({"to": "x" * 129}) == "to"
    assert _validate_trigger({"weird": {"k": 1}}) == "weird"                # 未知键 dict 拒
    assert _validate_trigger({"weird": ["a"]}) == "weird"


# ── F3：admin 阈值编辑不许把自动化静默改成"任何状态变化"触发 ────────
def _op_ctx_edit(ha):
    scenes = SimpleNamespace(triggers=[], all=lambda: [],
                             refresh=lambda **k: None)
    ctx = SimpleNamespace(ha=ha, scenes=scenes, settings=None, pipeline=None)
    return ctx


def _edit_req(ctx, payload):
    class R:
        def __init__(self): self.app = {CTX_KEY: ctx}
        async def json(self): return payload
    return R()


def test_f3_auto_edit_requires_threshold():
    ha = FakeHAClient(writes={("PUT", "/api/huijian-ai/automations/a1"): {"success": True}})
    ctx = _op_ctx_edit(ha)
    # entity-only（把阈值全清光）→ 拒
    r = _resp(asyncio.run(_auto_edit(_edit_req(ctx, {
        "automation_id": "a1", "trigger": {"entity_id": "sensor.t"}}))))
    assert r["ok"] is False and "需至少一个数值阈值（高于/低于）" in r["error"]
    # 表单清空串（above=""&below=""）同样拒
    r2 = _resp(asyncio.run(_auto_edit(_edit_req(ctx, {
        "automation_id": "a1",
        "trigger": {"entity_id": "sensor.t", "above": "", "below": None}}))))
    assert r2["ok"] is False and "阈值" in r2["error"]
    # 给一个数值阈值 → 照常过（不误伤）
    r3 = _resp(asyncio.run(_auto_edit(_edit_req(ctx, {
        "automation_id": "a1", "trigger": {"entity_id": "sensor.t", "above": "28"}}))))
    assert r3["ok"] is True
    assert ha.written[-1][2]["trigger"] == {"entity_id": "sensor.t", "above": 28.0}


def _resp(resp):
    import json as _json
    return _json.loads(resp.body)


# ── F4：测试执行失败如实折算 ────────────────────────────────────────
def test_f4_source_pins():
    assert '"{len(fails)}/{ntotal} 个动作执行失败：{first_err[:120]}"' in API_SRC
    assert '{"success": True, "executed": ntotal}' in API_SRC
    assert ") -> list[tuple[str, bool, str]]:" in IA_SRC
    assert 'f"不支持的意图: {intent_name}"' in IA_SRC
    # manage.html 测试按钮必须把 data.error 播报出来（success False ≠ 沉默）
    assert "data.error" in MANAGE_SRC and "data.success" in MANAGE_SRC


# ── F5：refresh/拉取成败改显式 bool，admin 页如实标注 ───────────────
class _Ha:
    def __init__(self, result): self.result = result; self.n = 0
    async def handle_intent(self, name, data, timeout=10.0):
        self.n += 1
        return self.result


def test_f5_scene_cache_refresh_returns_bool():
    import time as _t
    ha = _Ha({"success": False, "error": "offline"})
    sc = SceneCache(ha)
    assert asyncio.run(sc.refresh(force=True)) is False       # 折叠不抛→看返回值
    sc._last_refresh = _t.time()                              # 模拟缓存尚在 TTL 内
    assert asyncio.run(sc.refresh()) is True                  # TTL 内跳过=成功语义
    assert ha.n == 1                                          # 跳过不白打
    ha2 = _Ha({"success": True, "scenes": [{"trigger_phrase": "晚安"}]})
    sc2 = SceneCache(ha2)
    assert asyncio.run(sc2.refresh(force=True)) is True
    assert sc2.triggers == ["晚安"]
    # 失败保留旧缓存（HA 重启窗口不清空）
    sc2._last_refresh = 0.0
    sc2.ha = _Ha(None)
    assert asyncio.run(sc2.refresh(force=True)) is False
    assert sc2.triggers == ["晚安"]
    sc2._last_refresh -= TTL_S + 1


class _ScenesDown:
    triggers = []
    def all(self): return []
    async def refresh(self, force=False): return False        # 新契约：bool


def test_f5_scenes_route_flags_failed_refresh():
    ctx = SimpleNamespace(scenes=_ScenesDown())
    j = _resp(asyncio.run(_scenes(SimpleNamespace(app={CTX_KEY: ctx}))))
    assert "场景列表拉取失败（集成未装/未响应）" in j["error"]
    assert j["scenes"] == [] and j["triggers"] == []


def test_f5_automations_route_flags_missing_bridge():
    # 两个通道都拿不到（rest 折叠 None）：note 与核心错误拼接，双事实都在
    ctx = SimpleNamespace(ha=FakeHAClient())
    j = _resp(asyncio.run(_automations(SimpleNamespace(app={CTX_KEY: ctx}))))
    assert "语音自动化读取失败（集成未装/未响应）" in j["error"]
    assert "不可达" in j["error"]
    # huijian 通道 success False：带原因
    ha = FakeHAClient(rest={"/api/huijian-ai/automations": {"success": False, "error": "boom"}})
    ctx2 = SimpleNamespace(ha=ha)
    j2 = _resp(asyncio.run(_automations(SimpleNamespace(app={CTX_KEY: ctx2}))))
    assert j2["error"] == "" or "语音自动化读取失败：boom" in j2["error"]


# ── F6：模型表按 snapshot() 键形消费 ───────────────────────────────
def test_f6_models_table_consumes_snapshot_shape():
    assert 'pending:"排队"' in WWW and 'extracting:"解包中"' in WWW
    assert 'incomplete:"不完整"' in WWW and 'manual:"待手动"' in WWW
    assert "${v.size_mb" not in WWW and "${v.path" not in WWW   # 不存在的键已除根
    assert "${v.status" not in WWW
    assert 'const prog = (o.pct != null && s === "downloading") ? o.pct+"%" : "";' in WWW
    assert '<td>${o.ready?"":`<button class="act"' in WWW       # ready 才隐藏下载钮


# ── F7：status.json ts 停更检测（核心死了页面不再谎报"正常"）──────
def test_f7_status_freeze_detection():
    assert "_stTs = null, _stSame = 0" in WWW
    assert "_stSame >= 3" in WWW
    assert "状态停更（核心可能已退出）" in WWW
    assert "· 停更" in WWW                                       # 会话数同标陈旧
    assert "_stTs = null; _stSame = 0" in WWW                    # fetch 异常重新起算


# ── F8：网页触发词字符集与语音侧 creation 对齐（ASCII 逗号）────────
def test_f8_phrase_charset_rejects_ascii_comma():
    assert _PHRASE_RE.fullmatch("你好,世界") is None
    assert _PHRASE_RE.fullmatch("你好，世界") is None            # 全角原样
    assert _PHRASE_RE.fullmatch("你好世界") is not None
    src = (ROOT / "core" / "nlu" / "creation.py").read_text(encoding="utf-8")
    assert "，," in src                                          # 对齐依据仍在 creation


# ── F9：行操作在飞守卫（连点不并行 POST）──────────────────────────
def test_f9_op_busy_guard():
    assert "const _opBusy = new Set();" in WWW
    assert WWW.count("opGuard(op, ev.target, ") == 6            # 6 个操作位全收编
    assert 'el.classList.add("busy")' in WWW and 'el.classList.remove("busy")' in WWW
    assert ".copy.busy { pointer-events: none" in VCSS


# ── F10：配对页端点/token 转义拉平（data-c 与文本同源同险）────────
def test_f10_pair_page_escape_parity():
    assert '${esc0(r[k])}</td><td style="width:50px"><span class="copy" data-c="${esc0(r[k])}"' in WWW
    assert '<span class="mono">${esc0(r.token)}</span> <span class="copy" data-c="${esc0(r.token)}"' in WWW
    assert 'data-c="${r.token}"' not in WWW and "${r[k]}</td>" not in WWW


# ── F11：帘/窗语序归一（SOV/SVO → 标准动形）───────────────────────
def test_f11_cover_wordorder_rewrites():
    assert _cover_wordorder("客厅窗帘拉上") == "关闭客厅窗帘"
    assert _cover_wordorder("窗帘拉上了") == "关闭窗帘"          # 句尾了也要接
    assert _cover_wordorder("卧室窗户关上了") == "关闭卧室窗户"
    assert _cover_wordorder("拉上客厅窗帘") == "关闭客厅窗帘"
    assert _cover_wordorder("拉开窗帘") == "打开窗帘"            # 拉开不在动作表，改写补位
    assert _cover_wordorder("打开窗帘") is None                  # 已是标准动形→不动
    assert _cover_wordorder("关一下客厅窗帘") is None            # 裸 关 不收，不误拼
    assert _cover_wordorder("睡觉关窗帘") is None                # 非句尾动词不吞
    assert _cover_wordorder("窗帘拉上了吗") is None              # 疑问句绝不改写


class _Settings:
    def get(self, k, d=None):
        return {} if k == "nlu.corrections_extra" else d


class _FakeTC:
    available = True
    def __init__(self, mapping): self.mapping = mapping
    def predict(self, text):
        for kw, hit in self.mapping.items():
            if kw in text: return hit
        return None


def _fp():
    return FastPath(FakeScenes(), _FakeTC({"帘": ("OpenCover", 0.92),
                                           "窗": ("ControlWindow", 0.95)}),
                    _Settings())


@pytest.mark.parametrize("text,want", [
    ("把客厅窗帘拉上", "TurnDeviceOff"),
    ("窗帘拉上", "TurnDeviceOff"),
    ("拉上客厅窗帘", "TurnDeviceOff"),
    ("窗帘拉上了", "TurnDeviceOff"),
    ("把卧室窗户关上", "ControlWindow"),
    ("卧室窗户关上了", "ControlWindow"),
    ("拉开客厅窗帘", "TurnDeviceOn"),
])
def test_f11_match_end_to_end(text, want):
    plan = asyncio.run(_fp().match(text))
    assert plan is not None and plan.intent == want, (text, want, plan and plan.trace)
    assert plan.source == "t0" and any("帘窗语序" in t for t in plan.trace)


def test_f11_target_shape_after_rewrite():
    p = asyncio.run(_fp().match("把客厅窗帘拉上"))
    t = p.args["target"][0]
    assert t["area"] == "客厅" and t["devices"][0]["name"] == "窗帘"
    assert t["devices"][0]["domains"] == ["cover"]
    w = asyncio.run(_fp().match("把卧室窗户关上"))
    assert w.args["action"] == "close" and w.args["target"][0]["area"] == "卧室"


def test_f11_rewrite_never_touches_questions():
    # 改写层绝不吞疑问句/非句尾形态；match 级若真模型经 T1+方向纠正接管，
    # 那是 v1.0.40 既有语义——钉的是「帘窗语序」零参与。
    assert _cover_wordorder("窗帘拉上了吗") is None
    assert _cover_wordorder("睡觉关窗帘") is None
    fp = _fp()
    for t in ("窗帘拉上了吗", "睡觉关窗帘"):
        p = asyncio.run(fp.match(t))
        assert not (p and any("帘窗语序" in x for x in p.trace)), t


# ── F12：T1 方向纠正词表补齐（拉下来/闭合/拉严）────────────────────
@pytest.mark.parametrize("text,want", [
    ("拉下窗帘", "TurnDeviceOff"), ("闭合窗帘", "TurnDeviceOff"),
    ("把窗帘拉下", "TurnDeviceOff"), ("窗帘拉下来", "TurnDeviceOff"),
    ("拉严窗帘", "TurnDeviceOff"),
    ("拉开窗帘", "TurnDeviceOn"),
])
def test_f12_cover_direction(text, want):
    plan = asyncio.run(_fp().match(text))
    assert plan is not None and plan.intent == want, (text, plan and plan.trace)
    assert _cover_intent(text, "TurnDeviceOn") == want          # 词表与改写双路一致


def test_f12_open_words_dedup():
    from core.nlu.fast_path import _COVER_OPEN_WORDS, _COVER_CLOSE_WORDS
    assert len(_COVER_OPEN_WORDS) == len(set(_COVER_OPEN_WORDS))
    for w in ("拉下", "拉下来", "闭合", "拉严"):
        assert w in _COVER_CLOSE_WORDS


# ── F13：全屋动词在尾形态放行给控制链（守卫豁免收紧为疑问标记）────
@pytest.mark.parametrize("text,intent", [
    ("所有灯打开", "TurnDeviceOn"),
    ("家里的灯全部打开", "TurnDeviceOn"),
    ("帮我把所有灯都关掉", "TurnDeviceOff"),
    ("所有灯都关啦", "TurnDeviceOff"),
])
def test_f13_wholehouse_trailing_verb(text, intent):
    plan = asyncio.run(_fp().match(text))
    assert plan is not None and plan.intent == intent, (text, plan and plan.trace)
    assert plan.whole_house is True
    dev = plan.args["target"][0]["devices"][0]
    assert dev["name"] == "" and dev["domains"] == ["light"]    # v1.0.41 语义变更：
    # 「帮我把所有灯都关掉」自本版起从守卫放行句变为 T0 全屋命令（用户主诉命令句）。


def test_f13_query_counterpins_stay_guarded():
    fp = _fp()
    for t in ("所有灯现在什么状态", "家里灯都开着吗", "所有灯的开关在哪里",
              "客厅所有灯都打开"):          # 区域句不放行成全屋（误开全家灯）
        assert asyncio.run(fp.match(t)) is None, t
    assert _is_complex_query("所有灯现在什么状态") is True
    assert _is_complex_query("所有灯都关啦") is False            # 都/啦 不豁免
    assert _is_complex_query("客厅所有灯都打开") is True         # 守卫与尾动同判据
    # 认不出域仍不冒然全屋全动（会带上门锁）：
    assert _is_complex_query("所有设备现在什么状态") is True


def test_s12_verbfront_region_not_swallowed():
    """审查 S12：动词前置+区域前缀的「打开…所有/全部…」是**区域句**。
    旧实现有两条兜底路径（_wholehouse_plan 前置循环 + 主流程 :795「余下语序」）
    都会把区域词静默剥掉直产 whole_house——「打开卧室全部窗帘」开全家窗帘、
    「打开客厅所有灯」开全家灯（作用域扩大，v1.0.40 即存在）。修复后 rest 必须
    以全屋标记起头才判全屋；区域句落回正常 area/name 处理（真机可感，绝不冒然全屋）。"""
    fp = _fp()
    # 区域句：不得 whole_house（要么正确 area 化，要么交回上层 None）
    p = asyncio.run(fp.match("打开卧室全部窗帘"))
    assert p is not None and p.whole_house is False, p and p.trace
    assert p.args["target"][0]["area"] == "卧室", p.args
    assert p.args["target"][0]["devices"][0]["domains"] == ["cover"], p.args
    for t in ("打开客厅所有灯", "关闭书房所有灯"):     # 灯区域句当前无 area 兜底 → None
        assert asyncio.run(fp.match(t)) is None, t
    # 纯全屋句零漂移
    for t, intent in (("打开所有灯", "TurnDeviceOn"),
                      ("关闭全部窗帘", "TurnDeviceOff"),
                      ("关掉全部窗帘", "TurnDeviceOff"),
                      ("关掉家里所有的灯", "TurnDeviceOff")):
        p = asyncio.run(fp.match(t))
        assert p is not None and p.intent == intent and p.whole_house is True, t


# ── F14：_scene_row 平铺 dict target 不再被 hv 分支劫持成裸动词 ────
def test_f14_scene_row_flat_dict_target():
    row = _scene_row({"trigger_phrase": "t", "actions": [
        {"intent": "ControlWindow",
         "params": {"target": {"area": "客厅", "name": "推拉窗"}, "action": "close"}}]})
    assert row["actions"] == ["ControlWindow 客厅 推拉窗"]
    # 现役 list 形态仍走 _hv_action_cn（回归面零漂移）：
    row2 = _scene_row({"trigger_phrase": "t", "actions": [
        {"intent": "TurnDeviceOn",
         "params": {"target": [{"area": "客厅", "devices": [{"name": "灯"}]}]}}]})
    assert row2["actions"] == ["打开客厅灯"]
    # 历史平铺 entity_id 形态原样（test_scenes_and_reload 同源钉）：
    row3 = _scene_row({"trigger_phrase": "t", "actions": [
        {"intent": "HassTurnOff", "params": {"entity_id": "light.客厅"}}]})
    assert row3["actions"] == ["HassTurnOff light.客厅"]
