"""huijian_ai 集成 config_flow 行为钉桩（源码文本级，仿 transcode 通道钉桩先例）。

背景（三项目适配判定书 v1.0.2）：用户实机报
`Timeout waiting for setup data for <uuid>`——设备配对链路
（小程序扫码→贴令牌→BLE CMD20→设备 POST /setup/qrcode）的人肉耗时
远超原 60s 等待窗，config flow 静默超时后 qrcode_done 只报
“配置类型未知”，排障无从下手。本测试钉三条修复不回退。
"""

import re
from pathlib import Path

CONFIG_FLOW = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai" / "config_flow.py"


def _src():
    return CONFIG_FLOW.read_text(encoding="utf-8")


def test_wait_window_at_least_5_minutes():
    src = _src()
    rounds = int(re.search(r"_WAIT_SETUP_ROUNDS\s*=\s*(\d+)", src).group(1))
    interval = float(re.search(r"_WAIT_SETUP_INTERVAL\s*=\s*([\d.]+)", src).group(1))
    assert rounds * interval >= 300, f"setup data 等待窗 {rounds * interval:.0f}s < 5min，会复发实机超时"


def test_timeout_logs_actionable_hint():
    src = _src()
    i = src.index('Timeout waiting for setup data for %s (waited')  # 日志格式串本体（非注释）
    # 指引文案可能跨多个字符串字面量拼接，取 300 字符窗口判含
    assert "CMD20" in src[i : i + 300], "超时日志必须包含可操作指引（配对链路口径）"


def test_qrcode_done_none_branch_shows_guidance():
    src = _src()
    assert "等待超时：未收到设备配对数据" in src, (
        "setup_data 缺失时禁止裸报『配置类型未知』（用户无法定位），必须给引导话术"
    )


def test_qr_params_carry_lan_internal():
    # 二维码必须携带 ha_internal：设备 POST setup data 的局域网直连目标
    # （external 正文在开了远程访问的 HA 上是公网地址，固件常不可达 → 超时）
    assert '"ha_internal": internal' in _src()


def test_internal_bound_before_params_use():
    """UnboundLocalError 回归钉桩（v1.0.2 实发 500 的根因）。

    v1.0.2 首发把 "ha_internal": internal 塞进 params 字面量时，internal
    的赋值行还在字典之后 → async_step_qrcode 一进入即 500，向导打不开。
    文本级存在性检查拦不住求值顺序，这里钉：函数体内 internal 的赋值
    必须先于其任何字典值引用。
    """
    src = _src()
    body = src[src.index("async def async_step_qrcode") : src.index("async def async_step_qrcode_done")]
    # 去注释行（注释里会引用代码字面量，first-index 会撞注释——v1.0.2 500 钉桩首版即被自己骗了）
    code = "\n".join(ln.split("#")[0] for ln in body.splitlines())
    # 锚定裸赋值 "internal = "，不锁右侧表达式：v1.0.4 起 get_url 结果先过
    # _ensure_lan_port 归一（局域网裸 IP 补真实端口），钉的是求值顺序不是实现写法
    assign = code.index("internal = ")
    use = code.index('"ha_internal": internal')
    assert assign < use, "internal 必须在 params 字面量引用 ha_internal 之前赋值"


# ── v1.0.4 行为级钉桩：_ensure_lan_port（二维码 ha_internal 源头端口归一）──
# 用 AST 抠出真实函数体 exec——不是文本存在性检查（v1.0.2 求值序教训：
# 文本检查会被注释骗过，这里执行的是将随加载项出厂的同一份代码）。

def _ensure_lan_port_fn():
    import ast
    tree = ast.parse(_src())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_ensure_lan_port")
    ns = {}
    exec(compile(ast.get_source_segment(_src(), fn), "<ensure_lan_port>", "exec"), ns)
    return ns["_ensure_lan_port"]


def _clean_mcp_endpoint_fn():
    """同 AST 范式：执行的是将随加载项出厂的同一份 _clean_mcp_endpoint。"""
    import ast
    src = _src()
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_clean_mcp_endpoint")
    ns = {"Any": object, "annotations": None,
          "_LOGGER": __import__("logging").getLogger("pin")}
    exec(compile(ast.get_source_segment(src, fn), "<clean_mcp_endpoint>", "exec"), ns)
    return ns["_clean_mcp_endpoint"]


def test_clean_mcp_endpoint_behavior():
    """存量固件垃圾值防线（v2.1.4 前设备 mcp 双空 POST "?token="）——
    D1 门禁只判空串，垃圾值必须归 None，合法 URL 必须原样穿透。"""
    f = _clean_mcp_endpoint_fn()
    assert f("?token=") is None
    assert f("?token=abc") is None
    assert f("") is None
    assert f("   ") is None
    assert f(None) is None
    assert f("https://mcp.hi-chip.com/mcp/?token=***") == "https://mcp.hi-chip.com/mcp/?token=***"
    assert f("ws://192.168.1.9:8000/xiaozhi/v1/llm") == "ws://192.168.1.9:8000/xiaozhi/v1/llm"
    assert f("  HTTPS://x.test/mcp ") == "HTTPS://x.test/mcp"
    # 入驻两条分支必须都走消毒（device 与 assist 共用 mcp_endpoint 变量）
    src = _src()
    assert "mcp_endpoint = _clean_mcp_endpoint(" in src, "入驻入口消毒被回退"


class _Hass:
    def __init__(self, port=8124, have_http=True):
        import types
        self.http = types.SimpleNamespace(server_port=port) if have_http else None


def test_ensure_lan_port_behavior():
    f = _ensure_lan_port_fn()
    # 事故形态：局域网裸 IP 补真实监听端口（实况优先，不猜 8123）
    assert f(_Hass(), "http://192.168.1.91") == "http://192.168.1.91:8124"
    # 裸根路径归一：防下游 "//"+"api" 双斜杠
    assert f(_Hass(), "http://192.168.1.91/") == "http://192.168.1.91:8124"
    # 各放行面
    assert f(_Hass(), "http://192.168.1.91:9999") == "http://192.168.1.91:9999"
    assert f(_Hass(), "https://192.168.1.91") == "https://192.168.1.91"
    assert f(_Hass(), "http://ha.example.com") == "http://ha.example.com"
    assert f(_Hass(), "http://8.8.8.8") == "http://8.8.8.8"
    assert f(_Hass(), "") == ""
    # http 未就绪时序 → 官方默认 8123 兜底
    assert f(_Hass(have_http=False), "http://10.1.2.3") == "http://10.1.2.3:8123"
    # IPv6 字面量不碰（文档承诺域=IPv4；重拼 netloc 必产缺括号畸形 URL——
    # 2026-09-10 发布前审查实锤的越界，守卫钉死）
    assert f(_Hass(), "http://[fd12:3456::1]/") == "http://[fd12:3456::1]/"
    # 带路径的私有 IP 保留路径
    assert f(_Hass(), "http://192.168.4.30/ha") == "http://192.168.4.30:8124/ha"


# ── 配对后 6053 重启窗口自动重试（2026-09-08 11:02 实机 Errno 111 配套）──


def _load_reboot_fn():
    import asyncio as _a
    import logging as _lg
    import textwrap
    src = _src()
    m = re.search(r"(    async def _fetch_device_info_through_reboot.*?)\n    async def fetch_device_info",
                  src, re.S)
    assert m, "_fetch_device_info_through_reboot 方法被回退"
    ns = {"asyncio": _a, "_LOGGER": _lg.getLogger("t")}
    exec(textwrap.dedent(m.group(1)), ns)
    return ns["_fetch_device_info_through_reboot"]


class _FlowStub:
    _REBOOT_RETRY_DELAY = 0.01   # 测试免等
    _REBOOT_RETRY_ATTEMPTS = 2

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def fetch_device_info(self):
        self.calls += 1
        return self.results.pop(0) if self.results else None


def test_reboot_retry_recovers_connection_error():
    import asyncio
    fn = _load_reboot_fn()
    s = _FlowStub(["connection_error", "connection_error", None])
    assert asyncio.run(fn(s)) is None
    assert s.calls == 3


def test_reboot_retry_bounded_and_reports_last_error():
    import asyncio
    fn = _load_reboot_fn()
    s = _FlowStub(["connection_error", "connection_error", "connection_error"])
    assert asyncio.run(fn(s)) == "connection_error"
    assert s.calls == 1 + s._REBOOT_RETRY_ATTEMPTS   # 1 首发 + 2 重试封顶


def test_reboot_retry_no_retry_on_deterministic_errors():
    import asyncio
    fn = _load_reboot_fn()
    for err in ("invalid_password", "invalid_encryption_key", "resolve_error"):
        s = _FlowStub([err])
        assert asyncio.run(fn(s)) == err
        assert s.calls == 1, f"{err} 是确定性错误，重试只会拖慢配对面板"


def test_reboot_retry_window_covers_firmware_10s():
    src = _src()
    delay = float(re.search(r"_REBOOT_RETRY_DELAY\s*=\s*([\d.]+)", src).group(1))
    attempts = int(re.search(r"_REBOOT_RETRY_ATTEMPTS\s*=\s*(\d+)", src).group(1))
    # 固件 CMD20 后延迟 10s 重启才真正 listen :6053（ble_manager.cc:745 注释链）
    assert delay * attempts >= 10, f"重试窗 {delay * attempts:.0f}s 盖不住设备 10s 重启"
    # device 分支必须走带重试的封装，防回退成裸 fetch
    assert "error = await self._fetch_device_info_through_reboot()" in src


# ── 审查修复 B/F（2026-09-08 七项审查）：超时话术翻译 + rewait 续等 ──


def test_unknown_config_type_translated_both_langs():
    """B：超时/未知类型出口都挂 unknown_config_type 键，zh-Hans 与 en 必须
    都有翻译——此前 5 键表缺它，用户 5 分钟超时看到「未知错误查日志」式红字。"""
    import json
    tr = CONFIG_FLOW.parent / "translations"
    for name in ("zh-Hans.json", "en.json"):
        d = json.loads((tr / name).read_text(encoding="utf-8"))
        assert "unknown_config_type" in d["config"]["error"], name
        assert "no_setup_data" in d["config"]["abort"], name


def test_wait_timeout_flag_consumed_with_rewait():
    """F：_setup_wait_timed_out 不再是僵尸标志——init 显式初始化，qrcode_done
    超时分支消费它给出「再等一轮」（保持同一 setup_uuid 续等迟到 POST），
    取消勾选则干净退出；旧「再提交只会重复同一条」死胡同封死。"""
    src = _src()
    i = src.index("def init(self):")
    assert "self._setup_wait_timed_out = False" in src[i:i + 400], \
        "init 未初始化标志（getattr 兜底会复发）"
    assert 'user_input.get("rewait")' in src, "rewait 消费被回退"
    j = src.index('user_input.get("rewait")')
    win = src[j:j + 400]
    assert "self._wait_task = None" in win and "async_step_qrcode()" in win, \
        "再等一轮未重挂等待任务/未复用同一 uuid 通道"
    assert 'async_abort(reason="no_setup_data")' in src, "退出分支丢失"


# ── assist 语音引擎自动装配（2026-09 三端配合 P0-1 修复钉桩）──
# 背景：config_type="assist" 条目承载 conversation/stt/tts 三引擎实体，端点为
# 语音加载项 :8000 三通道；此前 assist 型条目无任何可达创建路径（固件 CMD20 恒
# device、小程序 setupData 恒 device、云函数 action 已删）→ 装了语音卫星也选不到
# 本地引擎。修复 = device 入驻成功后由 __init__ 自动 async_init(SOURCE_IMPORT)
# 补建 assist（默认端点 = HA internal host + :8000），并支持 reconfigure 改端点。
# 以下钉桩防修复回退。

def test_assist_auto_register_source_import_exists():
    """config_flow 必须具备 SOURCE_IMPORT 自动建 assist 的入口。"""
    src = _src()
    assert "SOURCE_IMPORT" in src
    assert "async def async_step_import" in src, "自动补建入口(SOURCE_IMPORT)被回退"


def test_assist_default_endpoint_builder_behavior():
    """默认端点构造：host 由 HA internal URL 解析，通道路径对齐加载项 ws_server。"""
    import ast
    src = _src()
    tree = ast.parse(src)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_voice_endpoint_url")
    ns = {"VOICE_WS_PORT": 8000, "annotations": None}
    exec(compile(ast.get_source_segment(src, fn), "<voice_endpoint_url>", "exec"), ns)
    f = ns["_voice_endpoint_url"]
    assert f("192.168.1.91", "llm") == "ws://192.168.1.91:8000/xiaozhi/v1/llm"
    assert f("ha.lan", "stt") == "ws://ha.lan:8000/xiaozhi/v1/stt"
    assert f("192.168.1.91", "tts") == "ws://192.168.1.91:8000/xiaozhi/v1/tts"


def test_assist_reconfigure_branches_away_from_qrcode():
    """assist 条目 reconfigure 必须走端点编辑，不能像 device 一样进 qrcode 等设备 POST。"""
    src = _src()
    assert "if data.get(CONF_CONFIG_TYPE) == \"assist\":" in src
    assert "async def async_step_assist_reconfigure" in src, "assist 端点编辑步被回退"


def test_qrcode_done_assist_branch_reuses_helper():
    """qrcode_done assist 分支抽成 _async_create_or_update_assist（防两套建/更逻辑漂移）。"""
    src = _src()
    assert "async def _async_create_or_update_assist" in src
    # assist 条目 data 必须以 haid 为唯一 id（每 HA 一条引擎服务）
    assert "async_entry_for_domain_unique_id(\n            DOMAIN, haid" in src or \
        "async_entry_for_domain_unique_id(DOMAIN, haid" in src


def test_init_auto_ensure_assist_hook():
    """__init__ 必须在 device 型 entry 装配后挂自动补建（fire-and-forget，不 await 阻塞）。"""
    import pathlib
    init = pathlib.Path(CONFIG_FLOW).parent / "__init__.py"
    s = init.read_text(encoding="utf-8")
    assert "async def _async_auto_ensure_assist" in s, "自动补建 helper 被回退"
    assert "_async_auto_ensure_assist(hass, entry)" in s, "device 装配后未触发自动补建"
    assert "SOURCE_IMPORT" in s


def _fn_seg(name, source=None, async_only=True):
    """取 config_flow 中某个函数定义的源码段（结构断言用）。"""
    import ast
    src = source if source is not None else _src()
    want = ast.AsyncFunctionDef if async_only else (ast.FunctionDef, ast.AsyncFunctionDef)
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, want) and n.name == name)
    return ast.get_source_segment(src, fn)


def test_assist_reauth_branches_away_from_qrcode():
    """B1（三端复核）：assist 条目 reauth 必须分流到端点编辑步。

    触发链真实存在：加载项开 require_token 或端点失效 → ws_transport 收 401
    清空端点并停重连 → 下次对话 get_entry_transport 抛 EntryAuthFailedError →
    entry.async_start_reauth。assist 没有设备侧 POST 来源，reauth 走 qrcode
    即 5 分钟必超时死等。v1.0.7 只分流了 reconfigure，本钉桩堵 reauth 缺口。
    """
    seg = _fn_seg("async_step_reauth")
    assert "CONF_CONFIG_TYPE" in seg and '"assist"' in seg, (
        "assist reauth 未分流——用户点『重新认证』会进扫码等待死端"
    )
    assert "async_step_assist_reconfigure" in seg, "assist reauth 应复用端点编辑表单"


def test_import_assist_accepts_device_name_alias():
    """B4：SOURCE_IMPORT 兼容 speak_name/device_name 两种键，条目名不再落空。"""
    src = _src()
    assert 'data.get("speak_name") or data.get(CONF_DEVICE_NAME' in src, (
        "import 只认 speak_name 时，调用方给 device_name 会被静默吞成空串"
    )


def test_create_or_update_assist_cleans_uuid_scratch():
    """B4：finalize 前 clean_setup 弹掉 hass.data[DOMAIN] 的 uuid 便签（防泄漏）。"""
    seg = _fn_seg("_async_create_or_update_assist")
    assert "self.clean_setup()" in seg, "create_entry 路径不过 async_abort，uuid 便签会永久滞留"


# ── B0：模块级名字解析钉桩（防 NameError 级失明回归）──
# 背景：v1.0.7 的 __init__._assist_default_data 用 CONF_DEVICE_NAME 键但漏
# import——真机 host 解析成功即 NameError，assist 自动注册从未生效；本仓
# 测试惯例是源码字符串钉桩，字符串断言对"用了未导入的名字"完全失明。此钉桩
# 用 AST 静态解析模块作用域，堵住这一类 bug。

_SPECIAL_GLOBALS = {
    "__name__", "__file__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__debug__", "__class__", "__all__",
}


def _collect_bindings(node, out):
    import ast
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            out.add(sub.id)
        elif isinstance(sub, ast.arg):
            out.add(sub.arg)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            out.add(sub.name)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(sub.name)
        elif isinstance(sub, ast.Lambda):
            for grp in (sub.args.posonlyargs, sub.args.args, sub.args.kwonlyargs):
                for a in grp:
                    out.add(a.arg)
            if sub.args.vararg:
                out.add(sub.args.vararg.arg)
            if sub.args.kwarg:
                out.add(sub.args.kwarg.arg)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for a in sub.names:
                if a.name != "*":
                    out.add((a.asname or a.name).split(".")[0])
        elif isinstance(sub, (ast.Global, ast.Nonlocal)):
            for nm in sub.names:
                out.add(nm)


def _unresolved_names(src):
    import ast
    import builtins
    tree = ast.parse(src)
    if any(isinstance(n, (ast.Import, ast.ImportFrom))
           and any(a.name == "*" for a in n.names) for n in ast.walk(tree)):
        return []  # 星号导入环境判不了，放行
    module_names = set(dir(builtins)) | _SPECIAL_GLOBALS
    star_ok = True
    for node in tree.body:
        _collect_bindings(node, module_names)
    problems = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local = set()
            _collect_bindings(fn, local)
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    if n.id not in module_names and n.id not in local \
                            and not hasattr(builtins, n.id):
                        problems.append((fn.name, n.id, n.lineno))
    return star_ok and problems


def test_integration_module_names_all_resolvable():
    """B0 钉桩：__init__/config_flow 禁止引用未定义模块级名字（NameError 类回归）。"""
    import pathlib
    base = pathlib.Path(CONFIG_FLOW).parent
    for fname in ("__init__.py", "config_flow.py"):
        src = (base / fname).read_text(encoding="utf-8")
        problems = _unresolved_names(src)
        assert not problems, f"{fname} 存在运行时必 NameError 的名字: {problems}"

