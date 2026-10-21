"""離線測試：共用記憶硬分兩層（交流層 roundtable-ops：留言、認領；原則記憶層 roundtable：決策、筆記、進度卡）。
寫入分流、兩層合併讀取（搬舊資料前後都讀得到）、memory_search 選層、搬家前後公告欄一致、還原、14 天封存。
用假的 Mem0（記憶體裡的清單，照 user_id 分區），不連任何真實服務。
用法：python test_two_layer.py <server.py 目錄>"""
import copy
import datetime as dt
import importlib.util
import os
import sys
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


for n in ["httpx", "starlette", "starlette.concurrency", "starlette.requests", "starlette.responses",
          "mcp", "mcp.server", "mcp.server.transport_security"]:
    _stubmod(n)
_stubmod("mcp.server.fastmcp", FastMCP=FastMCP, Image=_Any)
_stubmod("experience_store", register=lambda *a, **k: None)
for k in [k for k in os.environ if k.startswith("MCP_TOKEN_")]:
    del os.environ[k]
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MSG_ARCHIVE": "0",
                   "MCP_TOKEN_CLAUDE_LAPTOP": "t1", "MCP_TOKEN_CODEX_LAPTOP": "t2"})

DB, HIST, SEARCHES, TICK = [], {}, [], [0]
MEM, OPS = "roundtable", "roundtable-ops"


_BASE = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(hours=1)  # 寫死日期的話，過 24 小時認領就變過期


def _ts():
    TICK[0] += 1
    return (_BASE + dt.timedelta(seconds=TICK[0])).isoformat()


class R:
    def __init__(self, d=None): self.d = d if d is not None else {}
    def raise_for_status(self): pass
    def json(self): return self.d


def _hit(m, filters):
    for k, v in filters.items():
        if k in ("user_id", "agent_id"):
            if m.get(k) != v:
                return False
        elif (m.get("metadata") or {}).get(k) != v:
            return False
    return True


class FakeClient:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get(self, path, params=None, **k):
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[2] == "history":
            return R(HIST.get(parts[1], []))
        if len(parts) == 2:
            return R(next((copy.deepcopy(m) for m in DB if m["id"] == parts[1]), {}))
        if path == "/entities":
            return R([])
        p = params or {}
        return R({"results": [copy.deepcopy(m) for m in DB if m["user_id"] == p.get("user_id")
                              and (not p.get("agent_id") or m["agent_id"] == p["agent_id"])]})
    def post(self, path, json=None, **k):
        if path == "/search":
            f = json["filters"]
            SEARCHES.append(f["user_id"])
            words = [w for w in json["query"].lower().split() if w]
            out = [{**copy.deepcopy(m), "score": 0.9 if any(w in m["memory"].lower() for w in words) else 0.1}
                   for m in DB if _hit(m, f)]
            out.sort(key=lambda m: -m["score"])
            return R({"results": out[: json.get("top_k") or 20]})
        mid, t = str(uuid.uuid4()), _ts()
        DB.append({"id": mid, "memory": json["messages"][0]["content"], "metadata": copy.deepcopy(json.get("metadata") or {}),
                   "agent_id": json.get("agent_id"), "user_id": json["user_id"], "created_at": t, "updated_at": t})
        HIST[mid] = [{"event": "ADD"}]
        return R({"results": [{"id": mid, "event": "ADD"}]})
    def put(self, path, json=None, **k):
        mid = path.rsplit("/", 1)[1]
        for m in DB:  # 跟 Mem0 一樣：PUT 只換內文和 metadata，user_id（在哪一層）照舊
            if m["id"] == mid:
                HIST.setdefault(mid, []).append({"event": "UPDATE", "old_memory": m["memory"], "new_memory": json["text"]})
                m["memory"], m["metadata"], m["updated_at"] = json["text"], copy.deepcopy(json["metadata"]), _ts()
        return R()
    def delete(self, path, **k):
        DB[:] = [m for m in DB if m["id"] != path.rsplit("/", 1)[1]]
        return R()


spec = importlib.util.spec_from_file_location("srv", os.path.join(SRC, "server.py"))
S = importlib.util.module_from_spec(spec)
sys.path.insert(0, SRC)
spec.loader.exec_module(S)
S.STORE.http = lambda: FakeClient()
results = []


def check(name, ok):
    results.append((name, bool(ok)))


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
        return False
    except exc:
        return True


def seed(uid, text, md, agent="codex-laptop", created=None):
    t = created or _ts()
    mid = str(uuid.uuid4())
    DB.append({"id": mid, "memory": text, "metadata": md, "agent_id": agent, "user_id": uid, "created_at": t, "updated_at": t})
    return mid


def row(mid):
    return next((m for m in DB if m["id"] == mid), None)


def new_row(before):
    return next(m for m in DB if m["id"] not in before)


def layer_types(uid):
    return sorted({(m["metadata"] or {}).get("type") for m in DB if m["user_id"] == uid})


def as_(agent):
    S.current_agent.set(agent)
    return S


check("常數：原則記憶層還是 roundtable，交流層是 roundtable-ops", S.USER_ID == MEM and S.OPS_UID == OPS)

# ── 搬家前的舊資料：全部都在 roundtable ──
d_old = seed(MEM, "[決策] 公告欄 VM119 用 docker compose 部署", {"project": "mem0", "type": "decision"}, "claude-laptop")
seed(MEM, "[筆記] Mem0 三台 VM 在 host-a", {"project": "mem0", "type": "note"})
seed(MEM, "[狀態] 名稱：圓桌記憶\n現在：測試", {"project": "mem0", "type": "status", "stage": "implementing",
                                         "updated_by": "claude-laptop"}, "claude-laptop")
c_old = seed(MEM, "[進行中] 改 vm119", {"project": "mem0", "type": "claim", "target": "vm119"})
m_old = seed(MEM, "[訊息] codex-laptop→claude-laptop:mcp｜舊信 vm120 要搬家｜請看", {
    "type": "message", "to": "claude-laptop:mcp", "from": "codex-laptop", "subject": "舊信 vm120 要搬家", "body": "請看",
    "needs_action": "yes", "read_by": "", "project": "mem0", "ref": "", "attachments": ""})

# ── 1. 寫入依類型分流 ──
as_("claude-laptop")
ids = {m["id"] for m in DB}
S.memory_add("decision", "mem0", "兩層分家：留言改寫 roundtable-ops")
check("memory_add 決策寫原則記憶層", new_row(ids)["user_id"] == MEM)
ids = {m["id"] for m in DB}
S.memory_add("note", "mem0", "筆記也留在原則記憶層")
check("memory_add 筆記寫原則記憶層", new_row(ids)["user_id"] == MEM)
ids = {m["id"] for m in DB}
S.project_set_status("pve", "名稱：PVE\n現在：測試", "implementing")
check("新的進度卡寫原則記憶層", new_row(ids)["user_id"] == MEM and new_row(ids)["metadata"]["type"] == "status")
ids = {m["id"] for m in DB}
S.claim_create("mem0", "vm120", "搬資料")
c_new = new_row(ids)
check("新的認領寫交流層", c_new["user_id"] == OPS and c_new["metadata"]["type"] == "claim")
ids = {m["id"] for m in DB}
S.message_send(to="codex-laptop", subject="新信 vm120 備份好了", project="mem0", session="mcp")
m_new = new_row(ids)
check("新的留言寫交流層", m_new["user_id"] == OPS and m_new["metadata"]["type"] == "message")
ids = {m["id"] for m in DB}
S.message_send(to="codex-laptop", subject="回覆舊信", reply_to=m_old, project="mem0", session="mcp")
m_rep = new_row(ids)
check("回覆搬家前的舊信：寫交流層、reply_to 接得上", m_rep["user_id"] == OPS and m_rep["metadata"]["reply_to"] == m_old)

# ── 2. 兩層合併讀取（搬家前：舊信在 roundtable、新信在 roundtable-ops）──
box = S.inbox(session="mcp", projects="mem0")
check("收件匣讀得到還在 roundtable 的舊信", any(x.get("id") == m_old for x in box))
S.message_ack(m_old, session="mcp")
check("ACK 舊信：已讀記上、還在原本那層", "claude-laptop:mcp" in row(m_old)["metadata"]["read_by"] and row(m_old)["user_id"] == MEM)
check("ACK 後收件匣不再列", not any(x.get("id") == m_old for x in S.inbox(session="mcp", projects="mem0")))
as_("codex-laptop")
box = S.inbox()
check("對方收件匣讀得到交流層的新信和回覆", {m_new["id"], m_rep["id"]} <= {x.get("id") for x in box})
S.message_ack(m_new["id"])
check("ACK 交流層的信：還在交流層", row(m_new["id"])["user_id"] == OPS and "codex-laptop" in row(m_new["id"])["metadata"]["read_by"])
as_("claude-laptop")
S.message_close(m_new["id"], "測試結案", session="mcp")
check("結案交流層的信：還在交流層", row(m_new["id"])["user_id"] == OPS and row(m_new["id"])["metadata"].get("closed_by") == "claude-laptop:mcp")
check("claim_check 看得到 roundtable 的舊認領", any(x["id"] == c_old for x in S.claim_check(target="vm119")))
check("claim_check 看得到交流層的新認領", any(x["id"] == c_new["id"] for x in S.claim_check(target="vm120")))
check("舊認領還在 roundtable 也擋得住別人搶同一台", raises(ValueError, S.claim_create, "mem0", "vm119", "我也要改"))
S.claim_create("mem0", "vm120", "續期")
check("續期交流層的認領：同一筆、還在交流層", row(c_new["id"])["user_id"] == OPS
      and sum(1 for m in DB if (m["metadata"] or {}).get("target") == "vm120") == 1)
check("memory_get 拿得到交流層的留言", S.memory_get(m_new["id"][:8])[0].get("id") == m_new["id"])
check("memory_list 兩層都列（type=message 有新舊兩層的信）",
      {m_old, m_new["id"]} <= {x["id"] for x in S.memory_list(type="message", limit=200)["items"]})
DB.append({**copy.deepcopy(row(m_old)), "user_id": OPS})  # 搬到一半：同一則兩邊都在
check("搬到一半同一則兩邊都在：合併列表只算一次", sum(1 for m in S._list_all() if m["id"] == m_old) == 1)
DB.pop()

# ── 3. memory_search 選層 ──
S._kw_snap.clear()
SEARCHES.clear()
res = S.memory_search("vm120")
check("預設只搜原則記憶層", SEARCHES == [MEM])
check("預設結果沒有交流層的新留言、新認領（搬家前舊信還在 roundtable，照舊搜得到）",
      res and not {m_new["id"], m_rep["id"], c_new["id"]} & {r["id"] for r in res})
SEARCHES.clear()
res = S.memory_search("vm120", messages=True)
check("messages=true 兩層都搜", SEARCHES == [MEM, OPS])
check("messages=true 結果有留言", any(r["type"] == "message" for r in res))
SEARCHES.clear()
res = S.memory_search("vm120", type="message")
check("type=message 只搜交流層、結果全是留言", SEARCHES == [OPS] and res and all(r["type"] == "message" for r in res))
SEARCHES.clear()
S.memory_search("vm120", type="claim")
check("type=claim 只搜交流層", SEARCHES == [OPS])
SEARCHES.clear()
S.memory_search("vm120", type="decision")
check("type=decision 只搜原則記憶層", SEARCHES == [MEM])
SEARCHES.clear()
S.memory_search("vm120", archived=True)
check("archived=true 只搜封存區", SEARCHES == [S.MSG_ARCHIVE_UID])
SEARCHES.clear()
res = S.memory_search("vm120", project="mem0", messages=True)
check("messages=true 加 project 篩選照樣兩層都搜", SEARCHES == [MEM, OPS] and all(r["project"] == "mem0" for r in res))

# ── 4. 搬舊資料前後：公告欄、收件匣、白板、認領一模一樣 ──
as_("codex-laptop")
before = (S._feed(500), S._whiteboard(S._list_all()), S.inbox(include_read=True),
          sorted(x["id"] for x in S.claim_check()), S.memory_list(limit=200)["total"])
moved = 0
for m in DB:  # 等同 UPDATE memories SET payload.user_id='roundtable-ops' WHERE user_id='roundtable' AND type IN (message, claim)
    if m["user_id"] == MEM and (m["metadata"] or {}).get("type") in ("message", "claim"):
        m["user_id"], moved = OPS, moved + 1
after = (S._feed(500), S._whiteboard(S._list_all()), S.inbox(include_read=True),
         sorted(x["id"] for x in S.claim_check()), S.memory_list(limit=200)["total"])
check("搬了舊的留言和認領", moved == 2)
check("搬家後 roundtable 只剩決策、筆記、進度卡", layer_types(MEM) == ["decision", "note", "status"])
check("搬家後 roundtable-ops 只有留言、認領", layer_types(OPS) == ["claim", "message"])
check("搬家前後公告欄留言流一樣", before[0] == after[0])
check("搬家前後白板一樣", before[1] == after[1])
check("搬家前後收件匣一樣", before[2] == after[2])
check("搬家前後認領一樣", before[3] == after[3])
check("搬家前後記憶總數一樣", before[4] == after[4])
as_("claude-laptop")
S._kw_snap.clear()
check("搬家後預設搜尋沒有留言", not any(r["type"] == "message" for r in S.memory_search("vm120 搬家")))
check("搬家後 type=message 搜得到舊信", any(r["id"] == m_old for r in S.memory_search("vm120 搬家", type="message")))
check("搬家後回覆舊信照樣接得上",
      S.message_send(to="codex-laptop", subject="再回一次", reply_to=m_old, project="mem0", session="mcp") is not None)
check("搬家後放掉舊認領", as_("codex-laptop").claim_release("mem0", "vm119")["released"] == 1 and row(c_old) is None)

# ── 5. 刪除後還原：依類型寫回 ──
as_("claude-laptop")
m = row(d_old)
S.memory_delete(d_old, "測試刪除", S._version(m["memory"]))
trash = next(x for x in S.memory_trash() if x["memory_id"] == d_old)
ids = {m["id"] for m in DB}
S.memory_restore(trash["trash_id"], "測試還原")
back = new_row(ids)
check("還原的決策寫回原則記憶層", back["user_id"] == MEM and back["metadata"]["type"] == "decision")

# ── 6. 14 天封存：兩層的舊留言都搬，搬完兩層都不留 ──
old_t = "2026-10-01T00:00:00+00:00"
a1 = seed(OPS, "[訊息] codex-laptop→all｜交流層的舊通知", {"type": "message", "to": "all", "from": "codex-laptop",
          "subject": "交流層的舊通知", "body": "", "needs_action": "no", "read_by": "", "project": "mem0"}, created=old_t)
a2 = seed(MEM, "[訊息] codex-laptop→all｜沒搬到的舊通知", {"type": "message", "to": "all", "from": "codex-laptop",
          "subject": "沒搬到的舊通知", "body": "", "needs_action": "no", "read_by": "", "project": "mem0"}, created=old_t)
r = S._archive_old_messages(now=dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc))
arch = {(m["metadata"] or {}).get("orig_id") for m in DB if m["user_id"] == S.MSG_ARCHIVE_UID}
check("封存：交流層的舊留言搬進封存區、原處刪掉", a1 in arch and row(a1) is None)
check("封存：還在 roundtable 的舊留言也搬", a2 in arch and row(a2) is None)
check("封存沒出錯", not (r or {}).get("error"))
check("封存區不在合併列表裡", not any(m.get("user_id") == S.MSG_ARCHIVE_UID for m in S._list_all()))

fail = [n for n, ok in results if not ok]
for n, ok in results:
    print(("通過 " if ok else "失敗 ") + n)
print(f"共 {len(results)} 項，失敗 {len(fail)} 項")
sys.exit(1 if fail else 0)
