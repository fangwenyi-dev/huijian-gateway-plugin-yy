# -*- coding: utf-8 -*-
"""商店契约钉桩（从网关仓 test_v1716_store_schema.py 移植，v1.7.16 事故同源防线）。

schema/watchdog 必须逐字通过上游 Supervisor 校验正则——商店刷新时整份
config.yaml 校验失败会被静默跳过（无前端报错），新装客户「找不到卡片」。
下方两条正则逐字抄自 home-assistant/supervisor apps/options.py::
RE_SCHEMA_ELEMENT 与 apps/validate.py watchdog 分支（网关原件有完整事故注释）。
上游演进语法时同步本钉并复核，禁止给 config.yaml 塞回 `=`。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CFG = (ROOT / "config.yaml").read_text(encoding="utf-8")

# —— 逐字抄自 home-assistant/supervisor apps/options.py::RE_SCHEMA_ELEMENT
#    (2026.05.0 时代同物在 addons/options.py，两代一致) ——
RE_SCHEMA_ELEMENT = re.compile(
    r"^(?:"
    r"|bool"
    r"|email"
    r"|url"
    r"|port"
    r"|device(?:\((?P<filter>subsystem=[a-z]+)\))?"
    r"|str(?:\((?P<s_min>\d+)?,(?P<s_max>\d+)?\))?"
    r"|password(?:\((?P<p_min>\d+)?,(?P<p_max>\d+)?\))?"
    r"|int(?:\((?P<i_min>-?\d+)?,(?P<i_max>-?\d+)?\))?"
    r"|float(?:\((?P<f_min>-?\d*\.?\d+)?,(?P<f_max>-?\d*\.?\d+)?\))?"
    r"|match\((?P<match>.*)\)"
    r"|list\((?P<list>.+)\)"
    r")\??$"
)

# —— 逐字抄自 supervisor/apps/validate.py::_SCHEMA_APP_CONFIG["watchdog"] ——
RE_WATCHDOG = re.compile(
    r"^(?:https?|\[PROTO:\w+\]|tcp):\/\/\[HOST\]:(\[PORT:\d+\]|\d+).*$"
)


def _block(name):
    """顶层键块（到下一个顶格键为止），剔除注释与空行后的 (键, 值) 对。"""
    m = re.search(rf"^{name}:\n(.*?)(?=^\S|\Z)", CFG, re.M | re.S)
    assert m, f"config.yaml {name}: 块锚丢失"
    pairs = []
    for line in m.group(1).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        km = re.match(r"\s*(\w+):\s*(.+?)\s*$", line)
        if km:
            pairs.append((km.group(1), km.group(2)))
    return pairs


def test_every_schema_element_passes_supervisor_regex():
    """每个 schema 类型串必须匹配上游 RE_SCHEMA_ELEMENT——
    否则商店刷新整份 config.yaml 被拒、加载项静默消失（2026-09-06 事故）。"""
    pairs = _block("schema")
    assert pairs, "schema 块解析为空？"
    bad = [(k, t) for k, t in pairs if not RE_SCHEMA_ELEMENT.match(t)]
    assert not bad, (
        f"schema 值不是 Supervisor 合法类型 token（商店会静默跳过本加载项）: {bad}"
        " ——默认值请放 options: 块，勿在类型上挂 `=`"
    )


def test_no_equals_default_notation_anywhere_in_schema():
    """事故形态点名防回潮：`=值` 语法在上游任何版本都不存在。"""
    for k, t in _block("schema"):
        assert "=" not in t, f"schema {k}: `{t}` 复发 `=默认值` 假语法"


def test_required_schema_keys_have_options_defaults():
    """非 `?`（必填）schema 键必须在 options: 有默认值——
    这才是 Supervisor 新装零配置的唯一供给途径（v1.7.12 误以为 `=` 可行）。"""
    options_keys = {k for k, _ in _block("options")}
    missing = [k for k, t in _block("schema")
               if not t.endswith("?") and k not in options_keys]
    assert not missing, f"必填 schema 键缺 options 默认值（新装将零配置破功）: {missing}"


def test_watchdog_matches_upstream_regex():
    """watchdog 必须匹配官方 RE_WATCHDOG（tcp 协议 + [HOST] 占位为强制形态）。"""
    m = re.search(r"^watchdog:\s*(.+?)\s*$", CFG, re.M)
    assert m, "watchdog 键锚丢失"
    assert RE_WATCHDOG.match(m.group(1)), \
        f"watchdog `{m.group(1)}` 不符合上游商店校验正则——加载项会被拒收"


# —— v1.0.1 定案（v1.0.0 首装卡下载实发）：主源=用户自有阿里云 ACR 个人版
# （上海地域公开仓，国内原生线路；单仓多架构 manifest，docker 自动选架构，
# 故不再用 {arch} 仓名模板）。ghcr.io 保留灾备（FAQ 换源话术）。1ms/nju 透传
# 站退役——实测新 tag blob 33KB/s 慢滴/0B 假活，边缘覆盖靠运气。
# 与网关旧规矩「禁单值断言」的改道理由：当年主源是随时可能换的公共透传站，
# 如今主源是自有仓（域名即资产），单值钉死正是防"手滑改回透传站"的防线。
_ACR_HOST = "crpi-92gcrmsz8v7vvdy1.cn-shanghai.personal.cr.aliyuncs.com"
_IMAGE_WHITELIST = (_ACR_HOST, "ghcr.io")


def test_image_field_domain_whitelist_and_template():
    m = re.search(r"^image:\s*(\S+)", CFG, re.M)
    assert m, "image 键锚丢失"
    img = m.group(1)
    host = img.split("/")[0]
    assert host in _IMAGE_WHITELIST, f"镜像域 {host} 不在白名单 {list(_IMAGE_WHITELIST)}"
    assert host == _ACR_HOST, "主源必须是自有 ACR（透传站已退役，禁回退）"
    assert "{arch}" not in img, "ACR 路线=单仓多架构 index，image 不该再有 {arch}"
    assert img == f"{_ACR_HOST}/fangwenyi-dev/huijian-gateway-plugin-yy", \
        "ACR 路径必须与用户控制台实建仓逐字一致（空仓 push 前 registry 不认，勿改着玩）"


def test_homepage_and_repo_name_aligned():
    """config.yaml url / repository.yaml url 与本仓实名一致（防双远端改名漂移）。"""
    m = re.search(r"^url:\s*'?([^'\n]+)'?", CFG, re.M)
    assert m and m.group(1).strip().endswith("huijian-gateway-plugin-yy")
    import yaml
    repo = yaml.safe_load((ROOT.parent / "repository.yaml").read_text(encoding="utf-8"))
    assert repo["url"].strip("/").endswith("huijian-gateway-plugin-yy")
