# -*- coding: utf-8 -*-
"""前端内联 JS 语法守卫（v1.1.30 A12 事故）。

事故：v1.1.30 的 A12（面板 dry_run 复合链）在 `www/index.html` 内联 script 里
插入了一个**跨行双引号字符串** —— JS 规范不允许普通字符串字面量含未转义换行，
整块内联 script 当场 SyntaxError ⇒ 加载项管理面板**所有** JS 不执行
（设置/状态/按钮全死），而当时没有任何测试覆盖前端语法，四轮审计与金标都没拦住。

守卫=用 node --check 逐块解析：
  ① www/*.html 与 custom_components/huijian_ai/templates/*.html 的**内联**
     <script>（带 src 的外链块除外，它们单独查）；
  ② www/js/*.js 本地外链脚本。
node 缺席即 skip（CI=ubuntu-latest 自带 node；本机亦有）。
防假绿：内联块扫到少于 3 个 = 扫描面失效，判红。
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
WWW = HERE / "www"
TEMPLATES = HERE / "custom_components" / "huijian_ai" / "templates"

NODE = shutil.which("node")

_INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def _inline_blocks(path: Path) -> list[str]:
    return _INLINE.findall(path.read_text(encoding="utf-8"))


def _check_js(js: str) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(js)
        tmp = f.name
    return subprocess.run([NODE, "--check", tmp], capture_output=True,
                          encoding="utf-8", errors="replace")


@pytest.mark.skipif(NODE is None, reason="无 node，跳过前端语法守卫")
def test_inline_scripts_parse():
    html_files = sorted(list(WWW.glob("*.html")) + list(TEMPLATES.glob("*.html")))
    assert html_files, "HTML 扫描面失效（一个文件都没找到）"
    n_blocks = 0
    for f in html_files:
        for i, js in enumerate(_inline_blocks(f)):
            if not js.strip():
                continue
            n_blocks += 1
            r = _check_js(js)
            assert r.returncode == 0, (
                f"{f.name} 内联块{i} JS 语法错误（该文件整块脚本不执行=页面全死）：\n"
                f"{r.stderr[:600]}"
            )
    assert n_blocks >= 3, f"内联块只扫到 {n_blocks} 个——扫描面疑似失效（防假绿）"


@pytest.mark.skipif(NODE is None, reason="无 node，跳过前端语法守卫")
def test_local_js_assets_parse():
    files = sorted(WWW.glob("js/*.js"))
    assert files, "www/js 扫描面失效"
    for f in files:
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True,
                           encoding="utf-8", errors="replace")
        assert r.returncode == 0, f"{f.name} JS 语法错误：\n{r.stderr[:400]}"
