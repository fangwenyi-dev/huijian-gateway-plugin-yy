# -*- coding: utf-8 -*-
"""HA 桥接自愈与状态可诊断性钉（2026-09-12 真机实锤）。

现象：HAOS 18.2 / Core 2026.9.1 上加载项升到 1.0.22 后状态页常驻
「HA 桥接 不可达」，用户"手动触发一次"才变在线。根因：启动期 `ha.start()`
那次 `/api/states` 探测撞上 HA Core 启动窗口失败后，**没有任何周期重探**——
`reachable` 只在真实 HA 调用后被点亮，空闲加载项可以一直红着。

修复：状态循环每 5s 顺带一次轻量重探（`ha_health_tick`），并把 `last_error`
透出到状态面与 UI（灯能说明为什么红）。
"""
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", "/tmp/hv_selfheal")
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

from core.main import ha_health_tick          # noqa: E402


class FakeHA:
    def __init__(self, reachable=False, token="tok"):
        self._reachable = reachable
        self.token = token
        self.last_error = "connect error"
        self.calls = 0

    @property
    def reachable(self):
        return self._reachable

    @property
    def ok(self):
        return bool(self.token)

    async def refresh_states(self, force=False):
        self.calls += 1
        self._reachable = True      # 重探成功即自愈
        self.last_error = ""


def test_tick_reprobes_when_unreachable():
    ha = FakeHA(reachable=False)
    asyncio.run(ha_health_tick(ha))
    assert ha.calls == 1 and ha.reachable


def test_tick_skips_when_healthy():
    ha = FakeHA(reachable=True)
    asyncio.run(ha_health_tick(ha))
    assert ha.calls == 0, "已在线仍重探 = 白白打 HA"


def test_tick_skips_without_token():
    ha = FakeHA(reachable=False, token="")
    asyncio.run(ha_health_tick(ha))
    assert ha.calls == 0, "无令牌重探无意义（ok=False）"


def test_tick_never_raises():
    class Boom(FakeHA):
        async def refresh_states(self, force=False):
            raise RuntimeError("boom")

    asyncio.run(ha_health_tick(Boom()))          # 不得抛


def test_status_surfaces_expose_error_and_loop_tick():
    main_src = (HERE / "core" / "main.py").read_text(encoding="utf-8")
    assert "await ha_health_tick(self.ha)" in main_src, "状态循环未接入自愈重探"
    assert '"ha_error": self.ha.last_error' in main_src, "状态快照未透出失败原因"
    health = (HERE / "core" / "admin_api.py").read_text(encoding="utf-8")
    assert '"ha_error"' in health, "/api/health 未透出失败原因"
    ui = (HERE / "www" / "index.html").read_text(encoding="utf-8")
    assert "st.ha_error" in ui, "UI 未展示不可达原因"
