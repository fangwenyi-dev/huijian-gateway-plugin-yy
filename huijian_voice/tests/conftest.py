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
    def __init__(self, results=None, states=None, areas=None, entity_area=None):
        self.calls = []
        self.results = results or {}
        self._states = states or {}
        self.ok = True
        self.reachable = True
        self._areas = areas or {}
        self._entity_area = entity_area or {}

    async def handle_intent(self, name, data, timeout=10.0):
        self.calls.append((name, data))
        return self.results.get(name, {"success": True})

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
