"""v1.0.55「设备换 IP 后 HA 永久追不回」修复批的回归钉。

根因（2026-09-12 现场定谳，见 gujian 记忆体 insight 237048ea）：
固件 v2.1.24 起广播 `_esphomelib._tcp:6053`（TXT 带 mac），但本 fork 的
manifest.json 把上游 esphome 的 zeroconf 声明清成了 `[]` → HA 从不浏览该
服务类型 → config_flow 里现成的"MAC 命中既有条目 → 更新 host → reload"
迁移链成死代码。现场指纹：`Can't connect to ESPHome API … [Errno 113]` +
设备重启仍不回（设备换了 IP）+ 设备侧 rebind 后零 `Accepted`。

三层钉桩：
① manifest 形状钉（zeroconf 声明必须含 _esphomelib._tcp.local.，且迁移流
   方法齐全——上次断链恰是"合法 YAML 空数组"，字符串钉全瞎）；
② 迁移行为真执行钉：AST 抽 config_flow 四个真实方法进占位空间（py313 在
   def 时求值注解——MEMORY 教训），跑五个分支；
③ manager 不可达观测窗真执行 + 结构钉（on_connect 复位、on_connect_error
   分流用 AST 断言调用确实在 `not isinstance(auth)` 分支内）。
"""
import ast
import asyncio
import json
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "huijian_ai"
CF_SRC = (INTEGRATION / "config_flow.py").read_text(encoding="utf-8")
MANAGER_SRC = (INTEGRATION / "manager.py").read_text(encoding="utf-8")
MAIN_MANIFEST = INTEGRATION / "manifest.json"
STORE_MANIFEST = (ROOT.parent / "yyjicheng" / "custom_components"
                  / "huijian_ai" / "manifest.json")

ESPHOMELIB_TYPE = "_esphomelib._tcp.local."
FLOW_METHODS = ("async_step_zeroconf", "_async_validate_mac_abort_configured",
                "_abort_unique_id_configured_with_details", "async_step_dhcp")


# ── ① manifest 形状 + 流方法齐全 + 双语话术 ─────────────────────
def test_manifest_declares_esphomelib_zeroconf():
    m = json.loads(MAIN_MANIFEST.read_text(encoding="utf-8"))
    assert m.get("zeroconf"), (
        "zeroconf 声明再次被清成 []——固件的 mDNS 广播将无消费者，"
        "设备换 IP 后 HA 永久连不回（2026-09-12 现场定谳根因）"
    )
    assert ESPHOMELIB_TYPE in m["zeroconf"], \
        f"zeroconf 必须声明 {ESPHOMELIB_TYPE}（对齐上游 esphome，触发迁移流）"
    names = {n.name for n in ast.walk(ast.parse(CF_SRC))
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = set(FLOW_METHODS) - names
    assert not missing, f"声明恢复了但迁移流方法被删=另一种断链: {missing}"


def test_store_copy_manifest_declares_zeroconf_if_present():
    # 商店副本是嵌套仓工作区（外层 gitignore；CI 树内可能没有）——在就必须同步
    if not STORE_MANIFEST.exists():
        pytest.skip("本工作树不含 yyjicheng 商店副本（外层 gitignore，正常）")
    m = json.loads(STORE_MANIFEST.read_text(encoding="utf-8"))
    assert ESPHOMELIB_TYPE in m.get("zeroconf", []), \
        "商店副本 zeroconf 声明与主仓不一致（客户从商店装的就是断链版）"


def test_repair_issue_translations_bilingual():
    want = ("name", "address", "minutes", "attempts", "error")
    for lang in ("en.json", "zh-Hans.json"):
        d = json.loads((INTEGRATION / "translations" / lang)
                       .read_text(encoding="utf-8"))
        issue = d["issues"]["satellite_unreachable"]
        blob = issue["title"] + issue["description"]
        missing = [ph for ph in want if "{" + ph + "}" not in blob]
        assert not missing, f"{lang} satellite_unreachable 缺占位符 {missing}"


# ── ② 迁移行为真执行（AST 抽 config_flow 真实源码段）────────────
class AbortFlow(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _extract(source, names):
    tree = ast.parse(source)
    found = {}
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in names and node.name not in found):
            found[node.name] = ast.get_source_segment(source, node)
    assert set(found) == set(names), f"缺函数源码: {set(names) - set(found)}"
    return found


def _cf_namespace():
    """py313 在 def 时求值注解——为注解/体内的非内置裸名补占位（教训守则①）。"""
    async def _noop(*a, **k):
        return None

    class _Placeholder:
        def __class_getitem__(cls, item):
            return cls

    return {
        "AbortFlow": AbortFlow,
        "ConfigFlowResult": _Placeholder,
        "ZeroconfServiceInfo": _Placeholder,
        "DhcpServiceInfo": _Placeholder,
        "format_mac": lambda s: str(s).lower(),
        "SOURCE_IGNORE": "ignore",
        "SOURCE_RECONFIGURE": "reconfigure",
        "CONF_HOST": "host",
        "CONF_PORT": "port",
        "CONF_DEVICE_NAME": "device_name",
        "CONF_NOISE_PSK": "noise_psk",
        "DEFAULT_PORT": 6053,
        "Any": _Placeholder,
        "async_import_module": _noop,
    }


class FakeFlow:
    """只携带被抽方法需要的属性/协程；fetch/abort 均留痕可查。"""

    def __init__(self, entry, probed_mac):
        self.handler = "huijian_ai"
        self.source = "user"
        self.unique_id = None
        self._device_mac = None
        self._probed_mac = probed_mac
        self.fetch_calls = []
        self.abort = None
        self.confirm_called = False
        self.hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_entry_for_domain_unique_id=(
                    lambda handler, mac: (
                        entry if entry and entry.unique_id == mac else None)
                )
            )
        )

    async def async_set_unique_id(self, mac):
        self.unique_id = mac

    async def _fetch_device_info(self, host, port, psk):
        self.fetch_calls.append((host, port))
        self._device_mac = self._probed_mac

    def _abort_if_unique_id_configured(self, *, updates, error,
                                       description_placeholders=None):
        self.abort = {"updates": updates, "error": error}
        raise AbortFlow(error)

    async def async_step_discovery_confirm(self):
        self.confirm_called = True
        return "CONFIRM"

    def async_abort(self, *, reason):
        raise AbortFlow(reason)


def _make_flow(entry, probed_mac):
    ns = _cf_namespace()
    for fname, seg in _extract(CF_SRC, set(FLOW_METHODS)).items():
        exec(compile(seg, f"<cf:{fname}>", "exec"), ns)  # noqa: S102
    flow = FakeFlow(entry, probed_mac)
    for name in FLOW_METHODS:
        setattr(flow, name, types.MethodType(ns[name], flow))
    return flow


MAC = "aa:bb:cc:dd:ee:ff"


def _entry(host):
    return SimpleNamespace(unique_id=MAC, source="user", title="HUIJIAN-0BD0",
                           data={"host": host, "port": 6053, "noise_psk": "psk"})


def _zci(host, properties=None, port=6053):
    return SimpleNamespace(
        host=host, port=port, hostname="huijian-0bd0.local.",
        properties=properties if properties is not None else {"mac": MAC})


def _run(coro):
    return asyncio.run(coro)


def test_zeroconf_same_mac_new_ip_updates_host():
    """主通路：设备换 IP 重新广播 → 拨新址核验 mac → 以 updates 更新 host。

    updates 交回 HA 基类 `_abort_if_unique_id_configured`：对已加载条目
    写 data 并 schedule_reload（官方迁移语义），本钉只管我们递出去的内容。
    """
    flow = _make_flow(_entry("192.168.1.235"), probed_mac=MAC)
    with pytest.raises(AbortFlow) as exc:
        _run(flow.async_step_zeroconf(_zci("192.168.1.77")))
    assert exc.value.reason == "already_configured_updates"
    assert flow.abort["updates"] == {"host": "192.168.1.77", "port": 6053}
    assert flow.fetch_calls == [("192.168.1.77", 6053)], \
        "换 IP 分支必须真连设备核验（防把别的设备 IP 写进条目）"


def test_zeroconf_same_host_aborts_without_probe():
    flow = _make_flow(_entry("192.168.1.235"), probed_mac=MAC)
    with pytest.raises(AbortFlow) as exc:
        _run(flow.async_step_zeroconf(_zci("192.168.1.235")))
    assert exc.value.reason == "already_configured"
    assert flow.fetch_calls == []
    assert flow.abort is None


def test_zeroconf_missing_mac_txt_aborts():
    """TXT 无 mac → 直接 abort（固件 v2.1.24 起 TXT 必带 mac 的另一半契约）。"""
    flow = _make_flow(_entry("192.168.1.235"), probed_mac=MAC)
    with pytest.raises(AbortFlow) as exc:
        _run(flow.async_step_zeroconf(_zci("192.168.1.77", properties={})))
    assert exc.value.reason == "mdns_missing_mac"
    assert flow.fetch_calls == []


def test_zeroconf_probe_mac_mismatch_does_not_update():
    """新 IP 上其实是别的设备（mac 核验不过）→ 不得改 host。"""
    flow = _make_flow(_entry("192.168.1.235"), probed_mac="11:22:33:44:55:66")
    with pytest.raises(AbortFlow) as exc:
        _run(flow.async_step_zeroconf(_zci("192.168.1.77")))
    assert exc.value.reason == "already_configured_detailed"
    assert flow.abort["updates"] == {}, "mac 不符绝不能把别人 IP 写进条目"


def test_dhcp_step_updates_host_registered_device():
    """第二命脉：DHCP 发现（registered_devices）同 mac → 只更 host（无端口）。"""
    flow = _make_flow(_entry("192.168.1.235"), probed_mac=MAC)
    dhcp = SimpleNamespace(macaddress=MAC, ip="192.168.1.88", hostname=None)
    with pytest.raises(AbortFlow) as exc:
        _run(flow.async_step_dhcp(dhcp))
    assert exc.value.reason == "already_configured_updates"
    assert flow.abort["updates"] == {"host": "192.168.1.88"}


# ── ③ manager 不可达观测窗真执行 + 接线结构钉 ──────────────────
def _build_note_harness():
    seg = _extract(MANAGER_SRC, {"_async_note_connect_failure"})[
        "_async_note_connect_failure"]

    class FakeTime:
        now = 1000.0

        @staticmethod
        def monotonic():
            return FakeTime.now

    class FakeLog:
        def __init__(self):
            self.warnings = []

        def warning(self, fmt, *args):
            self.warnings.append(fmt % args)

    log = FakeLog()
    creates = []
    ns = {
        "time": FakeTime,
        "_LOGGER": log,
        "callback": lambda f: f,
        "async_create_issue": lambda *a, **k: creates.append((a, k)),
        "IssueSeverity": SimpleNamespace(WARNING="warning"),
        "DOMAIN": "huijian_ai",
        "CONF_PORT": "port",
        "DEFAULT_PORT": 6053,
        "UNREACHABLE_ISSUE_THRESHOLD_S": 300.0,
        "UNREACHABLE_WARN_INTERVAL_S": 300.0,
    }
    exec(compile(seg, "<mgr>", "exec"), ns)  # noqa: S102
    return ns["_async_note_connect_failure"], FakeTime, log, creates


class FakeSelf:
    def __init__(self):
        self._conn_fail_since = None
        self._conn_fail_count = 0
        self._conn_warn_at = 0.0
        self._unreachable_issue_open = False
        self.hass = object()
        self.host = "192.168.1.235"
        self.entry = SimpleNamespace(title="HUIJIAN-0BD0", unique_id=MAC,
                                     data={"port": 6053})
        self._unreachable_issue_id = "satellite_unreachable-" + MAC


def test_unreachable_window_issue_and_rate_limited_warn():
    note, FakeTime, log, creates = _build_note_harness()
    me = FakeSelf()

    FakeTime.now = 1000.0
    note(me, OSError("timed out"))
    assert creates == [] and log.warnings == [], "刚断不该吱声（<5min 阈值）"

    FakeTime.now = 1350.0
    note(me, OSError("timed out"))
    assert len(creates) == 1 and len(log.warnings) == 1, \
        "越阈第一个尝试即建 issue + 一条 WARNING"
    (args, kwargs) = creates[0]
    ph = kwargs["translation_placeholders"]
    assert kwargs["translation_key"] == "satellite_unreachable"
    assert args[1] == "huijian_ai" and args[2] == me._unreachable_issue_id
    assert ph["address"] == "192.168.1.235:6053"
    assert ph["attempts"] == "2" and ph["minutes"] == "5"
    assert "OSError" in ph["error"]

    FakeTime.now = 1400.0
    note(me, OSError("timed out"))
    assert len(log.warnings) == 1, "间隔未满 5min 不重复告警"

    for _ in range(2):
        FakeTime.now += 1.0
        note(me, OSError("timed out"))
    FakeTime.now = 1900.0
    note(me, OSError("timed out"))
    assert len(log.warnings) == 2 and len(creates) == 2, \
        "5min 节奏：WARNING 与 issue 占位同步刷新（时长/次数不许停在旧值）"
    ph2 = creates[1][1]["translation_placeholders"]
    assert ph2["attempts"] == "6" and ph2["minutes"] == "15"


def _walk_if_not_auth(node):
    """找 `if not isinstance(err, (...Auth...)):` 结构的 If 节点。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.If) and isinstance(sub.test, ast.UnaryOp) \
                and isinstance(sub.test.op, ast.Not) \
                and isinstance(sub.test.operand, ast.Call) \
                and getattr(sub.test.operand.func, "id", "") == "isinstance":
            names = {getattr(e, "id", "") for e in sub.test.operand.args[1].elts}
            if "InvalidAuthAPIError" in names:
                yield sub


def test_manager_wiring_structural():
    """on_connect_error 把连通类失败引到观测窗（结构断言，非字符串巧合）。"""
    seg = _extract(MANAGER_SRC, {"on_connect_error"})["on_connect_error"]
    fn = ast.parse("async def _probe(err):\n" + "\n".join(
        "    " + ln for ln in seg.splitlines()[1:]))
    hits = list(_walk_if_not_auth(fn))
    assert hits, "on_connect_error 不再按 auth 类型分流——v1.0.55 接线被重构挪走？"
    body_src = "\n".join(ast.unparse(s) for s in hits)
    assert "_async_note_connect_failure(err)" in body_src, \
        "连通类失败没有进可观测窗口（现场将再次零痕迹）"

    oc = _extract(MANAGER_SRC, {"on_connect"})["on_connect"]
    assert "async_delete_issue(self.hass, DOMAIN, self._unreachable_issue_id)" in oc
    assert "self._conn_fail_since = None" in oc, "恢复连接不复位窗口=issue 永挂"


# ── ④ 播报截断后再唤醒：卫星端"旧 run 先接管"（需求②集成侧半）──
SATELLITE_SRC = (INTEGRATION / "assist_satellite.py").read_text(encoding="utf-8")


def _build_drain_harness():
    seg = _extract(SATELLITE_SRC, {"_drain_stale_pipeline"})[
        "_drain_stale_pipeline"]

    class FakeLog:
        def __init__(self):
            self.warnings = []

        def warning(self, fmt, *args):
            self.warnings.append(fmt % args)

        def debug(self, *a):
            pass

    ns = {"asyncio": asyncio, "_LOGGER": FakeLog()}
    exec(compile(seg, "<sat>", "exec"), ns)  # noqa: S102
    return ns["_drain_stale_pipeline"], ns["_LOGGER"]


class _NoSelf:  # 方法体不碰实体字段，哑 self 即可
    pass


async def _sleepy():
    await asyncio.sleep(30)


async def _unkillable():
    # 模拟卡在收尾的旧 run：**吞首轮取消**继续睡（drain 必须走超时分支），
    # 再撤才死（防 asyncio.run 收尾挂起——写完这注释才想起上一版 while True
    # 把测试跑挂 120s，教训当场生效）。
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        pass
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        raise


def test_drain_cancels_stale_run():
    drain, log = _build_drain_harness()

    async def scenario():
        old = asyncio.create_task(_sleepy())
        await asyncio.sleep(0)          # 让 old 进入 await
        ok = await drain(_NoSelf(), old, timeout=1.0)
        assert ok is True and old.cancelled()
        assert log.warnings == []
        # 幂等：再来一次（已 done）不得再动刀
        assert await drain(_NoSelf(), old, timeout=1.0) is True

    asyncio.run(scenario())


def test_drain_no_old_is_noop():
    drain, _log = _build_drain_harness()
    assert asyncio.run(drain(_NoSelf(), None)) is True


def test_drain_timeout_does_not_block_new_round_but_warns():
    drain, log = _build_drain_harness()

    async def scenario():
        old = asyncio.create_task(_unkillable())
        await asyncio.sleep(0)
        ok = await drain(_NoSelf(), old, timeout=0.05)
        assert ok is False, "超时须让路给新一轮（卡死旧轮是确定的坏）"
        assert len(log.warnings) == 1 and "未在" in log.warnings[0]
        old.cancel()

    asyncio.run(scenario())


def test_drain_propagates_own_cancellation():
    """实体拆除冲我们来的撤销必须上抛——不得"吞掉撤销还开新轮"。

    用真 task.cancel()（生产语义：拆除=直接撤 _pipeline_task）。
    注意别用 wait_for 当撤销源：它把自导 CancelledError 折成 TimeoutError，
    测的就不是这条通道了。
    """
    drain, _log = _build_drain_harness()

    async def scenario():
        old = asyncio.create_task(_unkillable())
        await asyncio.sleep(0)
        t = asyncio.create_task(drain(_NoSelf(), old, timeout=5.0))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        old.cancel()

    asyncio.run(scenario())


def test_pipeline_start_wires_drain_before_accept():
    """结构钉：新轮协程里 drain 必须先于 accept（顺序倒了=双开窗口重开）。"""
    seg = _extract(SATELLITE_SRC, {"_handle_pipeline_start_impl"})[
        "_handle_pipeline_start_impl"]
    i_drain = seg.index("_drain_stale_pipeline(old_pipeline_task)")
    i_accept = seg.index("await self.async_accept_pipeline_from_satellite")
    assert i_drain < i_accept
    assert "_pipeline_task = self.config_entry.async_create_background_task" in seg
    # v1.0.49 陈旧回调守卫不许被顺手删掉（drain 后旧任务仍会 fire done-cb）
    fin = _extract(SATELLITE_SRC, {"handle_pipeline_finished"})[
        "handle_pipeline_finished"]
    assert "task is not self._pipeline_task" in fin
