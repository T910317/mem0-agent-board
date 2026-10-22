"""公告欄 POST 與審核清單上傳：超過上限要回 413，而且不先整包讀進記憶體。"""
import server
from starlette.testclient import TestClient

client = TestClient(server.app)


def test_login_rejects_huge_body_by_content_length():
    r = client.post("/board/login", content=b"u=a&p=" + b"x" * (server.BOARD_BODY_MAX + 10),
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_login_rejects_huge_chunked_body():
    def gen():
        for _ in range(40):
            yield b"x" * 65536  # 共 2.5MB，沒有 Content-Length

    r = client.post("/board/login", content=gen(), headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413


def test_login_still_works_after_body_is_pre_read():
    server._login_fails.clear()
    r = client.post("/board/login", data={"u": "tester", "p": "wrong-password"}, follow_redirects=False)
    assert r.status_code == 401
    server._login_fails.clear()
    r = client.post("/board/login", data={"u": "tester", "p": "test-board-password-123"}, follow_redirects=False)
    assert r.status_code == 303 and server.COOKIE in r.cookies


def test_review_upload_rejects_huge_body():
    tok = "test-token-claude-0123456789abcdef0123456789"

    def gen():
        for _ in range(140):
            yield b"x" * 65536  # 約 9MB

    r = client.post("/board/api/review/upload", content=gen(), headers={"authorization": "Bearer " + tok})
    assert r.status_code == 413


def test_embedding_cache_is_bounded(monkeypatch):
    calls = []

    class Resp:
        def __init__(self, n):
            self.n = n

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"embedding": [0.0]} for _ in range(self.n)]}

    def fake_post(url, json, timeout):
        calls.append(len(json["input"]))
        return Resp(len(json["input"]))

    monkeypatch.setattr(server.httpx, "post", fake_post)
    server._emb_cache.clear()
    for i in range(server.EMB_CACHE_MAX + 50):
        assert server._embed(["desc-a", "desc-b", f"query-{i}"]) == [[0.0]] * 3
    assert len(server._emb_cache) <= server.EMB_CACHE_MAX
