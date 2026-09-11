"""模型仓库：models.lock.json 驱动的首启下载 / 校验 / 手动导入 / 状态外泄。

纪律来源（网关工程模板 §偏离点4 + v3 定案不变）：
- 大模型不进镜像；解包落 /data/models/（Supervisor 持久卷，升级不丢、卸载即清）；
- 下载三级 URL 回退（gh-proxy 国内→官方→hf-mirror），sha256 校验；
- tmp + os.replace 原子落盘；进度写事实文件 models_status.json（nginx try_files 暴露）；
- model_auto_download=false 或全失败 → 手动导入口 /data/models/import/ 放 tarball 即用。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import const

logger = logging.getLogger("huijian.models")

# lock 文件带空格的键名笔误兜底：真实 required_files 以 core 侧硬校验为准
_STATUS_LOCK = threading.Lock()


class ModelStore:
    def __init__(self, settings, lock_path: Path = const.MODELS_LOCK_FILE,
                 models_dir: Path = const.MODELS_DIR, status_file: Path = const.MODELS_STATUS_FILE):
        self.settings = settings
        self.lock_path = Path(lock_path)
        self.models_dir = Path(models_dir)
        self.import_dir = self.models_dir / "import"
        self.status_file = Path(status_file)
        self._manifest = self._load_manifest()
        self._status: dict[str, dict] = {k: {"state": "pending", "pct": 0, "detail": ""} for k in self._manifest}
        self._threads: dict[str, threading.Thread] = {}
        # F2 single-flight：同 key 的 ensure 全局只许一个执行体（下载线程与
        # 识别路径惰性加载线程互斥）；F4：abort 位让停机即时打断下载循环。
        self._key_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.abort = threading.Event()

    def _key_lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._key_locks.setdefault(key, threading.Lock())

    # ── 清单 ────────────────────────────────────────────────────
    def _load_manifest(self) -> dict:
        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
            out = {k: v for k, v in data.items() if isinstance(v, dict) and "tarball" in v}
            for k, v in out.items():
                v.setdefault("_key", k)   # 状态回写用的稳定键（不依赖调用方传参）
            return out
        except Exception as e:
            logger.error("[模型] models.lock.json 读取失败: %s", e)
            return {}

    def keys(self):
        return list(self._manifest.keys())

    def voices_count_for(self, key: str) -> int:
        """TTS 包官方音色数（lock 的 voices_count 字段；缺省 0=不启用自定义注入）。
        单音尺寸由它推导：官方 voices.bin 字节数 ÷ 音色数（纯张量拼接、无 magic，
        v1_1 fp32 本机实测 53,790,720÷103=522,240B 整除）。"""
        try:
            return int((self._manifest.get(key) or {}).get("voices_count") or 0)
        except (TypeError, ValueError):
            return 0

    def model_dir_for(self, key: str) -> Optional[Path]:
        """返回已就绪模型的解包目录（含 top_dir 探测）。未就绪返回 None。"""
        entry = self._manifest.get(key)
        if not entry:
            return None
        # 完成标记=就绪唯一凭证：extractall 逐件落盘，大文件半写时 exists() 会
        # 误报就绪（v1.0.0 CI e2e 实锤：STT 读到半写 decoder.int8.onnx →
        # "Protobuf parsing failed"，run 间 flaky）。标记在完整解包后才写 →
        # 就绪判定升级为内容级原子。
        if not (self.models_dir / key / ".extracted_ok").is_file():
            return None
        top = entry.get("top_dir", "")
        try:
            cand = self.models_dir / key / top if top else self.models_dir / key
            if cand.is_dir() and self._files_ok(cand, entry.get("required_files", [])):
                return cand
            # 兼容直接解压在 models_dir/<key>/ 无 top_dir 层
            cand2 = self.models_dir / key
            if cand2.is_dir() and self._files_ok(cand2, entry.get("required_files", [])):
                return cand2
        except OSError as e:
            # 文件系统级异常（坏符号链接/reparse/权限）绝不上传——否则 is_ready
            # 会把 /api/health 打成 500、状态循环每 5s 抛栈（Windows 实证 WinError 1920）。
            logger.debug("[模型] %s 目录探测异常: %s", key, e)
        return None

    @staticmethod
    def _files_ok(base: Path, required: list) -> bool:
        for rf in required:
            if not (base / rf).exists():
                return False
        return True

    def is_ready(self, key: str) -> bool:
        return self.model_dir_for(key) is not None

    # ── 状态外泄 ────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with _STATUS_LOCK:
            out = {k: dict(v) for k, v in self._status.items()}
        for k in out:
            out[k]["ready"] = self.is_ready(k)
        return out

    def _set_status(self, key: str, **fields):
        with _STATUS_LOCK:
            self._status.setdefault(key, {})
            self._status[key].update(fields)
            snap = {k: dict(v) for k, v in self._status.items()}
        self._write_status(snap)

    def _write_status(self, snap: dict):
        # v1.0.41 审查 S17-2：旧实现中途异常会留孤儿——fchmod/fdopen 抛错时 fd 泄漏
        # （慢性耗尽 select fd 表），写/换名失败时 .mst-*.tmp 永久堆积在数据盘。
        # 统一 finally 兜底：fd 未移交必关、tmp 未转正必删。
        fd, tmp = None, None
        try:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            # F6：tmp 名必须唯一——与主循环 _atomic_write 共用固定 .tmp 名时，
            # 半写文件可被对方 rename 转正（双写者竞态）。
            fd, tmp = tempfile.mkstemp(dir=str(self.status_file.parent),
                                       prefix=".mst-", suffix=".tmp")
            # mkstemp 默认 0600：rename 转正后 nginx worker 读走 13（v1.0.0 实机
            # 实发：开下载起 models_status.json 被本写者反复刷回 600，与主循环
            # _atomic_write 的 fchmod 同源教训——事实文件必须世界可读）。
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = None          # 所有权移交 fdopen，finally 不再关
                f.write(json.dumps({"updated": time.time(), "models": snap}, ensure_ascii=False, indent=2))
            os.replace(tmp, self.status_file)
            tmp = None             # 已转正，不再是孤儿
        except Exception as e:  # 状态文件写失败不该阻断下载
            logger.debug("[模型] 状态文件写入失败: %s", e)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ── 下载/解包 ───────────────────────────────────────────────
    def ensure(self, key: str, force: bool = False) -> bool:
        """幂等确保模型就绪。同步执行（调用方放线程）。F2：per-key 锁 single-flight。"""
        entry = self._manifest.get(key)
        if not entry:
            return False
        with self._key_lock(key):
            return self._ensure_locked(key, entry, force)

    def _ensure_locked(self, key: str, entry: dict, force: bool) -> bool:
        if not force and self.is_ready(key):
            self._set_status(key, state="ready", pct=100, detail="已就绪")
            return True
        self._set_status(key, state="checking", pct=0, detail="检查导入/下载")
        # 1) 导入口
        imported = self.import_dir / entry["tarball"]
        if imported.exists():
            if self._extract(key, imported, entry):
                return True
        # 2) 三级 URL 下载
        auto = self.settings.get("power.auto_download", None)
        if auto is None:
            auto = True
        auto = auto and str(os.environ.get("HUIJIAN_OPT_MODEL_AUTO_DOWNLOAD", "true")).lower() != "false"
        if not auto:
            self._set_status(key, state="manual", pct=0, detail="自动下载关闭；放包到 import/ 或开开关")
            return self.is_ready(key)
        dest_tar = self.models_dir / entry["tarball"]
        dest_tar.parent.mkdir(parents=True, exist_ok=True)
        ok = self._download_any(entry, dest_tar)
        if ok and self._extract(key, dest_tar, entry):
            return True
        return self.is_ready(key)

    def ensure_async(self, key: str) -> None:
        if (t := self._threads.get(key)) and t.is_alive():
            return
        t = threading.Thread(target=self.ensure, args=(key,), name=f"model-{key}", daemon=True)
        self._threads[key] = t
        t.start()

    def ensure_all_async(self) -> None:
        for k in self.keys():
            if not self.is_ready(k):
                self.ensure_async(k)

    def _download_any(self, entry, dest: Path) -> bool:
        import urllib.request
        expected = entry.get("sha256", "")
        for url in entry.get("urls", []):
            if not url:
                continue
            try:
                size = entry.get("size_mb", 0) * 1e6
                self._set_status(entry["_key"], state="downloading", pct=0, detail=f"源 {url.split('/')[2]}")
                logger.info("[模型] 下载 %s ← %s", entry["tarball"], url)
                if self.abort.is_set():
                    return False
                part = str(dest) + ".part." + uuid.uuid4().hex[:8]   # F2：并发唯一 .part
                req = urllib.request.Request(url, headers={"User-Agent": "huijian-voice/1.0"})
                with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
                    got = 0
                    last_pct = -1
                    while True:
                        if self.abort.is_set():
                            raise TimeoutError("停机/中止：放弃当前下载")
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if size:
                            pct = int(got / size * 100)
                            if pct != last_pct and pct % 5 == 0:
                                last_pct = pct
                                self._set_status(entry["_key"], state="downloading", pct=pct, detail=f"{got//1024//1024}MB / {int(size//1e6)}MB")
                if expected:
                    h = hashlib.sha256()
                    with open(part, "rb") as f:
                        for blk in iter(lambda: f.read(1 << 20), b""):
                            h.update(blk)
                    if h.hexdigest() != expected:
                        logger.warning("[模型] %s sha256 不符（源 %s），换源", entry["tarball"], url)
                        Path(part).unlink(missing_ok=True)
                        continue
                os.replace(part, dest)
                return True
            except Exception as e:
                logger.warning("[模型] 源 %s 失败: %s", url, e)
                for stale in dest.parent.glob(dest.name + ".part.*"):
                    stale.unlink(missing_ok=True)
                if self.abort.is_set():
                    return False
                continue
        self._set_status(entry["_key"], state="failed", detail="全部源失败；可手动导入")
        return False

    def _extract(self, key: str, tar_path: Path, entry) -> bool:
        target = self.models_dir / key
        try:
            target.mkdir(parents=True, exist_ok=True)
            self._set_status(key, state="extracting", pct=100, detail="解包中")
            with tarfile.open(tar_path, "r:*") as tf:
                # F2 收口：越界路径之外，symlink/hardlink/设备成员一并拒绝
                # （link 指向目录外 + 同名后续成员可穿透）；py≥3.12 再叠
                # filter="data" 双保险（bookworm 3.11.2 无此参，TypeError 回落手筛）。
                def _safe(m):
                    if m.name.startswith(("/", "..")) or "/../" in m.name:
                        return False
                    return not (m.issym() or m.islnk() or m.isdev())
                members = [m for m in tf.getmembers() if _safe(m)]
                try:
                    tf.extractall(target, members=members, filter="data")
                except TypeError:
                    tf.extractall(target, members=members)
            # 全部成员解包成功后才盖章（见 model_dir_for 的原子性注释）
            with open(target / ".extracted_ok", "w", encoding="utf-8") as mf:
                mf.write(tar_path.name)
            if self.is_ready(key):
                self._set_status(key, state="ready", pct=100, detail="已就绪")
                logger.info("[模型] %s 就绪 @ %s", key, target)
                return True
            self._set_status(key, state="incomplete", detail="解包后校验文件缺失")
            return False
        except Exception as e:
            logger.error("[模型] 解包 %s 失败: %s", key, e)
            self._set_status(key, state="failed", detail=str(e))
            return False


