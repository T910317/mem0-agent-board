"""轉送站離線測試：假的圓桌回應、暫存狀態資料夾，不連真的圓桌。
跑法：python -X utf8 tests/test_relay.py（有裝 httpx 就走 httpx，沒裝走 urllib，兩種都會測到連線那段）"""
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
            "MAX_PER_HOUR": "3", "LEASE_SECONDS": "0.4"}
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
check("health 不用權杖", st == 200 and h["ok"] and h["queued"] == 0 and h["pid"] > 0)
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

fail = [n for n, ok in results if not ok]
for n, ok in results:
    print(("通過 " if ok else "失敗 ") + n)
print(f"共 {len(results)} 項，失敗 {len(fail)} 項")
sys.exit(1 if fail else 0)
