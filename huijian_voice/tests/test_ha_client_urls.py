"""ha_client URL 拼接回归钉桩（2026-09-11 真机 404 事故）。

真机现象：Web debug「真执行：打开办公室射灯」回「抱歉，没找到这个设备」——
curl 实证所有 HA REST 都打在 /api/api/... → HA 404 {"message":"Not Found"}
被话术层误译。根因：默认 base=http://supervisor/core/api 本身即 API 根
（网关 ha_api 定式 base+path 恒一个 /api），而 ha_client 7 处调用点又各拼
`/api/...`；且 refresh_states 用 status<500 判"可达"，404 也被点亮，状态灯
失明。本测试钉三件事不回退：
① 任一 base 写法（supervisor / 直连带 /api / 直连不带）都收敛为恰好一个 /api；
② 全部请求 URL 无双 /api；
③ states 探面对 404 判不可达（真机形态回归时健康灯必须变红而非假绿）。
"""
import asyncio

import pytest

from core import ha_client as hc_mod
from core.ha_client import HAClient

SUP = "http://supervisor/core/api"
# 终态 URL 口径：supervisor base 自身即 API 根，最终形态就是 SUP + "/子路径"
# （/api 只出现一次——由 base 提供），这是本回归的存在意义。
SUPAPI = "http://supervisor/core/api"


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload if payload is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        import json as _j
        return _j.dumps(self._payload)

    async def read(self):
        return b""


class _Session:
    """记录请求 URL 的假 aiohttp session。"""

    def __init__(self, status=200, payload=None):
        self.urls = []
        self.status = status
        self.payload = payload

    def get(self, url, **kw):
        self.urls.append(("GET", url))
        return _Resp(self.status, self.payload)

    def post(self, url, **kw):
        self.urls.append(("POST", url))
        return _Resp(self.status, self.payload)


@pytest.fixture(autouse=True)
def _token_env(monkeypatch):
    monkeypatch.setenv("HUIJIAN_HA_TOKEN", "test-token")


def _client(base, session):
    c = HAClient(session=session)
    c.base = base.rstrip("/")
    return c


@pytest.mark.parametrize("base,expect", [
    (SUP, SUPAPI + "/intent/handle"),                       # supervisor 代理形态
    ("http://192.168.1.5:8123",                             # 直连（E2E 旧配置）
     "http://192.168.1.5:8123/api/intent/handle"),
    ("http://192.168.1.5:8123/api",                         # 直连带 /api 不设防
     "http://192.168.1.5:8123/api/intent/handle"),
    (SUP + "/", SUPAPI + "/intent/handle"),                 # 尾斜杠由 rstrip 吃掉
])
def test_url_normalizes_to_exactly_one_api(base, expect):
    c = _client(base, _Session())
    assert c._url("/api/intent/handle") == expect
    assert "/api/api" not in expect
    # 双保险：_url 的输出永远不含重复段
    assert "/api/api" not in c._url("/api/intent/handle")


def test_default_base_env_shape(monkeypatch):
    """const 默认值必须保持 supervisor 定式（含 /api 的 API 根）。"""
    monkeypatch.delenv("HUIJIAN_HA_API", raising=False)
    assert hc_mod.const.HA_API_DEFAULT == SUP
    c = HAClient(session=_Session())          # __init__ 读 env → base=…/core/api
    assert c.base == SUP
    assert c._url("/api/states") == SUPAPI + "/states"


def test_every_call_site_single_api():
    """7 个调用点全量走一遍：捕获 URL 断言无一 /api/api 且路径正确。"""
    s = _Session(200, [])
    c = _client(SUP, s)

    async def _drive():
        await c.handle_intent("HassTurnOn", {"name": "x"})
        await c.refresh_states(force=True)   # 内部会带一次 _load_registries
        await c.get_config()
        await c.fire_event("huijian_turn", {"a": 1})
    asyncio.run(_drive())
    urls = [u for _, u in s.urls]
    assert urls == [
        SUPAPI + "/intent/handle",
        SUPAPI + "/states",
        SUPAPI + "/config/area_registry/list",
        SUPAPI + "/config/entity_registry/list",
        SUPAPI + "/config",
        SUPAPI + "/events/huijian_turn",
    ], "调用点 URL 集合漂移（新增端点须同步本清单）"
    assert all("/api/api" not in u for u in urls)


def test_legacy_fallback_url_also_clean():
    """主端点 404 → legacy 回落 POST /api/intent/<name> 同样不得双拼。"""
    s = _Session(404, {"message": "Not Found"})
    c = _client(SUP, s)
    asyncio.run(c.handle_intent("TurnDeviceOn", {}))
    assert s.urls[-1] == ("POST", SUPAPI + "/intent/TurnDeviceOn")
    assert all("/api/api" not in u for _, u in s.urls)


def test_states_404_does_not_light_reachable():
    """真机事故形态：路径错→404。健康灯必须红；401（URL 对鉴权错）算可达。"""
    c404 = _client(SUP + "/api/api", _Session(404, {"message": "Not Found"}))

    async def _t404():
        await c404.refresh_states(force=True)
    asyncio.run(_t404())
    assert c404.reachable is False, "404 点亮 reachable 即回退到本事故盲区"

    c401 = _client(SUP, _Session(401, {"message": "Unauthorized"}))

    async def _t401():
        await c401.refresh_states(force=True)
    asyncio.run(_t401())
    assert c401.reachable is True
