#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发版镜像探活器（v1.0.15 引入，实发教训驱动）。

两个实发背景：
  1. 商店竞态：main 一推，商店立刻可见新版本号，但镜像要等 CI 后段
     （e2e→manifest→push-acr，~15min）才到 ACR。窗口内客户点「更新」
     只得到 Supervisor 的笼统 unknown error。本脚本给发布方一个
     「镜像出厂了吗」的确定性回答（也适用于客户侧排障自证）。
  2. 看门狗教训（v1.0.13 实发）：manifest 探针 Accept 头必须带全
     （oci.index + docker.list + docker.manifest + oci.manifest），
     缺了对真实 OCI index 会假 404——本脚本钉死这个姿势，勿改。

用法：
  python3 scripts/verify_release.py            # 版本取 config.yaml 当前值
  python3 scripts/verify_release.py 1.0.15     # 显式版本
ACR 是公开仓，匿名 pull token 即可；无需任何凭据文件。
退出码：0=双源 GO；1=主源 ACR 未就位；2=仅 ghcr 缺（ACR 可用，可发但灾备
降级，DOCS 灾备换源步骤暂不可用）。
"""
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Windows 默认 GBK 控制台会把中文输出成乱码（实测 v1.0.15 首跑）
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# 全 Accept 头（v1.0.13 教训钉桩——顺序照 docker 官方兼容清单）
ACCEPT = ",".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])
CTX = ssl.create_default_context()
REG = "crpi-92gcrmsz8v7vvdy1.cn-shanghai.personal.cr.aliyuncs.com"
ACR_REPO = "fangwenyi-dev/huijian-gateway-plugin-yy"
GHCR_REPO = "fangwenyi-dev/huijian-voice"


def _get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        r = urllib.request.urlopen(req, timeout=timeout, context=CTX)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode()


def _token(host, repo, service_hint=None):
    """走 401 challenge（或缺省 realm）拿匿名 pull token。"""
    st, _ = _get(f"https://{host}/v2/{repo}/manifests/probe")
    # ACR 的 realm 不在本机网络可达路径给 challenge，两处已知 realm 兜底
    realms = [f"https://dockerauth.cn-hangzhou.aliyuncs.com/auth",
              "https://ghcr.io/token"]
    svc = ("registry.aliyuncs.com:cn-shanghai:26842" if "aliyuncs" in host
           else "ghcr.io")
    realm = realms[0] if "aliyuncs" in host else realms[1]
    q = urllib.parse.urlencode({"service": svc,
                                "scope": f"repository:{repo}:pull"})
    st, body = _get(f"{realm}?{q}")
    if st != 200:
        return None
    try:
        return json.loads(body).get("token")
    except json.JSONDecodeError:
        return None


def check(host, repo, tag):
    """index → 每个成员 manifest → 抽 1 个 blob。返回 (ok, detail)。"""
    tok = _token(host, repo)
    if not tok:
        return False, "token 获取失败（registry 不可达？）"
    h = {"Authorization": f"Bearer {tok}", "Accept": ACCEPT}
    st, body = _get(f"https://{host}/v2/{repo}/manifests/{tag}", h)
    if st != 200:
        return False, f"tag {tag} http={st} {body[:80]!r}"
    idx = json.loads(body)
    members = idx.get("manifests")
    if members is None:  # 单架构 manifest 直挂 tag
        members = [{"digest": tag, "platform": {"architecture": "-"}}]
    for m in members:
        arch = m.get("platform", {}).get("architecture", "?")
        hm = dict(h, Accept="application/vnd.oci.image.manifest.v1+json,"
                            "application/vnd.docker.distribution.manifest.v2+json")
        st, mb = _get(f"https://{host}/v2/{repo}/manifests/{m['digest']}", hm)
        if st != 200:
            return False, f"{arch} 成员 manifest http={st}"
        man = json.loads(mb)
        layers = man.get("layers") or []
        if not layers:
            return False, f"{arch} manifest 无层"
        st, _b = _get(f"https://{host}/v2/{repo}/blobs/{layers[-1]['digest']}",
                      h, timeout=45)
        if st not in (200, 206, 307, 301, 302):
            return False, f"{arch} 尾层 blob http={st}"
    return True, f"{len(members)} 架构 index+成员+尾层 blob 全绿"


def main():
    args = [a for a in sys.argv[1:]]
    if args:
        ver = args[0]
    else:
        cfg = Path(__file__).resolve().parents[1] / "huijian_voice" / "config.yaml"
        m = re.search(r'^version:\s*"?([\d.]+)"?', cfg.read_text(encoding="utf-8"), re.M)
        if not m:
            print("NO-GO: 读不到 config.yaml version")
            return 3
        ver = m.group(1)
    print(f"probe version {ver}")
    ok_a, det_a = check(REG, ACR_REPO, ver)
    print(f"ACR (主源)  : {'OK ' if ok_a else 'MISS'} {det_a}")
    ok_g, det_g = check("ghcr.io", GHCR_REPO, ver)
    print(f"ghcr (灾备) : {'OK ' if ok_g else 'MISS'} {det_g}")
    if not ok_a:
        print("NO-GO: 主源未就位——商店可见但客户点更新必报 unknown error，勿推 gitee")
        return 1
    if not ok_g:
        print("GO(降级): ACR 就位可发；ghcr 灾备缺，DOCS 灾备换源步骤暂不可用")
        return 2
    print("GO: 双源就位，客户点「更新」安全")
    return 0


if __name__ == "__main__":
    sys.exit(main())
