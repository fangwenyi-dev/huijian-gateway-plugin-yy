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
from urllib.parse import quote, urlsplit

from . import const

logger = logging.getLogger("huijian.firmware")

_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_ISSUE_TTL_S = 600
_DL_TIMEOUT_S = 300
_FN_URL_BUDGET = 150   # 文件名 quote 后长度预算：URL 全长 ≤255（BLE string8 帧 uint8 限长，
                       # 72B 固定开销 + 余量；超长设备侧静默截断 → 404 且令牌已烧）
# ── v1.0.65 OTA 审查批（安全 F1/F2/F3/F4 + 正确性 F-OTA-02/05/06/07/09/10/12）──
_FN_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]+\.bin$")   # 落盘名白名单：拒绝对路径/../控制字符
_URL_SCHEMES = ("http://", "https://")                # 拒 file:// 等本地读（urlopen 默认支持 file）
_DL_MAX_BYTES = 256 << 20                             # 下载绝对硬顶（/data 是 HA 共享卷）
_DL_OVERSIZE_RATIO = 1.2                              # 声明 size 的浮动上限
_IMPORT_SETTLE_S = 2.0                                # 投递包静止闸：mtime 距今 <2s 视为拷贝中
# ── OTA 载荷容量闸（2026-09-26 补）─────────────────────────────────
# 设计文档 esp32固件OTA升级方案-2026-09-23.md L88「size ≤ 槽容量-10% 服务端预检 +
# B3 双保险」与 L92「CI 加 bin 尺寸闸(>3.7MB 红)」两条**从未实现**。后果实测：lock 把
# 产线用的**合并出厂镜像**当 OTA 载荷登记（2.1.65/2.1.66 均 8,786,984B），面板点「升级」
# 后设备在分区闸上确定性拒绝——`Ota: Firmware 8786984 bytes exceeds OTA partition
# 4128768 bytes -> rejected`——原因只躺在设备串口里，面板侧只显示"已下发"。
# OTA 载荷必须是 **app 镜像**（合并镜像含 bootloader/分区表/assets，永远装不进 app 槽）。
OTA_SLOT_BYTES = 0x3F0000                 # 4,128,768B：ota_0/ota_1 实测值（IDF 同源）
OTA_MAX_BYTES = OTA_SLOT_BYTES * 9 // 10  # 留 10% 余量＝3,715,891B（≈文档的 3.7MB）


def ota_capacity_error(version: str, size: int) -> str:
    """纯函数：返回 ""＝可下发；否则＝拒下发具名原因（API/面板可直接展示）。"""
    sz = _to_int(size, f"capacity v{version}")
    if sz > OTA_MAX_BYTES:
        return (f"v{version} 载荷 {sz}B 超 OTA 槽可用上限 {OTA_MAX_BYTES}B"
                f"（槽 {OTA_SLOT_BYTES}B 留 10% 余量）——登记错了：OTA 要的是 app 镜像，"
                f"不是产线 flash_tool 用的合并出厂镜像")
    return ""


def _clip(s, n: int = 120) -> str:
    """入日志前净化：截断+剔控制字符（:8000 匿名面 fname 可含 CRLF → 日志注入）。"""
    return "".join(ch for ch in str(s)[:n] if ch.isprintable())


def _to_int(v, ctx: str = "") -> int:
    """手编 lock/index 的 size 字段折叠（"10KB" 类脏值不再毒死整表面板）。"""
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        logger.warning("[固件] %s size 字段非数值（%r），按 0 处理", _clip(ctx), v)
        return 0


def _url_hosts(urls: list) -> list:
    """对外数据面只给主机名——lock urls 可能内嵌私有仓访问串，不回显（安全 F3）。"""
    out = []
    for u in urls:
        try:
            h = urlsplit(u).hostname or ""
        except ValueError:
            h = ""
        if h:
            out.append(h)
    return out


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
        self._downloading: set[str] = set()   # 单飞闸：同版本下载中拒重入（安全 F2）
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
                logger.warning("[固件] index.json 非 dict，按实盘重建")
        except (OSError, ValueError) as e:
            logger.warning("[固件] index.json 损坏，按实盘重建：%s", e)
        return self._rebuild_index()

    def _rebuild_index(self) -> dict:
        """index 唯一账本损坏时按 public 实盘重算（F-OTA-10：不再让已收编
        固件变幽灵）。只重建内存账返回，不落盘——下次持锁写操作自然固化，
        避免无锁写文件竞态；重建成本=public 全量 sha（仅损坏时发生）。"""
        idx: dict = {}
        try:
            for p in sorted(self.public.glob("*.bin")):
                m = _VERSION_RE.search(p.name)
                if not m:
                    continue
                try:
                    st = p.stat()
                    idx[p.name] = {"version": m.group(1), "sha256": _sha256(p),
                                   "size": st.st_size, "source": "imported",
                                   "at": st.st_mtime}
                except OSError:
                    continue
        except OSError as e:
            logger.warning("[固件] index 重建失败：%s", e)
            return {}
        if idx:
            logger.warning("[固件] index 已从 public 实盘重建 %d 条（source 标记归 imported，"
                           "与 lock 声明 sha 的对账照常）", len(idx))
        return idx

    def _save_index(self, idx: dict) -> None:
        tmp = self.index_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(idx, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())   # 掉电不再造空账（F-OTA-10）
        os.replace(tmp, self.index_file)

    def scan_import(self) -> int:
        """收编投递口新包：静止闸→解析版本→算哈希（锁外）→原子移入 public
        →更新索引（锁内）。返回新收编数。文件名解析不出版本 = 拒收并留 WARN
        （不静默吞）。

        v1.0.65 审查批重构（F-OTA-02/08）：
        - 静止闸：mtime 距今 <2s 视为拷贝进行中，本轮跳过（下轮面板轮询再收）
          ——Windows/SMB 向 /data 复制 4MB+ 耗时数秒，旧版会把半截文件收编。
        - sha 重 IO 移出 _lock：不再拖停同锁的 take()（:8000 语音热路径）。
        - 收编后 size 突变判废：哈希计算与移动之间文件仍在被写 → 删残缺包不入账。
        """
        with self._lock:
            files = sorted(self.import_dir.glob("*.bin"))
        staged = []
        for f in files:
            m = _VERSION_RE.search(f.name)
            if not m:
                logger.warning("[固件] 投递包 %s 文件名不含 x.y.z 版本，拒收", _clip(f.name))
                continue
            try:
                st = f.stat()
            except OSError:
                continue   # 并发下已被移走
            if time.time() - st.st_mtime < _IMPORT_SETTLE_S:
                continue   # 拷贝未静止，下轮再收
            if st.st_size <= 0:
                logger.warning("[固件] 投递包 %s 为 0 字节，拒收（设备按 content_length==0 拒收）",
                               _clip(f.name))
                continue
            if len(quote(f.name)) > _FN_URL_BUDGET:
                logger.warning("[固件] 投递包 %s 文件名过长（BLE 代发 URL 预算 %sB），拒收",
                               _clip(f.name), _FN_URL_BUDGET)
                continue
            try:
                sha = _sha256(f)
            except OSError as e:
                logger.warning("[固件] %s 读取失败：%s", _clip(f.name), e)
                continue
            staged.append((f, m.group(1), sha, st.st_size))
        if not staged:
            return 0
        n = 0
        with self._lock:
            idx = self._index()
            for f, ver, sha, sz_before in staged:
                dest = self.public / f.name
                try:
                    os.replace(f, dest)
                    sz_after = dest.stat().st_size
                except OSError as e:
                    logger.warning("[固件] 收编移动失败 %s：%s", _clip(f.name), e)
                    continue
                if sz_after != sz_before:
                    logger.warning("[固件] %s 收编期间大小变化（%s→%s），判废删除——"
                                   "请等拷贝完成后重新投递", _clip(f.name), sz_before, sz_after)
                    dest.unlink(missing_ok=True)
                    continue
                idx[f.name] = {"version": ver, "sha256": sha, "size": sz_after,
                               "source": "imported", "at": time.time()}
                n += 1
                logger.info("[固件] 收编 %s（v%s, %s…）", _clip(f.name), ver, sha[:12])
            if n:
                self._save_index(idx)
        return n

    # ── 版本账 ──────────────────────────────────────────────────────
    def versions(self) -> list[dict]:
        """合并视图（降序）：public 实盘（imported/已下载）+ lock 登记（on_disk
        标记是否已就位）。

        v1.0.65 审查批（F-OTA-05/07 + 安全 F3 + F-OTA-07）：
        - size 脏值折叠为 0（手编 lock/index 一条坏行不再毒死整表面板 500）；
        - urls 不再原样外发（改 urls_count+urls_hosts——lock 可内嵌私有仓
          一次性访问串，凭据值不进对外数据面）；
        - imported 覆盖同版 lock 时保留 lock 侧源线索与**哈希对账**
          （「同版本不同字节」是最危险组合，必须照红）。
        """
        self.scan_import()
        idx = self._index()
        rows: dict[str, dict] = {}   # version → 行（imported 覆盖 lock）
        for rel in self._lock_releases():
            v = str(rel.get("version", ""))
            if not v:
                continue
            if v in rows:
                logger.warning("[固件] lock 重复登记 v%s（后条覆盖前条）——请去重", v)
            fn = str(rel.get("file", ""))
            e = idx.get(fn)
            urls = [u for u in (rel.get("urls") or []) if isinstance(u, str)]
            row = {
                "version": v, "file": fn,
                "sha256": str(rel.get("sha256", "")),
                "size": _to_int(rel.get("size"), f"lock v{v}"),
                "source": "lock",
                "on_disk": bool(e and (self.public / fn).is_file()),
                "urls_count": len(urls),
                "urls_hosts": _url_hosts(urls),
                "notes_zh": str(rel.get("notes_zh", "")),
            }
            if e:   # 实盘哈希与 lock 声明不符 → 面板标红，绝不默认信任
                row["sha_mismatch"] = bool(row["sha256"] and row["sha256"] != e.get("sha256"))
            rows[v] = row
        for fn, e in idx.items():
            v = e.get("version", "")
            lock_row = rows.get(v)
            if e.get("source") == "imported" or lock_row is None:
                row = {
                    "version": v, "file": fn, "sha256": e.get("sha256", ""),
                    "size": _to_int(e.get("size"), f"index {fn}"),
                    "source": e.get("source", "imported"),
                    "on_disk": (self.public / fn).is_file(),
                    "urls_count": 0, "urls_hosts": [], "notes_zh": "",
                }
                if lock_row is not None:
                    row["urls_count"] = lock_row["urls_count"]
                    row["urls_hosts"] = lock_row["urls_hosts"]
                    row["notes_zh"] = lock_row["notes_zh"]
                    if lock_row["sha256"] and lock_row["sha256"] != e.get("sha256"):
                        row["sha_mismatch"] = True
                rows[v] = row
        return sorted(rows.values(), key=lambda r: vkey(r["version"]), reverse=True)

    def latest(self) -> dict | None:
        for r in self.versions():
            if (r.get("on_disk") and not r.get("sha_mismatch")
                    and _to_int(r.get("size")) > 0):   # 0 字节包不入围（F-07）
                return r
        return None

    # ── 下载（运维点触，sha256 必核）─────────────────────────────────
    def download(self, version: str) -> tuple[bool, str]:
        """按 lock 条目拉包：urls 依次试，**必须**带 sha256 才允许下载
        （无校验值=不入库，方案 §4-P2 供应链闸）。同步执行，调用方放线程。

        v1.0.65 审查批（安全 F1/F2 + F-OTA-06/12）：
        - **写侧路径闸**：file 必须纯 basename 白名单形态——lock 是「现场热修」
          手编面，`/abs/path`、`../` 都会逃出 public 任意写（读侧 take 早有
          resolve+parents 闸，写侧此前没有=整条链唯一不对称点）；
        - urls 仅收 http/https——urllib 默认 opener 支持 file://，恶意/笔误
          lock 可读本地任意文件入 public 再从 :8000 匿名口合法领走；
        - 流式字节闸（声明 size×1.2，硬顶 256MB）+ 总时限：谎报 size 的大
          对象不再能灌爆 /data（HA 共享卷）；
        - 同版本单飞闸（并发点击不叠 tmp）；
        - os.replace+记账收进同一 _lock 临界区（消除与 scan_import 的交叉
          坏账窗口——lock 声明 sha 曾可标到 import 字节上）。
        """
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
        if not _FN_SAFE_RE.match(fn):
            logger.error("[固件] lock v%s file=%r 非纯文件名（路径穿越/非法字符），拒绝下载",
                         version, _clip(fn))
            return False, "lock file 字段非法（须为纯 <名>.bin 文件名）"
        urls = [u for u in urls if u.lower().startswith(_URL_SCHEMES)]
        if not urls:
            logger.error("[固件] lock v%s urls 无 http/https 源，拒绝下载", version)
            return False, "lock urls 无 http/https 源（file:// 等本地协议拒绝）"
        dest = self.public / fn
        if dest.resolve().parent != self.public.resolve():
            return False, "lock file 路径复验失败"
        declared = _to_int(rel.get("size"), f"lock v{version}")
        limit = (min(_DL_MAX_BYTES, int(declared * _DL_OVERSIZE_RATIO))
                 if declared > 0 else _DL_MAX_BYTES)
        with self._lock:
            if version in self._downloading:
                return False, "该版本正在下载中，请稍候"
            self._downloading.add(version)
        try:
            for url in urls:
                tmp = dest.with_name(dest.name + f".part.{secrets.token_hex(4)}")
                try:
                    logger.info("[固件] 下载 v%s ← %s", version, _clip(url.split("?")[0]))
                    req = urllib.request.Request(url, headers={"User-Agent": "huijian-voice/1.0"})
                    deadline = time.monotonic() + _DL_TIMEOUT_S
                    total = 0
                    with urllib.request.urlopen(req, timeout=_DL_TIMEOUT_S) as r, \
                            open(tmp, "wb") as f:
                        while True:
                            if time.monotonic() > deadline:
                                raise TimeoutError(f"下载超总时限 {_DL_TIMEOUT_S}s")
                            chunk = r.read(1 << 20)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > limit:
                                raise ValueError(
                                    f"下载超字节闸 {limit}B（声明 {declared}B），中止换源")
                            f.write(chunk)
                    got = _sha256(tmp)
                    if got != sha:
                        logger.warning("[固件] v%s sha256 不符（%s…≠%s…），换源",
                                       version, got[:12], sha[:12])
                        continue
                    sz = tmp.stat().st_size
                    with self._lock:   # replace+记账同临界区（F-OTA-06）
                        os.replace(tmp, dest)
                        idx = self._index()
                        idx[fn] = {"version": version, "sha256": sha,
                                   "size": sz, "source": "lock", "at": time.time()}
                        self._save_index(idx)
                    return True, "ok"
                except Exception as e:  # noqa: BLE001 多源换源继续
                    logger.warning("[固件] 源失败 %s: %s", _clip(url.split("?")[0]), e)
                finally:
                    tmp.unlink(missing_ok=True)
            return False, "所有源失败或校验不符"
        finally:
            with self._lock:
                self._downloading.discard(version)

    # ── 一次性领取令牌 ──────────────────────────────────────────────
    def capacity_error(self, version: str) -> str:
        """按账目 size 查容量闸；""＝可下发。给 API 层出**精确**拒因用（issue() 已自拦，
        但那里只能回 None，面板会糊成「不在盘」）。"""
        row = next((r for r in self.versions() if r["version"] == version), None)
        return "" if row is None else ota_capacity_error(version, row.get("size"))

    def issue(self, version: str, mac: str = "") -> dict | None:
        row = next((r for r in self.versions() if r["version"] == version and r["on_disk"]
                    and not r.get("sha_mismatch")), None)
        if not row:
            return None
        # 在盘复核（v1.0.65 审查批）：0 字节拒签（F-07：设备按 content_length==0
        # 拒收，白烧令牌）；size 与索引账不符=收编后被外部改过，拒签（F-OTA-02
        # 第二道闸）。
        try:
            actual = (self.public / row["file"]).stat().st_size
        except OSError as e:
            logger.warning("[固件] v%s 在盘复核失败：%s", version, e)
            return None
        if actual <= 0 or actual != _to_int(row.get("size")):
            logger.warning("[固件] v%s 在盘 size=%s 与账目 %s 不符（或为 0），拒签——请重新投递",
                           version, actual, row.get("size"))
            return None
        # BLE string8 帧长预算（F-06）：URL 总长 >255B 设备侧静默截断 → 404 且令牌已烧
        if len(quote(row["file"])) > _FN_URL_BUDGET:
            logger.warning("[固件] v%s 文件名超 URL 预算（%sB），不可 BLE 代发：%s",
                           version, _FN_URL_BUDGET, _clip(row["file"]))
            return None
        # 载荷容量闸：超槽的包签出去也必被设备拒（见 OTA_SLOT_BYTES 处注释），这里先拦，
        # 免得白烧一枚 10min 令牌并让面板以为"已下发"。
        if (cap := ota_capacity_error(version, row.get("size"))):
            logger.warning("[固件] %s", cap)
            return None
        tok = secrets.token_hex(16)
        now = time.time()
        with self._lock:
            self._tokens = {k: v for k, v in self._tokens.items() if v["exp"] > now}
            self._tokens[tok] = {"file": row["file"], "exp": now + _ISSUE_TTL_S}
        logger.info("[固件] 签发领取令牌 v%s mac=%s exp=%ds", version, _clip(mac, 40) or "-",
                    _ISSUE_TTL_S)
        return {"token": tok, "file": row["file"], "sha256": row["sha256"],
                "size": row["size"], "version": version, "expires_in": _ISSUE_TTL_S}

    def take(self, token: str, fname: str) -> Path | None:
        """消费即废（含文件名一致性——令牌不可指使他包）。
        v1.0.65：拒绝原因分类落日志（过期/名不符/未知令牌/盘缺不再混成一行，
        现场「链接刚发就失效」可定位）；错名仍先烧令牌（反 oracle，测试钉死）。"""
        if not token:
            return None
        now = time.time()
        with self._lock:
            rec = self._tokens.pop(token, None)
            if len(self._tokens) > 64:   # 顺手清过期（签发停止后废令牌不驻留内存）
                self._tokens = {k: v for k, v in self._tokens.items() if v["exp"] > now}
        if rec is None:
            reason = "未知令牌"
        elif rec["exp"] <= now:
            reason = "令牌过期"
        elif rec["file"] != fname:
            reason = "文件名不符"
        else:
            reason = ""
        if reason:
            logger.warning("[OTA] 领取被拒（%s）file=%s", reason, _clip(fname))
            return None
        path = (self.public / fname).resolve()
        if not path.is_file() or self.public.resolve() not in path.parents:
            logger.warning("[OTA] 领取被拒（在盘文件缺失或越界）file=%s", _clip(fname))
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
