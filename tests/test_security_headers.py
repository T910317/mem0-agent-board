"""每個 HTTP 回應都要帶防嵌入與 nosniff 標頭；公告欄的錯誤訊息要是純文字。"""
import os

import server
from starlette.testclient import TestClient

client = TestClient(server.app)


def _assert_headers(r):
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "same-origin"


def test_board_login_page_has_security_headers():
    r = client.get("/board")
    assert r.status_code == 200
    _assert_headers(r)


def test_health_and_unauthorized_have_security_headers(monkeypatch):
    monkeypatch.setattr(server.STORE, "ping", lambda: False)
    _assert_headers(client.get("/health"))
    _assert_headers(client.get("/mcp"))


def test_board_attachment_has_nosniff():
    os.makedirs(server.UPLOAD_DIR, exist_ok=True)
    fid = "0" * 24 + ".png"
    with open(os.path.join(server.UPLOAD_DIR, fid), "wb") as f:
        f.write(b"<html><script>alert(1)</script>")
    r = client.get("/board/files/" + fid, auth=("tester", "test-board-password-123"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    _assert_headers(r)


def test_board_error_is_plain_text():
    r = client.get("/board/files/%3Cimg%20src=x%3E", auth=("tester", "test-board-password-123"))
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("text/plain")
