"""场景模式句式钉（v1.0.30，060401/061701 SetMode 语料吸收）。

五拆(heat/cool/dry/fan_only/auto)之外的 HA preset 档：睡眠/节能/舒适/静音/
强力/标准——动词泛化 + 三词序（动宾/区域+设备+动/设备+动）全矩阵；集成侧
preset 双通道与 fan 域用源码级钉（HA 依赖模块本地不可导入，行为在 CI E2E）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.executor import MODE_CN          # noqa: E402
from core.nlu import fast_path as F        # noqa: E402
from core.nlu.fast_path import FastPath    # noqa: E402


class S:
    def get(self, k, default=None):
        return default


class SC:
    def check(self, t):
        return None

    def needs_blocking(self):
        return False

    def refresh_soon(self):
        pass


_FP = FastPath(SC(), None, S())


def _m(text):
    return asyncio.run(_FP.match(text))


# (句子, 期望 mode)——三词序 × 六档 × 后缀形态
MATRIX = [
    ("设为睡眠模式", "sleep"),
    ("客厅空调设为睡眠模式", "sleep"),
    ("卧室的空调调到节能", "eco"),
    ("客厅风扇设为静音模式", "silent"),
    ("书房空调切换为强力档", "boost"),
    ("把客厅空调设为舒适模式", "comfort"),
    ("客厅空调设为标准模式", "normal"),
    ("厨房空调变为省电模式", "eco"),
    ("书房空调进入夜间模式", "sleep"),
    ("卧室空调切到睡眠档位", "sleep"),
    ("客厅空调设为睡觉", "sleep"),            # 省"模式"
]


def test_matrix_all_hit():
    for text, want in MATRIX:
        p = _m(text)
        assert p is not None and p.intent == "SetDeviceMode" and p.args.get("mode") == want, \
            f"{text!r} → {p.intent if p else 'MISS'}/{p.args.get('mode') if p else ''}"


def test_new_modes_have_cn_echo():
    """话术回显防英文泄漏：动词表新档必须全在 MODE_CN。"""
    for pattern, intent_type, av in F._ACTION_PATTERNS:
        if intent_type == "SetDeviceMode" and isinstance(av, dict):
            assert av["mode"] in MODE_CN, f"缺中文映射: {av}"


def test_numeric_not_stolen():
    """防误吞：数值/亮度/色温句序优先，泛化模式行排其后不得截胡。"""
    p = _m("客厅空调调到26度")
    assert p and p.intent != "SetDeviceMode"
    p = _m("客厅空调设为除湿模式")             # 既有五拆仍走原行
    assert p and p.intent == "SetDeviceMode" and p.args["mode"] == "dry"


def test_unknown_mode_rejected():
    """词表外模式宁缺勿错（明天/看书/浪漫 → MISS，留给 LLM/兜底说真话）。"""
    assert _m("客厅空调设为明天模式") is None
    assert _m("客厅空调设为浪漫模式") is None


def test_bare_device_still_needs_area():
    """跨房间同名设备不猜——裸"空调"头句维持既有「缺区域」行为。"""
    p = _m("空调设为睡眠模式")
    assert p is None                          # 与"打开空调"同一产品铁律


CC = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"


def _src(name):
    return (CC / name).read_text(encoding="utf-8")


def test_integration_preset_channel_wired():
    """源码级钉：climate preset 回退 + fan 域 handler + platforms 含 FAN。
    （HA 依赖模块本地不可导入；行为在 CI e2e 真 HA 覆盖。）"""
    src = _src("intent_set_mode.py")
    assert '"preset_modes"' in src and 'target.service = "set_preset_mode"' in src
    assert '@register_handler("fan", "mode")' in src
    assert "Platform.FAN" in src
