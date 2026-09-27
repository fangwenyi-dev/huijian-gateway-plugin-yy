# -*- coding: utf-8 -*-
"""交付仓泄漏守卫：凭据/会话产物/内部文档不得进入 git 索引。

为什么要有这个文件（2026-09-22 优化盘点）：仓根历来堆着 70+ 个会话产物，
逐 ID `git check-ignore` 实测**全部未忽略**——防线只是"人不去 `git add -A`"。
而 ci.yaml 是 push main 即构建发版，所以一次批量 add 就会把现场 HA 十年期
长期令牌、客户内网 IP、会话纪要随代码推给全体客户，且 Gitee 镜像同步跟上
后不可回收。_field_topology.py 里那枚令牌此前就与 ~/gates/.hatok 逐字节相同
地内嵌在仓根脚本中（已迁 scripts/field_topology.py 改读外部凭据文件）。

`.gitignore` 是必要不充分条件：它挡不住已经 add 过的东西，也挡不住同事
`git add -f`，更挡不住未来新增的 scratch 命名形态。这里钉的是**索引事实**，
与 .gitignore 是否写全无关。
"""
import pathlib
import re
import subprocess

import pytest

ADDON = pathlib.Path(__file__).resolve().parents[1]
REPO = ADDON.parent

# ── 扫描规则 ──────────────────────────────────────────────────────
# 硬规则：任何受版文件命中即红，无白名单（2026-09-22 实测存量零命中，
# 所以白名单为空不等于"规则还没生效"）。
HARD_PATTERNS = {
    # HA 长期令牌 / 任意 JWT：三段 base64url，第二段以 eyJ 开头是 JWT 指纹
    "jwt-token": re.compile(rb"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}"),
    "private-key-block": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # 中国大陆手机号 11 位，前后不接数字以免命中长数字串
    "cn-mobile": re.compile(rb"(?<!\d)1[3-9]\d{9}(?!\d)"),
    # 常见扫描器/CLI 明文 token 形态
    "bearer-personal-access-token": re.compile(
        rb"(?i)(ghp_|github_pat_|gtpat_)[A-Za-z0-9_]{20,}"),
}

# 交付面：镜像/商店会把这两个目录整体带走（Dockerfile COPY custom_components/、
# boot.sh cp -a 落盘到客户 /homeassistant/custom_components）。其中出现内部
# 文档=直接进客户机。v1.1.5 前 MODIFICATION_RECORD.md 就在集成目录里躺着，
# 224 行内部改稿记录带死仓本地路径 e:\AI\0418huijianjicheng\ —— 已迁
# docs/internal/，此处钉死不许回流。
DELIVERY_DIRS = ("huijian_voice/custom_components/", "huijian_voice/www/")
DELIVERY_DOC_ALLOWLIST = {"huijian_voice/custom_components/huijian_ai/DOCS.md"}

# 仓根 scratch：会话一次性产物，历来靠"不去 add"保命。根 _* 与 .tmp-hj 一律禁入库。
SCRATCH_RE = re.compile(r"^(_[^/]+|huijian_voice/\.tmp-hj/.+|_quarantine/.+)$")

# 对外叙事文档里的现场内网 IP。tests/ 与 nginx.conf 里的 192.168.x 是夹具和
# 监听地址，属正常；只有 CHANGELOG/DOCS/README 这类给客户读的文本里出现具体
# 现场 IP 才是泄漏面。
OUTBOUND_DOC_GLOBS = ("CHANGELOG.md", "DOCS.md", "README.md", "CLAUDE.md")
PRIVATE_IP = re.compile(
    rb"(?<![\d.])(?:192\.168|10\.\d+|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d.])")
# 已经随公开仓发布过的历史条目：改写历史正文并不能把它从未发布中收回，
# 逐条登记而非整片放行——新增一个现场 IP 仍然会红。
OUTBOUND_IP_KNOWN_PUBLISHED = {
    "CHANGELOG.md": {"192.168.1.235", "192.168.1.91"},
}


def _tracked_files():
    """git 索引事实。git 不可用时**当场失败**而非静默跳过——守卫没牙比没守卫
    更危险（仓内已有 v1.0.94"全绿真机炸"与 CI 恒 skip 的教训）。"""
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=REPO,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.fail(f"守卫失去牙齿：git ls-files 失败 rc={proc.returncode} "
                    f"{proc.stderr.strip()[:200]}")
    return [f for f in proc.stdout.split("\0") if f]


def _scan_blob(blob: bytes):
    """纯函数：返回命中的硬规则名。单独可测，供反向钉做变异用。"""
    return sorted(name for name, rx in HARD_PATTERNS.items() if rx.search(blob))


def test_no_secret_anywhere_in_git_history():
    """**全历史对象扫**（v1.1.17 补）：上面几钉扫的是"索引 ∩ 工作树"，永远抓不到
    "已经提交过的凭据"——真令牌那次恰好在从未入库的仓根脚本 `/_field_topology.py`
    里，历史干净是**运气**，不是这道闸的功劳。这里遍历对象库里每个 blob，命中
    HARD_PATTERNS 即红（含已删除文件、已改历史正文——只要还在对象库里就查）。

    豁免面只有一处：`tests/` 下的 blob（仓内既有约定：测试夹具里的 192.168.x /
    合成手机号属正常，见上方 PRIVATE_IP 注释）。夹具一旦挪进交付面照样红。
    元钉：扫到的 blob 数必须成规模（防解析器退化/静默扫 0 个的真空绿）。
    """
    paths = _history_blob_paths()
    assert len(paths) >= 100, f"历史扫描疑似退化（只解析到 {len(paths)} 个 blob）"
    inp = ("\n".join(paths) + "\n").encode()
    proc = subprocess.run(["git", "cat-file", "--batch"], cwd=REPO,
                          input=inp, capture_output=True)
    if proc.returncode != 0:
        pytest.fail(f"守卫失去牙齿：cat-file --batch 失败 rc={proc.returncode} "
                    f"{proc.stderr[:200]!r}")
    buf, pos, hits, scanned = proc.stdout, 0, {}, 0
    while pos < len(buf):
        nl = buf.index(b"\n", pos)
        header = buf[pos:nl].split()
        if len(header) != 3:                     # missing/坏头：到此为止
            break
        sha, kind, size = header[0].decode(), header[1], int(header[2])
        body = buf[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1                  # 跳对象尾随 \n
        if kind != b"blob":                      # 只扫文件内容（bytes 比 bytes）：
            continue                             # commit/tree 的哈希数字串会假阳（实测三例）
        path = paths.get(sha, "")
        if path.startswith(("tests/", "huijian_voice/tests/")):
            continue
        scanned += 1
        for rule in _scan_blob(body):
            hits.setdefault(rule, []).append(path or sha[:12])
    assert scanned >= 100, f"实际扫描 blob 数过少（{scanned}）——解析或过滤失灵"
    assert not hits, f"历史对象里命中硬规则：{ {k: v[:5] for k, v in hits.items()} }"


def _history_blob_paths() -> dict:
    """全历史 blob sha → 路径（首次出现者）。rev-list --objects --all 覆盖所有
    分支/标签可达对象；sha 无路径者（如历史中间态）路径留空，照样扫。

    encoding 显式 utf-8：Windows 上 text=True 会用 GBK 解 git 输出，
    对象名含中文时 reader 线程 UnicodeDecodeError → stdout 变 None（本机实测）。"""
    proc = subprocess.run(["git", "rev-list", "--objects", "--all"], cwd=REPO,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        pytest.fail(f"守卫失去牙齿：rev-list 失败 rc={proc.returncode} "
                    f"{proc.stderr.strip()[:200]}")
    out: dict = {}
    for line in proc.stdout.splitlines():
        sha, _, path = line.partition(" ")
        if sha and sha not in out:
            out[sha] = path
    return out


def test_hard_secret_rules_are_nonempty():
    """元钉：规则表被清空/注释掉时本测试红，避免"守卫静默退化成永真"。"""
    assert len(HARD_PATTERNS) >= 4, "泄漏规则表被削减"
    assert DELIVERY_DIRS and OUTBOUND_DOC_GLOBS


def test_no_secrets_in_tracked_files():
    # 本文件自身必须豁免：里面的 JWT/私钥/手机号是**反向钉用的合成样本**（见
    # test_secret_detector_really_fires），不豁免则守卫会咬自己的测试——CI 首跑
    # 实锤：本地核验时该文件还没入库，git ls-files 不含它，扫不到就以为绿。
    # 豁免范围钉死在本文件，第二条豁免需要改代码才能加。
    self_rel = (pathlib.Path(__file__).resolve().relative_to(REPO)).as_posix()
    offenders = []
    for rel in _tracked_files():
        if rel == self_rel:
            continue
        path = REPO / rel
        if not path.is_file() or path.stat().st_size > 4_000_000:
            continue
        try:
            hit = _scan_blob(path.read_bytes())
        except OSError:
            continue
        if hit:
            offenders.append(f"{rel}: {','.join(hit)}")
    assert not offenders, (
        "受版文件含凭据形态字符串（长期令牌/私钥/手机号/PAT）。"
        "凭据只准放仓外（~/gates/.hatok 之类）或 CI Secrets：\n" + "\n".join(offenders))


def test_secret_detector_really_fires():
    """反向钉：把违例串喂给判据必须命中——否则上面那条绿可能只是规则写错。"""
    fake = (b"TOK = ('eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            b"eyJpc3MiOiIwYWMyNTEzZWE5Zjg0NzBhOWY0YWQ3YzM2YjE2YzQ3ZCIsImlhdCI6MTc4ODMzOTgwNCwi"
            b"ZXhwIjoyMTAzNjk5ODA0fQ.Ab0vzGlQ3aybZkvGku6HZAwbz2Wfkvxhn9codyBfJfQ')")
    assert "jwt-token" in _scan_blob(fake)
    assert "private-key-block" in _scan_blob(b"x\n-----BEGIN RSA PRIVATE KEY-----\ny")
    assert "cn-mobile" in _scan_blob(b"contact=13812345678")
    assert "bearer-personal-access-token" in _scan_blob(b"t=ghp_" + b"A" * 36)
    # 负例：正常中文文案与 127.0.0.1 不得误伤
    assert _scan_blob("版本号 1.1.5，采样率 16000，端口 8123".encode()) == []


def test_no_scratch_files_tracked():
    offenders = [rel for rel in _tracked_files() if SCRATCH_RE.match(rel)]
    assert not offenders, (
        "仓根会话 scratch 被纳入版本管理（应为 scripts/ 下的长期工具或忽略）：\n"
        + "\n".join(offenders))


def test_internal_docs_absent_from_delivery_dirs():
    offenders = []
    for rel in _tracked_files():
        if not rel.startswith(DELIVERY_DIRS):
            continue
        if not rel.endswith((".md", ".txt")) or rel.endswith((".json",)):
            continue
        if rel in DELIVERY_DOC_ALLOWLIST or rel.rsplit("/", 1)[-1] in {"LICENSE", "README.md"}:
            continue
        offenders.append(rel)
    assert not offenders, (
        "交付目录里出现内部文档，会随 cp -a 落进客户 /homeassistant 与镜像："
        "\n".join(offenders) + "\n请迁到 docs/internal/（不受任何 COPY 路径覆盖）")


def test_outbound_docs_carry_no_fresh_field_ips():
    offenders = []
    for rel in _tracked_files():
        if pathlib.PurePosixPath(rel).name not in OUTBOUND_DOC_GLOBS or "/" in rel:
            continue
        path = REPO / rel
        if not path.is_file():
            continue
        known = OUTBOUND_IP_KNOWN_PUBLISHED.get(rel, set())
        for ip in {m.decode() for m in PRIVATE_IP.findall(path.read_bytes())}:
            if ip not in known:
                offenders.append(f"{rel}: {ip}")
    assert not offenders, (
        "对外文档出现未登记的现场内网地址。历史已发布条目请逐条登记进 "
        "OUTBOUND_IP_KNOWN_PUBLISHED（写清是哪次工单），新 IP 应先脱敏：\n"
        + "\n".join(sorted(offenders)))


def test_outbound_ip_allowlist_is_load_bearing():
    """反向钉：登记簿里的 IP 若已不在文档中出现，说明登记过期或文档被改写；
    不双向判的话，白名单只会被单向越加越宽。"""
    stale = []
    for rel, ips in OUTBOUND_IP_KNOWN_PUBLISHED.items():
        path = REPO / rel
        body = path.read_text(encoding="utf-8") if path.is_file() else ""
        for ip in sorted(ips):
            if ip not in body:
                stale.append(f"{rel}: {ip}")
    assert not stale, "白名单存在过期条目，请一并删除：\n" + "\n".join(stale)
