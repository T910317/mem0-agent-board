"""離線測試：額度頁「請立刻收」旗標（usage.refresh_request／refresh_poll、POST /board/api/usage/refresh、MCP 工具 usage_refresh_poll）。
測：限次（60 秒一次、每小時 30 次）、讀後清除、過期作廢、完成時間、view() 的 refresh、權限（要登入、要 Origin、太大、舊版頁面）、
旗標不跑進提供商卡片。用假的 Mem0，真的 starlette 跑公告欄端點，資料庫在暫存資料夾，不連任何真實服務。
用法：uv run --no-project --with starlette --with httpx python test_usage_refresh.py <server.py 目錄>"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import types

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="usage-refresh-")


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
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE_LAPTOP": "t1",
                   "BOARD_USER": "u", "BOARD_PASSWORD": "p" * 16, "MSG_ARCHIVE": "0", "MONITOR_DIR": TMP, "MONITOR_ON": "0"})

spec = importlib.util.spec_from_file_location("srv", os.path.join(SRC, "server.py"))
S = importlib.util.module_from_spec(spec)
sys.path.insert(0, SRC)
spec.loader.exec_module(S)
U = S._usage
results = []


def check(name, ok):
    results.append((name, bool(ok)))


check("server 載入了額度模組", U is not None)
DB = os.path.join(TMP, "unit.db")
U._conn(DB)
T = 1_793_000_000.0

# ---- 單元：限次、讀後清除
r = U.refresh_request("user", now=T)
check("第一次按：ok、在等、請求時間＝現在", r["ok"] and r["pending"] and r["requested_at"] == U._iso(T))
check("第一次按：下次可以按＝60 秒後", r["next_allowed_at"] == U._iso(T + 60))
r = U.refresh_request("user", now=T + 30)
check("30 秒後再按：被擋，ok=false", r["ok"] is False and r["retry_after"] == 30 and r["next_allowed_at"] == U._iso(T + 60))
check("被擋時的說明是白話中文", "60 秒" in r["error"])
check("被擋時旗標還在", r["pending"] is True and r["requested_at"] == U._iso(T))
r = U.refresh_request("user", now=T + 60)
check("滿 60 秒可以再按；還沒被拿走就沿用原本的請求時間", r["ok"] and r["requested_at"] == U._iso(T))
p = U.refresh_poll("claude-laptop", now=T + 70)
check("電腦端查：有請求、回請求時間", p == {"pending": True, "requested_at": U._iso(T)})
p = U.refresh_poll("claude-laptop", now=T + 71)
check("再查一次：已經清掉（讀後清除）", p == {"pending": False})
v = U.view(now=T + 72)
rf = v["refresh"]
check("view 有 refresh：沒在等、最後一次查的時間、拿走的時間", rf["pending"] is False and rf["last_poll_at"] == U._iso(T + 71)
      and rf["taken_at"] == U._iso(T + 70) and rf["done_at"] is None)
check("view 寫明限次（60 秒、每小時 30 次）", rf["gap_seconds"] == 60 and rf["hour_max"] == 30)
check("旗標不會變成一家提供商卡片", all(not x["id"].startswith("_") for x in v["providers"]))

# 收集程式收完送報告，帶 refresh → 記完成時間；flags 表的東西不進 meta
U.put({"source": "pc", "providers": [{"id": "codex", "name": "Codex", "windows": [], "daily": []}],
       "refresh": {"requested_at": U._iso(T)}}, "pc", now=T + 90)
rf = U.view(now=T + 91)["refresh"]
check("帶 refresh 的報告記下完成時間", rf["done_at"] == U._iso(T + 90))
conn = sqlite3.connect(DB)
check("meta 表沒有旗標那一列（回滾到舊程式也不會多一張卡）",
      conn.execute("SELECT COUNT(*) FROM meta WHERE provider LIKE '%refresh%'").fetchone()[0] == 0)
check("旗標存在 flags 表", conn.execute("SELECT COUNT(*) FROM flags WHERE name = 'refresh'").fetchone()[0] == 1)
conn.close()
check("不帶 refresh 的一般報告不記完成", "refresh" not in U.put({"source": "pc", "providers": [{"id": "codex", "windows": []}]}, "pc", now=T + 95))

# 每小時最多 30 次：從乾淨的資料庫開始，每 60 秒按一次
U._conn(os.path.join(TMP, "hour.db"))
oks = [U.refresh_request("user", now=T + 60 * i)["ok"] for i in range(30)]
check("一小時內每 60 秒按一次，前 30 次都過", all(oks))
r = U.refresh_request("user", now=T + 60 * 30)
check("第 31 次（第 30 分鐘）被擋，說明寫每小時上限", r["ok"] is False and "30 次" in r["error"])
check("要等到最舊那次滿一小時", r["next_allowed_at"] == U._iso(T + 3600) and r["retry_after"] == 1800)
check("滿一小時又可以按", U.refresh_request("user", now=T + 3600)["ok"] is True)

# 過期：按了沒人收（電腦關機），超過一小時就作廢
U._conn(os.path.join(TMP, "ttl.db"))
U.refresh_request("user", now=T)
check("一小時內 view 顯示在等", U.view(now=T + 3500)["refresh"]["pending"] is True)
check("超過一小時 view 顯示沒在等", U.view(now=T + 3700)["refresh"]["pending"] is False)
p = U.refresh_poll("pc", now=T + 3700)
check("超過一小時才來查：不算請求、附作廢時間", p["pending"] is False and p.get("expired") == U._iso(T))
check("作廢的請求查完也清掉", U.refresh_poll("pc", now=T + 3701) == {"pending": False})
check("過期後可以重新按", U.refresh_request("user", now=T + 3702)["ok"] is True)

# ---- 端點：權限與限次
from starlette.testclient import TestClient  # noqa: E402

U._conn(os.path.join(TMP, "web.db"))
S.BOARD_REV_CHECK = False
web = TestClient(S.board_app)
ORIG = {"origin": "http://testserver"}
S._cookie_ok = lambda request: False
r = web.post("/board/api/usage/refresh", headers=ORIG)
check("沒登入：401", r.status_code == 401)
check("沒登入按的不算數", U.view()["refresh"]["pending"] is False)
S._cookie_ok = lambda request: True
check("沒有 Origin：403", web.post("/board/api/usage/refresh").status_code == 403)
check("別的網站的 Origin：403", web.post("/board/api/usage/refresh", headers={"origin": "https://evil.example"}).status_code == 403)
check("太大：413", web.post("/board/api/usage/refresh", headers=ORIG, content=b"x" * 2000).status_code == 413)
check("被擋的請求都沒設旗標", U.view()["refresh"]["pending"] is False)
check("GET 不能設旗標", web.get("/board/api/usage/refresh").status_code != 200 and U.view()["refresh"]["pending"] is False)
r = web.post("/board/api/usage/refresh", headers=ORIG)
check("登入＋Origin：200、ok、在等", r.status_code == 200 and r.json()["ok"] is True and r.json()["pending"] is True)
check("回傳下次可以按的時間", bool(r.json().get("next_allowed_at")))
r = web.post("/board/api/usage/refresh", headers=ORIG)
check("馬上再按：429、有 Retry-After、ok=false", r.status_code == 429 and r.json()["ok"] is False
      and int(r.headers.get("retry-after", "0")) > 0)
j = web.get("/board/api/usage").json()
check("GET /board/api/usage 有 refresh 欄位且在等", j["refresh"]["pending"] is True and j["refresh"]["requested_at"])

# MCP 工具：權杖那條路（這裡直接叫函式；權杖檢查在 AuthMiddleware，跟 usage_report 同一條）
S.current_agent.set("claude-laptop")
p = S.usage_refresh_poll()
check("MCP 工具 usage_refresh_poll：拿到請求", p["pending"] is True and p["requested_at"])
check("MCP 工具再叫一次：已清掉", S.usage_refresh_poll() == {"pending": False})
check("view 記下是誰來查的時間", U.view()["refresh"]["last_poll_at"] is not None)

# 舊版頁面擋寫入
check("新端點在「擋舊版頁面」的名單裡", "/board/api/usage/refresh" in S._REV_WRITE_PATHS)
S.BOARD_REV_CHECK = True
r = web.post("/board/api/usage/refresh", headers=ORIG)
check("舊版頁面（沒帶版號）按更新：409", r.status_code == 409)
S.BOARD_REV_CHECK = False

fail = [n for n, ok in results if not ok]
for n, ok in results:
    print(("通過 " if ok else "失敗 ") + n)
print(f"共 {len(results)} 項，失敗 {len(fail)} 項")
sys.exit(1 if fail else 0)
