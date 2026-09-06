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
    m2 = re.search(r'HUIJIAN_VERSION",\s*"([\d.]+)"', constpy)
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
    assert "huijian-voice" in cfg["image"]
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
                         "warm-mirrors", "release", "gitee-release"}, "九 job 链形变"
    assert jobs["manifest"]["needs"] == ["prepare", "init", "build", "e2e"], \
        "manifest 必须以真镜像 e2e 为硬前置"
    assert jobs["e2e"]["needs"] == ["prepare", "build"]
    assert not jobs["e2e"].get("continue-on-error"), "e2e 是发布硬门禁，禁软"
    assert not jobs["manifest"].get("continue-on-error")
    assert jobs["warm-mirrors"].get("continue-on-error") is True, "预热是 best-effort"
    assert jobs["gitee-release"]["needs"] == ["prepare", "release"]


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
    cfg = _config()
    assert cfg["image"] == "ghcr.1ms.run/fangwenyi-dev/{arch}-huijian-voice", \
        "商店 image 须指国内透传站（网关定案；CI 推源站+预热，源码注释有排障说明）"
    assert cfg["url"].endswith("ha-voice-plugin")
    repo = yaml.safe_load((ROOT.parent / "repository.yaml").read_text(encoding="utf-8"))
    assert repo["url"].endswith("ha-voice-plugin"), "仓库清单 URL 与本仓名漂移"


def test_schema_documented_grammar_only():
    """schema 全部落在官方文档列举文法内（str/int(range)/list(a|b)/bool）；
    未文档化的 `=` 默认值写法禁再出现（默认放 options 块）。"""
    cfg = _config()
    ok = re.compile(r'^(bool|str|password|port|email|url|float|int'
                    r'(\(\s*\d+\s*(,\s*\d+\s*)?\))?|list\([^|)]+\|[^)]+\)|'
                    r'str\?\??)$')
    for k, v in cfg["schema"].items():
        assert ok.match(str(v)), f"schema.{k} = {v!r} 非文档化语法"
        assert "=" not in str(v)


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
