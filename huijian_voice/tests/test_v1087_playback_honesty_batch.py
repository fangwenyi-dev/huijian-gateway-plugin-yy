"""v1.0.87 钉：播报诚实性 / 归属化 / 线程安全（现场 2026-09-16 四组日志逐条对应）。

① tts.py:118 跨线程 async_write_ha_state（裸同步闭包被 HassJob 判成 Any → 丢进
   默认线程池 → 写入被 frame 红线抛杀）＝ v1.0.79 可用态自愈一直空转，tts 实体
   卡"不可用"→ 引擎解析不到 → 整轮无播报。→ @callback 必修 + **全集成 AST 守卫**
   （同类缺陷不再靠人眼）。
② 上行音频"队列已满"63 条/累计 9300 块：本轮无消费者时帧永远不可能被转写 →
   直投丢弃并把归因说准；判活必须用 _round_outer_task（core accept 会重绑
   _pipeline_task，drain 窗内它恒为 None）。
③ v1.0.86 的 8s **纯时间窗**换成"被 drain 掉的旧轮还活着"归属判据：旧轮一收口
   窗即刻失效——新轮自己合法的 TTS 不再可能被误杀（硬上限保留兜极端）。
④ 级联重放闸：主发次"结果不确定"（超时/连接/5xx）时降级再放一发 = 双执行 +
   播报压后一整发（现场 15.4s 才出声 → 用户重唤醒 → 又拆一轮）。同一 exec_risk
   判据 LLM 复议闸早已用，这里补齐；话术同步改诚实（"没拿到回执"≠"没执行成功"）。
⑤ 连续对话四态诊断码 + echoed 有界复核 + 中继原样透传（现场"点了没反应"）。

真行为优先；AST 抽取**真实源码**执行，不用替身（仓内铁律：源码字符串钉 + 全绿
拦不住契约漂移）。少数只能源级的地方在函数 docstring 里明说。
"""
import ast
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CC = ROOT / "custom_components" / "huijian_ai"
sys.path.insert(0, str(ROOT))


def _src(path):
    return pathlib.Path(path).read_text(encoding="utf-8")


def _fn(path, name):
    """按 AST 取真实函数源码（含自身缩进基线，装饰器自动剥掉）。"""
    src = _src(path)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name):
            seg = ast.get_source_segment(src, node)
            assert seg, "取不到源码 " + name
            return seg
    raise AssertionError("找不到函数 " + name)


# ── ① 线程安全守卫：交给 async_track_*/bus.async_listen 的同步回调必须 @callback ──
_JOB_APIS = {"async_track_time_interval", "async_track_time_change",
             "async_track_state_change_event", "async_call_later",
             "async_listen", "async_listen_once", "HassJob"}


def _deco_is_callback(node):
    for d in node.decorator_list:
        if isinstance(d, ast.Name) and d.id == "callback":
            return True
        if isinstance(d, ast.Attribute) and d.attr == "callback":
            return True
    return False


def test_job_callbacks_are_loop_safe():
    """HA 对未标注的同步可调用按 HassJobType.Any 处理 → run_in_executor 执行，
    里面任何 async_write_ha_state / hass.data 写都是跨线程（frame ERROR 直接抛，
    现场表现为"自愈日志报错、状态永不回升"）。故：同步回调必须 @callback。"""
    offenders = []
    for path in sorted(CC.rglob("*.py")):
        src = _src(path)
        try:
            tree = ast.parse(src)
        except SyntaxError as e:                       # 语法坏了一眼看见
            raise AssertionError("%s 解析失败: %s" % (path, e))
        table = {}                                     # name -> (async?, @callback?)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                table[node.name] = (
                    isinstance(node, ast.AsyncFunctionDef),
                    _deco_is_callback(node))
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            f = call.func
            fname = (f.attr if isinstance(f, ast.Attribute)
                     else f.id if isinstance(f, ast.Name) else None)
            if fname not in _JOB_APIS:
                continue
            for arg in list(call.args) + [kw.value for kw in call.keywords]:
                if isinstance(arg, ast.Lambda):
                    offenders.append("%s: %s 传裸 lambda" % (path.name, fname))
                elif isinstance(arg, (ast.Name, ast.Attribute)):
                    nm = arg.id if isinstance(arg, ast.Name) else arg.attr
                    if nm in ("hass", "self"):
                        continue
                    info = table.get(nm)
                    if info and not info[0] and not info[1]:
                        offenders.append("%s: %s 的回调 %s 是未 @callback 的同步函数"
                                         % (path.name, fname, nm))
    assert not offenders, "跨线程作业风险回潮：\n  " + "\n  ".join(offenders)


def test_tts_avail_recheck_is_callback():
    """现场 ① 的那一行：单独钉住，别只靠上面的泛化守卫。"""
    seg = _src(CC / "tts.py")
    i = seg.index("def _avail_recheck")
    assert "@callback" in seg[max(0, i - 60):i], "可用态周期复核丢了 @callback"
    assert "from homeassistant.core import HomeAssistant, callback" in seg


# ── ③ 僵尸窗归属化：真实判据的行为（旧轮活着才拦）──────────────────────
def test_zombie_window_requires_live_old_round():
    ns = {"asyncio": asyncio}
    exec(compile(_fn(CC / "assist_satellite.py", "_zombie_tts_guard_active"),
                 "zombie", "exec"), ns)
    pred = ns["_zombie_tts_guard_active"]

    class S:
        _zombie_tts_guard_task = None
        _zombie_tts_guard_until = 0.0

    async def scenario():
        loop = asyncio.get_running_loop()
        s = S()
        assert pred(s) is False                      # 没 arm 就不拦
        ev = asyncio.Event()
        old = asyncio.ensure_future(ev.wait())       # 被 drain 掉的旧轮（僵尸）
        s._zombie_tts_guard_task = old
        s._zombie_tts_guard_until = loop.time() + 8.0
        assert pred(s) is True                       # 旧轮还活着 → 拦晚到 TTS 建流
        ev.set()
        await old
        assert pred(s) is False                      # 旧轮收口 → 窗即刻关
        assert s._zombie_tts_guard_task is None
        s._zombie_tts_guard_task = old
        s._zombie_tts_guard_until = loop.time() - 1  # 硬上限（旧轮永不收口兜底）
        assert pred(s) is False
        old.cancel()

    asyncio.run(scenario())
    src = _src(CC / "assist_satellite.py")
    assert "self._zombie_tts_guard_task = old_task" in src, "arm 处未交出旧轮身份"
    assert "_is_stale_round_event" not in src, "v1.0.83 整扇身份闸回潮（曾误杀健康轮）"


# ── ② 判活：无消费者直投丢弃的判据必须用外层轮任务 ─────────────────────
def test_round_alive_uses_outer_task_only():
    ns = {}
    exec(compile(_fn(CC / "assist_satellite.py", "_round_alive"),
                 "alive", "exec"), ns)
    alive = ns["_round_alive"]

    class T:
        def __init__(self, done):
            self._d = done
        def done(self):
            return self._d

    class S:
        _round_outer_task = None
        _pipeline_task = None

    s = S()
    assert alive(s) is False                         # 从未开轮
    s._round_outer_task = T(False)
    assert alive(s) is True                          # 本轮在跑
    s._round_outer_task = T(True)
    assert alive(s) is False                         # 本轮已收口
    # core 会重绑 _pipeline_task（drain 窗内恒 None）——判活不得看它
    src = _fn(CC / "assist_satellite.py", "_round_alive")
    assert "_round_outer_task" in src and "_pipeline_task" not in src.split(
        "本仓自有的外层身份")[0].split("\n\n")[-1]


# ── ⑤ 连续对话四态：http.py 真实三个纯函数的行为（HA 不可导入 → AST 抽取）──
def _cont_ns():
    src = _src(CC / "huijian" / "http.py")
    a = src.index("_CONT_SUFFIX = ")
    b = src.index("class HuijianSatelliteContinuousView")
    ns = {"er": None}
    exec(compile(src[a:b], "cont", "exec"), ns)
    return ns


class _Ent:
    def __init__(self, eid, uid, disabled=False):
        self.entity_id, self.unique_id, self.disabled = eid, uid, disabled


class _St:
    def __init__(self, state):
        self.state = state


class _Reg:
    """真 HA 的 EntityRegistry 对象**没有** async_entries_for_config_entry 方法
    （它是 homeassistant.helpers.entity_registry 的模块级函数，签名
    `(registry, config_entry_id)`）。桩必须与真表面同形——此前桩照着错误的
    方法形状定义，把 `reg.async_entries_for_config_entry(entry_id)` 一路放到生产 500。"""
    def __init__(self, ents):
        self._e = ents


class _States:
    def __init__(self, d):
        self._d = d
    def get(self, eid):
        return self._d.get(eid)


class _Hass:
    def __init__(self, ents, states):
        import types
        mod = types.SimpleNamespace(async_get=lambda h: _Reg(ents))
        self.er = mod
        self.states = _States(states)


def _run_cont(ents, states):
    ns = _cont_ns()
    import types
    ns["er"] = types.SimpleNamespace(
        async_get=lambda h: _Reg(ents),
        async_entries_for_config_entry=lambda reg, _eid: list(reg._e))

    class E:
        entry_id = "x"
    return (ns["_continuous_diag"](ns_er_hass(ents, states), E()),
            ns["_continuous_state"](ns_er_hass(ents, states), E()),
            ns["_continuous_entity"](ns_er_hass(ents, states), E()))


def ns_er_hass(ents, states):
    import types
    h = types.SimpleNamespace()
    h.er = None
    h.states = _States(states)
    return h


def test_continuous_diag_four_states():
    """四种 None 因必须各自有名有姓（v1.0.80 全塞"需固件≥2.1.46"，现场换固件白折腾）。"""
    ns = _cont_ns()
    import types

    def hass(ents, states):
        h = types.SimpleNamespace()
        h.states = _States(states)
        return h

    def call(ents, states):
        ns["er"] = types.SimpleNamespace(
            async_get=lambda h: _Reg(ents),
            async_entries_for_config_entry=lambda reg, _eid: list(reg._e))
        e = types.SimpleNamespace(entry_id="x")
        return (ns["_continuous_diag"](hass(ents, states), e),
                ns["_continuous_state"](hass(ents, states), e))

    mic = _Ent("switch.dev_mic", "AA:BB-switch-mic_switch")
    cont = _Ent("switch.dev_continuous", "AA:BB-switch-continuous_dialogue_switch")
    assert call([mic], {}) == ("no_entity", None), "无实体要报 no_entity"
    assert call([mic, cont], {}) == ("pending", None), "注册了但没态=待就绪"
    assert call([cont], {"switch.dev_continuous": _St("on")}) == ("on", True)
    assert call([cont], {"switch.dev_continuous": _St("off")}) == ("off", False)
    assert call([cont], {"switch.dev_continuous": _St("unavailable")}) == (
        "offline", None), "设备离线不得赖固件"
    d = _Ent("switch.dev_continuous", "AA:BB-switch-continuous_dialogue_switch",
             disabled=True)
    assert call([d], {}) == ("disabled", None), "实体在 HA 被禁用必须单独点名"
    assert call([d, cont], {"switch.dev_continuous": _St("on")}) == ("on", True), (
        "同前缀有可用实体时不因禁用条目瞎掉")
    err = _cont_ns()
    src = _src(CC / "huijian" / "http.py")
    for key in ("no_entity", "disabled", "offline", "pending"):
        assert '"' + key + '"' in src, "诊断码 " + key + " 丢失"
    assert "echoed" in src and "await asyncio.sleep(0.2)" in src, "回显有界复核丢失"


# ── ⑤ 中继原样透传 echoed（判据不装两面）───────────────────────────────
def test_relay_passes_through_echoed():
    from conftest import FakeHAClient
    from core.admin_api import make_admin_app
    # 直接复用 v1.0.80 那套已验证的中继夹具（_ctx/_serve/_jpost 同源，
    # 自己拼 AppContext 会拼出连不上的服务，见本批首轮红）
    from test_v1080_continuous_panel import _ctx, _jpost, _serve

    ha = FakeHAClient(writes={
        ("POST", "/api/huijian-ai/satellites/continuous"):
            {"success": True, "enabled": False, "echoed": False}})
    srv = _serve(make_admin_app(_ctx(ha)))   # 生成器必须留住引用，否则下一行
    port = next(srv)                          # 就被 GC → finally 收摊 → 连不上
    st, j = _jpost(port, "/api/device/continuous", {"mac": "aa", "enabled": False})
    assert st == 200 and j["success"] and j["enabled"] is False
    assert j["echoed"] is False, "集成说未回显，中继不得洗成成功"


# ── ④ 级联：主发次结果不确定 → 不降级重放（真实 _cascade 行为）──────────
def test_indeterminate_failure_does_not_replay_fallback():
    from core.pipeline import (select_primary_plan, select_fallback_plan,
                               Pipeline)
    from test_klar_nlu import _plan
    import test_nlu_llm_boundary as B

    kl = _plan("klar", "HassTurnOn", {})
    fp = _plan("t0", "TurnDeviceOn", {})
    # 前提自证：这组计划确实"会被降级"——否则本钉不咬人（v1.0.83 教训）
    assert select_primary_plan(fp, kl) is kl
    assert select_fallback_plan(kl, fp, kl, "x") is fp

    class FakeEx:
        def __init__(self, indeterminate):
            self.last_run = {}
            self.calls = []
            self.mode = indeterminate
        async def run(self, plan):
            self.calls.append(plan.source)
            self.last_run = {"steps": 1, "applied": 0,
                             "indeterminate": self.mode and plan.source == "klar"}
            return (False, "这一步没拿到执行回执，设备可能已经动作了"
                    if plan.source == "klar" else (True, "不该被走到"))

    def build(mode):
        ex = FakeEx(mode)
        pipe = B._pipe(executor=ex)

        async def match_pair(text):
            return fp, kl
        pipe._match_pair = match_pair
        return ex, pipe

    ex, pipe = build(True)
    r = B._casc(pipe, "打开办公室射灯")
    assert ex.calls == ["klar"], "超时轮被降级重放了一次：" + repr(ex.calls)
    assert r.ok is False and "降级跳过" in " ".join(r.trace)

    ex2, pipe2 = build(False)
    r2 = B._casc(pipe2, "打开办公室射灯")
    assert ex2.calls == ["klar", "t0"], "明确失败时降级照旧（本钉要会咬人）"


def test_changelog_sections_wellformed_and_within_ci_cap():
    """CHANGELOG 结构钉（本批实发教训：插 1.0.87 段时把 1.0.86 的标题整行吃掉，
    结果 CI 提取 1.0.87 正文越界把上一版记录一起塞进 release）。三条：
    ① 每个版本段标题格式合法且**版本连续不缺档**（从当前版往下 6 档）；
    ② 当前版本段必须能被 CI 的同形 awk 干净切出（下一段标题即边界）；
    ③ 当前版本段 ≤80 行——CI 用 `head -80` 截正文，超了就把段尾（含诚实账）
       静默丢掉，客户看到的 release 比仓库记录少一截。"""
    import re
    src = (ROOT.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    heads = re.findall(r"(?m)^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})", src)
    assert heads, "CHANGELOG 无任何版本段标题"
    for ver, date in heads[:8]:
        assert re.match(r"^\d+\.\d+\.\d+$", ver), ver
    ver = (ROOT / "config.yaml").read_text(encoding="utf-8")
    ver = re.search(r'(?m)^version:\s*"([\d.]+)"', ver).group(1)
    assert heads[0][0] == ver, "CHANGELOG 首段不是当前版本 %s" % ver
    缺 = [heads[i][0] for i in range(1, min(6, len(heads)))
          if int(heads[i][0].rsplit(".", 1)[1]) + 1
          != int(heads[i - 1][0].rsplit(".", 1)[1])]
    assert not 缺, "版本段缺档（上一版标题被吃掉？）：" + ", ".join(缺)
    lines = src.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("## [" + ver + "]"))
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## [")), len(lines))
    assert end - start <= 80, (
        "当前版本段 %d 行 > CI head -80 —— release 正文会被静默截尾" % (end - start))


def test_honest_phrasing_for_indeterminate():
    from core.executor import zh_error, is_indeterminate
    assert is_indeterminate("执行超时")
    say = zh_error("执行超时")
    assert "没有执行成功" not in say, "超时被说成确定没成功＝假确定，诱用户手动二发"
    assert "回执" in say and "不自动再试" in say, say
    assert "没有执行成功" in zh_error("unseen upstream failure"), (
        "确定没生效的话术不得一起改掉（本批只改 indeterminate 一支）")
