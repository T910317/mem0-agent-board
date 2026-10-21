"""離線測試：memory_update／memory_delete／memory_trash／memory_restore／memory_history（不連任何真實服務）。
用假的 Mem0（依 user_id 分區、支援 GET/POST/PUT/DELETE）跑新版 server.py，並比對舊版其他工具輸出不變。
用法：python test_memory_edit.py <舊版 server.py 目錄> <新版 server.py 目錄>"""
import copy
import importlib.util
import os
import sys
import types
import uuid

OLD_DIR, NEW_DIR = sys.argv[1], sys.argv[2]


def rows_of(mod):
    """memory_list 的全部全文。2026-10-18 起 memory_list 分頁、預設精簡版、新的在前：逐頁拿 full=true 再照 id 排，
    跟舊版（一次全部、Mem0 原順序）比內容。"""
    try:
        out, off = [], 0
        while off is not None:
            page = mod.memory_list(full=True, limit=200, offset=off)
            out += page["items"]
            off = page["next_offset"]
    except TypeError:
        out = mod.memory_list()
    return sorted(out, key=lambda r: r["id"])


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

DB = []  # 每筆：id, memory, metadata, agent_id, user_id, created_at, updated_at
TICK = [0]
FAIL = {"archive_mangle": False}


def _ts():
    TICK[0] += 1
    return f"2026-10-16T02:{TICK[0] // 60:02d}:{TICK[0] % 60:02d}+00:00"


class R:
    def __init__(self, d=None): self.d = d if d is not None else {}
    def raise_for_status(self): pass
    def json(self): return self.d


class FakeClient:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get(self, path, params=None, **k):
        return R({"results": [copy.deepcopy(m) for m in DB if m["user_id"] == params["user_id"]][: params.get("top_k", 100)]})
    def post(self, path, json=None, **k):
        if path == "/search":
            return R({"results": []})
        text = json["messages"][0]["content"]
        if FAIL["archive_mangle"] and json["user_id"] == "roundtable-archive":
            text = text[:-5]
        mid = str(uuid.uuid4())
        t = _ts()
        DB.append({"id": mid, "memory": text, "metadata": copy.deepcopy(json.get("metadata") or {}),
                   "agent_id": json.get("agent_id"), "user_id": json["user_id"], "created_at": t, "updated_at": t,
                   "hash": "x", "role": "user"})
        return R({"results": [{"id": mid, "memory": text, "event": "ADD"}]})
    def put(self, path, json=None, **k):
        mid = path.rsplit("/", 1)[1]
        for m in DB:
            if m["id"] == mid:
                m["memory"] = json["text"]
                m["metadata"] = copy.deepcopy(json["metadata"])
                m["updated_at"] = _ts()
        return R()
    def delete(self, path, **k):
        mid = path.rsplit("/", 1)[1]
        DB[:] = [m for m in DB if m["id"] != mid]
        return R()


def load(d, name):
    sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(name, os.path.join(d, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.path.pop(0)
    if hasattr(mod, "STORE"):  # 2.0.0 起走儲存層；舊版（1.0.0）還是 _http
        mod.STORE.http = lambda: FakeClient()
    else:
        mod._http = lambda: FakeClient()
    return mod


old = load(OLD_DIR, "server_old")
new = load(NEW_DIR, "server_new")
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


def as_(who):
    new.current_agent.set(who)
    old.current_agent.set(who)
    return new


def seed(text, type_, project, agent):
    t = _ts()
    mid = str(uuid.uuid4())
    DB.append({"id": mid, "memory": text, "metadata": {"project": project, "type": type_}, "agent_id": agent,
               "user_id": "roundtable", "created_at": t, "updated_at": t, "hash": "x", "role": "user"})
    return mid


def get(mid):
    return next((m for m in DB if m["id"] == mid), None)


def ver(mid):
    return new._version(get(mid)["memory"])


n_mine = seed("[筆記] 2026-10-10 10:00 claude 的舊筆記", "note", "mem0", "claude")
n_codex = seed("[決策] 2026-10-11 11:00 codex 的決策", "decision", "pve", "codex-max")
n_dup = seed("[筆記] 2026-10-12 12:00 重複的筆記", "note", "mem0", "hermes")
s_mem0 = seed("[狀態] mem0 進度", "status", "mem0", "claude")
c_claim = seed("[進行中] 某工作", "claim", "mem0", "codex-max")
m_msg = seed("[訊息] codex-max→claude｜hi", "message", "mem0", "codex-max")
DB[-1]["metadata"].update({"to": "claude", "from": "codex-max", "subject": "hi", "read_by": ""})

# 0. 其他工具輸出不變（只多 version 欄位）
for who in ["claude", "codex-max"]:
    as_(who)
    strip = lambda rows: [{k: v for k, v in r.items() if k != "version"} for r in rows]
    check(f"memory_list unchanged except version ({who})", strip(rows_of(new)) == strip(rows_of(old)))
    check(f"claim_check unchanged except version ({who})", strip(new.claim_check()) == strip(old.claim_check()))
    ps_new, ps_old = new.project_status("mem0"), old.project_status("mem0")
    ps_new["status"] = {k: v for k, v in ps_new["status"].items() if k != "version"}
    ps_old["status"] = {k: v for k, v in ps_old["status"].items() if k != "version"}
    if "pingpong" not in ps_old:  # 10/19 起 project_status 多回乒乓開關（沒設過是 false），其餘照舊
        check(f"project_status pingpong defaults false ({who})", ps_new.pop("pingpong", None) is False)
    if "blocked_on_user" in ps_new and "blocked_on_user" not in ps_old:  # 10/19 起多回「卡在等使用者」：這裡的測試卡片沒寫在等使用者
        check(f"project_status not blocked ({who})",
              (ps_new.pop("blocked_on_user", None), ps_new.pop("blocked_why", None)) == (False, ""))
    if "priority" in ps_new and "priority" not in ps_old:  # 10/19 起多回白板優先序：這裡的測試卡片沒設，0、沒暫停
        check(f"project_status no priority ({who})", (ps_new.pop("priority", None), ps_new.pop("paused", None)) == (0, False))
    check(f"project_status unchanged ({who})", ps_new == ps_old)
check("memory_list shows version", all("version" in r for r in rows_of(as_("claude"))))

# 1. 改自己的筆記
v0 = ver(n_mine)
res = as_("claude").memory_update(n_mine, "2026-10-16 02:00 claude 的新筆記（歸納後）", v0)
check("update own note ok", res["changed"] and get(n_mine)["memory"] == "[筆記] 2026-10-16 02:00 claude 的新筆記（歸納後）")
check("update keeps type/project, sets updated_by/revision",
      get(n_mine)["metadata"]["type"] == "note" and get(n_mine)["metadata"]["project"] == "mem0"
      and get(n_mine)["metadata"]["updated_by"] == "claude" and get(n_mine)["metadata"]["revision"] == 2)
arch = [m for m in DB if m["user_id"] == "roundtable-archive"]
check("old version archived in separate namespace", len(arch) == 1 and arch[0]["metadata"]["type"] == "memory_revision")
check("archive not visible in roundtable list", all(r["id"] != arch[0]["id"] for r in rows_of(new)))
h = new.memory_history(n_mine)
check("history shows old text", len(h) == 1 and h[0]["text"] == "[筆記] 2026-10-10 10:00 claude 的舊筆記" and h[0]["by"] == "claude")
check("update auto-adds date when missing", as_("claude").memory_update(n_mine, "沒寫日期", ver(n_mine))["changed"]
      and __import__("re").match(r"\[筆記\] \d{4}-\d{2}-\d{2} \d{2}:\d{2} 沒寫日期", get(n_mine)["memory"]) is not None)
check("same content -> unchanged, no archive", not as_("claude").memory_update(n_mine, get(n_mine)["memory"][5:], ver(n_mine))["changed"]
      and len([m for m in DB if m["user_id"] == "roundtable-archive"]) == 2)

# 2. 版本、權限、種類
check("stale base_version refused", raises(as_("claude").memory_update, n_mine, "x", "deadbeef0000"))
check("missing base_version refused", raises(as_("claude").memory_update, n_mine, "x", ""))
check("update others' memory without reason refused", raises(as_("claude").memory_update, n_codex, "改", ver(n_codex)))
check("update others' memory with reason ok",
      as_("claude").memory_update(n_codex, "codex 的決策（補正）", ver(n_codex), reason="補正錯字")["changed"])
check("archive records reason and author", any(m["metadata"].get("reason") == "補正錯字" and m["metadata"].get("author") == "codex-max"
                                               for m in DB if m["user_id"] == "roundtable-archive"))
for mid, name in [(s_mem0, "status"), (c_claim, "claim"), (m_msg, "message")]:
    check(f"update {name} refused", raises(as_("claude").memory_update, mid, "x", ver(mid)))
check("tag in content cannot change type", (as_("claude").memory_update(n_mine, "[決策] 冒充", ver(n_mine)) or True)
      and get(n_mine)["memory"].startswith("[筆記] ") and "[決策]" not in get(n_mine)["memory"])

# 3. 刪除
check("delete without reason refused", raises(as_("hermes").memory_delete, n_dup, "", ver(n_dup)))
check("delete stale base refused", raises(as_("hermes").memory_delete, n_dup, "重複", "000000000000"))
for mid, name in [(c_claim, "claim"), (m_msg, "message")]:
    check(f"delete {name} refused", raises(as_("claude").memory_delete, mid, "x", ver(mid)))
dup_text = get(n_dup)["memory"]
d = as_("claude").memory_delete(n_dup, f"已併入 {n_mine}", ver(n_dup))
check("delete removes from roundtable", get(n_dup) is None and all(r["id"] != n_dup for r in rows_of(new)))
tr = new.memory_trash()
check("trash lists who/when/why/text", len(tr) == 1 and tr[0]["trash_id"] == d["trash_id"] and tr[0]["deleted_by"] == "claude"
      and tr[0]["reason"] == f"已併入 {n_mine}" and tr[0]["text"] == dup_text and tr[0]["author"] == "hermes" and tr[0]["deleted_at"])
check("trash project filter", new.memory_trash(project="pve") == [] and len(new.memory_trash(project="mem0")) == 1)

# 4. 還原
r = as_("codex-max").memory_restore(d["trash_id"], reason="刪錯了")
back = get(r["restored"])
check("restore recreates same text/author/type/project", back and back["memory"] == dup_text and back["agent_id"] == "hermes"
      and back["metadata"]["type"] == "note" and back["metadata"]["project"] == "mem0" and back["metadata"]["orig_id"] == n_dup
      and back["metadata"]["restored_by"] == "codex-max")
check("restored hidden from trash by default", new.memory_trash() == [] and len(new.memory_trash(include_restored=True)) == 1)
check("second restore refused", raises(as_("claude").memory_restore, d["trash_id"]))
h2 = new.memory_history(r["restored"])
check("history of restored id includes old-id deletion", any(x["kind"] == "刪除" and x["memory_id"] == n_dup for x in h2))

# 5. status 刪除與還原
ds = as_("claude").memory_delete(s_mem0, "專案已結束", ver(s_mem0))
check("status deleted", new.project_status("mem0")["status"] is None)
as_("claude").project_set_status("mem0", "新的進度")
check("restore old status refused when new status exists", raises(as_("claude").memory_restore, ds["trash_id"]))
new_status = [m for m in DB if m["user_id"] == "roundtable" and m["metadata"].get("type") == "status"][0]
as_("claude").memory_delete(new_status["id"], "測試", new._version(new_status["memory"]))
check("restore old status ok when none exists", bool(as_("claude").memory_restore(ds["trash_id"])["restored"])
      and new.project_status("mem0")["status"]["text"] == "[狀態] mem0 進度")

# 6. 封存失敗就不動原記憶
FAIL["archive_mangle"] = True
before = copy.deepcopy(get(n_codex))
check("archive mismatch -> update aborted", raises(as_("codex-max").memory_update, n_codex, "會失敗", ver(n_codex)))
check("original untouched after failed archive (update)", get(n_codex) == before)
check("archive mismatch -> delete aborted", raises(as_("codex-max").memory_delete, n_codex, "x", ver(n_codex)))
check("original untouched after failed archive (delete)", get(n_codex) == before)
FAIL["archive_mangle"] = False
check("bad id format refused", raises(new.memory_delete, "not-an-id", "x", "y") and raises(new.memory_history, "zzz"))

print("\nFAILED:" if fails else "\nALL PASS", fails if fails else "")
sys.exit(1 if fails else 0)
