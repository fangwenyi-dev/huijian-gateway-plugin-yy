# -*- coding: utf-8 -*-
"""v1.1.9 钉桩：matcha 鸡生蛋根修——解包闸只判主包自带文件，附属(extra_files)交二跳补下。

2026-09-24 实锤：tts_matcha_zh_en 永久「不完整/解包后校验文件缺失」。根因 _extract
解包后用 is_ready 全量判 required_files（含 vocos 声码器），而 vocos 只由
_ensure_extra_files 二跳补下、且仅在 _extract 返回 True 后才被调用 ⇒ 附属未下时
_extract 恒 False ⇒ 补下永不被调用 ⇒ 死锁 incomplete。修：解包闸改判
required_files 剔除 extra_files 后的主包自带文件。

本测走 import/ 手动投放口（零网络）：主包 tar.bz2 只含主包自带文件 a.onnx，附属
v.onnx 放 import/。修复前 ensure 恒 False（解包闸卡附属）；修复后 ensure True 且
is_ready（附属从 import 补齐）。
"""
import json
import tarfile
import io
from pathlib import Path

from core.model_store import ModelStore


class _S:
    def get(self, k, d=None):
        return {"power.auto_download": True}.get(k, d)


def _mk_store(tmp_path: Path) -> ModelStore:
    models = tmp_path / "models"
    (models / "import").mkdir(parents=True)
    lock = tmp_path / "firmware_models.lock.json"
    lock.write_text(json.dumps({
        "t": {
            "tarball": "t.tar.bz2",
            "top_dir": "ttop",
            "required_files": ["a.onnx", "v.onnx"],
            "extra_files": [{"file": "v.onnx", "urls": []}],
            "sha256": "",
            "size_mb": 0,
        }
    }), encoding="utf-8")
    return ModelStore(settings=_S(), lock_path=lock, models_dir=models,
                      status_file=tmp_path / "status.json")


def _mk_tarbz2(path: Path, members: dict):
    with tarfile.open(path, "w:bz2") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            blob = data.encode()
            info.size = len(blob)
            tf.addfile(info, io.BytesIO(blob))


def test_extract_gate_passes_with_extra_pending_then_extras_fill(tmp_path):
    store = _mk_store(tmp_path)
    # 主包只含主包自带文件 a.onnx；附属 v.onnx 走 import 二跳口
    _mk_tarbz2(store.import_dir / "t.tar.bz2", {"ttop/a.onnx": "main"})
    (store.import_dir / "v.onnx").write_text("vocoder", encoding="utf-8")

    assert store.ensure("t") is True, "解包闸不应因附属未下而卡死（鸡生蛋回归）"
    assert store.is_ready("t") is True
    resolved = store.models_dir / "t" / "ttop"
    assert (resolved / "a.onnx").exists()
    assert (resolved / "v.onnx").exists(), "附属应被二跳补齐到解包目录"


def test_tarball_required_excludes_extra_files(tmp_path):
    store = _mk_store(tmp_path)
    entry = store.lock_entry("t")
    assert store._tarball_required(entry) == ["a.onnx"], "附属不得进解包即刻校验"


def test_extract_still_incomplete_when_main_file_missing(tmp_path):
    """主包自带文件真缺（坏包）仍须判 incomplete，不得被根修放宽。"""
    store = _mk_store(tmp_path)
    _mk_tarbz2(store.import_dir / "t.tar.bz2", {"ttop/WRONG.onnx": "x"})
    assert store.ensure("t") is False
    assert store.is_ready("t") is False
