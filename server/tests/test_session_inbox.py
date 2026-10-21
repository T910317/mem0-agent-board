"""離線測試：用假的 Mem0 資料比對新舊 server.py 的收件匣行為（不連任何真實服務）。
用法：python test_session_inbox.py <舊版 server.py 目錄> <新版 server.py 目錄>"""
import copy
import importlib.util
import os
import sys
import types
import uuid

OLD_DIR, NEW_DIR = sys.argv[1], sys.argv[2]

# ---- 假模組：讓 server.py 不需要 httpx / starlette / mcp 就能載入 ----
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
    return m

class FastMCP:
    def __init__(self, *a, **k): pass
    def tool(self, *a, **k): return lambda f: f
    def __getattr__(self, n): return _Any()

for n in ["httpx", "starlette", "starlette.concurrency", "starlette.requests", "starlette.responses",
          "mcp", "mcp.server", "mcp.server.transport_security"]:
    _stubmod(n)
_stubmod("mcp.server.fastmcp", FastMCP=FastMCP, Image=_Any)
_stubmod("experience_store", register=lambda *a, **k: None)

os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE": "t1", "MCP_TOKEN_CODEX_MAX": "t2", "MCP_TOKEN_CODEX_MINI": "t3",
                   "MCP_TOKEN_HERMES": "t4"})

STORE = []

class FakeResp:
    def __init__(self, data=None): self._d = data or {}
    def raise_for_status(self): pass
    def json(self): return self._d

class FakeClient:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def post(self, path, json=None, **k):
        mid = str(uuid.uuid4())
        STORE.append({"id": mid, "memory": json["messages"][0]["content"], "metadata": dict(json["metadata"]),
                      "created_at": f"2026-10-16T00:{len(STORE):02d}:00"})
        return FakeResp({"results": [{"id": mid}]})
    def put(self, path, json=None, **k):
        mid = path.rsplit("/", 1)[1]
        for m in STORE:
            if m["id"] == mid:
                m["metadata"] = dict(json["metadata"])
        return FakeResp()

def load(d, name):
    sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(name, os.path.join(d, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.path.pop(0)
    mod._list_all = lambda: copy.deepcopy(STORE)
    if hasattr(mod, "STORE"):  # 2.0.0 起走儲存層；舊版（1.0.0）還是 _http
        mod.STORE.http = lambda: FakeClient()
    else:
        mod._http = lambda: FakeClient()
    mod._proj = lambda p: p.strip().lower()
    return mod

old = load(OLD_DIR, "server_old")
new = load(NEW_DIR, "server_new")

def as_(mod, who):
    mod.current_agent.set(who)
    return mod

def msg(frm, to, subj, project="general", read_by="", **extra):
    md = {"type": "message", "to": to, "from": frm, "subject": subj, "body": "", "needs_action": "no",
          "read_by": read_by, "project": project, "ref": "", "attachments": ""}
    md.update(extra)
    mid = str(uuid.uuid4())
    STORE.append({"id": mid, "memory": f"[訊息] {frm}→{to}｜{subj}", "metadata": md,
                  "created_at": f"2026-10-15T10:{len(STORE):02d}:00"})
    return mid

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

def subjects(rows): return sorted(r["subject"] for r in rows)

# ---- 既有資料（改版前的樣子）----
m_mem0 = msg("codex-max", "claude", "mem0 換 IP", "mem0")
m_eval = msg("hermes", "claude", "EvalScope 跑分", "evalscope")
m_pve = msg("codex-mini", "all", "PVE 巡檢", "pve")
m_gen = msg("user", "claude", "一般問題")
m_old_read = msg("codex-max", "claude", "舊的已讀 mem0", "mem0", read_by="claude")
m_codex = msg("claude", "codex", "給 codex 的", "roundtable")
m_mine = msg("claude", "all", "我自己發的", "roundtable")
m_ask = msg("codex-max", "all", "徵求意見 mem0", "mem0", ask_opinion="yes")

def strip(rows):
    return [{k: v for k, v in r.items() if k != "from_session"} for r in rows]

# 1. 不帶參數：新舊完全相同（claude、codex-max、hermes 都比）
for who in ["claude", "codex-max", "codex-mini", "hermes"]:
    for inc in (False, True):
        a = strip(as_(old, who).inbox(include_read=inc))  # 舊版是 1.0.0 的話也會帶 from_session，兩邊都去掉再比
        b = strip(as_(new, who).inbox(include_read=inc))
        check(f"legacy inbox same: {who} include_read={inc}", a == b)

# 2. 分串：mem0-deploy 只看 mem0
A = as_(new, "claude").inbox(session="mem0-deploy", projects="mem0")
check("mem0-deploy sees only mem0 unread", subjects(A) == sorted(["mem0 換 IP", "徵求意見 mem0"]))
E = as_(new, "claude").inbox(session="evalscope", projects="evalscope")
check("evalscope sees only evalscope", subjects(E) == ["EvalScope 跑分"])
N = as_(new, "claude").inbox(session="navigator", projects="*,-mem0,-evalscope,-roundtable")
check("navigator catch-all gets unsubscribed projects", subjects(N) == sorted(["PVE 巡檢", "一般問題"]))
U = as_(new, "claude").inbox(session="lonely")
check("session without projects sees nothing routed", U == [])

# 3. A ack 只影響 A；另一串同樣訂閱 mem0 的仍看得到
r = as_(new, "claude").message_ack(m_mem0, session="mem0-deploy")
check("ack returns claude:label", r["by"] == "claude:mem0-deploy")
A2 = as_(new, "claude").inbox(session="mem0-deploy", projects="mem0")
B2 = as_(new, "claude").inbox(session="mem0-ip", projects="mem0")
check("A no longer sees acked", "mem0 換 IP" not in subjects(A2))
check("B (also mem0) still sees it", "mem0 換 IP" in subjects(B2))
check("old identity-level ack still counts as read for sessions", "舊的已讀 mem0" not in subjects(B2))
# 舊版（不帶參數）收件匣：任何一串 ACK 過就算讀過（跟改版前「一串 ACK 全體消失」相同）
L = as_(new, "claude").inbox()
check("legacy view treats per-session ack as read", "mem0 換 IP" not in subjects(L))
check("legacy view unaffected for others", "EvalScope 跑分" in subjects(L))

# 4. 直接寄給某一串
as_(new, "codex-max").message_send(to="claude:evalscope", subject="直寄 evalscope", project="mem0")
E3 = as_(new, "claude").inbox(session="evalscope", projects="evalscope")
A3 = as_(new, "claude").inbox(session="mem0-deploy", projects="mem0")
check("direct mail reaches only target session", "直寄 evalscope" in subjects(E3) and "直寄 evalscope" not in subjects(A3))
direct_id = [x for x in STORE if x["metadata"]["subject"] == "直寄 evalscope"][0]["id"]
try:
    as_(new, "claude").message_ack(direct_id, session="mem0-deploy")
    check("acking another session's direct mail is refused", False)
except ValueError:
    check("acking another session's direct mail is refused", True)
check("codex does not see claude:evalscope mail", "直寄 evalscope" not in subjects(as_(new, "codex-mini").inbox()))

# 5. 本串寄出 → 對方用 to=claude 回覆（別的專案）也會回到本串
res = as_(new, "claude").message_send(to="codex-max", subject="mem0 串問問題", project="mem0", session="mem0-deploy")
check("send with session reports claude:label", res["by"] == "claude:mem0-deploy")
q_id = [x for x in STORE if x["metadata"]["subject"] == "mem0 串問問題"][0]["id"]
check("from_session stored", [x for x in STORE if x["id"] == q_id][0]["metadata"].get("from_session") == "mem0-deploy")
as_(new, "codex-max").message_send(to="claude", subject="回覆 mem0 串", project="pve", reply_to=q_id)
A5 = as_(new, "claude").inbox(session="mem0-deploy", projects="mem0")
check("reply to my thread comes back even if project differs", "回覆 mem0 串" in subjects(A5))
codex_rows = as_(new, "codex-max").inbox()
check("codex inbox shows from_session", any(r.get("from_session") == "mem0-deploy" for r in codex_rows))

# 6. 本串之間互寄、寄給自己
as_(new, "claude").message_send(to="claude:evalscope", subject="串對串", session="mem0-deploy")
check("session-to-session mail arrives", "串對串" in subjects(as_(new, "claude").inbox(session="evalscope", projects="evalscope")))
check("sender session does not see own mail", "串對串" not in subjects(as_(new, "claude").inbox(session="mem0-deploy", projects="mem0")))
for bad_to, sess in [("claude:mem0-deploy", "mem0-deploy"), ("claude", ""), ("nobody:x", ""), ("claude:Bad Label", "")]:
    try:
        as_(new, "claude").message_send(to=bad_to, subject="x", session=sess)
        check(f"send refused: to={bad_to} session={sess}", False)
    except ValueError:
        check(f"send refused: to={bad_to} session={sess}", True)
try:
    as_(new, "claude").inbox(session="Bad Label")
    check("bad session label refused", False)
except ValueError:
    check("bad session label refused", True)

# 7. 徵求意見寄給某一串，那一串回覆就算回了
as_(new, "codex-max").message_send(to="claude:evalscope", subject="請 evalscope 給意見", ask_opinion=True)
ask_id = [x for x in STORE if x["metadata"]["subject"] == "請 evalscope 給意見"][0]["id"]
as_(new, "claude").message_ack(ask_id, session="evalscope")
row = [r for r in as_(new, "claude").inbox(session="evalscope", projects="evalscope") if r["id"] == ask_id]
check("acked ask_opinion still listed while owed", bool(row) and row[0]["you_owe_opinion"])
as_(new, "claude").message_send(to="codex-max", subject="意見", reply_to=ask_id, session="evalscope")
row = [r for r in as_(new, "claude").inbox(session="evalscope", projects="evalscope") if r["id"] == ask_id]
check("after session reply, opinion no longer owed", not row)
op = new._opinions([x for x in STORE if x["id"] == ask_id][0], STORE)
check("opinion responded lists claude:evalscope", op["responded"] == ["claude:evalscope"])

# 7b. 有串名的串寄 to=claude：依專案分給其他串，自己這串與舊版 claude 收件匣都不列
as_(new, "claude").message_send(to="claude", subject="交給 mem0 的串", project="mem0", session="homelab")
check("session->claude routed to subscriber", "交給 mem0 的串" in subjects(as_(new, "claude").inbox(session="mem0-deploy", projects="mem0")))
check("session->claude hidden from sender session", "交給 mem0 的串" not in subjects(as_(new, "claude").inbox(session="homelab", projects="mem0")))
check("session->claude hidden from legacy claude view", "交給 mem0 的串" not in subjects(as_(new, "claude").inbox()))
check("legacy claude-sent mail still hidden in session view", "我自己發的" not in subjects(as_(new, "claude").inbox(session="roundtable", projects="roundtable", include_read=True)))

# 8. 不帶 session 的 ACK 仍是舊行為（整個身分）
as_(new, "claude").message_ack(m_pve)
check("legacy ack writes identity", "claude" in [x for x in STORE if x["id"] == m_pve][0]["metadata"]["read_by"].split(","))

# 9. 公告欄使用者寄信給某一串
new._send("user", "claude:mem0-deploy", "使用者直寄", "", False, "", "")
check("board user can mail a session", "使用者直寄" in subjects(as_(new, "claude").inbox(session="mem0-deploy", projects="mem0")))

print("\nFAILED:" if fails else "\nALL PASS", fails if fails else "")
sys.exit(1 if fails else 0)
