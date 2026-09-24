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
        # M12：构造即清扫上次运行（含 SIGKILL/断电）遗留的 .part.* 孤儿。
        self.sweep_orphans()

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

    def lock_entry(self, key: str) -> dict:
        """manifest 条目只读浅拷贝（v1.0.61 审查批 P3-a：TTS 指纹等需要
        **包身份**的调用方使用——换包不换 sid 时旧盘缓存永不轮换）。缺 key → {}。"""
        return dict(self._manifest.get(key) or {})

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
        # M11（2026-09-23 深审）：完成章此前写 tar_path.name 却全码零读取——
        # 就绪判定与 lock 当前包身份（tarball+sha256）脱钩，models.lock.json:6
        # 自写处置预案「官方重传同版本包→人工回填 sha256」在存量设备上永不
        # 生效（章不校、永不复下，fresh 与存量跑不同字节）。现在读章比对：
        # 不符=不就绪（触发重下/force），旧格式章（仅包名）宽容为按名比对。
        stamp = self._read_stamp(key)
        if stamp is not None and stamp != "dev-migration":
            exp_tarball = entry.get("tarball", "")
            exp_sha = entry.get("sha256", "")
            name_part, _, sha_part = stamp.partition("|")
            if name_part != exp_tarball or (sha_part and sha_part != exp_sha):
                logger.warning("[模型] %s 完成章与 lock 包身份不符（章=%r），判不就绪",
                               key, stamp)
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

    @staticmethod
    def _tarball_required(entry: dict) -> list:
        """主包自带应检文件 = required_files 剔除 extra_files 附属。

        附属（如 matcha 的 vocos 声码器）由二跳补下，**不参与**解包后即刻校验——
        否则解包闸因附属缺失恒 False、补下逻辑永不被调用（鸡生蛋，2026-09-24
        matcha「解包后校验文件缺失」永久 incomplete 实锤）。"""
        extra = {ef.get("file") for ef in (entry.get("extra_files") or [])
                 if ef.get("file")}
        return [rf for rf in entry.get("required_files", []) if rf not in extra]

    def _read_stamp(self, key: str) -> Optional[str]:
        try:
            raw = (self.models_dir / key / ".extracted_ok").read_text(
                encoding="utf-8").strip()
            return raw or None
        except OSError:
            return None

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
    def sweep_orphans(self) -> int:
        """M12（2026-09-23 深审）：启动无条件回收下载残留 `.part.*`。
        旧清理只在 except 分支，而本仓把 SIGKILL 写成收尾常态（shutdown
        wait_for 5s < 下载块 60s）——半截 GB 级文件在 /data 永久堆积，
        与 M11 复合最坏态直推盘满。返回删除数。"""
        removed = 0
        try:
            for stale in self.models_dir.glob("*.part.*"):
                try:
                    stale.unlink()
                    removed += 1
                except OSError as e:
                    logger.warning("[模型] 孤儿残留删除失败 %s: %s", stale, e)
        except OSError as e:
            logger.debug("[模型] 孤儿清扫异常（不阻断）: %s", e)
        if removed:
            logger.warning("[模型] 启动清掉 %d 个下载残留 .part.*", removed)
        return removed

    def _invalidate(self, key: str) -> None:
        """force 逃生门：旧解包树+完成章整删（不清则新解包与旧文件混栈，
        _files_ok 依旧放行=「彻底 no-op」根因之二）。import/ 是用户手动放
        包口，绝不触碰。"""
        import shutil
        target = self.models_dir / key
        try:
            if target.is_dir():
                shutil.rmtree(target)
        except OSError as e:
            logger.warning("[模型] %s 旧树删除失败: %s", key, e)

    def ensure(self, key: str, force: bool = False) -> bool:
        """幂等确保模型就绪。同步执行（调用方放线程）。F2：per-key 锁 single-flight。
        force=True（M11 接线）：面板「重新下载」——清旧树旧章后重走校验下载。"""
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
        auto = self.settings.get("power.auto_download", None)
        if auto is None:
            auto = True
        auto = auto and str(os.environ.get("HUIJIAN_OPT_MODEL_AUTO_DOWNLOAD", "true")).lower() != "false"
        if force:
            # M11：先删旧树再重取（旧树不清，新解包与旧文件混栈、_files_ok
            # 照过=「下载」按钮对就绪错版包彻底 no-op 的第二根因）。但清树
            # 只在**确有重取来源**时执行——force 不是摧毁现有就绪态的许可证。
            if imported.exists() or auto:
                self._invalidate(key)
            else:
                self._set_status(key, state="manual", pct=0,
                                 detail="自动下载关闭且无导入包，拒绝清空重取")
                return self.is_ready(key)
        if imported.exists():
            if self._extract(key, imported, entry):
                return self._ensure_extra_files(key, entry)
        # 2) 三级 URL 下载
        if not auto:
            self._set_status(key, state="manual", pct=0, detail="自动下载关闭；放包到 import/ 或开开关")
            return self.is_ready(key)
        dest_tar = self.models_dir / entry["tarball"]
        dest_tar.parent.mkdir(parents=True, exist_ok=True)
        ok = self._download_any(entry, dest_tar)
        if ok and self._extract(key, dest_tar, entry):
            return self._ensure_extra_files(key, entry)
        return self.is_ready(key)

    # ── extra_files（v1.1.5：主 tarball 之外的附属单文件）──────────
    # 动机：matcha-icefall-zh-en 官方包**不含声码器**（README 明示另取
    # vocos-16khz-univ.onnx），而缺 vocoder 时 sherpa C++ 构造直接终止进程
    # （台架实锤，非可捕获异常）——附属文件必须与主包同级、且就绪判定硬闸。
    # 纪律与主包一致：sha256 校验、tmp+replace 原子落盘、多源回退、
    # import/ 同名文件优先（手动逃生门）。落盘目录=解包实际目录（top_dir 探测）。
    def _resolved_dir(self, key: str, entry: dict) -> Path:
        top = entry.get("top_dir", "")
        cand = self.models_dir / key / top
        if top and cand.is_dir():
            return cand
        return self.models_dir / key

    def _ensure_extra_files(self, key: str, entry: dict) -> bool:
        extras = entry.get("extra_files") or []
        if not extras:
            return True
        base = self._resolved_dir(key, entry)
        base.mkdir(parents=True, exist_ok=True)
        for ef in extras:
            name = ef.get("file") or ""
            if not name:
                continue
            dest = base / name
            imp = self.import_dir / name
            if dest.exists() and imp.exists():
                imp.unlink(missing_ok=True)   # 导入口残留让位已就位文件（防反复重下）
            if dest.exists():
                if not ef.get("sha256") or self._sha_ok(dest, ef["sha256"]):
                    continue
                dest.unlink(missing_ok=True)  # 坏字节：重取（导入口→urls 同序）
            if imp.exists():
                try:
                    tmp = dest.with_suffix(dest.suffix + ".tmp")
                    tmp.write_bytes(imp.read_bytes())
                    os.replace(tmp, dest)
                    if not ef.get("sha256") or self._sha_ok(dest, ef["sha256"]):
                        continue
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
            pseudo = {"_key": key, "tarball": name, "sha256": ef.get("sha256", ""),
                      "urls": ef.get("urls", []), "size_mb": ef.get("size_mb", 0)}
            if not self._download_any(pseudo, dest):
                self._set_status(key, state="incomplete",
                                 detail=f"附属文件 {name} 获取失败（可放 import/{name}）")
                return False
        if self.is_ready(key):
            self._set_status(key, state="ready", pct=100, detail="已就绪")
        return self.is_ready(key)

    @staticmethod
    def _sha_ok(path: Path, expected: str) -> bool:
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for blk in iter(lambda: f.read(1 << 20), b""):
                    h.update(blk)
            return h.hexdigest() == expected
        except OSError:
            return False

    def ensure_async(self, key: str, force: bool = False) -> None:
        if (t := self._threads.get(key)) and t.is_alive():
            return
        t = threading.Thread(target=self.ensure, args=(key, force),
                             name=f"model-{key}", daemon=True)
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
            # 全部成员解包成功后才盖章（见 model_dir_for 的原子性注释）。
            # M11：章内容升级为包身份 `tarball|sha256`（lock 当时期望值）——
            # lock 回填新 sha 后章比对失配 → 自动判不就绪重验，处置预案生效。
            with open(target / ".extracted_ok", "w", encoding="utf-8") as mf:
                mf.write(f"{entry.get('tarball', tar_path.name)}|"
                         f"{entry.get('sha256', '')}")
            # 鸡生蛋根修（2026-09-24 matcha 实锤）：此处只判**主包自带**文件
            # （required_files 剔除 extra_files 附属），齐即返回 True 交
            # _ensure_extra_files 补附属（vocos 声码器等）。此前直接 is_ready 全量
            # 判（含附属）⇒ 附属未下时恒 False ⇒ 补下永不被调用 ⇒ 永久 incomplete。
            base = self._resolved_dir(key, entry)
            if not self._files_ok(base, self._tarball_required(entry)):
                self._set_status(key, state="incomplete", detail="解包后校验文件缺失")
                return False
            if self.is_ready(key):
                self._set_status(key, state="ready", pct=100, detail="已就绪")
                logger.info("[模型] %s 就绪 @ %s", key, target)
                # M12（2026-09-23 深审）：解包成功盖章后即删下载归档——
                # SenseVoice+kokoro ≈1.4GB 死重再无删除点（本仓 _write_status
                # S17-2「必关必删」同纪律）。import/ 目录是用户手动导入口
                # （lock:5「放包即用」），**不动**；校验失败路径也不动（留待重试）。
                # 附属未就绪时暂留归档：补下失败重试不必重拉主包。
                if tar_path.parent == self.models_dir:
                    try:
                        tar_path.unlink(missing_ok=True)
                    except OSError as e:
                        logger.warning("[模型] 归档删除失败（不阻断）: %s", e)
            return True
        except Exception as e:
            logger.error("[模型] 解包 %s 失败: %s", key, e)
            self._set_status(key, state="failed", detail=str(e))
            return False


