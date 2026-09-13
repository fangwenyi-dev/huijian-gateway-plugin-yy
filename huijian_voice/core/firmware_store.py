"""ESP32 固件仓（OTA 方案 Phase 2 加载项侧，2026-09-23）。

职责（对齐《esp32固件OTA升级方案》§3/§4-P2，本批为「近场链接兜底」形态）：
  1. 版本账：随镜像 firmware.lock.json（/data/firmware/firmware.lock.json 可覆盖）
     + 投递口 /data/firmware/import/*.bin（文件名须含 x.y.z 版本，收编即算
     sha256 入索引）双源；`versions()` 合并去重（import 覆盖同版 lock）。
  2. 领取闸：issue(version, mac) 签一次性令牌（10min TTL），GET :8000
     /firmware/<file>?t=<tok> 消费即废——设备/手机领取，杜绝 LAN 匿名枚举
     与链接外泄复用。令牌只存内存（重启作废可接受：现场重发一次点击）。
  3. 下载：lock 条目 urls（GitHub→Gitee 容灾序）由运维在面板显式点触
     （POST /api/firmware/download），落盘前 sha256 必核——加载项是第一道
     供应链闸（方案 §3），**HA 重启/后台循环绝不自动拉公网**（运行期零
     GitHub 依赖定案不破：自动链只碰本地盘与 LAN）。

纪律沿用：写文件 tmp→os.replace 原子；sha256 不符绝不入 public；日志沿用
v1.0.48 口径（URL 不含令牌值）。firmware.bin 物理可达性=同 LAN（host_network
:8000），与 CMD20 入驻 POST 同一信任域；HTTPS/包签名属固件仓 Phase 1 收口，
本批不假装有。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.request
from pathlib import Path

from . import const

logger = logging.getLogger("huijian.firmware")

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_ISSUE_TTL_S = 600
_DL_TIMEOUT_S = 300


def vkey(v: str) -> tuple:
    """版本比较键（非 x.y.z 一律最小，比较永不炸）。"""
    m = _VERSION_RE.search(v or "")
    if not m:
        return (0, 0, 0)
    return tuple(int(x) for x in m.group(1).split("."))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


class FirmwareStore:
    def __init__(self, root: Path | None = None, lock_path: Path | None = None):
        self.root = Path(root if root is not None else const.FIRMWARE_DIR)
        self.public = self.root / "public"
        self.import_dir = self.root / "import"
        self.index_file = self.root / "index.json"
        self.lock_path = Path(lock_path if lock_path is not None else const.FIRMWARE_LOCK_FILE)
        self.override_lock = self.root / "firmware.lock.json"   # 持久卷热修位
        self._lock = threading.Lock()
        self._tokens: dict[str, dict] = {}
        for d in (self.public, self.import_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ── lock ────────────────────────────────────────────────────────
    def _lock_releases(self) -> list[dict]:
        for p in (self.override_lock, self.lock_path):
            try:
                if p.is_file():
                    data = json.loads(p.read_text(encoding="utf-8"))
                    rel = data.get("releases")
                    if isinstance(rel, list):
                        return [r for r in rel if isinstance(r, dict)]
            except (OSError, ValueError) as e:
                logger.warning("[固件] lock %s 读取失败：%s", p, e)
        return []

    # ── 索引 / 导入 ─────────────────────────────────────────────────
    def _index(self) -> dict:
        try:
            if self.index_file.is_file():
                d = json.loads(self.index_file.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
        except (OSError, ValueError) as e:
            logger.warning("[固件] index.json 损坏，重建：%s", e)
        return {}

    def _save_index(self, idx: dict) -> None:
        tmp = self.index_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.index_file)

    def scan_import(self) -> int:
        """收编投递口新包：解析版本→算哈希→原子移入 public→更新索引。
        返回新收编数。文件名解析不出版本 = 拒收并留 WARN（不静默吞）。"""
        n = 0
        with self._lock:
            idx = self._index()
            for f in sorted(self.import_dir.glob("*.bin")):
                m = _VERSION_RE.search(f.name)
                if not m:
                    logger.warning("[固件] 投递包 %s 文件名不含 x.y.z 版本，拒收", f.name)
                    continue
                ver = m.group(1)
                try:
                    sha = _sha256(f)
                except OSError as e:
                    logger.warning("[固件] %s 读取失败：%s", f.name, e)
                    continue
                dest = self.public / f.name
                os.replace(f, dest)
                idx[f.name] = {"version": ver, "sha256": sha, "size": dest.stat().st_size,
                               "source": "imported", "at": time.time()}
                n += 1
                logger.info("[固件] 收编 %s（v%s, %s…）", f.name, ver, sha[:12])
            if n:
                self._save_index(idx)
        return n

    # ── 版本账 ──────────────────────────────────────────────────────
    def versions(self) -> list[dict]:
        """合并视图（降序）：public 实盘（imported/已下载）+ lock 登记（on_disk
        标记是否已就位，未下载条目给出 urls 数与 sha 供下载校验）。"""
        self.scan_import()
        idx = self._index()
        # 统一行结构：version/file/sha256/size/source/on_disk/notes_zh/urls
        rows: dict[str, dict] = {}   # version → 行（imported 覆盖 lock）
        for rel in self._lock_releases():
            v = str(rel.get("version", ""))
            if not v:
                continue
            fn = str(rel.get("file", ""))
            e = idx.get(fn)
            row = {
                "version": v, "file": fn,
                "sha256": str(rel.get("sha256", "")),
                "size": int(rel.get("size", 0) or 0),
                "source": "lock",
                "on_disk": bool(e and (self.public / fn).is_file()),
                "urls": [u for u in (rel.get("urls") or []) if isinstance(u, str)],
                "notes_zh": str(rel.get("notes_zh", "")),
            }
            if e:   # 实盘哈希与 lock 声明不符 → 面板标红，绝不默认信任
                row["sha_mismatch"] = bool(row["sha256"] and row["sha256"] != e.get("sha256"))
            rows[v] = row
        for fn, e in idx.items():
            v = e.get("version", "")
            if e.get("source") == "imported" or v not in rows:
                rows[v] = {
                    "version": v, "file": fn, "sha256": e.get("sha256", ""),
                    "size": int(e.get("size", 0) or 0), "source": e.get("source", "imported"),
                    "on_disk": (self.public / fn).is_file(), "urls": [],
                    "notes_zh": "",
                }
        return sorted(rows.values(), key=lambda r: vkey(r["version"]), reverse=True)

    def latest(self) -> dict | None:
        for r in self.versions():
            if r.get("on_disk") and not r.get("sha_mismatch"):
                return r
        return None

    # ── 下载（运维点触，sha256 必核）─────────────────────────────────
    def download(self, version: str) -> tuple[bool, str]:
        """按 lock 条目拉包：urls 依次试，**必须**带 sha256 才允许下载
        （无校验值=不入库，方案 §4-P2 供应链闸）。同步执行，调用方放线程。"""
        rel = next((r for r in self._lock_releases()
                    if str(r.get("version", "")) == version), None)
        if not rel:
            return False, f"lock 无 v{version} 登记"
        sha = str(rel.get("sha256", "") or "")
        fn = str(rel.get("file", "") or "")
        urls = [u for u in (rel.get("urls") or []) if isinstance(u, str)]
        if not fn or not urls:
            return False, "lock 条目缺 file/urls"
        if len(sha) != 64:
            return False, "lock 条目缺 sha256（无校验值拒绝下载）"
        dest = self.public / fn
        for url in urls:
            tmp = dest.with_name(dest.name + f".part.{secrets.token_hex(4)}")
            try:
                logger.info("[固件] 下载 v%s ← %s", version, url.split("?")[0])
                req = urllib.request.Request(url, headers={"User-Agent": "huijian-voice/1.0"})
                with urllib.request.urlopen(req, timeout=_DL_TIMEOUT_S) as r, open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                got = _sha256(tmp)
                if got != sha:
                    logger.warning("[固件] v%s sha256 不符（%s…≠%s…），换源", version, got[:12], sha[:12])
                    continue
                os.replace(tmp, dest)
                with self._lock:
                    idx = self._index()
                    idx[fn] = {"version": version, "sha256": sha,
                               "size": dest.stat().st_size, "source": "lock", "at": time.time()}
                    self._save_index(idx)
                return True, "ok"
            except Exception as e:  # noqa: BLE001 多源换源继续
                logger.warning("[固件] 源失败 %s: %s", url.split("?")[0], e)
            finally:
                tmp.unlink(missing_ok=True)
        return False, "所有源失败或校验不符"

    # ── 一次性领取令牌 ──────────────────────────────────────────────
    def issue(self, version: str, mac: str = "") -> dict | None:
        row = next((r for r in self.versions() if r["version"] == version and r["on_disk"]
                    and not r.get("sha_mismatch")), None)
        if not row:
            return None
        tok = secrets.token_hex(16)
        now = time.time()
        with self._lock:
            self._tokens = {k: v for k, v in self._tokens.items() if v["exp"] > now}
            self._tokens[tok] = {"file": row["file"], "exp": now + _ISSUE_TTL_S}
        logger.info("[固件] 签发领取令牌 v%s mac=%s exp=%ds", version, mac or "-", _ISSUE_TTL_S)
        return {"token": tok, "file": row["file"], "sha256": row["sha256"],
                "size": row["size"], "version": version, "expires_in": _ISSUE_TTL_S}

    def take(self, token: str, fname: str) -> Path | None:
        """消费即废（含文件名一致性——令牌不可指使他包）。"""
        if not token:
            return None
        with self._lock:
            rec = self._tokens.pop(token, None)
        if not rec or rec["exp"] <= time.time() or rec["file"] != fname:
            return None
        path = (self.public / fname).resolve()
        if not path.is_file() or self.public.resolve() not in path.parents:
            return None
        return path

    def status(self) -> dict:
        rows = self.versions()
        lat = next((r for r in rows if r["on_disk"] and not r.get("sha_mismatch")), None)
        return {
            "latest": lat["version"] if lat else "",
            "items": rows,
            "import_dir": str(self.import_dir),
            "lock_override": self.override_lock.is_file(),
            "note_zh": "投递口放 <名>-x.y.z.bin 自动收编；lock 登记的版本需点「拉取」显式下载（sha256 必核）",
        }
