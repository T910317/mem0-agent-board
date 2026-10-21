"""離線測試：撤回（message_recall／公告欄撤回）。沒撤回時輸出跟改版前一樣；撤回後收件匣、待辦、徵求意見、結案的互動。
用法：python test_message_recall.py <改版前 server.py 目錄> <改版後 server.py 目錄>（不連任何真實服務）"""
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

# ---- 資料 ----
msg("codex-pc", "codex-laptop", "PC 很久以前的信", "roundtable", read_by="codex-laptop", days_ago=10)
a1 = msg("claude-laptop", "codex-laptop", "寄錯的交辦", "mem0", needs_action="yes", from_session="master", days_ago=1)
a2 = msg("claude-laptop", "codex-laptop", "已讀過的交辦", "mem0", needs_action="yes", from_session="master",
         read_by="codex-laptop", days_ago=1)
a3 = msg("claude", "codex-laptop", "舊名、沒記串名的信", "mem0", needs_action="yes", days_ago=1)
g1 = msg("codex-laptop", "claude", "寄給全部 Claude", "roundtable", read_by="claude-laptop:navigator", days_ago=1)
u1 = msg("user", "hermes-gamma", "使用者交辦 Hermes", needs_action="yes", days_ago=1)
op = msg("claude-laptop", "all", "徵求意見：放哪", "roundtable", ask_opinion="yes", from_session="mcp", days_ago=1)
r1 = msg("codex-laptop", "claude-laptop:mcp", "意見：放 VM119", "roundtable", reply_to=op, days_ago=0.5)
r2 = msg("hermes-gamma", "claude-laptop:mcp", "意見：放 LXC", "roundtable", reply_to=op, days_ago=0.5)
k1 = msg("claude-laptop", "codex-laptop", "已結案的信", "mem0", needs_action="yes", from_session="master",
         closed_by="claude-laptop:master", closed_at="2026-10-16 10:00", closed_reason="已完成", days_ago=1)
w1 = msg("claude-laptop", "codex-laptop", "回覆寄錯的交辦", "mem0", reply_to=a1, from_session="master", days_ago=0.5)

IDS = ["claude-laptop", "codex-laptop", "codex-pc", "hermes-gamma"]
SESS = [("navigator", "*,-mem0"), ("mcp", ""), ("master", "*")]

# 1. 還沒撤回任何一則：收件匣、公告欄資料都跟改版前一樣（含已結案那則的輸出）
for who in IDS:
    for inc in (False, True):
        check(f"inbox same before recall: {who} include_read={inc}",
              as_(old, who).inbox(include_read=inc) == as_(new, who).inbox(include_read=inc))
for s, p in SESS:
    for inc in (False, True):
        check(f"session inbox same before recall: {s} include_read={inc}",
              as_(old, "claude-laptop").inbox(session=s, projects=p, include_read=inc)
              == as_(new, "claude-laptop").inbox(session=s, projects=p, include_read=inc))
fo, fn = old._feed(500), new._feed(500)
if "avatars" in fn and "avatars" not in fo:  # 10/19 起 feed 多帶 avatars（AI 頭像；multi_to 要頁面帶參數才給），其餘照舊
    check("feed avatars / no multi_to", (fn.pop("avatars", None), "multi_to" in fn) == ({}, False))
check("feed same before recall", fo == fn)
check("normal close output keeps only by/at/reason", set(new._closed(get(k1))) == {"by", "at", "reason"})

# 2. 權限
check("recipient cannot recall", raises(lambda: as_(new, "codex-laptop").message_recall(a1, "不想做")))
check("other identity cannot recall", raises(lambda: as_(new, "hermes-gamma").message_recall(a1, "x")))
check("same identity, other session cannot recall", raises(lambda: as_(new, "claude-laptop").message_recall(a1, "x", session="mcp")))
check("same identity without session cannot recall a session-sent message",
      raises(lambda: as_(new, "claude-laptop").message_recall(a1, "x")))
check("reason required", raises(lambda: as_(new, "claude-laptop").message_recall(a1, "  ", session="master")))
check("AI cannot recall user's message", raises(lambda: as_(new, "claude-laptop").message_recall(u1, "x", session="master")))
check("undo when not recalled rejected", raises(lambda: as_(new, "claude-laptop").message_recall(a1, "x", session="master", undo=True)))

# 3. 撤回沒人讀過的：收件者不再看到；include_read 才看到並標 kind=recall；不算待辦
n_todo = new._feed(500)["todo"]
r = as_(new, "claude-laptop").message_recall(a1, "寄錯對象", session="master")
check("recall result", r["closed"]["kind"] == "recall" and r["closed"]["by"] == "claude-laptop:master"
      and r["closed"]["reason"] == "寄錯對象" and r["notified"] == [])
check("replies counted before recall", r["closed"]["replies"] == 1)
check("read_by untouched", get(a1)["metadata"]["read_by"] == "")
check("unread recipient no longer sees it", a1 not in [x["id"] for x in as_(new, "codex-laptop").inbox()])
row = [x for x in as_(new, "codex-laptop").inbox(include_read=True) if x["id"] == a1]
check("include_read shows recalled", bool(row) and row[0]["closed"]["kind"] == "recall")
check("recalled is not todo", new._feed(500)["todo"] == n_todo - 1)
fm = {m["id"]: m for m in new._feed(500)["messages"]}
check("feed carries recall", fm[a1]["closed"]["kind"] == "recall" and fm[a1]["todo"] is False)
row = [x for x in as_(new, "codex-laptop").inbox(include_read=True) if x["id"] == w1]
check("reply_to_info shows parent recalled", bool(row) and row[0]["reply_to_info"]["closed"]["kind"] == "recall")

# 4. 撤回已讀過的：讀過的人收件匣再列一次，ACK 後消失；已讀名單不變
r = as_(new, "claude-laptop").message_recall(a2, "內容錯了，會重寄", session="master")
check("notified lists readers", r["notified"] == ["codex-laptop"])
L = as_(new, "codex-laptop").inbox()
check("reader sees recall notice", [x["closed"]["kind"] for x in L if x["id"] == a2] == ["recall"])
as_(new, "codex-laptop").message_ack(a2)
check("ack records recall_seen", get(a2)["metadata"]["recall_seen"] == "codex-laptop")
check("read_by not duplicated", get(a2)["metadata"]["read_by"] == "codex-laptop")
check("notice gone after ack", a2 not in [x["id"] for x in as_(new, "codex-laptop").inbox()])

# 5. 串：哪一串讀過就只有那一串再看到
as_(new, "codex-laptop").message_recall(g1, "寄錯了")
check("reading session sees notice",
      g1 in [x["id"] for x in as_(new, "claude-laptop").inbox(session="navigator", projects="*,-mem0")])
check("other session does not", g1 not in [x["id"] for x in as_(new, "claude-laptop").inbox(session="master", projects="*")])
as_(new, "claude-laptop").message_ack(g1, session="navigator")
check("session notice gone after its ack",
      g1 not in [x["id"] for x in as_(new, "claude-laptop").inbox(session="navigator", projects="*,-mem0")])

# 6. 沒記串名的舊信：同身分任何一串都能撤回；使用者能撤回任何一則
r = as_(new, "claude-laptop").message_recall(a3, "舊信作廢", session="mcp")
check("old-name, no from_session: any session of the identity can recall", r["closed"]["by"] == "claude-laptop:mcp")
r = new._recall("user", u1, "使用者撤回")
check("user can recall", r["closed"]["by"] == "user" and "使用者交辦 Hermes" not in subjects(as_(new, "hermes-gamma").inbox()))

# 7. 跟結案的互動
check("close on recalled rejected", raises(lambda: as_(new, "claude-laptop").message_close(a1, "x", session="master")))
check("close-undo on recalled rejected", raises(lambda: as_(new, "claude-laptop").message_close(a1, "x", session="master", undo=True)))
check("user close on recalled rejected", raises(lambda: new._close("user", a1, "x")))
n0 = len(STORE)
check("closes with a recalled id rejected",
      raises(lambda: as_(new, "claude-laptop").message_send("codex-laptop", "新交辦", session="master", closes=a1)))
check("nothing sent when closes rejected", len(STORE) == n0)
r = new._recall("user", k1, "其實寄錯了")
check("closed message can still be recalled", r["closed"]["kind"] == "recall")
check("recall twice rejected", raises(lambda: new._recall("user", k1, "again")))

# 8. 徵求意見：撤回的回覆不算回了；撤回徵求意見本身就不再欠回覆
check("before: both replied", new._opinions(get(op), STORE)["responded"] == ["codex-laptop", "hermes-gamma"])
as_(new, "hermes-gamma").message_recall(r2, "回錯了")
o = new._opinions(get(op), STORE)
check("recalled reply no longer counts", o["responded"] == ["codex-laptop"] and "hermes-gamma" in o["pending"])
row = [x for x in as_(new, "hermes-gamma").inbox() if x["id"] == op]
check("hermes owes opinion again", bool(row) and row[0]["you_owe_opinion"])
r = as_(new, "claude-laptop").message_recall(op, "問題問錯了", session="mcp")
check("opinion recall counts only live replies", r["closed"]["replies"] == 1)
check("recalled opinion no longer owed", op not in [x["id"] for x in as_(new, "hermes-gamma").inbox()])

# 9. 取消撤回
r = as_(new, "claude-laptop").message_recall(a1, "x", session="master", undo=True)
check("undo clears", r["closed"] is None and get(a1)["metadata"]["closed_kind"] == "" and get(a1)["metadata"]["recall_notify"] == "")
check("undo brings it back to unread recipient", a1 in [x["id"] for x in as_(new, "codex-laptop").inbox()])
check("after undo it is todo again", {m["id"]: m for m in new._feed(500)["messages"]}[a1]["todo"])
check("after undo, close works again", bool(as_(new, "claude-laptop").message_close(a1, "做完了", session="master")["closed"]["by"]))

# 10. 舊版公告欄頁面照樣產得出來
try:
    new._board_html()
    check("legacy board renders", True)
except Exception as e:
    check(f"legacy board renders ({e})", False)

print()
print("ALL PASS" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
