"""v1.0.80 钉桩：面板「连续对话」开关（加载项→集成→设备 switch 实体）。

真源在设备 NVS cDialogue（小程序 BLE/HA 实体/本面板三边同源，固件 v2.1.46
deferred publish 互回显）；加载项纯转发不装判据。行为钉打真 admin app：
happy/桥断/缺 mac/中继拒绝四形态；集成侧与面板走源级契约钉（suffix=
-continuous_dialogue_switch 与固件 set_object_id 一对一，防任一侧改名失联）。
"""
import time
from pathlib import Path

from conftest import FakeHAClient

from core.admin_api import make_admin_app
from core.ws_server import AppContext
from test_ota_firmware import SettingsFake, _jpost, _serve

ROOT = Path(__file__).resolve().parents[1]
HTTP_PY = ROOT / "custom_components" / "huijian_ai" / "huijian" / "http.py"


def _ctx(ha):
    return AppContext(settings=SettingsFake(), ha=ha, firmware=None,
                      started_at=time.time(), host="10.0.0.9")


def test_continuous_happy_relay():
    ha = FakeHAClient()
    srv = _serve(make_admin_app(_ctx(ha)))
    port = next(srv)
    st, j = _jpost(port, "/api/device/continuous", {"mac": "AA:BB", "enabled": True})
    assert st == 200 and j["success"] and j["enabled"] is True, j
    m, p, b = ha.written[0]
    assert (m, p) == ("POST", "/api/huijian-ai/satellites/continuous")
    assert b == {"mac": "AA:BB", "enabled": True}, "转发体必须逐字给集成（判据不装两面）"


def test_continuous_bridge_down_502():
    ha = FakeHAClient()
    ha.ok = False
    srv = _serve(make_admin_app(_ctx(ha)))
    port = next(srv)
    st, j = _jpost(port, "/api/device/continuous", {"mac": "aa", "enabled": False})
    assert st == 502 and not j["success"] and not ha.written


def test_continuous_requires_mac():
    srv = _serve(make_admin_app(_ctx(FakeHAClient())))
    port = next(srv)
    st, j = _jpost(port, "/api/device/continuous", {"enabled": True})
    assert st == 400 and not j["success"]


def test_continuous_relay_reject_collapses():
    ha = FakeHAClient(writes={
        ("POST", "/api/huijian-ai/satellites/continuous"):
            {"success": False, "error": "该设备无「连续对话」实体（固件需 ≥v2.1.46）"}})
    srv = _serve(make_admin_app(_ctx(ha)))
    port = next(srv)
    st, j = _jpost(port, "/api/device/continuous", {"mac": "aa", "enabled": True})
    assert st == 200 and j["success"] is False and "v2.1.46" in j["error"], \
        "设备侧话术原样回显（单一事实源在集成/设备，加载项不重写）"


def test_integration_contract_source_pins():
    src = HTTP_PY.read_text(encoding="utf-8")
    assert "register_view(HuijianSatelliteContinuousView)" in src
    i = src.index("class HuijianSatelliteContinuousView")
    block = src[i:src.index("def parse_tts_stt_options")]
    assert "requires_auth = True" in block, "写命令通道必须 HA 令牌闸（OTA 中继同规）"
    assert '/api/huijian-ai/satellites/continuous"' in block
    assert '"switch", "turn_on" if enabled else "turn_off"' in block
    assert "raise" not in block.split('"""')[-1], "视图主体禁裸 raise"
    # unique_id 后缀契约 = 固件 set_object_id("continuous_dialogue_switch")
    assert '_CONT_SUFFIX = "-continuous_dialogue_switch"' in src
    # 台账视图带态（True/False/None 三态如实）
    assert '"continuous_dialogue": _continuous_state(hass, entry)' in src


def test_panel_wiring_source_pins():
    html = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
    assert "<th>连续对话</th>" in html
    assert 'data-cont="' in html and "/api/device/continuous" in html
    assert 'colspan="7"' in html, "设备表加列后空行 colspan 未同步"
