"""发布一致性钉桩（工作区规矩：版本四点一致 + 翻译三份同步且 schema 键全覆盖）。

钉桩对象：
  · config.yaml version == const.APP_VERSION 兜底 == www CURRENT_VERSION == CHANGELOG 首段
  · schema 每键 × 三份翻译 = name+description 齐全（Supervisor 缺 name 拒收整份）
  · options 默认键 ⊆ schema 键
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

yaml = pytest.importorskip("yaml")


def _config():
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def _parse_translations():
    # 仓内不强制装 pyyaml？——网关测试同款依赖面；若缺则跳过（E2E 机 CI 必装）
    out = {}
    for lang in ("zh-Hans", "zh-CN", "en"):
        f = ROOT / "translations" / f"{lang}.yaml"
        assert f.exists(), f"缺翻译文件 translations/{lang}.yaml"
        out[lang] = yaml.safe_load(f.read_text(encoding="utf-8"))
    return out


def test_version_four_point_consistency():
    cfg = _config()
    ver = cfg["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", ver)
    www = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'CURRENT_VERSION = "([\d.]+)"', www)
    assert m and m.group(1) == ver, "www/index.html CURRENT_VERSION 未随版本 bump"
    import json
    vj = json.loads((ROOT / "www" / "version.json").read_text(encoding="utf-8"))
    assert vj["addon_version"] == ver, "version.json.addon_version 漂移"
    mf = json.loads((ROOT / "custom_components" / "huijian_ai" / "manifest.json")
                    .read_text(encoding="utf-8"))
    assert mf["version"] == ver, "vendored 集成 manifest 版本未与加载项同链（网关四源范式）"
    changelog = (ROOT.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(ver)}\]", changelog, re.M), \
        "根 CHANGELOG 缺 `## [ver]` 段（Keep a Changelog 头，CI Release 正文/awk 提取依赖）"
    constpy = (ROOT / "core" / "const.py").read_text(encoding="utf-8")
    # const 最终兜底字面量（毒值过滤后的 else 分支）必须与 config 同版本
    m2 = re.search(r'HUIJIAN_VERSION", ""\).*\nAPP_VERSION = .*else "([\d.]+)"', constpy)
    assert m2 and m2.group(1) == ver, "const.py 版本兜底未 bump"


def test_options_are_covered_by_schema():
    cfg = _config()
    for k in cfg.get("options", {}):
        assert k in cfg["schema"], f"option {k} 无 schema 约束"


def test_translations_cover_every_schema_key():
    cfg = _config()
    trans = _parse_translations()
    for lang, doc in trans.items():
        conf = (doc or {}).get("configuration", {})
        for k in cfg["schema"]:
            entry = conf.get(k)
            assert entry, f"{lang}.yaml 缺 configuration.{k}（Supervisor 会拒收整份）"
            assert (entry.get("name") or "").strip(), f"{lang}.{k} 缺 name"
            assert (entry.get("description") or "").strip(), f"{lang}.{k} 缺 description"


def test_zh_hans_and_zh_cn_identical():
    a = (ROOT / "translations" / "zh-Hans.yaml").read_text(encoding="utf-8")
    b = (ROOT / "translations" / "zh-CN.yaml").read_text(encoding="utf-8")
    assert a == b, "zh-CN 与 zh-Hans 定案为同文兜底，请保持逐字一致"


def test_slug_and_naming():
    cfg = _config()
    assert cfg["slug"] == "huijian_voice"
    # v1.0.1：ACR 单仓多架构路线下镜像仓名=仓库名（用户在 ACR 控制台实建的
    # huijian-gateway-plugin-yy），"镜像名含 slug 词根"的 ghcr 时代规矩由
    # test_store_schema 的逐字路径钉桩接管；ghcr 灾备仓仍守 huijian-voice 词根
    # （见 DOCS FAQ 灾备串）。
    assert cfg["image"].endswith("/fangwenyi-dev/huijian-gateway-plugin-yy")
    assert cfg["arch"] == ["amd64", "aarch64"]
    assert cfg["watchdog"].startswith("tcp://")


# ── 基础设施契约钉桩（infra 审查环教训：builder 文法/基镜像/静态根/Ingress）──

_BAD_IMPORT = re.compile(r"\bimport\b[^\n]*?\bopus\b(?!\w)")  # opuslib_next 不算


def test_no_bare_opus_import_in_build_chain():
    """opuslib-next 导入名是 opuslib_next（audio.py 实证）；Dockerfile/boot.sh
    自检若写 `import opus` 会分别挡构建 / 让 s6 halt。"""
    for f in ("Dockerfile", "boot.sh"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert not _BAD_IMPORT.search(src), f"{f} 存在 `import opus` 裸名自检"


def test_base_image_single_source_of_truth():
    """build.yaml（builder 版唯一生效入口）与 Dockerfile ARG 默认必须同 tag，
    且 tag 真实存在性发布前人工对 ghcr 核（7.2.6 幻觉 tag 教训）。"""
    build = yaml.safe_load((ROOT / "build.yaml").read_text(encoding="utf-8"))
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"ARG BUILD_FROM=(\S+)", dockerfile)
    assert m, "Dockerfile 缺 ARG BUILD_FROM 默认（Supervisor≥2026.04 不再注入）"
    for arch in ("amd64", "aarch64"):
        assert build["build_from"][arch] == m.group(1), f"{arch} 基镜像双源漂移"


def test_workflow_uses_supported_builder_args():
    """交付标准=网关仓 ci.yaml 的 builder split-actions 范式（@2026.06.0，网关
    CI 长期跑通实证）。单体 home-assistant/builder@ 的参数文法陷阱（--docker/
    --build-arg/--tag/--label → catch-all exit.nok）自此结构性消失。"""
    wf_path = ROOT.parent / ".github" / "workflows" / "ci.yaml"
    raw = wf_path.read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    jobs = doc["jobs"]
    for act in ("prepare-multi-arch-matrix@2026.06.0",
                "actions/build-image@2026.06.0",
                "publish-multi-arch-manifest@2026.06.0"):
        assert act in raw, f"缺网关同款 action：{act}"
    assert "uses: home-assistant/builder@" not in raw, "禁回退单体 builder"
    assert set(jobs) == {"lint", "prepare", "init", "build", "e2e", "manifest",
                         "push-acr", "release", "gitee-release"}, "九 job 链形变"
    assert jobs["manifest"]["needs"] == ["prepare", "init", "build", "e2e"], \
        "manifest 必须以真镜像 e2e 为硬前置"
    assert jobs["e2e"]["needs"] == ["prepare", "build"]
    assert not jobs["e2e"].get("continue-on-error"), "e2e 是发布硬门禁，禁软"
    assert not jobs["manifest"].get("continue-on-error")
    # v1.0.1 ACR 主源：push-acr 是发布硬前置（image 指 ACR，ACR 无 tag 即发布=
    # 全体客户安装失败）——透传站 warm-mirrors（best-effort）已随主源退役。
    assert not jobs["push-acr"].get("continue-on-error"), "push-acr 是硬门禁，禁软"
    assert "push-acr" in jobs["release"]["needs"], "release 必须等 ACR 推送成功"
    assert jobs["gitee-release"]["needs"] == ["prepare", "release"]


def test_image_source_acr_strategy():
    """v1.0.1 定案钉桩（v1.0.0 首装卡下载实发）：主源=自有 ACR 单仓多架构，
    ghcr.io 灾备；透传站体系（1ms/nju 预热 job、warm.yaml 手动补热通道）整体
    退役且**禁复活**——域名白名单钉桩在 test_store_schema，本测试守 CI 面与
    文档面：①warm.yaml 不得存在（复活=双分发体系漂移）；②ci.yaml 不得再引用
    透传站；③DOCS FAQ 必须给客户「卡下载→等 ACR 恢复期→改 ghcr.io 灾备」话术
    （灾备仓名 {arch}-huijian-voice 与 ghcr 实仓一致，抄了就能装）。"""
    warm_path = ROOT.parent / ".github" / "workflows" / "warm.yaml"
    assert not warm_path.exists(), "透传站手动补热通道已退役，禁复活"
    ci = (ROOT.parent / ".github" / "workflows" / "ci.yaml").read_text(encoding="utf-8")
    for gone in ("ghcr.1ms.run", "ghcr.nju.edu.cn", "warm-mirrors"):
        assert gone not in ci, f"CI 不得残留透传站体系：{gone}"
    assert "push-acr" in ci and "acr_transcode.py" in ci, "ACR 转码推送链在位"
    assert "buildx imagetools create" not in ci, "imagetools 复制路线已被转码取代（zstd 层 ACR 拒收）"
    docs = (ROOT / "DOCS.md").read_text(encoding="utf-8")
    assert "Downloading docker image" in docs, "FAQ 必须教客户识别卡下载症状"
    assert "ghcr.io/fangwenyi-dev/{arch}-huijian-voice" in docs, \
        "FAQ 灾备换源串必须完整可抄（含 {arch} 模板）"


def test_repo_standard_artifacts():
    """网关仓同款交付面：根五件套 + 商店图标 + e2e 编排脚本 + 镜像国内透传站。"""
    for f in ("README.md", "CHANGELOG.md", "CLAUDE.md", "LICENSE", "repository.yaml"):
        assert (ROOT.parent / f).exists(), f"仓根缺 {f}（网关同款标准）"
    for img in ("icon.png", "logo.png"):
        f = ROOT / img
        assert f.exists() and f.stat().st_size > 1000, f"缺商店图标 {img}"
    for sc in ("run_e2e.sh", "run_local.sh", "e2e_client.py", "e2e_server.py"):
        body = (ROOT / "tests" / "e2e" / sc).read_text(encoding="utf-8")
        assert body.strip(), f"tests/e2e/{sc} 为空"
    assert (ROOT / "tests" / "e2e" / "assets" / "0.wav").stat().st_size > 100_000
    # image 域白名单/{arch} 模板与仓名一致性由 test_store_schema 统一看守
    # （v1.7.17 定案：禁在此类测试硬编码具体镜像主源域名）。


def test_version_stamp_poison_guard():
    """0.0.0（裸 build 烘的占位 ENV）与 dev 都不准作为对外版本穿透；
    boot 版本戳权威源必须是镜像内 version.json（四源一致已被上游钉保护）。"""
    boot = (ROOT / "boot.sh").read_text(encoding="utf-8")
    assert "version.json" in boot and "0.0.0" in boot, \
        "boot.sh 版本戳未走 version.json 权威源/未防 0.0.0 毒值"
    constpy = (ROOT / "core" / "const.py").read_text(encoding="utf-8")
    assert '"0.0.0"' in constpy, "const 缺 0.0.0 毒值过滤"
    from core import const
    import pathlib, tempfile
    with tempfile.TemporaryDirectory() as td:
        orig = const.DATA_DIR
        try:
            const.DATA_DIR = pathlib.Path(td)
            for poison in ("dev", "0.0.0", ""):
                pathlib.Path(td, "version.txt").write_text(poison, encoding="utf-8")
                assert const.addon_version() != poison or poison == ""
                assert re.fullmatch(r"\d+\.\d+\.\d+", const.addon_version())
        finally:
            const.DATA_DIR = orig


def test_atomic_write_public_readable():
    """status/models 事实文件经 _atomic_write 落 nginx 静态根：mkstemp 默认
    0600 会让 www-data 读 403（v1.0.0 CI e2e 实锤），必须 fchmod 0644。"""
    import inspect
    from core import main as m
    src = inspect.getsource(m._atomic_write)
    assert "fchmod" in src and "0o644" in src, "_atomic_write 丢失世界可读 chmod"
    import os as _os, pathlib, stat, tempfile
    if _os.name != "nt":
        with tempfile.TemporaryDirectory() as td:
            tgt = pathlib.Path(td, "status.json")
            m._atomic_write(tgt, "{}")
            assert stat.S_IMODE(tgt.stat().st_mode) & 0o044, "落盘文件 others 不可读"


def test_dockerfile_no_redundant_init_cmd():
    """base ENTRYPOINT 已=/init；Dockerfile 再放 CMD/ENTRYPOINT ["/init"] 会
    变 `/init /init` → s6 v3 legacy services 崩（v1.0.0 CI e2e 实锤）。"""
    txt = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    for ln in txt.splitlines():
        t = ln.strip()
        assert not (t.startswith("CMD") or t.startswith("ENTRYPOINT")) or "/init" not in t, \
            f"禁对 /init 出 CMD/ENTRYPOINT：{t[:60]}"


def test_dockerfile_pre_from_scope_only_args():
    """首个 FROM 前是全局作用域，Docker 只接受 ARG/parser-directive/comment。
    v1.0.0 CI 首炸实证：stage 前放 LABEL → buildx 报 "no build stage in current
    context"，两架构 build job 双红、e2e/manifest/release 全链 skipped。"""
    txt = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^FROM\s", txt, re.M)
    assert m, "Dockerfile 无 FROM？"
    for ln in txt[:m.start()].splitlines():
        t = ln.strip()
        assert not t or t.startswith("#") or re.match(r"^(ARG\s|syntax\s*=|\/\/#)", t), \
            f"FROM 之前出现非法全局指令：{t[:60]}"


def test_www_ingress_relative_and_static_root_clean():
    www = (ROOT / "www" / "index.html").read_text(encoding="utf-8")
    assert "INGRESS_BASE" in www, "www 缺 Ingress 前缀适配（网关同款范式）"
    assert not re.search(r'fetch\(["\']/[^\s"\']+', www), \
        "www 存在根绝对路径 fetch——Ingress 下全部失效"
    main = (ROOT / "core" / "main.py").read_text(encoding="utf-8")
    assert 'NGINX_HTML / "endpoints' not in main, \
        "endpoints.json（含 WS token）禁止写入 nginx 静态根"


def test_repository_manifest_current_spec():
    repo = ROOT.parent
    assert (repo / "repository.yaml").exists(), "商店清单须为 repository.yaml（新规范）"
    assert not (repo / "configuration.yaml").exists(), "旧名 configuration.yaml 须移除"
    doc = yaml.safe_load((repo / "repository.yaml").read_text(encoding="utf-8"))
    for k in ("name", "url", "maintainer"):
        assert doc.get(k), f"repository.yaml 缺 {k}"


def test_version_chain_single_source():
    """三处对外版本（health/mDNS/状态文件）必须走 const.addon_version；
    禁再出现独立 "dev" 字面量兜底（本会话实证过的漂移）。"""
    for f in ("admin_api.py", "main.py"):
        src = (ROOT / "core" / f).read_text(encoding="utf-8")
        assert "addon_version" in src or "_version" in src
        assert ', "dev")' not in src and "return \"dev\"" not in src, \
            f"{f} 存在独立 dev 兜底版本链"
    src = (ROOT / "core" / "const.py").read_text(encoding="utf-8")
    assert "def addon_version" in src
