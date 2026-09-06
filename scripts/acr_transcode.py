#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ghcr → 阿里云 ACR 镜像转码推送器（v1.0.1 定案，push-acr run1/run2 教训）.

根因（本地实证 2026-09-06）：hassio builder 以 buildx 默认压缩推 ghcr，
层 mediaType=application/vnd.oci.image.layer.v1.tar+zstd；ACR 个人版对
非 gzip 层流在上传阶段即拒（PATCH 403 → 前端表现为 PUT "unknown: blob
type invalid"）。鉴别实验：同一 PUT 通道下 真gzip流=202 / zstd魔数=403 /
纯字节=403 —— ACR 校验的是层内容 gzip 帧本身，不只是 mediaType 字符串。
`imagetools create` 按 digest 复制原 blob 无法过此关，故必须转码：
下载 zstd 层 → 解压 → 重压缩 gzip（mtime=0 确定性）→ 重写 manifest
（层 mediaType 换 tar+gzip，config/diff_ids 不动）→ 推送。

用法（凭据只从环境变量 ACR_USER / ACR_PASS 读，绝不落参数/日志）：
  # 转码单架构子镜像：src 指定 manifest digest，推成 dst 仓的 tag
  python3 acr_transcode.py transcode \
      --src ghcr.io --src-repo OWNER/amd64-huijian-voice \
      --src-ref sha256:... --dst REG --dst-repo NS/repo --dst-tag 1.0.1-amd64
  # 无 docker 环境下合成多架构 index（CI 用 imagetools，二者等价）
  python3 acr_transcode.py index --dst REG --dst-repo NS/repo \
      --tag 1.0.1 --member amd64=sha256:... --member arm64=sha256:...

正确性硬约束：解压后的层 sha256 必须逐条等于 config.diff_ids（镜像合法性
的定义），不等立即中止——保证转码只换压缩容器、不动文件系统内容。
"""
import argparse
import base64
import gzip
import hashlib
import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b"
MEDIA_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_INDEX = "application/vnd.oci.image.index.v1+json"
MEDIA_CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
CHUNK = 8 * 1024 * 1024
UA = {"User-Agent": "acr-transcode/1.0 (huijian_voice CI)"}


def _cred(env_name, file_path):
    """CI：Secrets→env。本地：~/.acr_user / ~/.acr_pass（600 权限凭证文件，
    绝不入库）——WSL→Windows exe 桥不透传 env，本地实证必须走 WSL python3 +
    文件路径（2026-09-06 实发）。"""
    v = os.environ.get(env_name, "")
    if v:
        return v
    try:
        with open(os.path.expanduser(file_path), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def log(msg):
    print(f"[acr-transcode] {msg}", flush=True)


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def http_get(url, headers=None, timeout=60, binary=False):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    resp = urllib.request.urlopen(req, timeout=timeout)
    data = resp.read()
    return (data if binary else data.decode("utf-8", "replace")), dict(resp.headers)


def zstd_decompress(data: bytes) -> bytes:
    try:
        import zstandard
    except ImportError as e:
        raise SystemExit("需要 zstandard 库: pip install zstandard（CI step 已自动装）") from e
    dctx = zstandard.ZstdDecompressor()
    return dctx.stream_reader(__import__("io").BytesIO(data)).read()


# ────────────────────────── registry 认证面 ──────────────────────────

class Registry:
    """token 链全动态发现（realm/service 都从 401 头拿，不硬编码——防
    ACR 端点漂移臆造；对 ghcr 同样成立）。"""

    def __init__(self, host, user=None, password=None):
        self.host = host
        self.user = user
        self.password = password
        self._realm = None
        self._service = None
        self._tokens = {}

    def _challenge(self):
        if self._realm:
            return
        try:
            http_get(f"https://{self.host}/v2/", timeout=15)
            realm, service = f"https://{self.host}/v2/token", self.host
        except urllib.error.HTTPError as e:
            auth = e.headers.get("Www-Authenticate") or e.headers.get("WWW-Authenticate") or ""
            m = re.search(r'realm="([^"]+)"', auth)
            if not m:
                raise RuntimeError(f"{self.host}: 401 无 realm（auth头={auth!r}）")
            s = re.search(r'service="([^"]+)"', auth)
            realm, service = m.group(1), (s.group(1) if s else self.host)
        self._realm, self._service = realm, service

    def token(self, repo, actions):
        self._challenge()
        scope = f"repository:{repo}:{','.join(actions)}"
        key = scope
        if key in self._tokens:
            return self._tokens[key]
        hdr = {}
        if self.user:
            b = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            hdr["Authorization"] = "Basic " + b
        url = f"{self._realm}?service={urllib.parse.quote(self._service)}&scope={urllib.parse.quote(scope)}"
        body, _ = http_get(url, hdr, timeout=20)
        tok = json.loads(body)
        t = tok.get("token") or tok.get("access_token")
        if not t:
            raise RuntimeError(f"{self.host}: token 交换失败（repo={repo}）")
        self._tokens[key] = t
        return t

    def get(self, path, repo, actions=("pull",), headers=None, binary=False, timeout=60):
        t = self.token(repo, list(actions))
        h = {"Authorization": "Bearer " + t}
        h.update(headers or {})
        return http_get(f"https://{self.host}{path}", h, timeout=timeout, binary=binary)

    def _conn(self, timeout=120):
        return http.client.HTTPSConnection(self.host, timeout=timeout)

    def request(self, method, path, repo, body: bytes, headers=None,
                actions=("pull", "push"), status_ok=(200, 201, 202, 204)):
        t = self.token(repo, list(actions))
        h = {"Authorization": "Bearer " + t}
        h.update(headers or {})
        c = self._conn()
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        loc = r.getheader("Location")
        c.close()
        if r.status not in status_ok:
            raise RuntimeError(f"{method} {path} → {r.status} {data[:200].decode('utf-8','replace')}")
        return r.status, loc, data


# ────────────────────────── blob 上传（分块 PATCH 流，ACR 已实证） ──────────────────────────

def push_blob(dst: Registry, repo: str, digest: str, data: bytes, label: str):
    # 秒传：HEAD 命中直接跳（双 tag 复用同层时省 130MB 上传）
    try:
        dst.get(f"/v2/{repo}/blobs/{digest}", repo, ("pull",), binary=True, timeout=20)
        log(f"  blob 已存在（秒传）: {label}")
        return
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    _, loc, _ = dst.request("POST", f"/v2/{repo}/blobs/uploads/", repo, b"",
                            headers={"Content-Length": "0"})
    if not loc:
        raise RuntimeError("ACR 未返回 upload Location")
    offset = 0
    use = loc
    while True:
        part = data[offset:offset + CHUNK]
        if not part and offset:
            break
        if part:
            sep = "&" if "?" in use else "?"
            _, nloc, _ = dst.request(
                "PATCH", use + sep + "stage=1", repo, part,
                headers={"Content-Type": "application/octet-stream",
                         "Content-Range": f"{offset}-{offset + len(part) - 1}",
                         "Content-Length": str(len(part))},
                status_ok=(202, 200))
            offset += len(part)
            use = nloc or use
            log(f"  ↑ {label}: {offset}/{len(data)}B")
        if offset >= len(data):
            break
    sep = "&" if "?" in use else "?"
    dst.request("PUT", use + sep + f"digest={digest}", repo, b"",
                headers={"Content-Length": "0"})
    log(f"  blob 完成: {label} ({len(data)}B)")


def push_manifest(dst: Registry, repo: str, ref: str, manifest: bytes, media_type: str):
    dst.request("PUT", f"/v2/{repo}/manifests/{ref}", repo, manifest,
                headers={"Content-Type": media_type, "Content-Length": str(len(manifest))})
    log(f"  manifest 已推: {repo}@{ref[:24]}")
    return sha256_hex(manifest)


# ────────────────────────── transcode ──────────────────────────

def cmd_transcode(a):
    src = Registry(a.src)
    dst_user = _cred("ACR_USER", "~/.acr_user")
    dst_pass = _cred("ACR_PASS", "~/.acr_pass")
    if not dst_user or not dst_pass:
        sys.exit("ACR 凭据缺失：env ACR_USER/ACR_PASS（CI 用）或家目录 ~/.acr_user / ~/.acr_pass（本地用）")
    dst = Registry(a.dst, dst_user, dst_pass)

    body, _ = src.get(f"/v2/{a.src_repo}/manifests/{a.src_ref}", a.src_repo,
                      headers={"Accept": f"{MEDIA_MANIFEST},{MEDIA_CONFIG}"})
    man = json.loads(body)
    assert man.get("schemaVersion") == 2 and "layers" in man, "源不是 OCI image manifest（勿指 index digest）"
    cfg_dgst = man["config"]["digest"]
    cfg_bytes, _ = src.get(f"/v2/{a.src_repo}/blobs/{cfg_dgst}", a.src_repo, binary=True, timeout=180)
    cfg = json.loads(cfg_bytes)
    diff_ids = cfg["rootfs"]["diff_ids"]
    assert len(diff_ids) == len(man["layers"]), "diff_ids 与层数不符"

    log(f"源 {a.src_repo}@{a.src_ref[:19]}: {len(man['layers'])} 层，config {len(cfg_bytes)}B")
    new_layers = []
    for i, (layer, want) in enumerate(zip(man["layers"], diff_ids)):
        raw, _ = src.get(f"/v2/{a.src_repo}/blobs/{layer['digest']}", a.src_repo,
                         binary=True, timeout=600)
        mt = layer.get("mediaType", "")
        if mt.endswith("+zstd") or raw[:4] == ZSTD_MAGIC:
            plain = zstd_decompress(raw)
            data = gzip.compress(plain, mtime=0)   # 确定性重压缩
            got = sha256_hex(plain)
            if got != want:
                sys.exit(f"层{i} 解压 sha256={got[:20]}… ≠ config.diff_ids {want[:20]}…（层损坏/非标准帧）")
            media = LAYER_GZIP
        elif mt.endswith("+gzip") or raw[:2] == GZIP_MAGIC:
            data, media = raw, layer.get("mediaType") or "application/vnd.oci.image.layer.v1.tar+gzip"
        else:
            sys.exit(f"层{i} 未知压缩形态 mediaType={mt!r}，拒绝盲转")
        ann = layer.get("annotations")
        nl = {"mediaType": media, "digest": sha256_hex(data), "size": len(data)}
        if ann:
            nl["annotations"] = ann
        new_layers.append(nl)
        push_blob(dst, a.dst_repo, nl["digest"], data, f"layer{i} {len(raw)//2**20}MB→{len(data)//2**20}MB")
        del raw, data
    push_blob(dst, a.dst_repo, cfg_dgst, cfg_bytes, "config")
    nm = {"schemaVersion": 2, "mediaType": MEDIA_MANIFEST,
          "config": dict(man["config"]), "layers": new_layers}
    if "annotations" in man:
        nm["annotations"] = man["annotations"]
    digest = push_manifest(dst, a.dst_repo, a.dst_tag,
                           json.dumps(nm, separators=(",", ":")).encode(), MEDIA_MANIFEST)
    print(digest)  # stdout 最后一行=新 manifest digest（CI 拿它拼 index）
    log(f"✅ 转码完成 {a.dst}/{a.dst_repo}:{a.dst_tag} → {digest}")


def cmd_index(a):
    dst = Registry(a.dst, _cred("ACR_USER", "~/.acr_user"), _cred("ACR_PASS", "~/.acr_pass"))
    members = []
    for m in a.member:
        arch, dg = m.split("=", 1)
        plat = {"amd64": "linux/amd64", "arm64": "linux/arm64"}[arch]
        os_, arch_ = plat.split("/")
        body, hdr = dst.get(f"/v2/{a.dst_repo}/manifests/{dg}", a.dst_repo,
                            headers={"Accept": MEDIA_MANIFEST})
        members.append({"mediaType": MEDIA_MANIFEST, "digest": dg,
                        "size": int(hdr.get("Content-Length", len(body))),
                        "platform": {"architecture": arch_, "os": os_}})
    idx = json.dumps({"schemaVersion": 2, "mediaType": MEDIA_INDEX,
                      "manifests": members}, separators=(",", ":")).encode()
    digest = push_manifest(dst, a.dst_repo, a.tag, idx, MEDIA_INDEX)
    print(digest)
    log(f"✅ index 已合成 {a.dst}/{a.dst_repo}:{a.tag} → {digest}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("transcode")
    t.add_argument("--src", required=True); t.add_argument("--src-repo", required=True)
    t.add_argument("--src-ref", required=True)
    t.add_argument("--dst", required=True); t.add_argument("--dst-repo", required=True)
    t.add_argument("--dst-tag", required=True)
    t.set_defaults(fn=cmd_transcode)
    x = sub.add_parser("index")
    x.add_argument("--dst", required=True); x.add_argument("--dst-repo", required=True)
    x.add_argument("--tag", required=True); x.add_argument("--member", action="append", required=True)
    x.set_defaults(fn=cmd_index)
    a = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a.fn(a)


if __name__ == "__main__":
    main()
