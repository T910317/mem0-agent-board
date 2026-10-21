"""離線測試：換版後擋下舊版頁面送出的寫入。
用假的 UI 資料夾和真的 starlette，不連任何真實服務。
用法：uv run --with starlette --with httpx python test_board_rev_guard.py <server.py 所在目錄>"""
import importlib.util
import os
import sys
import tempfile
import types

SRC = sys.argv[1]


class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k):
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]
        return _Any()
    def __getattr__(self, name): return _Any()


def _stubmod(name, **attrs):
    m = types.ModuleType(name)
    m.__getattr__ = lambda n: _Any()
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m


class FastMCP:
    def __init__(self, *a, **k): pass
    def tool(self, *a, **k): return lambda f: f
    def __getattr__(self, n): return _Any()


for n in ["mcp", "mcp.server", "mcp.server.transport_security"]:
    _stubmod(n)
_stubmod("mcp.server.fastmcp", FastMCP=FastMCP, Image=_Any)
_stubmod("experience_store", register=lambda *a, **k: None)
for k in [k for k in os.environ if k.startswith("MCP_TOKEN_")]:
    del os.environ[k]
ROOT = tempfile.mkdtemp(prefix="revguard-")
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE_LAPTOP": "t1", "UI_DIR": ROOT,
                   "BOARD_USER": "u", "BOARD_PASSWORD": "p" * 16})
os.environ.pop("BOARD_REV_CHECK", None)
sys.path.insert(0, SRC)
spec = importlib.util.spec_from_file_location("srv", os.path.join(SRC, "server.py"))
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

from starlette.testclient import TestClient  # noqa: E402

FAILS = []


def check(name, ok):
    print(("通過 " if ok else "失敗 ") + name)
    if not ok:
        FAILS.append(name)


def put(which, rev, version):
    d = os.path.join(ROOT, which)
    os.makedirs(d, exist_ok=True)
    for name, kind in zip(S._UI_FILES, ("board", "memory", "release")):
        page = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="%s-version" content="%s">'
                '<meta name="%s-revision" content="%d"></head><body>%s</body></html>' % (kind, version, kind, rev, name))
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(page)


sent = []
S._cookie_ok = lambda request: True
S._send = lambda *a, **k: sent.append(a) or {"id": "x"}
S._close = lambda *a, **k: {"ok": True}
S._list_all = lambda: []
S._ui_promote = lambda: "stamp"
S._ui_state = lambda: {}
c = TestClient(S.board_app, base_url="http://board.test")
FORM = {"Content-Type": "application/x-www-form-urlencoded"}

put("stable", 31, "1.8.0")
put("preview", 32, "1.9.0")

# 送頁面時塞進版號
html_ = c.get("/board").text
check("正式版公告欄塞了版號 stable:31", '"stable:31"' in html_ and html_.index("<script>") < html_.index("<meta charset"))
check("預覽版公告欄塞了版號 preview:32", '"preview:32"' in c.get("/board/preview").text)
check("版本紀錄頁也塞了", '"stable:31"' in c.get("/board/versions").text)
check("頁面內容沒被弄壞", "board.html</body>" in html_)

# 帶現在的版號：照常寫入
r = c.post("/board/send", data={"to": "all", "subject": "a"}, headers={**FORM, "X-Board-Rev": "stable:31", "Accept": "application/json"})
check("現在的正式版頁面可以送出", r.status_code == 200 and len(sent) == 1)
r = c.post("/board/send", data={"to": "all", "subject": "b"}, headers={**FORM, "X-Board-Rev": "preview:32", "Accept": "application/json"})
check("現在的預覽版頁面可以送出", r.status_code == 200 and len(sent) == 2)

# 舊版頁面：擋下、什麼都不寫
r = c.post("/board/send", data={"to": "all", "subject": "old"}, headers={**FORM, "X-Board-Rev": "stable:29", "Accept": "application/json"})
check("舊版號 stable:29 回 409", r.status_code == 409 and r.headers.get("x-board-stale") == "1")
check("409 說明現在是 1.8.0", "1.8.0" in r.json()["error"] and r.json()["stale"] is True)
check("舊版沒有寫進去", len(sent) == 2)
r = c.post("/board/send", data={"to": "all", "subject": "old"}, headers={**FORM, "Accept": "application/json"})
check("沒帶版號（部署前就開著的分頁）回 409", r.status_code == 409 and len(sent) == 2)
r = c.post("/board/preview/close", data={"message_id": "m1"}, headers={**FORM, "Origin": "http://board.test", "X-Board-Rev": "preview:31"})
check("舊預覽版按結案也擋", r.status_code == 409)
r = c.post("/board/promote", headers={"X-Board-Rev": "stable:30"})
check("舊頁面按採用也擋", r.status_code == 409)
r = c.post("/board/api/memory/abc/footnote", json={"text": "x"}, headers={"Origin": "http://board.test"})
check("改記憶註腳沒帶版號也擋", r.status_code == 409)

# 換版後：原本的分頁變舊版
put("stable", 32, "1.9.0")
r = c.post("/board/send", data={"to": "all", "subject": "c"}, headers={**FORM, "X-Board-Rev": "stable:31", "Accept": "application/json"})
check("採用新版後，舊的正式版分頁被擋", r.status_code == 409 and "1.9.0" in r.json()["error"] and len(sent) == 2)
r = c.post("/board/send", data={"to": "all", "subject": "c"}, headers={**FORM, "X-Board-Rev": "stable:32", "Accept": "application/json"})
check("重新載入後的分頁可以送", r.status_code == 200 and len(sent) == 3)

# 伺服器自己產生的舊式頁面（一般表單）
legacy = c.get("/board/legacy").text
check("舊式頁面塞了啟動代號", '"legacy:%s"' % S._BOOT in legacy)
r = c.post("/board/send", data={"to": "all", "subject": "d", "_board_rev": "legacy:" + S._BOOT}, headers=FORM, follow_redirects=False)
check("舊式頁面的表單帶隱藏欄位可以送", r.status_code == 303 and len(sent) == 4)
r = c.post("/board/send", data={"to": "all", "subject": "e", "_board_rev": "legacy:deadbeef"}, headers=FORM, follow_redirects=False)
check("伺服器重啟前的舊式頁面被擋，回看得懂的網頁", r.status_code == 409 and "重新載入" in r.text and "<p>" in r.text and len(sent) == 4)

# 不在檢查範圍的：登出、讀取
check("讀取不受影響", c.get("/board/api/feed").status_code in (200, 400))
S.BOARD_REV_CHECK = False
r = c.post("/board/send", data={"to": "all", "subject": "f"}, headers={**FORM, "Accept": "application/json"})
check("BOARD_REV_CHECK=0 時不檢查", r.status_code == 200 and len(sent) == 5)

print("全部通過" if not FAILS else "有 %d 項失敗" % len(FAILS))
sys.exit(1 if FAILS else 0)
