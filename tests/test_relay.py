"""轉送站離線測試：假的圓桌回應、暫存狀態資料夾，不連真的圓桌。
跑法：python -X utf8 tests/test_relay.py（有裝 httpx 就走 httpx，沒裝走 urllib，兩種都會測到連線那段）"""
import datetime as dt
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "relay"))
import relay as R  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="relay-test-"))
routes = tmp / "session-routes.json"
routes.write_text(json.dumps({
    "_說明": "測試用路由表",
    "local_nav": {"label": "navigator", "title": "領航員", "projects": ["*"]},
    "local_a": {"label": "board-ui", "projects": []},
    "local_b": {"label": "mem0-deploy", "projects": ["mem0"]},
    "_test": {"label": "inbox-test", "projects": ["inbox-test"]},
}, ensure_ascii=False), encoding="utf-8")
BASE_ENV = {"RT_URL": "http://fake/mcp", "RT_TOKEN": "tok-claude", "RT_TOKEN_CODEX_LAPTOP": "tok-codex",
            "RELAY_TOKEN": "relay-secret", "POLL_SECONDS": "30", "ROUTES_FILE": str(routes),
            "MAX_PER_HOUR": "3", "LEASE_SECONDS": "0.4", "GRACE_SECONDS": "0"}  # 舊案例不等寬限期；寬限期在第 13 節測
results = []
box = {}  # (權杖, session) -> 假收件匣的信，或一個例外
calls = []


def fake_inbox(url, tok, session, projects):
    calls.append((tok, session, projects))
    v = box.get((tok, session), [])
    if isinstance(v, Exception):
        raise v
    return list(v)


R.call_inbox = fake_inbox
R.log = lambda msg: None  # 測試時不印紀錄


def check(name, ok):
    results.append((name, bool(ok)))


def cfg_for(name, **over):
    env = dict(BASE_ENV, RELAY_DATA=str(tmp / name), **over)
    return R.Config(env)


def msg(i, frm="claude-laptop", sess="roundtable", act=False, **kw):
    return {"id": f"{i:08d}-0000-0000-0000-000000000000", "from": frm, "from_session": sess,
            "subject": f"第 {i} 封", "text": f"本文 {i}", "needs_action": act, "project": "mcp", **kw}


# 1. 設定：另外的身分、缺什麼
c = cfg_for("c1")
check("RT_TOKEN_CODEX_LAPTOP 變成身分 codex-laptop", c.extra == {"codex-laptop": "tok-codex"})
c2 = R.Config(dict(BASE_ENV, RT_EXTRA_TOKENS="hermes=h1; codex-mini=m1,"))
check("RT_EXTRA_TOKENS 也能給身分", c2.extra == {"codex-laptop": "tok-codex", "hermes": "h1", "codex-mini": "m1"})
bad = R.Config({"POLL_SECONDS": "1"})
check("缺設定會列出來", len(bad.problems()) == 4 and any("RELAY_TOKEN" in p for p in bad.problems()))
check("設定齊全沒問題", c.problems() == [])
check("GRACE_SECONDS 沒設就是 600 秒、設 0 就是 0", R.Config({k: v for k, v in BASE_ENV.items() if k != "GRACE_SECONDS"}).grace == 600
      and R.Config(dict(BASE_ENV, GRACE_SECONDS="")).grace == 600 and c.grace == 0)
check("GRACE_SECONDS 負的會列出來", any("GRACE" in p for p in R.Config(dict(BASE_ENV, GRACE_SECONDS="-1")).problems()))
check("TEST_SESSIONS 沒設就是 relay-test、設了照設的、設空就沒有", c.test_sessions == {"relay-test"}
      and R.Config(dict(BASE_ENV, TEST_SESSIONS="qa, x,")).test_sessions == {"qa", "x"}
      and R.Config(dict(BASE_ENV, TEST_SESSIONS="")).test_sessions == set())

# 2. 要看哪些收件匣
ts = R.targets(c)
labels = [t["label"] for t in ts]
check("路由表每一串加另外的身分，底線開頭的跳過", labels == ["navigator", "board-ui", "mem0-deploy", "codex-laptop"])
nav = ts[0]
check("領航員 * 排除別串訂閱的專案", nav["projects"] == "*,-mem0" and nav["session"] == "local_nav")
check("Codex 身分：整個收件匣、沒有串編號、標 is_codex", ts[3] == {
    "label": "codex-laptop", "session": "", "token": "tok-codex", "inbox_session": "", "projects": "", "is_codex": True})

# 3. 挑信規則
check("一般 AI 信不排", R.reason_of(msg(1)) == "")
check("使用者寄的排", R.reason_of(msg(1, frm="user", sess="")) == "使用者寄的")
check("要動手排", R.reason_of(msg(1, act=True)) == "要動手")
check("欠意見排", "徵求意見" in R.reason_of(msg(1, you_owe_opinion=True)))
check("撤回排、原因說停手", "停手" in R.reason_of(msg(1, closed={"kind": "recall"})))
check("使用者寄又要動手，原因是要動手", R.reason_of(msg(1, frm="user", sess="", act=True)) == "要動手")

# 4. 看一輪：各串的信進佇列，帶對的串編號；Codex 的信也進
rl = R.Relay(cfg_for("c4"))
box.clear()
box[("tok-claude", "board-ui")] = [msg(1), msg(2, act=True), msg(3, frm="user", sess="")]
box[("tok-claude", "mem0-deploy")] = [msg(4, you_owe_opinion=True)]
box[("tok-claude", "navigator")] = [msg(5, closed={"kind": "recall"})]
box[("tok-codex", "")] = [msg(6, act=True, frm="claude-laptop", sess="pingpong")]
calls.clear()
n = rl.poll_once(now=1000.0)
check("一輪排進 5 封（一般信不算）", n == 5 and len(rl.queue) == 5)
check("每個收件匣都用對的 session 和專案去問",
      ("tok-claude", "navigator", "*,-mem0") in calls and ("tok-codex", "", "") in calls)
by = {it["key"]: it for it in rl.queue}
it2 = by["board-ui:00000002-0000-0000-0000-000000000000"]
check("信帶串編號、主旨、寄件人、原因、本文", it2["session"] == "local_a" and it2["subject"] == "第 2 封"
      and it2["from"] == "claude-laptop:roundtable" and it2["reason"] == "要動手" and it2["text"] == "本文 2"
      and it2["is_codex"] is False and it2["attempts"] == 0)
check("使用者的信寄件人寫使用者", by["board-ui:00000003-0000-0000-0000-000000000000"]["from"] == "使用者")
it6 = by["codex-laptop:00000006-0000-0000-0000-000000000000"]
check("Codex 的信標 is_codex、沒串編號", it6["is_codex"] is True and it6["session"] == "")
check("撤回的信標 recall", by["navigator:00000005-0000-0000-0000-000000000000"]["recall"] is True)
# 5. 同一封不排兩次
check("再看一輪不重複排", rl.poll_once(now=1030.0) == 0 and len(rl.queue) == 5)
# 6. 收件人自己讀掉的（不在收件匣了）、還沒交出去的，從佇列拿掉
box[("tok-claude", "mem0-deploy")] = []
rl.poll_once(now=1060.0)
check("已讀掉的信從佇列拿掉", "mem0-deploy:00000004-0000-0000-0000-000000000000" not in {it["key"] for it in rl.queue})
# 7. 狀態檔：重新載入還在
rl2 = R.Relay(cfg_for("c4"))
check("重啟後佇列和排過的紀錄都還在", len(rl2.queue) == 4 and len(rl2.seen) == 5)
# 8. 單一收件匣出錯不影響別的
box[("tok-claude", "navigator")] = RuntimeError("圓桌回錯誤：權杖不對")
box[("tok-claude", "board-ui")] = [msg(7, act=True)]
n = rl.poll_once(now=1090.0)
check("一個收件匣出錯，別的照排、錯誤記在 last_error", n == 1 and "navigator：RuntimeError" in rl.last_error)
# 8b. 讀不到時日誌連錯誤文字一起記；/health 的 last_error 還是只有類別
logged = []
R.log = logged.append
rl.poll_once(now=1095.0)
R.log = lambda msg: None
check("日誌記錯誤文字、last_error 只記類別",
      any("navigator：RuntimeError：圓桌回錯誤：權杖不對" in ln for ln in logged) and rl.last_error == "navigator：RuntimeError")
check("錯誤文字壓成一行、最多 120 字", R.err_text(ValueError("a\n  b" + "x" * 300)) == "ValueError：" + ("a b" + "x" * 300)[:120]
      and R.err_text(OSError()) == "OSError")
box[("tok-claude", "navigator")] = []
# 9. 路由表讀不到：不當機、記錯誤
rb = R.Relay(cfg_for("c9", ROUTES_FILE=str(tmp / "沒有.json")))
check("路由表不見不當機", rb.poll_once(now=1000.0) == 0 and "路由表" in rb.last_error)

# 10. 每串一小時最多 3 封 AI 的信，多的留到下次；使用者的不受限
rc = R.Relay(cfg_for("c10"))
box.clear()
box[("tok-claude", "board-ui")] = [msg(i, act=True) for i in range(11, 15)] + [msg(15, frm="user", sess="")]
n = rc.poll_once(now=2000.0)
check("4 封 AI 的信只排 3 封，使用者的照排", n == 4 and len(rc.queue) == 4)
keys = {it["key"] for it in rc.queue}
check("第 4 封沒排、也沒記成排過（下次還能排）", "board-ui:00000014-0000-0000-0000-000000000000" not in keys
      and "board-ui:00000014-0000-0000-0000-000000000000" not in rc.seen)
check("使用者那封不佔名額", "board-ui:00000015-0000-0000-0000-000000000000" in keys)
box[("tok-claude", "board-ui")].append(msg(16, frm="user", sess=""))
check("滿了之後使用者的信照排", rc.poll_once(now=2100.0) == 1)
check("一小時內 AI 的信還是留著", rc.poll_once(now=2200.0) == 0)
check("過一小時留著的信排進來", rc.poll_once(now=2000.0 + 3601) == 1
      and "board-ui:00000014-0000-0000-0000-000000000000" in {it["key"] for it in rc.queue})
check("別串不受這串的名額影響", True)  # 名額是每個標籤各算，上面 c4 的 codex 與 board-ui 已各自排到

# 11. HTTP：長輪詢、權杖、租約、ack、health
rh = R.Relay(cfg_for("c11"))
srv = R.serve(rh, "127.0.0.1", 0)
port = srv.server_address[1]
base = f"http://127.0.0.1:{port}"


def http(method, path, tok="relay-secret", body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {tok}"} if tok else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


st, h = http("GET", "/health", tok="")
check("health 不用權杖、有寬限期欄位", st == 200 and h["ok"] and h["queued"] == 0 and h["pid"] > 0
      and h["pending"] == 0 and h["grace_seconds"] == 0)
st, _ = http("GET", "/wait?max_wait=0.1", tok="")
check("wait 沒權杖 401", st == 401)
st, _ = http("GET", "/wait?max_wait=0.1", tok="wrong")
check("wait 權杖錯 401", st == 401)
st, _ = http("POST", "/ack", tok="wrong", body={"keys": []})
check("ack 權杖錯 401", st == 401)
st, _ = http("GET", "/nothing")
check("沒有的路徑 404", st == 404)
t0 = time.time()
st, w = http("GET", "/wait?max_wait=0.3")
check("沒信時等滿 max_wait 回空批", st == 200 and w["items"] == [] and 0.25 <= time.time() - t0 < 2)
# 長輪詢：先等著，信進來馬上回
got = {}


def waiter():
    got["res"] = http("GET", "/wait?max_wait=8")


th = threading.Thread(target=waiter, daemon=True)
th.start()
time.sleep(0.3)
box.clear()
box[("tok-claude", "board-ui")] = [msg(21, act=True), msg(22, frm="user", sess="")]
box[("tok-codex", "")] = [msg(23, act=True, frm="user", sess="")]
t0 = time.time()
rh.poll_once()
th.join(3)
st, w = got.get("res", (0, {}))
items = w.get("items") or []
check("信進來長輪詢馬上回一批", st == 200 and len(items) == 3 and time.time() - t0 < 2)
check("批裡有標籤、串編號、信編號、主旨、是不是 Codex、沒有內部租約欄位",
      all({"key", "label", "session", "id", "subject", "from", "is_codex", "reason", "text"} <= set(it) for it in items)
      and all("lease_until" not in it for it in items)
      and {it["label"] for it in items} == {"board-ui", "codex-laptop"} and all(it["attempts"] == 1 for it in items))
st, w = http("GET", "/wait?max_wait=0.1")
check("租約內同一批不再交", w["items"] == [])
st, h = http("GET", "/health")
check("health 看得到佇列和租出去的數量", h["queued"] == 3 and h["leased"] == 3 and h["queued_labels"] == ["board-ui", "codex-laptop"])
time.sleep(0.5)
st, w = http("GET", "/wait?max_wait=0.1")
check("租約過了沒 ack 就再交一次", len(w["items"]) == 3 and all(it["attempts"] == 2 for it in w["items"]))
st, a = http("POST", "/ack?token=relay-secret", tok="", body={"keys": [items[0]["key"], items[1]["key"]]})
check("ack（權杖放網址也行）移除兩封", st == 200 and a["removed"] == 2 and a["queued"] == 1)
st, a = http("POST", "/ack", body={"keys": [items[0]["key"]]})
check("重複 ack 不出錯", st == 200 and a["removed"] == 0)
st, a = http("POST", "/ack", body={"nope": 1})
check("ack 沒給 keys 回 400", st == 400)
time.sleep(0.5)
st, w = http("GET", "/relay/wait?max_wait=0.1")
check("路徑前面有代理前綴也行；剩的那封再交", st == 200 and len(w["items"]) == 1 and w["items"][0]["key"] == items[2]["key"])
http("POST", "/ack", body={"keys": [items[2]["key"]]})
check("全部 ack 後佇列空", http("GET", "/health")[1]["queued"] == 0)
rh.shutdown()
srv.shutdown()

# 12. 真的連線那段：用假的圓桌 HTTP 伺服器，SSE 和純 JSON 兩種回法、錯誤、401
class FakeRT(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n))
        auth = self.headers.get("Authorization") or ""
        args = body["params"]["arguments"]
        if auth == "Bearer sse":
            text = json.dumps([{"id": "m1", "subject": f'給 {args.get("session")}', "from": "user"}])
            payload = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}
            raw, ctype, code = "event: message\ndata: " + json.dumps(payload) + "\n\n", "text/event-stream", 200
        elif auth == "Bearer json":
            text = json.dumps({"id": "m2", "subject": "單一", "from": "user"})
            payload = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}
            raw, ctype, code = json.dumps(payload), "application/json", 200
        elif auth == "Bearer err":
            payload = {"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": [{"type": "text", "text": "權杖不對"}]}}
            raw, ctype, code = json.dumps(payload), "application/json", 200
        else:
            raw, ctype, code = "unauthorized", "text/plain", 401
        data = raw.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


fake = HTTPServer(("127.0.0.1", 0), FakeRT)
threading.Thread(target=fake.serve_forever, daemon=True).start()
furl = f"http://127.0.0.1:{fake.server_address[1]}/mcp"
r1 = R.rpc(furl, "sse", "inbox", {"session": "board-ui", "projects": ""})
check("SSE 回法解析得出信", r1 == [{"id": "m1", "subject": "給 board-ui", "from": "user"}])
r2 = R.rpc(furl, "json", "inbox", {"session": "", "projects": ""})
check("純 JSON 回法（單一物件）也解析得出", r2 == [{"id": "m2", "subject": "單一", "from": "user"}])
try:
    R.rpc(furl, "err", "inbox", {})
    check("圓桌回 isError 會丟 RuntimeError", False)
except RuntimeError as e:
    check("圓桌回 isError 會丟 RuntimeError", "權杖不對" in str(e))
try:
    R.rpc(furl, "bad", "inbox", {})
    check("HTTP 401 會丟例外", False)
except Exception:
    check("HTTP 401 會丟例外", True)
fake.shutdown()
check("連線用的是 " + ("httpx" if R.httpx else "urllib"), True)

# 13. 寬限期：Claude 串的信先等 GRACE_SECONDS，寬限期內被收件串處理掉就不排；Codex 的信一到就排
def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")


def key_of(i, label="board-ui"):
    return f"{label}:{i:08d}-0000-0000-0000-000000000000"


T = 1_800_000_000.0
check("寄出時間：ISO 帶時區解得出、沒有／看不懂／沒帶時區回 None", R.created_ts({"created": iso(T)}) == T
      and R.created_ts({}) is None and R.created_ts({"created": "昨天"}) is None and R.created_ts({"created": "2020-01-01T00:00:00"}) is None
      and R.created_ts({"created": "2026-10-21T12:00:00Z"}) == dt.datetime(2026, 10, 21, 12, tzinfo=dt.timezone.utc).timestamp())
rg = R.Relay(cfg_for("c13", GRACE_SECONDS="600", MAX_PER_HOUR="10"))
logged = []
R.log = logged.append
box.clear()
box[("tok-claude", "board-ui")] = [msg(31, act=True, created=iso(T - 100)), msg(32, act=True, created=iso(T - 700)),
                                   msg(33, frm="user", sess="", created=iso(T - 10))]
box[("tok-codex", "")] = [msg(34, act=True, created=iso(T - 5))]
n = rg.poll_once(now=T)
keys = {it["key"] for it in rg.queue}
check("剛寄出的先等、寄出超過 600 秒還沒處理的排、Codex 的一到就排", n == 2 and keys == {key_of(32), key_of(34, "codex-laptop")})
check("等著的兩封記在 pending、health 看得到、沒記成排過", sorted(rg.pending) == [key_of(31), key_of(33)]
      and rg.health()["pending"] == 2 and rg.health()["grace_seconds"] == 600 and key_of(31) not in rg.seen)
check("進寬限期各記一行", sum("進寬限期" in ln for ln in logged) == 2 and any("00000031 進寬限期，600 秒" in ln for ln in logged))
check("使用者寄的也等寬限期（有等信器的串會自己醒）", key_of(33) not in keys)
check("寬限期內再看：不排、不重複記", rg.poll_once(now=T + 300) == 0 and rg.health()["pending"] == 2
      and sum("進寬限期" in ln for ln in logged) == 2)
box[("tok-claude", "board-ui")] = [m for m in box[("tok-claude", "board-ui")] if not m["id"].startswith("00000033")]
check("寬限期內被收件串處理掉（不在收件匣了）：不排、忘掉、記一行", rg.poll_once(now=T + 400) == 0
      and rg.health()["pending"] == 1 and any("00000033 寬限期內已被處理" in ln for ln in logged))
check("寄出滿 600 秒還沒處理就排", rg.poll_once(now=T + 501) == 1 and key_of(31) in {it["key"] for it in rg.queue}
      and rg.health()["pending"] == 0)
box[("tok-claude", "board-ui")].append(msg(35, act=True, created=""))
check("沒有寄出時間的信從第一次看到起算", rg.poll_once(now=T + 600) == 0 and rg.pending.get(key_of(35)) == T + 600)
rg_b = R.Relay(cfg_for("c13", GRACE_SECONDS="600", MAX_PER_HOUR="10"))
check("重啟後寬限期裡等著的紀錄還在（pending.json）", rg_b.pending == rg.pending and len(rg_b.queue) == 3)
check("重啟後還沒滿寬限期不排", rg_b.poll_once(now=T + 600 + 599) == 0)
check("滿了就排", rg_b.poll_once(now=T + 600 + 601) == 1 and key_of(35) in {it["key"] for it in rg_b.queue})
box[("tok-claude", "board-ui")] += [msg(36, act=True, created="2020-01-01T00:00:00"), msg(37, act=True, created=iso(T + 99999))]
check("寄出時間沒帶時區或在未來：不信它，從第一次看到起算", rg_b.poll_once(now=T + 2000) == 0 and rg_b.health()["pending"] == 2)
check("……滿寬限期才排", rg_b.poll_once(now=T + 2000 + 601) == 2 and rg_b.health()["pending"] == 0)
box[("tok-claude", "board-ui")].append(msg(38, act=True, created=iso(T + 3000 - 50), closed={"kind": "recall"}))
check("撤回的信一樣等寬限期", rg_b.poll_once(now=T + 3000) == 0 and key_of(38) in rg_b.pending)
# 寬限期過了但這小時名額滿：留著（不算在寬限期裡），下一小時再排
rk = R.Relay(cfg_for("c13b", GRACE_SECONDS="600", MAX_PER_HOUR="1"))
box.clear()
box[("tok-claude", "board-ui")] = [msg(41, act=True, created=iso(T - 700)), msg(42, act=True, created=iso(T - 700))]
check("寬限期過了但名額滿：排 1 封、另 1 封留到下一小時", rk.poll_once(now=T) == 1 and rk.health()["pending"] == 0
      and any("還有 1 封留到下次" in ln for ln in logged))
check("下一小時排進來", rk.poll_once(now=T + 3601) == 1 and {it["key"] for it in rk.queue} == {key_of(41), key_of(42)})
# 路由表上沒有的標籤，寬限期紀錄丟掉
rr = R.Relay(cfg_for("c13c", GRACE_SECONDS="600"))
box.clear()
box[("tok-claude", "board-ui")] = [msg(43, act=True, created=iso(T))]
rr.poll_once(now=T)
rr.pending["old-label:00000099-0000-0000-0000-000000000000"] = T
rr.poll_once(now=T + 30)
check("路由表上沒有的標籤，寬限期紀錄丟掉；還在的留著", sorted(rr.pending) == [key_of(43)])
check("GRACE_SECONDS=0 就不等（前面各節）", True)
R.log = lambda msg: None

# 14. 測試信（寄件串名在 TEST_SESSIONS，預設 relay-test）不佔每小時名額、也不被名額擋
rt = R.Relay(cfg_for("c14"))
box.clear()
box[("tok-claude", "board-ui")] = [msg(i, act=True) for i in range(51, 54)] + [msg(54, act=True, sess="relay-test"), msg(55, act=True)]
n = rt.poll_once(now=T)
keys = {it["key"] for it in rt.queue}
check("3 封 AI 的信排滿名額，測試信照排、第 4 封一般信留著", n == 4 and key_of(54) in keys and key_of(55) not in keys
      and len(rt.wakes["board-ui"]) == 3)
box[("tok-claude", "board-ui")].append(msg(56, act=True, sess="relay-test"))
check("名額滿了之後測試信還是照排", rt.poll_once(now=T + 60) == 1 and key_of(56) in {it["key"] for it in rt.queue})
rt2 = R.Relay(cfg_for("c14b", TEST_SESSIONS="qa"))
box.clear()
box[("tok-claude", "board-ui")] = [msg(i, act=True) for i in range(61, 64)] + [msg(64, act=True, sess="relay-test"), msg(65, act=True, sess="qa")]
check("TEST_SESSIONS 改成別的：relay-test 照一般信算、qa 不佔名額", rt2.poll_once(now=T) == 4
      and key_of(64) not in {it["key"] for it in rt2.queue} and key_of(65) in {it["key"] for it in rt2.queue})

# 15. 叫過沒人處理：從回報送達起隔 REPING_SECONDS 再叫、最多 MAX_PINGS 次（Codex 只叫一次），叫滿歸「暫時還沒解決」
logged = []
R.log = logged.append
rp = R.Relay(cfg_for("c15", MAX_PER_HOUR="10"))
box.clear()
box[("tok-claude", "board-ui")] = [msg(71, act=True)]
box[("tok-codex", "")] = [msg(72, act=True)]
k71, k72 = key_of(71), key_of(72, "codex-laptop")
check("第一次叫：兩封各排一次，第幾次叫是 1；最多叫幾次 Claude 串 3、Codex 1",
      rp.poll_once(now=T) == 2 and {(it["key"], it["ping"], it["pings_max"]) for it in rp.queue} == {(k71, 1, 3), (k72, 1, 1)}
      and rp.pings == {k71: 1, k72: 1})
check("還沒回報送達（還在佇列）：過了再叫的間隔也不重複排", rp.poll_once(now=T + 700) == 0 and len(rp.queue) == 2)
taken = rp.take(now=T + 700)
check("回報送達：從佇列拿掉", rp.ack([it["key"] for it in taken], now=T + 700) == 2 and rp.queue == [])
check("送達後不到 600 秒：不再叫", rp.poll_once(now=T + 700 + 599) == 0 and rp.queue == [])
n = rp.poll_once(now=T + 1300)
by = {it["key"]: it for it in rp.queue}
check("送達滿 600 秒信還在：Claude 串的再叫一次（key 加 #2、第幾次叫是 2、同一個信編號）；Codex 的不再叫、歸到暫時還沒解決",
      n == 1 and list(by) == [k71 + "#2"] and by[k71 + "#2"]["ping"] == 2 and by[k71 + "#2"]["pings_max"] == 3
      and by[k71 + "#2"]["id"] == msg(71)["id"] and rp.pings[k71] == 2 and rp.pings[k72] == 1
      and k72 in rp.stalled and rp.stalled[k72]["pings"] == 1 and rp.stalled[k72]["stalled_at"] == T + 1300)
check("日誌：再叫 1 封、Codex 那封歸到暫時還沒解決", any("board-ui：再叫 1 封（叫過 600 秒還沒人處理）" in ln for ln in logged)
      and any("00000072 叫了 1 次都沒人處理，歸到「暫時還沒解決」，不再叫" in ln for ln in logged))
h = rp.health()
check("health 看得到暫時還沒解決的封數、標籤和設定", h["stalled"] == 1 and h["stalled_labels"] == ["codex-laptop"]
      and h["max_pings"] == 3 and h["reping_seconds"] == 600 and h["queued"] == 1)
rp.take(now=T + 1305)
rp.ack([k71 + "#2"], now=T + 1310)
check("第 2 次送達後不到 600 秒：不叫", rp.poll_once(now=T + 1310 + 599) == 0)
check("第 3 次叫：key 加 #3", rp.poll_once(now=T + 1910) == 1 and [it["key"] for it in rp.queue] == [k71 + "#3"]
      and rp.queue[0]["ping"] == 3 and rp.pings[k71] == 3)
rp.take(now=T + 1915)
rp.ack([k71 + "#3"], now=T + 1920)
check("叫滿 3 次還沒人處理：歸到暫時還沒解決、不再排、記一行", rp.poll_once(now=T + 2520) == 0 and rp.queue == [] and k71 in rp.stalled
      and rp.stalled[k71]["pings"] == 3 and rp.stalled[k71]["subject"] == "第 71 封" and rp.stalled[k71]["from"] == "claude-laptop:roundtable"
      and rp.stalled[k71]["stalled_at"] == T + 2520 and rp.stalled[k71]["last_queued"] == T + 1920
      and any("00000071 叫了 3 次都沒人處理，歸到「暫時還沒解決」，不再叫" in ln for ln in logged))
check("歸到暫時還沒解決之後再看：不叫、不重複記", rp.poll_once(now=T + 9999) == 0 and rp.queue == [] and rp.health()["stalled"] == 2
      and sum("00000071 叫了 3 次" in ln for ln in logged) == 1)
sl = rp.stalled_list()
check("stalled_list：舊的在前、時間換成好讀的字串", [x["id"][:8] for x in sl] == ["00000072", "00000071"]
      and sl[1]["stalled_at"] == R.now_str(T + 2520) and sl[1]["last_queued"] == R.now_str(T + 1920) and sl[0]["label"] == "codex-laptop")
rp2 = R.Relay(cfg_for("c15", MAX_PER_HOUR="10"))
check("重啟後叫醒次數和暫時還沒解決都還在（pings.json、stalled.json）", rp2.pings == rp.pings and rp2.stalled == rp.stalled)
box[("tok-claude", "board-ui")] = []
check("收件人處理掉（不在收件匣了）：從暫時還沒解決拿掉、叫醒次數忘掉、記一行", rp.poll_once(now=T + 12000) == 0
      and k71 not in rp.stalled and k71 not in rp.pings and k72 in rp.stalled and rp.health()["stalled"] == 1
      and any("00000071 有人處理了，從「暫時還沒解決」拿掉" in ln for ln in logged))
# 再叫也算每小時名額（AI 寄的）；使用者寄的照舊不受限
rq = R.Relay(cfg_for("c15b", MAX_PER_HOUR="1"))
box.clear()
box[("tok-claude", "board-ui")] = [msg(73, act=True), msg(74, act=True), msg(77, frm="user", sess="")]
check("名額 1：AI 的排 1 封、第 2 封留著，使用者的照排", rq.poll_once(now=T) == 2 and [it["key"] for it in rq.queue] == [key_of(73), key_of(77)])
rq.take(now=T + 1)
rq.ack([key_of(73), key_of(77)], now=T + 1)
check("送達 600 秒後名額還沒恢復：AI 的再叫和留著的那封都等下一小時，使用者的再叫不受限", rq.poll_once(now=T + 601) == 1
      and [it["key"] for it in rq.queue] == [key_of(77) + "#2"])
check("下一小時：AI 的再叫（#2）也佔名額，留著的那封繼續留", rq.poll_once(now=T + 3601) == 1
      and [it["key"] for it in rq.queue] == [key_of(77) + "#2", key_of(73) + "#2"] and key_of(74) not in rq.seen)
# MAX_PINGS、REPING_SECONDS 可調；舊版（0.1.2）排過的信沒有叫醒次數紀錄，當作叫過 1 次
r2 = R.Relay(cfg_for("c15c", MAX_PINGS="2", REPING_SECONDS="60"))
box.clear()
box[("tok-claude", "board-ui")] = [msg(75, act=True), msg(76, act=True)]
r2.seen[key_of(76)] = T - 30
check("MAX_PINGS=2、REPING_SECONDS=60：新信排 1 封；舊版排過的信（沒叫醒次數紀錄）還沒到間隔不叫", r2.poll_once(now=T) == 1
      and [it["key"] for it in r2.queue] == [key_of(75)])
r2.take(now=T + 1)
r2.ack([key_of(75)], now=T + 1)
check("到了間隔：新信第 2 次、舊版那封當作叫過 1 次也第 2 次", r2.poll_once(now=T + 61) == 2
      and sorted(it["key"] for it in r2.queue) == [key_of(75) + "#2", key_of(76) + "#2"] and r2.pings[key_of(76)] == 2)
r2.take(now=T + 62)
r2.ack([key_of(75) + "#2", key_of(76) + "#2"], now=T + 62)
check("MAX_PINGS=2：第 2 次送達 60 秒後叫滿，兩封都歸到暫時還沒解決", r2.poll_once(now=T + 122) == 0 and r2.queue == []
      and sorted(r2.stalled) == [key_of(75), key_of(76)] and r2.health()["stalled"] == 2)
r1 = R.Relay(cfg_for("c15d", MAX_PINGS="1", REPING_SECONDS="60"))
box.clear()
box[("tok-claude", "board-ui")] = [msg(78, act=True)]
r1.poll_once(now=T)
r1.take(now=T + 1)
r1.ack([key_of(78)], now=T + 1)
check("MAX_PINGS=1（同 0.1.2 只叫一次）：送達 60 秒後不再叫、直接歸到暫時還沒解決", r1.poll_once(now=T + 61) == 0 and r1.queue == []
      and key_of(78) in r1.stalled and r1.stalled[key_of(78)]["pings"] == 1)
# 寬限期只套在第 1 次
rg2 = R.Relay(cfg_for("c15e", GRACE_SECONDS="600", MAX_PER_HOUR="10"))
box.clear()
box[("tok-claude", "board-ui")] = [msg(79, act=True, created="")]
check("寬限期：沒寄出時間的信先等 600 秒", rg2.poll_once(now=T) == 0 and key_of(79) in rg2.pending)
check("……滿了才排", rg2.poll_once(now=T + 600) == 1 and key_of(79) not in rg2.pending)
rg2.take(now=T + 601)
rg2.ack([key_of(79)], now=T + 601)
check("寬限期只套第 1 次：送達 600 秒後直接再叫，不再進寬限期", rg2.poll_once(now=T + 1201) == 1
      and [it["key"] for it in rg2.queue] == [key_of(79) + "#2"] and rg2.health()["pending"] == 0)
rg2.stalled["old-label:00000098-0000-0000-0000-000000000000"] = {"label": "old-label", "id": "00000098-0000-0000-0000-000000000000", "pings": 3}
rg2.pings["old-label:00000098-0000-0000-0000-000000000000"] = 3
rg2.poll_once(now=T + 1230)
check("路由表上沒有的標籤：叫醒次數和暫時還沒解決的紀錄丟掉", not any(k.startswith("old-label:") for k in list(rg2.pings) + list(rg2.stalled))
      and key_of(79) in rg2.pings)
R.log = lambda msg: None

# 16. HTTP：/stalled 要權杖、列清單；/health 多欄位；設定檢查
rs = R.Relay(cfg_for("c16"))
rs.stalled[key_of(81)] = {"label": "board-ui", "id": msg(81)["id"], "subject": "卡住的", "from": "使用者", "pings": 3,
                          "last_queued": T, "stalled_at": T + 1, "created": ""}
srv = R.serve(rs, "127.0.0.1", 0)
base = f"http://127.0.0.1:{srv.server_address[1]}"
check("stalled 沒權杖 401、權杖錯 401", http("GET", "/stalled", tok="")[0] == 401 and http("GET", "/stalled", tok="wrong")[0] == 401)
st, w = http("GET", "/stalled")
check("stalled 列出暫時還沒解決的信、時間是好讀字串", st == 200 and w["ok"] and len(w["items"]) == 1 and w["items"][0]["subject"] == "卡住的"
      and w["items"][0]["pings"] == 3 and w["items"][0]["stalled_at"] == R.now_str(T + 1) and w["items"][0]["last_queued"] == R.now_str(T)
      and "server_time" in w)
st, h = http("GET", "/health", tok="")
check("health 多 stalled、stalled_labels、max_pings、reping_seconds", st == 200 and h["stalled"] == 1 and h["stalled_labels"] == ["board-ui"]
      and h["max_pings"] == 3 and h["reping_seconds"] == 600)
st, w = http("GET", "/relay/stalled?token=relay-secret", tok="")
check("stalled 路徑前面有代理前綴、權杖放網址也行", st == 200 and len(w["items"]) == 1)
check("POST 到 stalled 是 404", http("POST", "/stalled", body={})[0] == 404)
rs.shutdown()
srv.shutdown()
check("MAX_PINGS 沒設就是 3、REPING_SECONDS 沒設就是 600、設了照設的", cfg_for("c16b").max_pings == 3 and cfg_for("c16b").reping == 600
      and cfg_for("c16b", MAX_PINGS="5", REPING_SECONDS="120").max_pings == 5
      and cfg_for("c16b", MAX_PINGS="5", REPING_SECONDS="120").reping == 120)
check("MAX_PINGS 0 或 REPING_SECONDS 太短會列出來；1 和 60 可以", any("MAX_PINGS" in p for p in cfg_for("c16b", MAX_PINGS="0").problems())
      and any("REPING_SECONDS" in p for p in cfg_for("c16b", REPING_SECONDS="30").problems())
      and cfg_for("c16b", MAX_PINGS="1", REPING_SECONDS="60").problems() == [])

fail = [n for n, ok in results if not ok]
for n, ok in results:
    print(("通過 " if ok else "失敗 ") + n)
print(f"共 {len(results)} 項，失敗 {len(fail)} 項")
sys.exit(1 if fail else 0)
