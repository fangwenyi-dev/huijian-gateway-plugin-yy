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
    assign = code.index("internal = get_url")
    use = code.index('"ha_internal": internal')
    assert assign < use, "internal 必须在 params 字面量引用 ha_internal 之前赋值"
