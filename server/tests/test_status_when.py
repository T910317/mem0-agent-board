"""離線測試：白板卡「最後更新」要跟著 project_set_status 走。
真的 Mem0 更新時會合併舊 metadata，這裡的假 Mem0 也照合併做（test_board_230 的假 Mem0 是整筆覆寫，所以沒抓到）。
看：切乒乓、優先序不算更新；切完再 project_set_status 要換成新時間；10/21 前留下的舊 status_at 讀取時也要認得。
用法：uv run --no-project --with starlette --with httpx python test_status_when.py <server.py 目錄>"""
import copy
import importlib.util
import json
import os
import sys
import tempfile
import types
import uuid

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
UP = tempfile.mkdtemp(prefix="rt-up-")
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE_LAPTOP": "t1",
                   "MCP_TOKEN_CODEX_LAPTOP": "t2", "MCP_TOKEN_CODEX_PC": "t3",
                   "BOARD_USER": "u", "BOARD_PASSWORD": "p" * 16, "MSG_ARCHIVE": "0", "UPLOAD_DIR": UP})

DB, HIST, TICK = [], {}, [0]
FAIL = {"put_ignored": False}


def _ts():
    TICK[0] += 1
    return f"2026-10-19T02:{TICK[0] // 60:02d}:{TICK[0] % 60:02d}+00:00"


class R:
    def __init__(self, d=None): self.d = d if d is not None else {}
    def raise_for_status(self): pass
    def json(self): return self.d


class FakeClient:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get(self, path, params=None, **k):
        if path.endswith("/history"):
            return R(HIST.get(path.split("/")[2], []))
        return R({"results": [copy.deepcopy(m) for m in DB if m["user_id"] == (params or {}).get("user_id")]})
    def post(self, path, json=None, **k):
        mid, t = str(uuid.uuid4()), _ts()
        DB.append({"id": mid, "memory": json["messages"][0]["content"], "metadata": copy.deepcopy(json.get("metadata") or {}),
                   "agent_id": json.get("agent_id"), "user_id": json["user_id"], "created_at": t, "updated_at": t})
        HIST[mid] = [{"event": "ADD"}]
        return R({"results": [{"id": mid, "event": "ADD"}]})
    def put(self, path, json=None, **k):
        mid = path.rsplit("/", 1)[1]
        if FAIL["put_ignored"]:
            return R()
        for m in DB:
            if m["id"] == mid:
                HIST.setdefault(mid, []).append({"event": "UPDATE", "old_memory": m["memory"], "new_memory": json["text"]})
                m["memory"], m["updated_at"] = json["text"], _ts()
                m["metadata"] = {**m["metadata"], **copy.deepcopy(json["metadata"])}  # 真的 Mem0 會合併舊 metadata
        return R()


spec = importlib.util.spec_from_file_location("srv", os.path.join(SRC, "server.py"))
S = importlib.util.module_from_spec(spec)
sys.path.insert(0, SRC)
spec.loader.exec_module(S)
if hasattr(S, "STORE"):  # 2.0.0 儲存層之後
    S.STORE.http = lambda: FakeClient()
else:  # 線上 7f6e205
    S._http = lambda: FakeClient()
S._cookie_ok = lambda request: True
S.BOARD_REV_CHECK = False
from starlette.testclient import TestClient  # noqa: E402

web = TestClient(S.board_app)
H = {"origin": "http://testserver"}
results = []


def check(name, ok):
    results.append((name, bool(ok)))


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
        return False
    except exc:
        return True


def err(fn, *a, **k):
    try:
        fn(*a, **k)
        return ""
    except ValueError as e:
        return str(e)


def as_(who, fn, *a, **k):
    tok = S.current_agent.set(who)
    try:
        return fn(*a, **k)
    finally:
        S.current_agent.reset(tok)


def card(p):
    return next(m for m in DB if (m["metadata"] or {}).get("project") == p and m["metadata"].get("type") == "status")


def wb(p):
    return next(s for s in S._whiteboard(S._list_all())[0] if s["project"] == p)


def ps(p):
    return as_("claude-laptop", S.project_status, p)


def setst(p, text):
    v = ps(p)["version"] if ps(p)["status"] else ""
    as_("claude-laptop", S.project_set_status, p, "名稱：測試\n- [ ] " + text, "", v)


setst("p1", "第一版")
t0 = card("p1")["updated_at"]
check("新卡：updated＝建立時間", ps("p1")["status"]["updated"] == t0 and wb("p1")["tw"] == S._tw(t0))

as_("user", S._project_pingpong, "p1", True)
check("切乒乓：updated 不變", ps("p1")["status"]["updated"] == t0 and wb("p1")["tw"] == S._tw(t0))
as_("user", S._project_priority, "p1", 2)
check("設優先序：updated 不變", ps("p1")["status"]["updated"] == t0 and wb("p1")["tw"] == S._tw(t0))

setst("p1", "第二版")
t1 = card("p1")["updated_at"]
check("切完再改內容：updated 換新", t1 != t0 and ps("p1")["status"]["updated"] == t1 and wb("p1")["tw"] == S._tw(t1))
check("status_at 寫成空字串", card("p1")["metadata"].get("status_at") == "")
check("乒乓、優先序留著", (card("p1")["metadata"].get("pingpong"), ps("p1")["priority"]) == ("yes", 2))

as_("user", S._project_pingpong, "p1", False)
check("再切乒乓：updated 停在第二版", ps("p1")["status"]["updated"] == t1 and wb("p1")["tw"] == S._tw(t1))
check("再切時記的是第二版時間", card("p1")["metadata"].get("status_at") == t1)
as_("user", S._project_priority, "p1", 5)
check("再設優先序：還是第二版時間", ps("p1")["status"]["updated"] == t1 and card("p1")["metadata"].get("status_at") == t1)

# 10/21 前的舊資料：status_at 留在很久以前，之後又改過內容（最後寫入比最後切開關晚很多）
setst("p2", "舊資料")
m = card("p2")
m["metadata"].update(status_at="2026-10-18T20:15:00+00:00", pingpong="yes", pingpong_at="2026-10-19T21:02:00+08:00")
m["updated_at"] = "2026-10-19T18:54:00+00:00"
check("舊資料：切開關後又改過，用最後寫入時間", ps("p2")["status"]["updated"] == "2026-10-19T18:54:00+00:00"
      and wb("p2")["tw"] == S._tw("2026-10-19T18:54:00+00:00"))
m["updated_at"] = "2026-10-19T13:02:30+00:00"
check("舊資料：最後寫入就是那次切開關，用 status_at", ps("p2")["status"]["updated"] == "2026-10-18T20:15:00+00:00")
m["metadata"]["pingpong_at"] = "壞掉"
check("切開關時間讀不出：照舊用 status_at", ps("p2")["status"]["updated"] == "2026-10-18T20:15:00+00:00")

bad = [n for n, ok in results if not ok]
for n, ok in results:
    print(("OK  " if ok else "FAIL") + " " + n)
print(f"{len(results) - len(bad)}/{len(results)} 通過")
sys.exit(1 if bad else 0)
