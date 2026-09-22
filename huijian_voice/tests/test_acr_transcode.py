"""v1.1.5 ACR 发布脚本钉：Registry.request 重试/401 换 token、push_blob 换 session。

背景（三连 run 实证的三种死法，全部要有本地钉——不许再拿 CI 当测试机）：
- run 35626978134/35675256894：块写满超时（TimeoutError/SSLEOF）→ request 级重试；
- run 35676700243：layer4 推到 97% 被掐后，**缓存 token 已过期**，新 session 的
  POST 全 401 → 每次重试必须 fresh=True 重取 token；
- session 被服务端掐掉后复用旧 Location 秒败 → push_blob 整块级重启（新 session
  从 0 重传，最多 3 轮）。
"""
import sys
import urllib.error
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT.parent / "scripts"))

import acr_transcode as ac  # noqa: E402

ac.CHUNK = 4   # 本文件全部上传用例按 4B 块走 2 块节奏（模块级一次性，勿在用例内改）

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """退避 sleep 在测试里全部短路（穷尽用例真睡要 155s）。"""
    monkeypatch.setattr(ac.time, "sleep", lambda s: None)


class FakeResp:
    def __init__(self, status, body=b"", loc=None):
        self.status, self._body, self._loc = status, body, loc

    def read(self):
        return self._body

    def getheader(self, name, default=None):
        return self._loc if name == "Location" else default


class FakeConn:
    """按脚本队列吐响应；'EIO' 项表示该次 request 抛连接级异常。"""

    def __init__(self, script):
        self.script = list(script)
        self.reqs = []

    def request(self, method, path, body=None, headers=None):
        self.reqs.append((method, path, dict(headers or {})))
        if self.script and self.script[0] == "EIO":
            self.script.pop(0)
            raise TimeoutError("The write operation timed out")

    def getresponse(self):
        return self.script.pop(0)

    def close(self):
        pass


def _reg(script):
    r = ac.Registry("cr.example")
    r._realm, r._service = "https://cr.example/token", "cr.example"
    r._tokens = {}
    fresh_calls = []

    def fake_token(repo, actions, fresh=False):
        fresh_calls.append(fresh)
        return "TOK%d" % len(fresh_calls)
    r.token = fake_token
    conn = FakeConn(script)
    r._conn = lambda timeout=300: conn
    return r, conn, fresh_calls


# ── request：重试与 token 刷新 ─────────────────────────────────

def test_request_success_first_try_uses_cached_token_path():
    r, conn, fresh = _reg([FakeResp(202, loc="/uploads/abc")])
    st, loc, _ = r.request("POST", "/v2/x/blobs/uploads/", "x", b"",
                           headers={"Content-Length": "0"})
    assert (st, loc) == (202, "/uploads/abc")
    assert fresh == [False], "首试不必重取 token"
    assert conn.reqs[0][2]["Authorization"] == "Bearer TOK1"


def test_request_io_error_retry_refreshes_token_and_succeeds():
    r, conn, fresh = _reg(["EIO", FakeResp(202, loc="/u/2")])
    st, loc, _ = r.request("PATCH", "/u/2?stage=1", "x", b"chunk",
                           headers={"Content-Type": "application/octet-stream"})
    assert (st, loc) == (202, "/u/2")
    assert fresh == [False, True], "第二次必须 fresh=True 重取"
    assert conn.reqs[-1][2]["Authorization"] == "Bearer TOK2"


def test_request_401_then_refresh_succeeds():
    # run 35678680808 主签名：POST 新 session 拿 401（token 过期）→ 重取后成功
    r, conn, fresh = _reg([FakeResp(401, b'{"errors":[{"code":"UNAUTHORIZED"}]}'),
                           FakeResp(202, loc="/u/new")])
    st, loc, _ = r.request("POST", "/v2/x/blobs/uploads/", "x", b"")
    assert (st, loc) == (202, "/u/new")
    assert fresh == [False, True]


def test_request_persistent_401_raises_not_silent():
    r, _, fresh = _reg([FakeResp(401, b"nope"), FakeResp(401, b"nope"),
                        FakeResp(401, b"nope"), FakeResp(401, b"nope"),
                        FakeResp(401, b"nope"), FakeResp(401, b"nope")])
    try:
        r.request("POST", "/v2/x/blobs/uploads/", "x", b"")
        raise AssertionError("持续 401 必须抛")
    except RuntimeError as e:
        assert "401" in str(e)


def test_request_status_error_no_retry():
    r, conn, _ = _reg([FakeResp(404, b"missing")])
    try:
        r.request("GET", "/v2/x/whatever", "x", b"")
        raise AssertionError("非 401 状态错必须抛")
    except RuntimeError as e:
        assert "404" in str(e)
    assert len(conn.reqs) == 1, "语义错不得重试"


def test_request_exhausts_and_raises():
    r, _, _ = _reg(["EIO"] * 6)
    try:
        r.request("PATCH", "/u/x", "x", b"z")
        raise AssertionError("重试穷尽必须抛")
    except RuntimeError as e:
        assert "重试 6 次" in str(e)


# ── push_blob：秒传 / 换 session 重启 ──────────────────────────

class _Store:
    def __init__(self, get_raises=None):
        self.get_raises = get_raises
        self.calls = []
        self.conn = None

    def make(self, script):
        self.conn = FakeConn(script)
        return self.conn

    def request(self, method, path, repo, body, headers=None,
                actions=("pull", "push"), status_ok=(200, 201, 202, 204),
                attempts=6):
        self.calls.append((method, path))
        if self.conn.script and self.conn.script[0] == "EIO":
            self.conn.script.pop(0)
            raise RuntimeError("PATCH /u 重试 6 次仍失败：boom")
        r = self.conn.script.pop(0)
        if r.status not in status_ok:
            raise RuntimeError(f"{method} {path} → {r.status}")
        return r.status, r.getheader("Location"), r.read()


def test_push_blob_instant_skip_no_upload():
    s = _Store()
    s.get = lambda path, repo, actions=("pull",), **kw: (b"", {})
    ac.push_blob(s, "x", "sha256:d", b"data", "layer0")
    assert s.calls == [], "秒传命中不得走上传"


def test_push_blob_instant_skip_only_on_404():
    s = _Store()
    s.get = lambda path, repo, actions=("pull",), **kw: (_ for _ in ()).throw(
        urllib.error.HTTPError(path, 500, "boom", {}, None))
    try:
        ac.push_blob(s, "x", "sha256:d", b"data", "layer0")
        raise AssertionError("非 404 的 HEAD 异常必须原样抛")
    except urllib.error.HTTPError as e:
        assert e.code == 500


def test_push_blob_happy_path_chunk_then_finalize():
    s = _Store()
    s.get = lambda path, repo, actions=("pull",), **kw: (_ for _ in ()).throw(
        urllib.error.HTTPError(path, 404, "nf", {}, None))
    s.conn = FakeConn([
        FakeResp(202, loc="/u/1"),                       # POST session
        FakeResp(202, loc="/u/1"), FakeResp(202, loc="/u/1"),  # 2×PATCH(8B/4B)
        FakeResp(201),                                    # PUT finalize
    ])
    ac.push_blob(s, "x", "sha256:d", b"abcdefgh", "layer9")
    assert [m for m, _ in s.calls] == ["POST", "PATCH", "PATCH", "PUT"]
    assert "digest=sha256:d" in s.calls[-1][1]


def test_push_blob_session_death_restarts_with_new_session():
    s = _Store()
    s.get = lambda path, repo, actions=("pull",), **kw: (_ for _ in ()).throw(
        urllib.error.HTTPError(path, 404, "nf", {}, None))
    # 第一轮：POST ok → PATCH ok → 第二轮 PATCH 死（重试穷尽）；
    # 重启轮：POST 新 session → 2×PATCH → PUT 成功。
    s.conn = FakeConn([
        FakeResp(202, loc="/u/old"), FakeResp(202, loc="/u/old"), "EIO",
        FakeResp(202, loc="/u/new"), FakeResp(202, loc="/u/new"),
        FakeResp(202, loc="/u/new"), FakeResp(201),
    ])
    ac.push_blob(s, "x", "sha256:d", b"abcdefgh", "layer4")
    posts = [p for m, p in s.calls if m == "POST"]
    assert len(posts) == 2, "整块失败必须重开 session"
    patches = [p for m, p in s.calls if m == "PATCH"]
    assert all("/u/new" in p for p in patches[2:]), "重启轮 PATCH 走新 session"
    assert s.calls[-1][0] == "PUT"
