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
