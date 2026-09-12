"""v1.0.49 批次钉：查询族两新族（人感/电量）+ 守卫放行 + 删除变体 + rename 诊断。

现场主诉四条（2026-09-21）：
  ①「办公室温度多少」「查询办公室温度」被 fast_path「多少|几 → 上层」粗闸截走；
  ②「现在办公室是否有人」查询族无人感维度；
  ③「办公室平开窗电池电量多少」无电量维度且同样被「多少」截走；
  ④页内改名对旧版集成（无 PUT 端点）只回天书 HTTP 码。
"""
import asyncio

from conftest import FakeHAClient
from core.nlu.query import QueryZone, looks_local_query
from core.nlu.fast_path import _is_complex_query
from core.nlu import creation as cr


def _q():
    states = {
        "sensor.office_temp": {"entity_id": "sensor.office_temp", "state": "26.5",
                               "attributes": {"friendly_name": "办公室温度", "device_class": "temperature"}},
        "binary_sensor.office_occupancy": {"entity_id": "binary_sensor.office_occupancy",
                                           "state": "on",
                                           "attributes": {"friendly_name": "办公室人感", "device_class": "occupancy"}},
        "sensor.office_window_batt": {"entity_id": "sensor.office_window_batt", "state": "85",
                                      "attributes": {"friendly_name": "办公室平开窗电池", "device_class": "battery"}},
        "sensor.hall_motion": {"entity_id": "sensor.hall_motion", "state": "not_detected",
                               "attributes": {"friendly_name": "走廊活动", "device_class": "motion"}},
    }
    return QueryZone(FakeHAClient(
        states=states,
        areas={"a1": "办公室", "a2": "走廊"},
        entity_area={"sensor.office_temp": "办公室",
                     "binary_sensor.office_occupancy": "办公室",
                     "sensor.office_window_batt": "办公室",
                     "sensor.hall_motion": "走廊"}), None)


def test_presence_query():
    ans = asyncio.run(_q().answer("现在办公室是否有人"))
    assert ans and "有人" in ans and "没人" not in ans
    ans2 = asyncio.run(_q().answer("走廊有没有人"))
    assert ans2 and "没人" in ans2
    ans3 = asyncio.run(_q().answer("卧室有人吗"))   # 该区域无人感传感器 → 让位不猜
    assert ans3 is None


def test_battery_query():
    ans = asyncio.run(_q().answer("办公室平开窗电池电量多少"))
    assert ans and "85" in ans and "平开窗" in ans
    ans2 = asyncio.run(_q().answer("查询办公室电量"))  # 无设备词 → 区域命中
    assert ans2 and "85" in ans2


def test_temp_query_lead_forms():
    for t in ("办公室温度多少", "查询办公室温度", "办公室的温度是多少", "现在办公室多少度"):
        ans = asyncio.run(_q().answer(t))
        assert ans and "26.5" in ans, t


def test_guard_passes_local_dims():
    # 现场原句必须不再被"交上层"截走
    for t in ("办公室温度多少", "查询办公室温度", "现在办公室是否有人",
              "办公室平开窗电池电量多少", "办公室有没有人"):
        assert looks_local_query(t), t
        assert not _is_complex_query(t), t
    # 控制/创作句不受影响（False=不是复杂查询，照常走快链）
    assert not looks_local_query("打开客厅灯")
    # 真·上层句仍交上层（无本地量纲词）
    assert _is_complex_query("今天天气怎么样")


def test_scene_delete_name_before_word():
    cases = {
        "晚安场景删了": ("delete_scene", "晚安"),
        "把睡觉的场景给我删除": ("delete_scene", "睡觉"),
        "客厅那个场景删掉": ("delete_scene", "客厅"),
        "把回家自动化删了": ("delete_automation", "回家"),
        "出门那个自动化删除": ("delete_automation", "出门"),
    }
    for sent, (kind, name) in cases.items():
        p = cr.parse(sent)
        assert p and p.get("kind") == kind, sent
        assert p.get("trigger_phrase", p.get("target")) == name, sent
    # 裸删契约不回归（v1.0.33 钉）：把场景删了=引导清单，不精准删
    p = cr.parse("把场景删了")
    assert p and p["kind"] == "delete_scene" and p["trigger_phrase"] == ""
    assert cr.parse("删除所有场景") is None or \
        cr.parse("删除所有场景").get("kind") != "delete_scene"


def test_rename_405_diagnosis():
    """旧版集成无 PUT 端点：405/404 天书码必须翻译成"升级集成+重启 HA"。"""
    import json
    from unittest.mock import MagicMock
    from aiohttp import web
    from core.admin_api import _scene_rename, CTX_KEY

    ha = FakeHAClient(writes={
        ("PUT", "/api/huijian-ai/voice-scenes/s9"): {"success": False, "error": "HTTP 405"}})
    ctx = MagicMock(ha=ha, scenes=MagicMock(refresh=asyncio.sleep))
    req = MagicMock()
    req.app = {CTX_KEY: ctx}
    req.json = asyncio.coroutine(lambda: {"scene_id": "s9", "new_phrase": "午安"}) \
        if False else None

    async def _json():
        return {"scene_id": "s9", "new_phrase": "午安"}
    req.json = _json
    resp = asyncio.run(_scene_rename(req))
    body = json.loads(resp.body)
    assert body["ok"] is False
    assert "重启 HA" in body["error"] and "405" not in body["error"]


def test_window_plan_keeps_area_exhibit_hall():
    """现场 2026-09-12 11:01「展厅推拉窗开到百分之五十」——加载项 NLU 实证
    target 必带 area=展厅（丢区域的是旧集成执行侧话术与回退匹配，v1.0.48
    移植的 area 约束 + 本批话术区域化收口）。"""
    import asyncio
    from core.nlu.fast_path import FastPath

    class SC:
        def __getattr__(self, n):
            if n.startswith("__"):
                raise AttributeError(n)
            async def _a(*a, **k):
                return None
            def _f(*a, **k):
                return None
            return _a if n.startswith(("verify", "refresh")) else _f

    class S:
        def get(self, k, d=None):
            return d
    fp = FastPath(SC(), None, S())
    p = asyncio.run(fp.match("展厅推拉窗开到百分之五十"))
    assert p and p.intent == "ControlWindow"
    t = p.args["target"][0]
    assert t.get("area") == "展厅" and t["devices"][0]["name"] == "推拉窗"
    p2 = asyncio.run(fp.match("办公室平开窗关上三分之一"))
    assert p2 and p2.args["target"][0].get("area") == "办公室"


def test_window_speech_carries_area():
    """集成窗意图话术钉：设备名形态必须「区域+的+设备」，全屋形态「区域的所有窗户」。"""
    import pathlib
    # 路径按测试文件锚定（v1.0.49 CI 实锤）：裸相对路径 "custom_components/..."
    # 只在 cwd=huijian_voice/ 时成立，而 CI 从仓库根跑 `pytest huijian_voice/tests`
    # → FileNotFoundError（本地绿、CI 红）。仓内既有钉子一律用 parents[1] 锚定。
    root = pathlib.Path(__file__).resolve().parents[1]
    src = (root / "custom_components/huijian_ai/intent_window_control.py").read_text(
        encoding="utf-8")
    assert 'label = f"{area_name}的{_dev_label}"' in src, "区域话术丢失"
    assert "所有窗户" in src
