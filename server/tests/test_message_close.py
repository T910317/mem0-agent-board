"""離線測試：結案（message_close／closes）、待辦分「等誰」、徵求意見只等活躍身分、測試信不上公告欄、同身分串間信要帶 session。
用法：python test_message_close.py <舊版 server.py 目錄> <新版 server.py 目錄>（不連任何真實服務）"""
import copy
import datetime as dt
import importlib.util
import os
import sys
import types
import uuid

OLD_DIR, NEW_DIR = sys.argv[1], sys.argv[2]


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

for k in [k for k in os.environ if k.startswith("MCP_TOKEN_")]:
    del os.environ[k]
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE_LAPTOP": "t1",
                   "MCP_TOKEN_CODEX_LAPTOP": "t2", "MCP_TOKEN_CODEX_PC": "t3", "MCP_TOKEN_CODEX_MAX": "t4",
                   "MCP_TOKEN_HERMES_GAMMA": "t5", "MCP_TOKEN_HERMES_BETA": "t6"})

NOW = dt.datetime.now(dt.timezone.utc)
STORE = []


def _ts(days_ago=0.0):
    return (NOW - dt.timedelta(days=days_ago)).isoformat()


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
                      "agent_id": json.get("agent_id"), "created_at": _ts(), "updated_at": _ts()})
        return FakeResp({"results": [{"id": mid, "event": "ADD"}]})
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


def msg(frm, to, subj, project="general", read_by="", days_ago=1.0, needs_action="no", **extra):
    md = {"type": "message", "to": to, "from": frm, "subject": subj, "body": "", "needs_action": needs_action,
          "read_by": read_by, "project": project, "ref": "", "attachments": ""}
    md.update(extra)
    mid = str(uuid.uuid4())
    STORE.append({"id": mid, "memory": f"[訊息] {frm}→{to}｜{subj}", "metadata": md, "agent_id": frm,
                  "created_at": _ts(days_ago), "updated_at": _ts(days_ago)})
    return mid


fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


def subjects(rows): return sorted(r["subject"] for r in rows)
def get(mid): return [m for m in STORE if m["id"] == mid][0]


# ---- 資料：像真實的樣子 ----
# codex-max、hermes-beta 從沒動過；codex-pc 只在 10 天前寄過一則
msg("codex-pc", "codex-laptop", "PC 很久以前的信", "roundtable", read_by="codex-laptop", days_ago=10)
a1 = msg("claude", "codex-laptop", "IP 搬遷核准", "mem0", needs_action="yes", days_ago=2)
a2 = msg("claude", "codex-laptop", "撤回今晚停手", "mem0", needs_action="yes", days_ago=2)
a3 = msg("claude", "codex-laptop", "IP 清單審查", "mem0", needs_action="yes", read_by="codex-laptop", days_ago=2)
h1 = msg("user", "hermes-gamma", "試 DC 頻道", needs_action="yes", days_ago=2)
msg("hermes-gamma", "claude", "DC 頻道成功", "homelab-network", read_by="claude", days_ago=2, reply_to=h1)
u1 = msg("claude", "user", "給使用者的通知", "roundtable", days_ago=1)
u2 = msg("codex-laptop", "user", "請使用者按採用", "roundtable", needs_action="yes", read_by="claude-laptop:navigator", days_ago=1)
t1 = msg("claude-laptop", "claude-laptop:inbox-test-b", "[測試] 直寄", "inbox-test", needs_action="yes", from_session="inbox-test", days_ago=1)
c1 = msg("codex-laptop", "claude:mem0-deploy", "備份清理完成請複查", "mem0", needs_action="yes", days_ago=0.1)
op_all = msg("claude", "all", "原始碼放哪（徵求意見）", "roundtable", needs_action="yes", read_by="codex-laptop",
             ask_opinion="yes", days_ago=2)
msg("codex-laptop", "claude", "意見：放 VM119", "roundtable", read_by="claude", days_ago=2, reply_to=op_all)
msg("hermes-gamma", "claude", "意見：獨立 LXC", "roundtable", read_by="claude", days_ago=2, reply_to=op_all)
op_sess = msg("codex-laptop", "claude:evalscope", "請 evalscope 給意見", "evalscope", ask_opinion="yes", days_ago=1)

# 1. 沒有結案時，收件匣跟改版前一樣（徵求意見名單：舊的 pending＝新的 pending＋offline）
def norm(rows):
    out = []
    for r in rows:
        r = dict(r)
        if r.get("opinion"):
            o = dict(r["opinion"])
            o["pending"] = sorted(o.get("pending", []) + o.pop("offline", []))
            r["opinion"] = o
        r.pop("you_owe_opinion", None)
        out.append(r)
    return out

for who in ["claude-laptop", "codex-laptop", "codex-pc", "codex-max", "hermes-gamma", "hermes-beta"]:
    for inc in (False, True):
        a = norm(as_(old, who).inbox(include_read=inc))
        b = norm(as_(new, who).inbox(include_read=inc))
        check(f"inbox same before any close: {who} include_read={inc}", a == b)
for sess, projs in [("mem0-deploy", "mem0"), ("navigator", "*,-mem0,-evalscope"), ("evalscope", "evalscope")]:
    a = norm(as_(old, "claude-laptop").inbox(session=sess, projects=projs, include_read=True))
    b = norm(as_(new, "claude-laptop").inbox(session=sess, projects=projs, include_read=True))
    check(f"session inbox same before any close: {sess}", a == b)

# 2. 徵求意見只等活躍身分
op = new._opinions(get(op_all), STORE)
check("opinion responded", op["responded"] == ["codex-laptop", "hermes-gamma"])
check("never-active identities are offline, not pending",
      op["pending"] == [] and op["offline"] == ["codex-max", "codex-pc", "hermes-beta"])
op2 = new._opinions(get(op_sess), STORE)
check("opinion to a session: owed by that session (identity active)", op2["pending"] == ["claude-laptop:evalscope"] and "offline" not in op2)
row = [r for r in as_(new, "claude-laptop").inbox(session="evalscope", projects="evalscope") if r["id"] == op_sess]
check("session still owes opinion", bool(row) and row[0]["you_owe_opinion"])
row = [r for r in as_(new, "codex-max").inbox(include_read=True) if r["id"] == op_all]
check("inactive identity calling inbox sees itself as owing (caller counts as active)",
      bool(row) and "codex-max" in row[0]["opinion"]["pending"] and row[0]["you_owe_opinion"])

# 3. 待辦分「等誰」、測試信不上公告欄
f = new._feed(500)
ids = [m["id"] for m in f["messages"]]
check("test messages hidden from feed", t1 not in ids and all("[測試]" not in m["subject"] for m in f["messages"]))
check("todo_by before close", f["todo_by"] == {"codex": 2, "hermes": 1, "user": 1, "claude": 1})
check("todo total matches", f["todo"] == 5)
fm = {m["id"]: m for m in f["messages"]}
check("message to user with an AI reader still waits on user", fm[u2]["todo"] and fm[u2]["waiting_on"] == "user")
check("feed has closed=None and waiting_on", fm[a1]["closed"] is None and fm[a1]["waiting_on"] == "codex")
check("feed opinion has offline list", fm[op_all]["opinion"]["offline"] == ["codex-max", "codex-pc", "hermes-beta"])
check("session address waits on claude", fm[c1]["waiting_on"] == "claude")

# 4. 結案權限
check("recipient cannot close", raises(lambda: as_(new, "codex-laptop").message_close(a1, "做完了")))
check("other identity cannot close", raises(lambda: as_(new, "hermes-gamma").message_close(a1, "做完了")))
check("reason required", raises(lambda: as_(new, "claude-laptop").message_close(a1, "  ")))
r = as_(new, "claude-laptop").message_close(a1, "IP 搬遷已完成", session="mcp")
check("sender (old name claude) closes via new name with session", r["closed"]["by"] == "claude-laptop:mcp"
      and r["closed"]["reason"] == "IP 搬遷已完成" and r["closed"]["at"])
check("read_by untouched by close", get(a1)["metadata"]["read_by"] == "")
r = new._close("user", h1, "Hermes 已回報成功")
check("user can close anyone's message", r["closed"]["by"] == "user")

# 5. 結案後：收件人 inbox 預設不列、include_read 才列並附 closed；待辦少掉
L = as_(new, "codex-laptop").inbox()
check("closed message gone from recipient default inbox", "IP 搬遷核准" not in subjects(L) and "撤回今晚停手" in subjects(L))
L = as_(new, "codex-laptop").inbox(include_read=True)
row = [x for x in L if x["id"] == a1][0]
check("include_read lists closed with closed field", row["closed"]["by"] == "claude-laptop:mcp")
check("unclosed rows have no closed key", all("closed" not in x for x in L if x["id"] != a1))
check("hermes default inbox drops closed", "試 DC 頻道" not in subjects(as_(new, "hermes-gamma").inbox()))
f = new._feed(500)
check("todo_by after close", f["todo_by"] == {"codex": 1, "user": 1, "claude": 1})

# 6. closes：追問／撤回時順手結自己的舊信；不能結別人的（而且不會先寄出去）
n0 = len(STORE)
check("closes rejects someone else's message",
      raises(lambda: as_(new, "claude-laptop").message_send("codex-laptop", "合併", closes=c1, session="mcp")))
check("nothing sent when closes rejected", len(STORE) == n0)
res = as_(new, "claude-laptop").message_send("codex-laptop", "IP 搬遷今晚照做（取代撤回信）", needs_action=True,
                                             project="mem0", session="mcp", closes=a2)
check("closes returns closed ids", res["closed"] == [a2])
check("closed reason names the new message", get(a2)["metadata"]["closed_reason"].startswith("已由 " + res["result"]["results"][0]["id"]))
check("closed_by records session", get(a2)["metadata"]["closed_by"] == "claude-laptop:mcp")

# 7. 徵求意見結案：不再欠回覆
res = as_(new, "codex-laptop").message_send("claude:evalscope", "evalscope 的結論已有", closes=op_sess)
row = [r for r in as_(new, "claude-laptop").inbox(session="evalscope", projects="evalscope") if r["id"] == op_sess]
check("closed opinion no longer owed / listed", row == [])
row = [r for r in as_(new, "claude-laptop").inbox(session="evalscope", projects="evalscope", include_read=True) if r["id"] == op_sess]
check("closed opinion visible with include_read, not owed", bool(row) and not row[0]["you_owe_opinion"] and row[0]["closed"])

# 8. 取消結案
r = as_(new, "claude-laptop").message_close(a1, "x", undo=True)
check("undo clears closed", r["closed"] is None and get(a1)["metadata"]["closed_by"] == "")
check("undo brings it back to recipient inbox", "IP 搬遷核准" in subjects(as_(new, "codex-laptop").inbox()))
check("only sender/user can undo", raises(lambda: as_(new, "codex-laptop").message_close(h1, "x", undo=True)))

# 9. 同身分串間信要帶 session；沒串名寄給自己身分仍擋；別的身分寄給串不受影響
check("same-identity session mail without session rejected",
      raises(lambda: as_(new, "claude-laptop").message_send("claude-laptop:board-ui", "交辦")))
check("old-name address also rejected without session",
      raises(lambda: as_(new, "claude-laptop").message_send("claude:board-ui", "交辦")))
r = as_(new, "claude-laptop").message_send("claude-laptop:board-ui", "交辦", session="mcp")
check("same-identity session mail with session ok", r["sent"])
check("still cannot send to self without session", raises(lambda: as_(new, "claude-laptop").message_send("claude-laptop", "自己")))
r = as_(new, "codex-laptop").message_send("claude:board-ui", "Codex 直寄某串")
check("other identity to a session unaffected", r["sent"])

# 10. 舊版公告欄頁面（/board/legacy）照樣產得出來，且不含測試信
try:
    page = new._board_html()
    check("legacy board renders without test messages", "[測試] 直寄" not in page)
except Exception as e:  # BOARD_TEMPLATE 在假模組下也應能 format
    check(f"legacy board renders ({e})", False)

print()
print("ALL PASS" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
