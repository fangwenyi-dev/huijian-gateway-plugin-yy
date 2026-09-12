"""pytest 基建：把持久卷指向临时目录，NLU 资产指向仓内 vendored nlu_data/。
跑法（本机实证环境）：cd huijian_voice && python -m pytest tests -q
"""
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ["HUIJIAN_DATA"] = tempfile.mkdtemp(prefix="huijian_test_")
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

import pytest  # noqa: E402


@pytest.fixture()
def settings():
    from core.settings import Settings
    return Settings(Path(os.environ["HUIJIAN_DATA"]) / f"settings-{os.getpid()}.json")


_REAL_INTENTS_CACHE = None


def _real_executable_intents():
    """真实可执行意图全集（集成 intent_type ∪ HA core 内置；懒载一次）。

    v1.0.52（A-F6）：FakeHAClient 默认裁决依据——替身不得比真机更宽容
    （v1.0.20 HassUnlock 恒成功事故教训，派生自 test_intent_contract 同一张表）。
    """
    global _REAL_INTENTS_CACHE
    if _REAL_INTENTS_CACHE is None:
        from test_intent_contract import real_executable_intents
        _REAL_INTENTS_CACHE = real_executable_intents()
    return _REAL_INTENTS_CACHE


class FakeStore:
    """离线仓内模型目录袋（E2E 机上有解包资产时用真目录，CI 上返回 None 走跳过分支）。"""
    def __init__(self, dirs=None):
        self.dirs = dirs or {}
    def model_dir_for(self, key):
        p = self.dirs.get(key)
        return Path(p) if p and Path(p).exists() else None
    def is_ready(self, key):
        return self.model_dir_for(key) is not None
    def ensure(self, key):
        return self.is_ready(key)
    def keys(self):
        return list(self.dirs)


class FakeHAClient:
    def __init__(self, results=None, states=None, areas=None, entity_area=None,
                 rest=None, writes=None):
        self.calls = []
        self.results = results or {}
        self._states = states or {}
        self.ok = True
        self.reachable = True
        self._areas = areas or {}
        self._entity_area = entity_area or {}
        self._rest = rest or {}
        self._writes = writes or {}       # {(method, path): 返回 dict}
        self.written = []                 # [(method, path, body)] 留痕

    async def rest_get(self, path, timeout=6.0):
        return self._rest.get(path)   # 无条目=None（真实语义：一切失败折叠 None）

    async def rest_write(self, method, path, body=None, timeout=8.0):
        self.written.append((method, path, body))
        key = (method, path)
        if key in self._writes:
            v = self._writes[key]
            return dict(v) if isinstance(v, dict) else v
        return {"success": True}

    async def handle_intent(self, name, data, timeout=10.0):
        self.calls.append((name, data))
        if name in self.results:
            return self.results[name]
        # v1.0.52（A-F6）：替身不得比真机更宽容——未知意图恒成功正是
        # v1.0.20 HassUnlock 事故的放行通道。默认按**真实注册表派生**裁决
        # （集成 intent_type ∪ HA core 内置）；测试确需自定义意图名时显式
        # 传 results= 注入，属有意行为。
        if name not in _real_executable_intents():
            return {"success": False,
                    "error": (f"intent {name!r} 未在集成注册且非 HA core 内置"
                              "（FakeHAClient 按真实注册表裁决，见 "
                              "tests/test_intent_contract.py）")}
        return {"success": True}

    async def states(self):
        return dict(self._states)

    async def area_names(self):
        return sorted(set(self._areas.values()))

    async def get_config(self):
        return {"time_zone": "Asia/Shanghai"}

    async def find_entities(self, area="", domains=(), name_contains=""):
        out = []
        for eid, ent in self._states.items():
            dom = eid.split(".", 1)[0]
            if domains and dom not in domains:
                continue
            if area and self._entity_area.get(eid) != area:
                continue
            out.append(ent)
        return out

    async def fire_event(self, t, d):
        pass
