# -*- coding: utf-8 -*-
"""v1.0.98 播报语音路由钉：text.play_voice_text → assist_satellite.announce 推流。

VM 真机联测（2026-09-19）定罪旧 URL 路三重山：/local 首建 404（重启才自愈，
anon/auth HEAD 均 404 len=14 逐字复现）、edge-tts 云依赖、API 音频设备
url_play 内部 RAM spawn 失败（固件 2.1.57 侧根修）。本批把主通道改 core
announce→_do_announce 自合成推流（对话应答同音色同链路），旧路仅兜底。

钉三层：
① _find_satellite 真函数行为（AST 抽源码 exec，仓内先例）：命中同设备
   assist_satellite 实体；disabled/异设备/无 device_entry 三种都必须 None；
② 路由顺序：play_voice_text 分支里 announce 派发先于 _play_tts 回退，且
   回退仍完整保留（不倒退）；非播报 text 命令路径一字不动；
③ 反回退计数：text.py 里 assist_satellite 派发串恰 1 处。
"""
import ast
import os
import sys
import tempfile
import textwrap
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HUIJIAN_DATA", tempfile.mkdtemp(prefix="hv_v1098_"))
os.environ.setdefault("HUIJIAN_NLU_DATA", str(HERE / "nlu_data"))

import test_window_speed_behavior as bench  # noqa: E402  替身装载

CC_DIR = HERE / "custom_components" / "huijian_ai"
SRC = (CC_DIR / "text.py").read_text(encoding="utf-8")


def _extract_func_src(name: str) -> str:
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return textwrap.dedent(ast.get_source_segment(SRC, node))
    raise AssertionError(f"text.py 找不到 {name}")


@pytest.fixture(autouse=True)
def _stubs():
    bench._install_ha_stubs()
    yield


def _mk_self(entries, device_id="devA", has_device=True):
    ns = types.SimpleNamespace()
    ns.hass = types.SimpleNamespace(_er=bench._ER(entries))
    ns.device_entry = None if not has_device else types.SimpleNamespace(id=device_id)
    return ns


def _entry(eid, domain, dev, disabled_by=None):
    e = types.SimpleNamespace(entity_id=eid, domain=domain, device_id=dev,
                              disabled_by=disabled_by)
    return e


def _load_find_satellite():
    ns = {}
    exec(compile(_extract_func_src("_find_satellite"), "<text.extract>", "exec"), ns)  # noqa: S102
    return ns["_find_satellite"]


def test_finds_satellite_on_same_device():
    fn = _load_find_satellite()
    self = _mk_self([_entry("assist_satellite.foo", "assist_satellite", "devA"),
                     _entry("media_player.foo", "media_player", "devA")])
    assert fn(self) == "assist_satellite.foo"


def test_disabled_or_absent_returns_none():
    fn = _load_find_satellite()
    self = _mk_self([_entry("assist_satellite.foo", "assist_satellite", "devA",
                            disabled_by="user")])
    assert fn(self) is None
    self2 = _mk_self([_entry("assist_satellite.other", "assist_satellite", "devB")])
    assert fn(self2) is None          # 异设备绝不串
    assert fn(_mk_self([], has_device=False)) is None


def test_dispatch_order_and_fallback_kept():
    tree = ast.parse(SRC)
    setv = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_set_value":
            setv = node
    assert setv is not None
    body = ast.get_source_segment(SRC, setv)
    i_ann = body.find('"assist_satellite"')
    i_find = body.find("self._find_satellite()")
    i_fb = body.find("self._play_tts(value)")
    assert 0 < i_find < i_ann < i_fb, \
        "路由次序被改：必须先查卫星→announce 派发→失败才回退 _play_tts"
    assert "except Exception" in body[i_find:i_fb], "回退分支失去兜底=老环境直接坏"
    # 派发单发：async_set_value 体内 service 域串恰 1 处；全文件=派发 1+卫星
    # 查表域比对 1，再多即双发播报/串道风险。
    assert body.count('"assist_satellite"') == 1
    assert SRC.count('"assist_satellite"') == 2


def test_plain_text_command_path_untouched():
    tree = ast.parse(SRC)
    setv = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "async_set_value")
    body = ast.get_source_segment(SRC, setv)
    assert 'static_info.object_id != "play_voice_text"' in body
    i_guard = body.find("play_voice_text")
    i_cmd = body.find("self._client.text_command")
    assert 0 < i_guard < i_cmd < body.find("self._find_satellite()"), \
        "非播报文本实体必须先 early-return，不得被路由改道"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
