"""離線測試：舊留言自動封存、搜尋與列表的精簡版、memory_get（2026-10-18）。
用法：python test_msg_archive.py <改版後 server.py 目錄>（用假的 Mem0，不連任何真實服務）"""
import datetime as dt
import importlib.util
import json
import os
import sys
import threading
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
for k in [k for k in os.environ if k.startswith("MCP_TOKEN_")]:
    del os.environ[k]
os.environ.pop("MSG_ARCHIVE", None)
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma",
                   "MCP_TOKEN_CLAUDE_LAPTOP": "t1", "MCP_TOKEN_CODEX_LAPTOP": "t2"})

NOW = dt.datetime.now(dt.timezone.utc)
DB = {}            # 假的 Mem0：id → 紀錄
CALLS = []
FAULT = {"add_mangle": False, "delete_noop": False}


def ago(days):
    return (NOW - dt.timedelta(days=days)).isoformat()


def put(i, text, user_id="roundtable", agent="codex-laptop", days=20, **md):
    DB[i] = {"id": i, "memory": text, "user_id": user_id, "agent_id": agent, "metadata": dict(md),
             "created_at": ago(days), "updated_at": ago(days)}
    return DB[i]


def msg(i, days=20, to="claude-laptop", frm="codex-laptop", subject="主旨", needs_action="no", read_by="claude-laptop", **md):
    return put(i, f"[訊息] {frm}→{to}｜{subject}", agent=frm, days=days, type="message", to=to, **{"from": frm},
               subject=subject, body="", needs_action=needs_action, read_by=read_by, project=md.pop("project", "mem0"),
               ref="", attachments="", **md)


def uid(n):
    return str(uuid.UUID(int=n))


class Resp:
    def __init__(self, d, code=200): self.d, self.status_code = d, code
    def raise_for_status(self): pass
    def json(self): return self.d


def _out(r):
    return {k: (dict(v) if k == "metadata" else v) for k, v in r.items()}


class Client:
    def __enter__(self): return self
    def __exit__(self, *a): return False

    def get(self, path, params=None):
        CALLS.append(("get", path, (params or {}).get("user_id")))
        if path == "/memories":
            return Resp({"results": [_out(r) for r in DB.values() if r["user_id"] == params["user_id"]]})
        r = DB.get(path[len("/memories/"):])
        return Resp(_out(r) if r else None)

    def post(self, path, json=None):
        CALLS.append(("post", path, json))
        if path == "/memories":
            i = str(uuid.uuid4())
            md = dict(json.get("metadata") or {})
            if FAULT["add_mangle"]:
                md["subject"] = "被改掉了"
            DB[i] = {"id": i, "memory": json["messages"][0]["content"], "user_id": json["user_id"],
                     "agent_id": json.get("agent_id"), "metadata": md,
                     "created_at": NOW.isoformat(), "updated_at": NOW.isoformat()}
            return Resp({"results": [{"id": i, "memory": DB[i]["memory"], "event": "ADD"}]})
        f = dict(json["filters"])
        zone = f.pop("user_id")
        out = []
        for r in DB.values():
            if r["user_id"] != zone:
                continue
            if any((r["agent_id"] if k == "agent_id" else r["metadata"].get(k)) != v for k, v in f.items()):
                continue
            score = 0.8 if json["query"] in r["memory"] else 0.3
            out.append({**_out(r), "score": score})
        out.sort(key=lambda x: -x["score"])
        return Resp({"results": out[: json.get("top_k") or 20]})

    def put(self, path, json=None):
        r = DB[path[len("/memories/"):]]
        r["memory"], r["metadata"] = json["text"], dict(json["metadata"])
        return Resp({})

    def delete(self, path):
        CALLS.append(("delete", path, None))
        if not FAULT["delete_noop"]:
            DB.pop(path[len("/memories/"):], None)
        return Resp({"message": "ok"})


sys.path.insert(0, NEW_DIR)
spec = importlib.util.spec_from_file_location("srv_new", os.path.join(NEW_DIR, "server.py"))
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)
S.STORE.http = lambda: Client()
S.current_agent.set("claude-laptop")
fails = []


def check(name, cond):
    print(("通過 " if cond else "失敗 ") + name)
    if not cond:
        fails.append(name)


def reset():
    DB.clear(); CALLS.clear(); S._kw_snap.clear()
    FAULT.update(add_mangle=False, delete_noop=False)


def zone(z):
    return {r["id"]: r for r in DB.values() if r["user_id"] == z}


ARCH = S.MSG_ARCHIVE_UID

# ── 1. 哪些留言要封存 ──
reset()
msg(uid(1), days=20)                                                     # 舊、讀過 → 搬
msg(uid(2), days=10)                                                     # 不到 14 天 → 留
msg(uid(3), days=20, needs_action="yes", read_by="")                     # 舊但還沒人做的待辦 → 留
msg(uid(4), days=20, needs_action="yes", read_by="", closed_by="user")   # 待辦已結案 → 搬
msg(uid(5), days=20, to="user", needs_action="no", read_by="")           # 寫給使用者的通知 → 搬
msg(uid(6), days=20, to="user", needs_action="yes", read_by="")          # 等使用者處理 → 留
msg(uid(7), days=20, ask_opinion="yes")                                  # 徵求意見沒結案 → 留
msg(uid(8), days=20, ask_opinion="yes", closed_by="codex-laptop")         # 徵求意見結案了 → 搬
msg(uid(9), days=30)                                                     # 原信很舊
msg(uid(10), days=3, reply_to=uid(9))                                    # 但有新回覆 → 整串留
msg(uid(11), days=30)                                                    # 原信很舊
msg(uid(12), days=20, reply_to=uid(11))                                  # 回覆也舊
msg(uid(13), days=16, reply_to=uid(12))                                  # 回覆的回覆也舊 → 整串搬
msg(uid(14), days=20, read_by="", needs_action="no")                     # 沒人讀的通知，不用動手 → 搬
msg(uid(15), days=20, project="inbox-test")                              # 上線測試信 → 搬
msg(uid(16), days=20)                                                    # 讀過
msg(uid(17), days=20, reply_to=uid(16), needs_action="yes", read_by="")  # 但回覆是還沒做的待辦 → 整串留
put(uid(20), "[筆記] 很舊的筆記", days=60, type="note", project="mem0")
put(uid(21), "[決策] 很舊的決策", days=60, type="decision", project="mem0")
put(uid(22), "[狀態] 很舊的進度", days=60, type="status", project="mem0")
due = {m["id"] for m in S._archive_due(list(zone("roundtable").values()), NOW)}
expect = {uid(n) for n in (1, 4, 5, 8, 11, 12, 13, 14, 15)}
check("封存名單照規則（舊、不是待辦、整串一起）", due == expect)
if due != expect:
    print("   多：", sorted(due - expect), "少：", sorted(expect - due))

# ── 2. 搬一輪：副本照抄、原留言刪掉、其他不動 ──
before = {i: json.dumps(r, sort_keys=True) for i, r in zone("roundtable").items()}
S.MSG_ARCHIVE_BATCH = 4  # 讓它分好幾批
res = S._archive_old_messages(now=NOW)
rt, ar = zone("roundtable"), zone(ARCH)
check("搬了 9 則、剩 0 則、沒有錯", (res["moved"], res["left"], res["error"]) == (9, 0, ""))
check("搬走的不在共用區", not (expect & set(rt)))
check("沒搬的一字不動", all(json.dumps(rt[i], sort_keys=True) == before[i] for i in rt))
check("封存區正好 9 份副本", len(ar) == 9)
copy_ok = True
for a in ar.values():
    o = json.loads(before[a["metadata"]["orig_id"]])
    am = a["metadata"]
    copy_ok &= (a["memory"] == o["memory"] and a["agent_id"] == o["agent_id"]
                and all(am.get(k) == v for k, v in o["metadata"].items())
                and am["orig_created"] == o["created_at"] and am["orig_updated"] == o["updated_at"] and bool(am.get("archived_at")))
check("副本內文、作者、所有欄位照抄，另記原編號與原時間", copy_ok)
check("筆記、決策、狀態不搬", {uid(20), uid(21), uid(22)} <= set(rt))
res = S._archive_old_messages(now=NOW)
check("再跑一次沒有東西可搬", (res["moved"], res["left"]) == (0, 0) and len(zone(ARCH)) == 9)

# ── 3. 上次搬到一半（副本存了、原留言沒刪）：不再存第二份 ──
reset()
m = msg(uid(30), days=20)
snap = json.loads(json.dumps(m))
S._archive_msg(m)
DB[uid(30)] = snap  # 假裝原留言沒刪掉
res = S._archive_old_messages(now=NOW)
check("重來時沿用原本的副本、不重複存", len(zone(ARCH)) == 1 and uid(30) not in DB and res["moved"] == 1)

# ── 4. 副本讀回對不上：原留言不刪、這輪停下來 ──
reset()
msg(uid(40), days=20); msg(uid(41), days=19)
FAULT["add_mangle"] = True
res = S._archive_old_messages(now=NOW)
check("副本對不上就不刪原留言", uid(40) in DB and uid(41) in DB and res["moved"] == 0 and "對不上" in res["error"])
check("出錯就停，不繼續搬下一則", sum(1 for c in CALLS if c[0] == "post" and c[1] == "/memories") == 1)

# ── 5. 刪了還讀得到：報錯停下 ──
reset()
msg(uid(50), days=20)
FAULT["delete_noop"] = True
res = S._archive_old_messages(now=NOW)
check("刪不掉就報錯", res["moved"] == 0 and "還讀得到" in res["error"])

# ── 6. 一次最多搬 limit 則 ──
reset()
for n in range(60, 70):
    msg(uid(n), days=20)
S.MSG_ARCHIVE_BATCH = 3
res = S._archive_old_messages(now=NOW, limit=7)
check("一次最多搬 limit 則，剩下的留到下次", (res["moved"], res["left"]) == (7, 3))

# ── 7. 搬走後：收件匣、搜尋、讀取 ──
reset()
S.MSG_ARCHIVE_BATCH = 25
OLD, OLD2, NOTE = "c80c80c8-0000-4000-8000-000000000080", "d82d82d8-0000-4000-8000-000000000082", "b81b81b8-0000-4000-8000-000000000081"
msg(OLD, days=30, to="codex-laptop", frm="claude-laptop", from_session="mcp", subject="很舊的 VM119 問題", project="mem0")
msg(OLD2, days=30, to="claude-laptop:mcp", frm="codex-laptop", subject="很舊的通知", read_by="", project="mem0")
put(NOTE, "[筆記] VM119 的現況筆記，" + "很長的內容" * 40 + "，最後提到 10.0.0.93", type="note", project="mem0", days=2)
S._archive_old_messages(now=NOW)
# 封存後才有人回覆那則舊信（原信已不在共用區）
S.current_agent.set("codex-laptop")
r = S._send("codex-laptop", "claude-laptop", "回覆舊信", "補充", False, "other", "", reply_to=OLD)
S.current_agent.set("claude-laptop")
new_id = r["results"][0]["id"]
check("回覆已封存的舊信照樣接得上原編號", DB[new_id]["metadata"]["reply_to"] == OLD)
box = S.inbox(session="mcp", projects="mem0")
check("封存的舊信不在收件匣（沒讀過也一樣）", OLD2 not in [x["id"] for x in box])
got = [x for x in box if x["id"] == new_id]
check("回覆本串舊信（專案不在訂閱內）照樣收得到", len(got) == 1)
check("回覆附的原信資料從封存區補上", bool(got) and (got[0]["reply_to_info"] or {}).get("subject") == "很舊的 VM119 問題")
try:
    S._send("codex-laptop", "claude-laptop", "回覆不存在的信", "", False, "", "", reply_to=uid(999))
    check("回覆不存在的編號要報錯", False)
except ValueError:
    check("回覆不存在的編號要報錯", True)

r = S.memory_search("VM119")
check("一般搜尋找不到封存的留言", OLD not in [x["id"] for x in r])
check("一般搜尋找得到筆記", NOTE in [x["id"] for x in r])
r = S.memory_search("VM119", archived=True)
check("archived=true 找得到，編號是原編號", [x["id"] for x in r][:1] == [OLD] and OLD2 in [x["id"] for x in r])
check("archived=true 標出封存日", bool(r) and r[0].get("archived") == S._now_tw()[:10])
check("archived=true 的日期是原寄出日", bool(r) and r[0]["date"] == ago(30)[:10])
rf = S.memory_search("VM119", archived=True, full=True)
check("archived=true 帶 full 給全文和原時間", bool(rf) and rf[0]["created"] == ago(30) and rf[0]["text"].startswith("[訊息]"))

# ── 8. 精簡版 ──
r = S.memory_search("10.0.0.93")
b = next(x for x in r if x["id"] == NOTE)
check("精簡版欄位", set(b) == {"id", "type", "project", "agent", "date", "text", "chars", "score"})
check("精簡版去掉 [筆記] 標籤", not b["text"].startswith("["))
check("精簡版截短並註明全文字數", b["text"].startswith("VM119 的現況筆記") and b["chars"] == len(DB[NOTE]["memory"]))
check("關鍵字在後面時補上那一段", "10.0.0.93" in b["text"] and len(b["text"]) < 200)
f = next(x for x in S.memory_search("10.0.0.93", full=True) if x["id"] == NOTE)
check("full=true 給全文和 version", f["text"] == DB[NOTE]["memory"] and f["version"] == S._version(f["text"]))
check("短的內文整段給", S._snip("[決策] 短短一句", []) == "短短一句")

# ── 9. memory_get ──
g = S.memory_get(NOTE)
check("memory_get 給全文", len(g) == 1 and g[0]["text"] == DB[NOTE]["memory"] and g[0]["version"])
g = S.memory_get(NOTE[:8].upper() + "  ," + OLD)
check("前 8 碼（大小寫都行）和封存的原編號一起讀", [x.get("id") for x in g] == [NOTE, OLD] and g[1].get("archived"))
g = S.memory_get(OLD[:13])
check("封存的留言也能用前幾碼讀", len(g) == 1 and g[0].get("id") == OLD and g[0]["text"].startswith("[訊息]"))
r = S.memory_search(NOTE[:8])
check("搜尋只打編號就回那則全文（舊連線沒有 memory_get 也能用）", [x["id"] for x in r] == [NOTE] and r[0]["text"] == DB[NOTE]["memory"] and r[0]["version"])
r = S.memory_search(OLD)
check("只打已封存留言的原編號也回全文", [x["id"] for x in r] == [OLD] and r[0].get("archived"))
r = S.memory_search("deadbeef")
check("看起來像編號但找不到就照常搜尋", isinstance(r, list) and all("chars" in x for x in r))
g = S.memory_get(uid(998))
check("找不到的回錯誤說明", len(g) == 1 and "找不到" in g[0].get("error", ""))
put("aaaaaaaa-0000-0000-0000-000000000001", "[筆記] 甲", type="note", project="x")
put("aaaaaaaa-0000-0000-0000-000000000002", "[筆記] 乙", type="note", project="x")
put("aaaaaaaa-0000-0000-0000-000000000003", "[筆記] 丙", type="note", project="x")
g = S.memory_get("aaaaaaaa")
check("前幾碼對到好幾筆要多給幾碼", "多給幾碼" in g[0].get("error", ""))
for bad, why in (("xyz", "格式"), (",".join([NOTE] * 11), "最多 10 個"), ("", "至少")):
    try:
        S.memory_get(bad)
        check("memory_get 擋下：" + why, False)
    except ValueError:
        check("memory_get 擋下：" + why, True)

# ── 10. memory_list 分頁 ──
reset()
for n in range(120):
    put(uid(200 + n), f"[筆記] 第 {n} 則", type="note", project="mem0", days=n % 50)
    DB[uid(200 + n)]["updated_at"] = (NOW - dt.timedelta(minutes=n)).isoformat()
put(uid(400), "[筆記] 別的專案", type="note", project="pve")
r = S.memory_list(project="mem0")
check("預設一頁 50 則、總數 120、下一頁從 50", (len(r["items"]), r["total"], r["next_offset"]) == (50, 120, 50))
check("最近有動靜的在前", [x["id"] for x in r["items"][:3]] == [uid(200), uid(201), uid(202)])
check("列表也是精簡版", "version" not in r["items"][0] and "chars" in r["items"][0])
r = S.memory_list(project="mem0", offset=100)
check("最後一頁沒有下一頁", (len(r["items"]), r["next_offset"]) == (20, None))
check("limit 最多 200", len(S.memory_list(limit=999)["items"]) == 121)
check("full=true 給全文", "version" in S.memory_list(project="pve", full=True)["items"][0])
check("不帶專案也只回一頁", len(S.memory_list()["items"]) == 50)

# ── 11. 排程 ──
check("載入模組不會自己開排程（只有正式啟動才開）", not any(t.name == "msg-archive" for t in threading.enumerate()))
check("預設開啟排程", S.MSG_ARCHIVE_ON is True)
slept = []


class _Stop(Exception):
    pass


def fake_sleep(s):
    slept.append(s)
    raise _Stop


real_sleep = S.time.sleep
S.time.sleep = fake_sleep
try:
    S._archive_loop()
except _Stop:
    pass
finally:
    S.time.sleep = real_sleep
tpe = dt.timezone(dt.timedelta(hours=8))
when = dt.datetime.now(tpe) + dt.timedelta(seconds=slept[0])
check("下一輪排在台北時間 04:30、一天內", 0 < slept[0] <= 86400 and (when.hour, when.minute) in ((4, 30), (4, 29)))

print("\n全部通過" if not fails else "\n有 %d 項失敗：%s" % (len(fails), "、".join(fails)))
sys.exit(1 if fails else 0)
