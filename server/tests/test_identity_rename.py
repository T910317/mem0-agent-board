"""離線測試：claude 改名 claude-laptop（只改 .env 變數名、不搬資料）前後的行為。
同一份假資料：舊版程式用 claude 身分 vs 新版程式用 claude-laptop 身分，看到的要一樣；舊地址、舊 claim、舊票、回復都要正常。
用法：python test_identity_rename.py <舊版 server.py 目錄> <新版 server.py 目錄>"""
import copy
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


class FastMCP:
    def __init__(self, *a, **k): pass
    def tool(self, *a, **k): return lambda f: f
    def __getattr__(self, n): return _Any()


for n in ["httpx", "starlette", "starlette.concurrency", "starlette.requests", "starlette.responses",
          "mcp", "mcp.server", "mcp.server.transport_security"]:
    _stubmod(n)
_stubmod("mcp.server.fastmcp", FastMCP=FastMCP, Image=_Any)
_stubmod("experience_store", register=lambda *a, **k: None)

BASE_ENV = {"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CODEX_LAPTOP": "t2", "MCP_TOKEN_CODEX_MAX": "t3",
            "MCP_TOKEN_HERMES": "t4", "MCP_TOKEN_HERMES_ALPHA": "t5"}
DB = []
TICK = [0]


def _ts():
    TICK[0] += 1
    return f"2026-10-15T{TICK[0] // 3600:02d}:{TICK[0] // 60 % 60:02d}:{TICK[0] % 60:02d}+00:00"


class R:
    def __init__(self, d=None): self.d = d if d is not None else {}
    def raise_for_status(self): pass
    def json(self): return self.d


class FakeClient:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get(self, path, params=None, **k):
        if path == "/entities":
            return R([])
        return R({"results": [copy.deepcopy(m) for m in DB if m["user_id"] == params["user_id"]]})
    def post(self, path, json=None, **k):
        mid = str(uuid.uuid4())
        t = _ts()
        DB.append({"id": mid, "memory": json["messages"][0]["content"], "metadata": copy.deepcopy(json.get("metadata") or {}),
                   "agent_id": json.get("agent_id"), "user_id": json["user_id"], "created_at": t, "updated_at": t})
        return R({"results": [{"id": mid}]})
    def put(self, path, json=None, **k):
        mid = path.rsplit("/", 1)[1]
        for m in DB:
            if m["id"] == mid:
                m["memory"], m["metadata"], m["updated_at"] = json["text"], copy.deepcopy(json["metadata"]), _ts()
        return R()
    def delete(self, path, **k):
        DB[:] = [m for m in DB if m["id"] != path.rsplit("/", 1)[1]]
        return R()


def load(d, name, claude_var):
    for k in list(os.environ):
        if k.startswith("MCP_TOKEN_"):
            del os.environ[k]
    os.environ.update(BASE_ENV)
    os.environ[claude_var] = "t1"
    sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(name, os.path.join(d, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.path.pop(0)
    if hasattr(mod, "STORE"):  # 2.0.0 起走儲存層；舊版（1.0.0）還是 _http
        mod.STORE.http = lambda: FakeClient()
    else:
        mod._http = lambda: FakeClient()
    mod._proj = lambda p: p.strip().lower()
    return mod


old = load(OLD_DIR, "server_old", "MCP_TOKEN_CLAUDE")          # 改名前：舊程式、身分 claude
new = load(NEW_DIR, "server_new", "MCP_TOKEN_CLAUDE_LAPTOP")    # 改名後：新程式、身分 claude-laptop
back = load(NEW_DIR, "server_back", "MCP_TOKEN_CLAUDE")        # 回復：新程式、身分改回 claude
fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def raises(fn, *a, **k):
    try:
        fn(*a, **k)
        return False
    except (ValueError, RuntimeError):
        return True


def as_(mod, who):
    mod.current_agent.set(who)
    return mod


def msg(frm, to, subj, project="general", read_by="", agent=None, **extra):
    md = {"type": "message", "to": to, "from": frm, "subject": subj, "body": "", "needs_action": "no",
          "read_by": read_by, "project": project, "ref": "", "attachments": ""}
    md.update(extra)
    mid = str(uuid.uuid4())
    t = _ts()
    DB.append({"id": mid, "memory": f"[訊息] {frm}→{to}｜{subj}", "metadata": md, "agent_id": agent or frm,
               "user_id": "roundtable", "created_at": t, "updated_at": t})
    return mid


def subjects(rows): return sorted(r["subject"] for r in rows)
def _cn(n): return "claude-laptop" if (n or "").split(":")[0] == "claude" and ":" not in (n or "") else ("claude-laptop:" + n.split(":", 1)[1] if (n or "").startswith("claude:") else n)


def norm(rows):
    """比對用：去掉 from_session；徵求意見名單裡的舊名 claude 換成 claude-laptop（同一個身分，只是名字換了）。"""
    out = []
    for r in rows:
        r = {k: v for k, v in r.items() if k not in ("from_session",)}
        if r.get("opinion"):
            r["opinion"] = {k: sorted(_cn(n) for n in v) for k, v in r["opinion"].items()}
        out.append(r)
    return out


# ---- 改名前就存在的資料（全部用舊名 claude）----
msg("codex-max", "claude", "寄給 claude 未讀", "mem0")
msg("codex-max", "claude", "寄給 claude 已讀", "mem0", read_by="claude")
msg("hermes", "all", "寄給 all 本串已讀", "mem0", read_by="claude:mem0-deploy")
msg("codex-max", "claude:mem0-deploy", "直寄舊串地址", "mem0")
msg("claude", "all", "claude 自己寄的", "roundtable")
mine_q = msg("claude", "codex-max", "claude 串發問", "mem0", from_session="mem0-deploy")
msg("codex-max", "claude", "回覆 claude 串", "pve", reply_to=mine_q)
ask = msg("codex-max", "claude", "徵求 claude 意見", "mem0", ask_opinion="yes", read_by="claude")
msg("claude", "codex-max", "claude 的回覆", "mem0", reply_to=ask)
ask_all = msg("claude", "all", "claude 徵求大家", "mem0", ask_opinion="yes")
msg("codex-max", "claude", "被投過票的", "mem0", votes='{"claude": 1}', read_by="claude")
claim_id = str(uuid.uuid4())
DB.append({"id": claim_id, "memory": "[進行中] board", "metadata": {"type": "claim", "project": "roundtable", "target": "board-preview"},
           "agent_id": "claude", "user_id": "roundtable", "created_at": _ts(), "updated_at": _ts()})
note_id = str(uuid.uuid4())
DB.append({"id": note_id, "memory": "[筆記] 2026-10-10 10:00 claude 寫的", "metadata": {"type": "note", "project": "mem0"},
           "agent_id": "claude", "user_id": "roundtable", "created_at": _ts(), "updated_at": _ts()})

# 1. 改名後看到的跟改名前一樣（claude → claude-laptop）
for kw in [{}, {"include_read": True}, {"session": "mem0-deploy", "projects": "mem0"},
           {"session": "mem0-deploy", "projects": "mem0", "include_read": True}, {"session": "navigator", "projects": "*,-mem0"}]:
    a = norm(as_(old, "claude").inbox(**kw))
    b = norm(as_(new, "claude-laptop").inbox(**kw))
    check(f"inbox same after rename {kw}", a == b)
check("old unread still unread, old read still read",
      "寄給 claude 未讀" in subjects(as_(new, "claude-laptop").inbox()) and "寄給 claude 已讀" not in subjects(new.inbox()))
check("old per-session read (claude:mem0-deploy) still read",
      "寄給 all 本串已讀" not in subjects(as_(new, "claude-laptop").inbox(session="mem0-deploy", projects="mem0")))
check("old direct address claude:mem0-deploy delivered",
      "直寄舊串地址" in subjects(as_(new, "claude-laptop").inbox(session="mem0-deploy", projects="mem0")))
check("own old messages (from claude) not shown as incoming", "claude 自己寄的" not in subjects(as_(new, "claude-laptop").inbox(include_read=True)))
check("reply to old session message still routed back",
      "回覆 claude 串" in subjects(as_(new, "claude-laptop").inbox(session="mem0-deploy", projects="mem0")))
check("old answered opinion not owed again",
      "徵求 claude 意見" not in subjects(as_(new, "claude-laptop").inbox()))
op = new._opinions([m for m in DB if m["id"] == ask_all][0], DB)
check("old ask by claude does not expect claude-laptop to answer", "claude-laptop" not in op["pending"] and "claude" not in op["pending"])

# 2. Codex／Hermes 不受影響
for who in ["codex-max", "codex-laptop", "hermes", "hermes-alpha"]:
    check(f"other identity inbox same: {who}", norm(as_(old, who).inbox(include_read=True)) == norm(as_(new, who).inbox(include_read=True)))
check("recipients still have claude (as group) and add claude-laptop",
      {"claude", "claude-laptop", "codex", "hermes"} <= new._recipients())
check("groups for codex/hermes unchanged", {"codex", "hermes"} <= new._groups() and old._groups() == {"codex", "hermes"})

# 3. 舊地址寄信照收
as_(new, "codex-max").message_send(to="claude", subject="新寄 to=claude", project="mem0")
as_(new, "hermes").message_send(to="claude:mem0-deploy", subject="新寄舊串地址", project="mem0")
as_(new, "codex-max").message_send(to="claude-laptop:mem0-deploy", subject="新寄新串地址", project="mem0")
as_(new, "codex-max").message_send(to="claude-laptop", subject="新寄 to=claude-laptop", project="mem0")
got = subjects(as_(new, "claude-laptop").inbox(session="mem0-deploy", projects="mem0"))
check("to=claude delivered", "新寄 to=claude" in got)
check("to=claude:label delivered", "新寄舊串地址" in got)
check("to=claude-laptop:label delivered", "新寄新串地址" in got)
check("to=claude-laptop delivered", "新寄 to=claude-laptop" in got)
check("codex does not get claude mail", not ({"新寄 to=claude", "新寄舊串地址"} & set(subjects(as_(new, "codex-max").inbox()))))

# 4. 寄給自己的判斷
check("claude-laptop -> claude (only itself) refused without session", raises(as_(new, "claude-laptop").message_send, to="claude", subject="x"))
check("claude-laptop -> claude allowed with session", bool(as_(new, "claude-laptop").message_send(to="claude", subject="交給別串", project="pve", session="mem0-deploy")))
check("session to own label (old form) refused", raises(as_(new, "claude-laptop").message_send, to="claude:mem0-deploy", subject="x", session="mem0-deploy"))
check("hermes -> hermes still refused (unchanged)", raises(as_(new, "hermes").message_send, to="hermes", subject="x"))
check("codex-max -> codex still allowed", bool(as_(new, "codex-max").message_send(to="codex", subject="給 codex 組")))

# 5. ACK：寫正式名，不重複寫
dmid = [m["id"] for m in DB if m["metadata"].get("subject") == "直寄舊串地址"][0]
r = as_(new, "claude-laptop").message_ack(dmid, session="mem0-deploy")
check("ack writes canonical claude-laptop:label", r["by"] == "claude-laptop:mem0-deploy")
rid = [m["id"] for m in DB if m["metadata"].get("subject") == "寄給 all 本串已讀"][0]
as_(new, "claude-laptop").message_ack(rid, session="mem0-deploy")
check("ack does not duplicate when old-name read exists",
      [m for m in DB if m["id"] == rid][0]["metadata"]["read_by"] == "claude:mem0-deploy")

# 6. claim、記憶、票、人格
r = as_(new, "claude-laptop").claim_create("roundtable", "board-preview", "續期")
check("old claude claim renewable (not blocked)", "renewed" in r)
check("old claude claim releasable", as_(new, "claude-laptop").claim_release("roundtable", "board-preview")["released"] == 1)
check("memory by old name counts as own (no reason needed)",
      as_(new, "claude-laptop").memory_update(note_id, "改過", new._version([m for m in DB if m["id"] == note_id][0]["memory"]))["changed"])
vid = [m["id"] for m in DB if m["metadata"].get("subject") == "被投過票的"][0]
check("old vote shows as mine", new._votes([m for m in DB if m["id"] == vid][0], "claude-laptop")["mine"] == 1)
res = as_(new, "claude-laptop").message_vote(vid, 1)
check("re-vote does not double count", res["up"] == 1 and res["mine"] == 1)
check("persona writer permission kept", bool(new._names("claude-laptop") & new.PERSONA_WRITERS))

# 7. 回復（新程式、身分改回 claude）：改名期間寫的 claude-laptop 紀錄算自己的
b_rows = norm(as_(back, "claude").inbox(session="mem0-deploy", projects="mem0", include_read=True))
n_rows = norm(as_(new, "claude-laptop").inbox(session="mem0-deploy", projects="mem0", include_read=True))
check("rollback sees same session mailbox", b_rows == n_rows)
check("rollback: ack by claude-laptop counts as read",
      not any(r["subject"] == "直寄舊串地址" for r in as_(back, "claude").inbox(session="mem0-deploy", projects="mem0")))
check("rollback: recipients back to normal (no claude group)", "claude" in back._recipients() and "claude" not in back._groups())

print("\nFAILED:" if fails else "\nALL PASS", fails if fails else "")
sys.exit(1 if fails else 0)
