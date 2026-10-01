# -*- coding: utf-8 -*-
"""v1.1.36 发布后方差复核批（三处今天引入/修一半的问题 + 一条假绿钉）。

靶子全部来自 2026-09-30 深夜自己跑的复现探针，不是推测：

①`nlu/query.py` 陈旧快照加注**无条件**贴在所有查询应答上，而时钟三支
  （几点/星期几/几号）取的是 `datetime.now()`，跟状态快照毫无关系。
  探针原文（真模块 + 假 ha，states_stale 恒返回"HA 状态尚未取到"）：
      「现在几点」 → 现在是 0 点 11 分（注：HA 状态尚未取到，数字可能不是最新）
      「今天星期几」→ 今天是星期四（注：…）
      「几号了」   → 今天是 2026 年 10 月 1 号（注：…）
  而 `_states_ts` 初值 0（ha_client.py:71）⇒ **升级后重启的第一句就中招**。
  旧钉 `test_audit4_p2.test_p2_stale_snapshot_annotates_query_answer` 把
  `_answer_inner` 打桩成状态类答案 ⇒ 时钟支一直是盲区。

②`core/pipeline.py:207` 注释写"全族同闸"，实测 `_KLAR_WRITE_INTENTS` 只有 8 个
  意图，而 klar 白名单 `KLAR_CONTROL_INTENTS`（nlu/klar_client.py:46-54）有 12 个。
  探针实跑（原话完全不含目标证据，句里点名设备家里查无）：
      HassLock        「给故事机上个锁」    → 放行执行
      HassLock        「把会飞的门锁上」    → 放行执行
      HassVacuumStart 「启动会飞的扫地机」  → 放行执行
      HassFanSetPresetMode「会飞的风扇设成睡眠」→ 放行执行
      HassTurnOn      「关掉会飞的灯」      → 拦（同形句，唯一被闸的那族）
  锁是**高风险面**：intent_turn.py:326 `# off = unlock`。

③`core/ha_client.py:265-266` refresh_states 只在 `except` 支置 `_states_ok=False`，
  HTTP 非 200（401/403/500，令牌过期就是这一形）只写 last_error ⇒ "上次状态
  读取失败"这个具名原因永不出现，用户最长 90s 拿到无注旧数。

④假绿钉收口：`test_v1070_audit_sweep` 的 `_bridge_ok` 文本钉对**未修版**
  58815ab 同样成立（`_devices`/`_issue` 本来就有该调用）⇒ 零判别力。
"""
import asyncio
import ast
import re
import sys
from pathlib import Path

import pytest

from conftest import FakeHAClient
from core.executor import Executor, zh_error
from core.ha_client import HAClient
from core.nlu.fast_path import Plan
from core.nlu.query import QueryZone

HERE = Path(__file__).resolve().parent.parent


class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._p = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._p

    async def text(self):
        return str(self._p)


class _Session:
    """假 aiohttp session：states 与非 200 两支都要能答。"""

    def __init__(self, status=200, payload=None):
        self.status = status
        self.payload = [] if payload is None else payload

    def get(self, url, **kw):
        return _Resp(self.status, self.payload)

    def post(self, url, **kw):
        # _load_registries 会 POST 三个注册表面，空表最省事且不炸
        return _Resp(self.status if self.status != 200 else 200,
                     self.payload if self.status == 200 else [])


def _client(status, payload=None):
    c = HAClient(session=_Session(status, payload))
    c.base = "http://supervisor/core/api"
    return c


# ①/③ 共用的假快照面：states_stale 恒有具名原因（=刷新失败/快照过期态）
STATES = {
    "sensor.living_temp": {"entity_id": "sensor.living_temp", "state": "26.5",
                           "attributes": {"friendly_name": "客厅温度",
                                          "device_class": "temperature"}},
    "light.living": {"entity_id": "light.living", "state": "on",
                     "attributes": {"friendly_name": "客厅筒灯"}},
}
STALE_WHY = "HA 状态尚未取到"


def _qha():
    ha = FakeHAClient(states=STATES, areas={"a1": "客厅"},
                      entity_area={"sensor.living_temp": "客厅",
                                   "light.living": "客厅"})
    ha.states_stale = lambda: STALE_WHY
    return ha


# ── ① 时钟支不得被贴陈旧注 ─────────────────────────────────────
@pytest.mark.parametrize("utt", ["现在几点", "今天星期几", "几号了", "现在什么时间"])
def test_local_clock_answer_not_caveated(utt):
    """答案来自本地时钟 ⇒ 与状态快照无关 ⇒ 加注就是撒谎（回归探针原文）。"""
    q = QueryZone(_qha(), None)
    ans = asyncio.run(q.answer(utt))
    assert ans, f"时钟支空答：{utt}"
    assert "注：" not in ans, f"本地时钟答案被贴了陈旧注：{utt} -> {ans!r}"


def test_state_derived_answer_still_caveated():
    """反向不变量：只摘时钟支的注，**快照来源**的注必须还在。

    没这条的话，把 `answer()` 里的加注整段删掉也能骗过上一条——单向钉守不住
    "收窄"这种改法（结构守卫纪律：单向不变量必配反向）。
    """
    q = QueryZone(_qha(), None)
    ans = asyncio.run(q.answer("客厅现在多少度"))
    assert ans and "26" in ans, f"读数句本身跑歪：{ans!r}"
    assert STALE_WHY in ans, f"快照来源的答案丢了陈旧注（不得整体删）：{ans!r}"


def test_clock_exempt_set_matches_branch_regexes():
    """**双向前后夹**：时钟豁免判据与 `_answer_inner` 那三条分支必须同一集合。

    方向一（缺豁免）：新增一条时钟分支而没进豁免表 ⇒ 该支被贴假注；
    方向二（多豁免）：把不相关的正则塞进豁免表 ⇒ 快照来源的答案被**免**注。
    两头都红，才钉得住（旧形只断言"分支在跑"，方向二是开的）。
    """
    from core.nlu import query as Q

    src = (HERE / "core" / "nlu" / "query.py").read_text(encoding="utf-8")
    body = src[src.index("async def _answer_inner"):]
    # 三条时钟分支的正则原文（按分支顺序；到 _find_area 之前的调用点为止）
    seg = body[:body.index("area = self._find_area")]
    pats = re.findall(r're\.search\(r?"(.*?)"', seg)
    clock_srcs = [p for p in pats
                  if any(k in p for k in ("几点", "星期几", "几号"))]
    assert len(clock_srcs) == 3, f"时钟分支正则应有 3 条，实得 {clock_srcs}"
    exempt_srcs = [Q._LOCAL_CLOCK_RE.pattern, Q._LOCAL_CLOCK_RE2.pattern,
                   Q._LOCAL_CLOCK_RE3.pattern]
    assert sorted(clock_srcs) == sorted(exempt_srcs), (
        f"时钟分支与豁免表漂移：分支={clock_srcs} 豁免={exempt_srcs}")

    probe = ["现在几点", "今天星期几", "几号了", "现在什么时间", "周几", "几月几号",
             "昨天星期几"]        # 昨天/明天都走同一条 星期几 分支（时钟来源）
    for t in probe:
        assert Q._answer_from_local_clock(t) is True, f"时钟句未免注：{t}"
    others = ["客厅现在多少度", "灯开着吗", "设个10分钟定时", "预约明早开空调",
              "办公室湿度多少", "明天天气怎么样", "筒灯亮度多少"]
    # 注：「什么时候关灯比较好」不在这里——它**含**"什么时候"，按分支顺序就是
    # 时钟支答的（时钟三支排在 _answer_inner 最前面）。豁免表跟着分支走才对，
    # 那条句子的过宽匹配是 v1.1.2 扩表带进来的旧账，另案。
    for t in others:
        assert Q._answer_from_local_clock(t) is False, f"非时钟句被免注（漏加陈旧注）：{t}"


# ── ② refresh_states：HTTP 非 200 同样是"读取失败" ──────────────
@pytest.mark.parametrize("status", [401, 403, 500, 502, 404])
def test_non_200_marks_snapshot_failed(status):
    """令牌过期=401、Supervisor 抖=500/502 是现场两形。

    旧形 else 支只写 last_error（ha_client.py:265-266），`_states_ok` 留在 True
    ⇒ `states_stale()` 的具名原因永不出现 ⇒ 用户拿无注旧数当现行。
    措辞分两形态（这条钉第一版我把它们混成一句，假红）：**从未**取到过快照
    ="HA 状态尚未取到"；取到过但这次刷新失败="上次状态读取失败（…）"。
    两种都必须非空。reachable 口径不许动（401/403=可达，v1.0 定案）。
    """
    c = _client(status, {"message": "denied"})
    asyncio.run(c.refresh_states(force=True))
    assert c._states_ok is False, f"HTTP {status} 未置读取失败位"
    why = c.states_stale()
    assert why, f"HTTP {status} 却报不出任何陈旧原因"


def test_stale_snapshot_after_failure_names_read_failure():
    """真事故形态：先成功拿到快照，随后刷新失败 ⇒ 必须具名"上次状态读取失败"。

    旧形在这里返回**空串**（`_states_ok` 留 True、`_states_ts` 是上次成功时刻、
    age<90s ⇒ 两道判据都不触发）——用户听到的是旧数且毫无提示，最长 90s。
    """
    rows = [{"entity_id": "light.a", "state": "on", "attributes": {}}]
    s = _Session(200, rows)
    c = HAClient(session=s)
    c.base = "http://supervisor/core/api"
    asyncio.run(c.refresh_states(force=True))
    assert c.states_stale() == "", "前置态：刚成功不该有任何注"
    s.status, s.payload = 401, {"message": "Unauthorized"}
    asyncio.run(c.refresh_states(force=True))
    assert c._states_ok is False
    assert c.states_stale().startswith("上次状态读取失败"), c.states_stale()
    # 快照仍握在手里（失败不得把缓存清空——清空后查询族会答"没数据"，
    # 而真实现场是"有旧值"，两种谎形态不同，这里钉住后者不被改成前者）
    assert c._states, "非 200 之后不得清空存量快照"


def test_non_200_does_not_break_reachable_wording():
    """401/403 仍算可达（解耦：可达≠快照可用）；404 仍算不可达。"""
    for status, expect in ((401, True), (403, True), (404, False)):
        c = HAClient(session=_Session(status, {"message": "x"}))
        c.base = "http://supervisor/core/api"
        asyncio.run(c.refresh_states(force=True))
        assert c.reachable is expect, f"{status} 的 reachable 漂移"


def test_200_clears_failure_and_no_caveat():
    """反向不变量：成功刷新必须清位、`states_stale()` 回到空串。

    没有这条，把 `_states_ok` 写成恒 False（或把 `states_stale` 写死返回原因）
    也能骗过上一条——单向钉守不住"置位"这种改法。
    """
    rows = [{"entity_id": "light.a", "state": "on",
             "attributes": {"friendly_name": "台灯"}}]
    c = HAClient(session=_Session(200, rows))
    c.base = "http://supervisor/core/api"
    asyncio.run(c.refresh_states(force=True))
    assert c._states_ok is True
    assert c.states_stale() == "", c.states_stale()


def test_failure_then_recovery_rearms():
    """成功→失败→恢复 三段：中间那段必须**由 True 翻成 False**。

    只断言"失败后是 False"会被 `__init__` 的初值 False 白过（我第一版就这么写错
    了——结构守卫纪律：fixture 不许把被测分支写成恒不执行那侧）。
    """
    rows = [{"entity_id": "light.a", "state": "on", "attributes": {}}]
    s = _Session(200, rows)
    c = HAClient(session=s)
    c.base = "http://supervisor/core/api"
    asyncio.run(c.refresh_states(force=True))
    assert c._states_ok is True, "前置态没立起来，本钉无判别力"
    s.status, s.payload = 500, []
    asyncio.run(c.refresh_states(force=True))
    assert c._states_ok is False, "500 之后仍留 True＝复核②回潮"
    assert c.states_stale().startswith("上次状态读取失败")
    s.status, s.payload = 200, rows
    asyncio.run(c.refresh_states(force=True))
    assert c._states_ok is True and c.states_stale() == "", "恢复没重新武装"


# ── ③ 失败播报话术必须接住调用侧已判定的"结果不确定" ─────────────
_INDET_SAY_KEY = "可能已经动作"


def test_indeterminate_flag_overrides_text_heuristic():
    """ha_client 折叠出的 message 是中文短句"HA 通道异常"，文本启发式认不出来
    （_INDETERMINATE_HINTS 全英文/超时形）⇒ 旧形播"没有执行成功"=**假确定**，
    正是 executor.py:89-93 注释明令禁止的那条（"话术必须与判据同源"）。"""
    say = zh_error("HA 通道异常", indeterminate=True)
    assert _DET_KEY(say), f"仍播假确定：{say!r}"
    assert "没有执行成功" not in say, say


def test_indeterminate_flag_beats_map_misattribution():
    """5xx 最毒的一形：`_EN_ERR_MAP` 第 68 行先命中"ha 内部错误"⇒ 播"集成还没
    生效"，把用户引去装集成（假确定＋假归因）。带旗时必须走不确定话术。"""
    say = zh_error("ha 内部错误", klar=False, indeterminate=True)
    assert "集成" not in say, f"5xx 仍被译成集成话术：{say!r}"
    assert _DET_KEY(say), say
    say2 = zh_error("ha 内部错误", klar=True, indeterminate=True)
    assert "集成" not in say2 and _DET_KEY(say2), say2


def test_no_flag_keeps_old_wording():
    """反向不变量：**不传旗时逐字不变**——文本启发式与映射表的行为一概不动，
    否则我把"不确定"当成万能挡箭牌，把真·集成没装也播成"可能已动作"。"""
    assert zh_error("HA 通道异常") == zh_error("HA 通道异常", indeterminate=False)
    assert "没有执行成功" in zh_error("HA 通道异常")
    assert "集成还没生效" in zh_error("ha 内部错误")
    assert "执行超时了" in zh_error("Timeout on closing request")


def _DET_KEY(say):
    return "可能已经动作" in say


# ── ⑦ 属性步进：色温必须用它自己的步进（其余三处经查不是同一个洞）──
ADJ_SRC = (HERE / "custom_components" / "huijian_ai"
           / "intent_adjust_attribute.py").read_text(encoding="utf-8")


def _call_args(fn_name):
    """取某个注册函数里 `calc_target(...)` 的实参 AST（按函数体切，别拿全文）。"""
    tree = ast.parse(ADJ_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "calc_target"):
                    return sub.args, sub.keywords
    raise AssertionError(f"没找到 {fn_name} 里的 calc_target 调用")


# ── ⑥ 接线：三份原料必须同时进**每一个**裁决点（不只主入口）────────
def test_all_adjudication_sites_pass_real_areas():
    """本仓反复栽在"只修一个调用点"（v1.0.90 半道闸、面板旁路、链内静默丢腿）。
    所以这里把**每一处** select_primary_plan / select_fallback_plan /
    _klar_named_absent_target 调用都扫一遍：少传 real_areas ⇒ 位置豁免退回静态表
    ⇒ 「关掉阳台的灯」在家里没阳台时照样动别的房间那台。"""
    src = (HERE / "core" / "pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    targets = ("select_primary_plan", "select_fallback_plan",
               "_klar_named_absent_target")
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in targets:
            sites.append(node)
    assert len(sites) >= 6, f"裁决点数量变了（现 {len(sites)} 处），本钉需复核"
    for call in sites:
        has_kw = any(k.arg == "real_areas" for k in call.keywords)
        pos = ast.unparse(ast.Tuple(elts=list(call.args[1:]), ctx=ast.Load())) \
            if len(call.args) > 1 else ""
        assert has_kw or "self._real_areas()" in pos, (
            f"{call.func.id} 第{call.lineno}行没把真区域表传进去")


def test_color_temp_snaps_to_its_own_step():
    """空调 0.5 度那个洞的同型：色温步进 500K，却把吸附网格传成 1
    ⇒ "暖一点"落到 3417K 这种设备上根本不存在的档位。"""
    args, _kw = _call_args("adjust_light_temperature")
    # (current, level_step, min_change, min, max)
    assert len(args) >= 3, "calc_target 形参形状变了，本钉要看一眼"
    assert ast.unparse(args[1]) == ast.unparse(args[2]) == "color_temperature_step", \
        f"level_step 与 min_change 必须同一个步进：{[ast.unparse(a) for a in args[:3]]}"


def test_climate_temperature_step_still_snaps_to_device_step():
    """v1.1.33 那次修的 0.5 度洞不许被这次的"三处不算洞"口径带走。"""
    args, _kw = _call_args("adjust_climate_temperature")
    assert ast.unparse(args[1]) == ast.unparse(args[2]) == "temperature_step", \
        [ast.unparse(a) for a in args[:3]]


@pytest.mark.parametrize("fn,step_var", [("adjust_light_brightness", "percentage_step"),
                                         ("adjust_cover_position", "percentage_step"),
                                         ("adjust_humidifier_humidity", "adjustment_step")])
def test_domains_without_device_step_keep_grid_one(fn, step_var):
    """**反向记录**：亮度/开度/湿度这三处传 1 不是漏修——HA 这三个域没有设备侧
    步进属性，1 就是真实粒度。把这条钉住，是防将来有人"看着像洞"就一杆子改成
    百分比步进（那会把「调到 45%」吸成 50%）。
    """
    args, _kw = _call_args(fn)
    assert ast.unparse(args[2]) == "1", f"{fn} 的吸附网格被改动，见上方注释的口径"
    assert step_var in ast.unparse(args[1]), f"{fn} 的步进来源变了"


# ── ④-1 `?nocache`：接线（真值表钉在 test_audit4_p2 同题里）──────
def test_nocache_call_site_wired():
    """接线：视图必须把 `request.query` 交给那条判据（只加函数不改调用点＝没修）。

    定位方式用 AST 找**含 `use_file_cache` 的那个函数体**——旧写法在这里锚
    `src.index("async def post(self, request")` 会撞到同文件里第一个 post（别的视图）。
    """
    HTTP_SRC = (HERE / "custom_components" / "huijian_ai" / "huijian"
                / "http.py").read_text(encoding="utf-8")
    tree = ast.parse(HTTP_SRC)
    seg = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            s = ast.get_source_segment(HTTP_SRC, node) or ""
            if "use_file_cache=not _file_cache_disabled" in s:
                seg = s
                break
    assert seg is not None, "没找到把 request.query 交给判据的视图调用点"
    assert "use_file_cache=not _file_cache_disabled(request.query)" in seg, \
        "调用点还在用旧的 falsy 判等写法"
    assert 'query.get("nocache") in (None, "")' not in seg, "旧 no-op 写法回潮"


# ── ④-2 族差集不变量：白名单控制族必须全部进证据闸 ────────────────
def test_whitelisted_klar_intents_all_gated():
    """机器化版"全族同闸"：pipeline.py:207 那句注释此前不实（8 vs 12）。
    今后往 klar 白名单加意图而忘了加闸，这条自己转红。"""
    from core.nlu.klar_client import KLAR_CONTROL_INTENTS
    from core.pipeline import _KLAR_WRITE_INTENTS

    missing = sorted(set(KLAR_CONTROL_INTENTS) - set(_KLAR_WRITE_INTENTS))
    assert not missing, f"这些引擎可接管的控制族在证据闸外：{missing}"


# ── ③ 接线：判据→话术必须走真入口（不是只改函数签名）──────────────
def _exec(result):
    return Executor(FakeHAClient(results={"TurnDeviceOn": result}), None)


@pytest.mark.parametrize("raw,klar", [("HA 通道异常", True), ("ha 内部错误", True),
                                      ("HA 通道异常", False)])
def test_run_speech_follows_call_side_flag(raw, klar):
    """真跑 `Executor.run`：加载项侧已判定 indeterminate ⇒ 播报不得断言"没执行成功"。

    这条是接线钉——只给 zh_error 加形参而调用点不传旗，本钉转红。
    """
    plan = Plan(intent="TurnDeviceOn", args={},
                source="klar" if klar else "t1", utterance="打开灯")
    ex = _exec({"success": False, "message": raw, "indeterminate": True})
    ok, msg = asyncio.run(ex.run(plan))
    assert ok is False
    assert _DET_KEY(msg), f"判据算了却没进话术：{msg!r}（raw={raw}, klar={klar}）"
    assert "没有执行成功" not in msg, msg
    if raw == "ha 内部错误":
        assert "集成" not in msg, f"5xx 仍被译成装集成：{msg!r}"
    assert ex.last_run["indeterminate"] is True


def test_run_speech_unchanged_without_flag():
    """反向不变量：没有旗（真·设备拒绝）时**不得**播"可能已动作"——
    否则我把不确定当成万能挡箭牌，用户会以为每条都做过。"""
    plan = Plan(intent="TurnDeviceOn", args={}, source="t1", utterance="打开灯")
    ex = _exec({"success": False, "message": "HA 通道异常"})
    ok, msg = asyncio.run(ex.run(plan))
    assert ok is False
    assert not _DET_KEY(msg), f"无旗却播不确定话术：{msg!r}"
    assert "没有执行成功" in msg, msg


