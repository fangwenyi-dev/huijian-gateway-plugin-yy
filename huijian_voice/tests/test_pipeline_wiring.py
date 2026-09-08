"""v1.0.17 管道自动接线防回退钉桩（源码级）。

行为级实证在真实栈完成（in-process HA 2026.8.3 + 真 assist_pipeline
组件 + 真 config flow，见发布记录）；本文件只钉"接线代码不得被改回
只建引擎不建管道"的半截状态。
"""
from pathlib import Path

CC = Path(__file__).resolve().parents[1] / "custom_components" / "huijian_ai"


def _src(name: str) -> str:
    return (CC / name).read_text(encoding="utf-8")


def test_pipeline_wiring_present_and_called():
    init = _src("__init__.py")
    assert "HUIJIAN_PIPELINE_NAME" in init
    assert "async_create_default_pipeline" in init
    assert "async_set_preferred_item" in init
    # assist 分支必须在平台 forward 之后调用接线（顺序错误=引擎未注册先接线）
    seg = init.split('config_type == "assist"', 1)[1]
    seg = seg.split("return True", 1)[0]
    assert seg.index("async_forward_entry_setups") < seg.index(
        "_async_ensure_huijian_pipeline"
    )


def test_pipeline_takeover_guarded():
    """默认管道接管只救"无 STT 空壳"，绝不允许无条件抢占用户默认。"""
    init = _src("__init__.py")
    body = init.split("def _async_ensure_huijian_pipeline", 1)[1]
    assert "not cur.stt_engine" in body, "preferred 接管必须以当前默认缺 STT 为条件"
    assert "except Exception" in body, "接线必须 fail-open"
