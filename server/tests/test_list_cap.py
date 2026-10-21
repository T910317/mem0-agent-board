"""離線測試：_list_uid 超過 1000 筆時依作者分批撈、用 /entities 總數核對（不連任何真實服務）。
用法：python test_list_cap.py <新版 server.py 目錄>"""
import importlib.util
import os
import sys
import types
import uuid

NEW_DIR = sys.argv[1]


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


for n in ["httpx", "starlette", "starlette.concurrency", "starlette.requests", "starlette.responses",
          "mcp", "mcp.server", "mcp.server.transport_security"]:
    _stubmod(n)
_stubmod("mcp.server.fastmcp", FastMCP=FastMCP, Image=_Any)
_stubmod("experience_store", register=lambda *a, **k: None)
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE": "t1",
                   "MCP_TOKEN_CODEX_MAX": "t2", "MCP_TOKEN_HERMES": "t3"})

DB = []
CALLS = []


class R:
    def __init__(self, d): self.d = d
    def raise_for_status(self): pass
    def json(self): return self.d


class FakeClient:
    """模仿真的 Mem0：/memories 依 user_id（＋agent_id）篩、最多回 top_k（上限 1000）筆；/entities 掃全部算總數。"""
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get(self, path, params=None, **k):
        CALLS.append((path, dict(params or {})))
        if path == "/entities":
            out = {}
            for m in DB[:10000]:
                for typ, field in (("user", "user_id"), ("agent", "agent_id")):
                    if m.get(field):
                        out[(typ, m[field])] = out.get((typ, m[field]), 0) + 1
            return R([{"type": t, "id": i, "total_memories": n} for (t, i), n in out.items()])
        top = min(params.get("top_k", 100), 1000)
        rows = [m for m in DB if m["user_id"] == params["user_id"]
                and ("agent_id" not in params or m.get("agent_id") == params["agent_id"])]
        return R({"results": [dict(m) for m in rows[:top]]})


spec = importlib.util.spec_from_file_location("server_new", os.path.join(NEW_DIR, "server.py"))
new = importlib.util.module_from_spec(spec)
sys.path.insert(0, NEW_DIR)
spec.loader.exec_module(new)
new.STORE.http = lambda: FakeClient()
DEFAULT_CAP = new.STORE.cap
new.STORE.cap = 1000  # 假 Mem0 上限 1000，用小數字測分批邏輯（程式是依 STORE.cap 運作）
fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def raises(fn, *a):
    try:
        fn(*a)
        return False
    except RuntimeError:
        return True


def add(uid, agent, n, type_="note"):
    for i in range(n):
        DB.append({"id": str(uuid.uuid4()), "memory": f"[筆記] {agent} {i}", "user_id": uid, "agent_id": agent,
                   "metadata": {"type": type_, "project": "mem0"}, "created_at": f"2026-10-16T00:00:{i % 60:02d}"})


check("module default STORE.cap is 10000", DEFAULT_CAP == 10000)

# 1. 不到 1000 筆：只打一次，結果完整
add("roundtable", "claude", 300)
add("personas", "hermes", 25)
CALLS.clear()
check("under cap: complete", len(new._list_all()) == 300)
# 2026-10-19 兩層分家：合併列表＝原則記憶層＋交流層各列一次
check("under cap: one request per layer, no /entities", CALLS == [("/memories", {"user_id": "roundtable", "top_k": 1000}),
                                                               ("/memories", {"user_id": "roundtable-ops", "top_k": 1000})])

# 2. 超過 1000 筆：依作者分批撈，一筆不漏、不重複
add("roundtable", "codex-laptop", 700)
add("roundtable", "user", 400)
add("roundtable", "hermes-gamma", 100)
add("profile", "codex-laptop", 900)  # 別區的同一作者不會混進來
items = new._list_all()
ids = [m["id"] for m in items]
check("over cap: all 1500 roundtable items", len(items) == 1500 and len(set(ids)) == 1500)
check("over cap: no other namespace mixed in", all(m["user_id"] == "roundtable" for m in items))
check("over cap: used /entities and per-agent requests", any(p == "/entities" for p, _ in CALLS))
check("personas still fine", len(new._list_uid("personas")) == 25)

# 3. 上層工具照常運作
new.current_agent.set("claude")
lst = new.memory_list()  # 2026-10-18 起分頁：回 {"total", "items", ...}
check("memory_list works over cap", (lst["total"], len(lst["items"])) == (1500, 50) if isinstance(lst, dict) else len(lst) == 1500)
check("claim_check works over cap", new.claim_check() == [])
check("inbox works over cap", new.inbox() == [])

# 4. 核對不過就照舊報錯（寧可停）
DB.append({"id": str(uuid.uuid4()), "memory": "沒有作者", "user_id": "roundtable", "agent_id": None,
           "metadata": {"type": "note"}, "created_at": "x"})
check("item without agent -> count mismatch -> error", raises(new._list_all))
DB.pop()
add("roundtable", "claude", 800)  # claude 變 1100 筆
check("single agent over cap -> error", raises(new._list_all))

print("\nFAILED:" if fails else "\nALL PASS", fails if fails else "")
sys.exit(1 if fails else 0)
