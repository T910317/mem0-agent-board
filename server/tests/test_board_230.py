"""離線測試：一則寄給好幾位（公告欄 2.2.0：to 用逗號隔開、feed 帶 multi_to）、專案卡優先序 P1～P5
（POST /board/api/project/priority、feed 與 project_status 的 priority／paused）、AI 自訂頭像（POST /board/api/avatar、
feed 的 avatars）。用假的 Mem0（記憶體裡的清單＋歷史），真的 starlette 跑公告欄端點，不連任何真實服務。
用法：uv run --no-project --with starlette --with httpx python test_board_230.py <server.py 目錄>"""
import copy
import importlib.util
import json
import os
import sys
import tempfile
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


for n in ["mcp", "mcp.server", "mcp.server.transport_security"]:
    _stubmod(n)
_stubmod("mcp.server.fastmcp", FastMCP=FastMCP, Image=_Any)
_stubmod("experience_store", register=lambda *a, **k: None)
for k in [k for k in os.environ if k.startswith("MCP_TOKEN_")]:
    del os.environ[k]
UP = tempfile.mkdtemp(prefix="rt-up-")
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE_LAPTOP": "t1",
                   "MCP_TOKEN_CODEX_LAPTOP": "t2", "MCP_TOKEN_CODEX_PC": "t3",
                   "BOARD_USER": "u", "BOARD_PASSWORD": "p" * 16, "MSG_ARCHIVE": "0", "UPLOAD_DIR": UP})

DB, HIST, TICK = [], {}, [0]
FAIL = {"put_ignored": False}


def _ts():
    TICK[0] += 1
    return f"2026-10-19T02:{TICK[0] // 60:02d}:{TICK[0] % 60:02d}+00:00"


class R:
    def __init__(self, d=None): self.d = d if d is not None else {}
    def raise_for_status(self): pass
    def json(self): return self.d


class FakeClient:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get(self, path, params=None, **k):
        if path.endswith("/history"):
            return R(HIST.get(path.split("/")[2], []))
        return R({"results": [copy.deepcopy(m) for m in DB if m["user_id"] == (params or {}).get("user_id")]})
    def post(self, path, json=None, **k):
        mid, t = str(uuid.uuid4()), _ts()
        DB.append({"id": mid, "memory": json["messages"][0]["content"], "metadata": copy.deepcopy(json.get("metadata") or {}),
                   "agent_id": json.get("agent_id"), "user_id": json["user_id"], "created_at": t, "updated_at": t})
        HIST[mid] = [{"event": "ADD"}]
        return R({"results": [{"id": mid, "event": "ADD"}]})
    def put(self, path, json=None, **k):
        mid = path.rsplit("/", 1)[1]
        if FAIL["put_ignored"]:
            return R()
        for m in DB:
            if m["id"] == mid:
                HIST.setdefault(mid, []).append({"event": "UPDATE", "old_memory": m["memory"], "new_memory": json["text"]})
                m["memory"], m["metadata"], m["updated_at"] = json["text"], copy.deepcopy(json["metadata"]), _ts()
        return R()


spec = importlib.util.spec_from_file_location("srv", os.path.join(SRC, "server.py"))
S = importlib.util.module_from_spec(spec)
sys.path.insert(0, SRC)
spec.loader.exec_module(S)
S.STORE.http = lambda: FakeClient()
S._cookie_ok = lambda request: True
S.BOARD_REV_CHECK = False
from starlette.testclient import TestClient  # noqa: E402

web = TestClient(S.board_app)
H = {"origin": "http://testserver"}
results = []


def check(name, ok):
    results.append((name, bool(ok)))


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
        return False
    except exc:
        return True


def err(fn, *a, **k):
    try:
        fn(*a, **k)
        return ""
    except ValueError as e:
        return str(e)


def msgs():
    return [m for m in DB if (m["metadata"] or {}).get("type") == "message"]


def last():
    return msgs()[-1]


def as_(who, fn, *a, **k):
    tok = S.current_agent.set(who)
    try:
        return fn(*a, **k)
    finally:
        S.current_agent.reset(tok)


def ids(lst):
    return {x["id"] for x in lst}


def feed_msg(mid):
    return next(x for x in S._feed(500)["messages"] if x["id"] == mid)


# ── 一、一則寄給好幾位 ──────────────────────────────────────────
S._send("user", " Claude-laptop:mcp , codex-laptop ", "多位測試", "內文", True, "board", "")
m1 = last()
check("寄給兩位只寫一則", len(msgs()) == 1)
check("metadata.to 存逗號字串（小寫、去空白）", m1["metadata"]["to"] == "claude-laptop:mcp,codex-laptop")
check("內文的收件人也是逗號字串", "user→claude-laptop:mcp,codex-laptop｜多位測試" in m1["memory"])

S._send("user", "codex-laptop,codex-laptop,claude:mcp,claude-laptop:mcp", "去重", "", False, "board", "")
check("同一位（含舊名）只留第一次", last()["metadata"]["to"] == "codex-laptop,claude:mcp")
n = len(msgs())
check("有一位寫錯就整則不寄", raises(ValueError, S._send, "user", "claude-laptop,bogus", "x", "", False, "", "")
      and len(msgs()) == n)
e = err(S._send, "claude-laptop", "codex-laptop,claude-laptop", "x", "", False, "", "")
check("好幾位裡有自己：不能寄，錯誤訊息點名是哪一位", e.startswith("不能寄給自己（claude-laptop）") and len(msgs()) == n)
check("有串名時寄給自己這一串也擋",
      raises(ValueError, S._send, "claude-laptop", "codex-laptop,claude-laptop:mcp", "x", "", False, "", "", from_session="mcp"))
check("寄給自己身分的其他串照舊要帶串名",
      raises(ValueError, S._send, "claude-laptop", "codex-laptop,claude-laptop:board-ui", "x", "", False, "", ""))
nine = ",".join(f"claude-laptop:t{i}" for i in range(9))
check("超過 8 位不寄", err(S._send, "user", nine, "x", "", False, "", "").startswith("一則最多寄給 8 位") and len(msgs()) == n)
check("剛好 8 位可以寄", not err(S._send, "user", ",".join(f"claude-laptop:t{i}" for i in range(8)), "八位", "", False, "", ""))
check("一位時錯誤訊息跟以前一樣", err(S._send, "user", "user", "x", "", False, "", "") == "不能寄給自己"
      and "逗號" not in err(S._send, "user", "bogus", "x", "", False, "", ""))
S._send("user", "codex-laptop", "單一位", "", True, "board", "")
m_single = last()
check("一位的照舊存單一名字", m_single["metadata"]["to"] == "codex-laptop")
S._send("user", "claude-laptop:board-ui,codex", "群組加一串", "", True, "board", "")
m2 = last()
S._send("user", "claude,codex-pc", "身分加別人", "", False, "board", "")
m3 = last()
S._send("codex-pc", "user,codex-laptop", "含使用者", "", True, "board", "")  # AI 寄給使用者和別的 AI
m4 = last()

# 收件匣：只看寫給我的那幾位
inbox_mcp = ids(as_("claude-laptop", S.inbox, session="mcp"))
inbox_ui = ids(as_("claude-laptop", S.inbox, session="board-ui", projects="board"))
inbox_other = ids(as_("claude-laptop", S.inbox, session="other"))
check("寄給 claude-laptop:mcp 的那一串收得到", m1["id"] in inbox_mcp)
check("同身分別串（訂閱 board）收不到直寄 mcp 串的", m1["id"] not in inbox_ui)
check("直寄 board-ui 串的，board-ui 收得到、mcp 收不到", m2["id"] in inbox_ui and m2["id"] not in inbox_mcp)
check("寄給 claude（身分）的照專案分：訂閱 board 的收得到", m3["id"] in inbox_ui)
check("寄給 claude（身分）的照專案分：沒訂閱的收不到", m3["id"] not in inbox_other and m3["id"] not in inbox_mcp)
cx = ids(as_("codex-laptop", S.inbox))
w6 = ids(as_("codex-pc", S.inbox))
check("codex-laptop 收得到寄給它的多位信", {m1["id"], m2["id"], m4["id"], m_single["id"]} <= cx)
check("寄給 codex 群組的，codex-pc 也收得到", m2["id"] in w6)
check("沒寄給 codex-pc 的它收不到（m1、m4）", m1["id"] not in w6 and m4["id"] not in w6)
check("寄給 codex-pc 的它收得到（m3）", m3["id"] in w6)
check("收件匣的 to 照原樣給逗號字串",
      next(x for x in as_("claude-laptop", S.inbox, session="mcp") if x["id"] == m1["id"])["to"] == "claude-laptop:mcp,codex-laptop")

# ACK
check("別串不能替直寄 mcp 的信 ACK", raises(ValueError, as_, "claude-laptop", S.message_ack, m1["id"], session="board-ui"))
check("不是寄給我的不能 ACK", raises(ValueError, as_, "codex-pc", S.message_ack, m1["id"]))

# 待辦與「等誰」
f = feed_msg(m1["id"])
check("feed 預設不帶 multi_to（2.2.0 正式版看到會寄逗號但顯示不拆）", "multi_to" not in S._feed(50))
check("頁面要了（multi=True）才帶 multi_to=true", S._feed(50, multi=True)["multi_to"] is True)
check("端點：/board/api/feed 不帶參數沒有 multi_to", "multi_to" not in web.get("/board/api/feed").json())
check("端點：/board/api/feed?multi_to=1 才有", web.get("/board/api/feed?multi_to=1").json().get("multi_to") is True)
check("多位信一開始是待辦，等 claude 和 codex", f["todo"] and f["waiting_all"] == ["claude", "codex"] and f["waiting_on"] == "claude")
check("一位的信沒有 waiting_all 欄位、waiting_on 照舊", "waiting_all" not in feed_msg(m_single["id"])
      and feed_msg(m_single["id"])["waiting_on"] == "codex")
fd = S._feed(500)
todo_msgs = [x for x in fd["messages"] if x["todo"]]
check("待辦總數一則算一次", fd["todo"] == len(todo_msgs))
check("todo_by 每位各算一次", fd["todo_by"] == {"claude": 2, "codex": 4, "user": 1})
as_("claude-laptop", S.message_ack, m1["id"], session="mcp")
f = feed_msg(m1["id"])
check("mcp 串 ACK 後只剩等 codex", f["todo"] and f["waiting_all"] == ["codex"] and f["waiting_on"] == "codex")
as_("codex-pc", S.message_ack, m2["id"])
check("群組任一位 ACK 就算 codex 處理了，還在等 board-ui 串", feed_msg(m2["id"])["waiting_all"] == ["claude"]
      and feed_msg(m2["id"])["todo"])
as_("codex-laptop", S.message_ack, m1["id"])
f = feed_msg(m1["id"])
check("兩位都 ACK 後不是待辦，waiting_all 列全部", not f["todo"] and f["waiting_all"] == ["claude", "codex"])
as_("claude-laptop", S.message_ack, m2["id"])  # 不帶串名＝整個身分 ACK，寄給某一串的也算處理了
check("不帶串名的整個身分 ACK 也算那一串處理了", not feed_msg(m2["id"])["todo"])
as_("codex-laptop", S.message_ack, m4["id"])
check("其中有使用者的：AI 都 ACK 了還是等使用者", feed_msg(m4["id"])["todo"] and feed_msg(m4["id"])["waiting_all"] == ["user"])
check("別串 ACK 不算 mcp 串處理了", not S._handled("claude-laptop:mcp", ["claude-laptop:board-ui"])
      and S._handled("claude-laptop:mcp", ["claude:mcp"]) and S._handled("claude", ["claude-laptop:x"]))

# 徵求意見、標籤、公告欄寄信
S._send("user", "claude-laptop,codex", "請表示意見", "", False, "board", "", ask_opinion=True)
check("徵求意見：好幾位的聯集都要回", S._expected(last()["metadata"]["to"], "user") == {"claude-laptop", "codex-laptop", "codex-pc"})
check("記憶頁「寄給」標籤拆開顯示", S._group_label("codex,claude-laptop:mcp") == "全部 Codex、claude-laptop:mcp"
      and S._group_label("codex") == "全部 Codex" and S._group_label("?") == "?")
n = len(msgs())
r = web.post("/board/send", data={"to": "claude-laptop:mcp,codex-laptop", "subject": "公告欄寄兩位", "project": "board"},
             headers={**H, "accept": "application/json"})
check("公告欄 /board/send 收逗號：寄出一則", r.status_code == 200 and len(msgs()) == n + 1
      and last()["metadata"]["to"] == "claude-laptop:mcp,codex-laptop" and last()["metadata"]["from"] == "user")

# ── 二、專案卡優先序 ────────────────────────────────────────────


def status_md(p):
    return next(m for m in DB if (m["metadata"] or {}).get("type") == "status" and m["metadata"].get("project") == p)


def card(p):
    return next(s for s in S._whiteboard(S._list_all())[0] if s["project"] == p)


def prio(p, v, origin=True):
    return web.post("/board/api/project/priority", json={"project": p, "priority": v}, headers=H if origin else {})


S.current_agent.set("claude-laptop")
ps = S.project_status("mem0")
check("沒有進度卡時 priority=0、paused=false", ps["priority"] == 0 and ps["paused"] is False)
S.project_set_status("mem0", "名稱：圓桌記憶\n現在：測試中\n- [ ] 第一步", "implementing")
v0 = S.project_status("mem0")
t0 = status_md("mem0")["updated_at"]
check("預設沒設（project_status 0、白板 0）", v0["priority"] == 0 and card("mem0")["priority"] == 0)
r = prio("mem0", 2)
check("設 P2：回 200 和 {project, priority:2}", r.status_code == 200 and r.json() == {"project": "mem0", "priority": 2})
md = status_md("mem0")["metadata"]
check("metadata 記 priority=\"2\"、誰改的是 user、有時間、改過 1 次", md.get("priority") == "2" and md.get("priority_by") == "user"
      and md.get("priority_at", "").startswith("20") and md.get("priority_flips") == "1")
check("內文、version 沒動", status_md("mem0")["memory"] == "[狀態] 名稱：圓桌記憶\n現在：測試中\n- [ ] 第一步"
      and S.project_status("mem0")["version"] == v0["version"])
c = card("mem0")
check("白板 priority=2、時間還是原本的、更新次數不算", c["priority"] == 2 and c["tw"] == S._tw(t0) and c["updates"] == 0)
check("project_status priority=2、沒暫停", S.project_status("mem0")["priority"] == 2 and not S.project_status("mem0")["paused"])
h = len(HIST[status_md("mem0")["id"]])
check("設成一樣的不寫入", prio("mem0", 2).json() == {"project": "mem0", "priority": 2}
      and len(HIST[status_md("mem0")["id"]]) == h and status_md("mem0")["metadata"]["priority_flips"] == "1")
check("字串 \"3\" 也收", prio("mem0", "3").json() == {"project": "mem0", "priority": 3})
web.post("/board/api/project/pingpong", json={"project": "mem0", "on": True}, headers=H)
S.project_set_status("mem0", "名稱：圓桌記憶\n現在：第二步\n- [x] 第一步", "", S.project_status("mem0")["version"])
md = status_md("mem0")["metadata"]
check("AI 更新進度後優先序和乒乓都還在", md.get("priority") == "3" and md.get("priority_flips") == "2" and md.get("pingpong") == "yes")
check("更新次數扣掉乒乓和優先序：只算 AI 那一次", card("mem0")["updates"] == 1)
prio("mem0", 5)
ps = S.project_status("mem0")
check("P5＝暫停：paused=true、project_status 的 pingpong 回 false（乒乓跳過）", ps["paused"] is True and ps["priority"] == 5
      and ps["pingpong"] is False)
check("白板卡片照樣顯示乒乓開關原本的值", card("mem0")["pingpong"] is True and card("mem0")["priority"] == 5)
prio("mem0", 0)
ps = S.project_status("mem0")
check("0＝清除：priority 寫空字串、回 0、乒乓恢復", status_md("mem0")["metadata"]["priority"] == ""
      and ps["priority"] == 0 and ps["pingpong"] is True and not ps["paused"])
check("更新次數還是 1", card("mem0")["updates"] == 1)
check("沒有進度卡的專案回 404", prio("nosuch", 1).status_code == 404)
bad = [prio("mem0", x).status_code for x in (6, -1, True, "x", 1.5, None)]
check("priority 不是 0～5 的整數回 400", bad == [400] * 6)
check("專案名稱不合法回 400", prio("Bad Name!", 1).status_code == 400)
check("不是公告欄頁面送的（沒有 origin）回 403", prio("mem0", 1, origin=False).status_code == 403)
check("太大回 413", web.post("/board/api/project/priority", content=b"x" * 5000, headers=H).status_code == 413)
check("錯誤的請求都沒改到資料", status_md("mem0")["metadata"]["priority"] == "")
FAIL["put_ignored"] = True
r = prio("mem0", 1)
FAIL["put_ignored"] = False
check("寫完讀回對不上就回錯（400）", r.status_code == 400 and "讀回" in r.json().get("error", ""))
check("新端點在「擋舊版頁面」的名單裡", "/board/api/project/priority".startswith(S._REV_WRITE_PREFIXES))
S.BOARD_REV_CHECK = True
check("舊版頁面（沒帶版號）送出回 409", prio("mem0", 1).status_code == 409)
S.BOARD_REV_CHECK = False
S.project_set_status("pve", "名稱：PVE", "implementing")
check("project_set_status 不會無中生有優先序欄位", not any(k in status_md("pve")["metadata"] for k in S.PRIORITY_KEYS)
      and card("pve")["priority"] == 0)
check("看不懂的值當沒設", S._priority({"priority": "9"}) == 0 and S._priority({"priority": "abc"}) == 0
      and S._priority({}) == 0 and S._priority({"priority": "4"}) == 4)

# ── 三、AI 自訂頭像 ──────────────────────────────────────────────
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-avatar"


def avatar(who, file, origin=True):
    return web.post("/board/api/avatar", json={"who": who, "file": file}, headers=H if origin else {})


check("沒設過：feed avatars 是 {}", S._feed(10)["avatars"] == {})
fid = web.post("/board/upload", content=PNG, headers={**H, "content-type": "image/png"}).json()["id"]
r = avatar("claude", fid)
check("設頭像：回 200 和 {avatars:{claude:fid}}", r.status_code == 200 and r.json() == {"avatars": {"claude": fid}})
check("存在 UPLOAD_DIR/avatars.json", json.load(open(os.path.join(UP, "avatars.json"), encoding="utf-8")) == {"claude": fid})
check("feed 帶 avatars", S._feed(10)["avatars"] == {"claude": fid})
check("大寫代號轉小寫", avatar("XD", fid).json()["avatars"] == {"claude": fid, "xd": fid})
check("空字串＝換回預設", avatar("xd", "").json() == {"avatars": {"claude": fid}})
check("本來就沒設的換回預設也不會錯", avatar("mini", "").status_code == 200)
bad = [avatar(w, fid).status_code for w in ("", "a b", "a" * 65, "克勞德", None)]
check("代號格式不對回 400", bad == [400] * 5)
bad = [avatar("codex", x).status_code for x in ("x.png", "0" * 24 + ".png", "../avatars.json", "avatars.json")]
check("圖片編號不對或檔案不在回 400", bad == [400] * 4)
check("錯誤的請求都沒改到", S._avatars() == {"claude": fid})
check("不是公告欄頁面送的回 403", avatar("codex", fid, origin=False).status_code == 403)
check("太大回 413", web.post("/board/api/avatar", content=b"x" * 5000, headers=H).status_code == 413)
check("頭像端點在「擋舊版頁面」的名單裡", "/board/api/avatar" in S._REV_WRITE_PATHS)
S.BOARD_REV_CHECK = True
check("舊版頁面（沒帶版號）送出回 409", avatar("codex", fid).status_code == 409)
S.BOARD_REV_CHECK = False
check("/board/files/ 讀不到 avatars.json", web.get("/board/files/avatars.json").status_code in (400, 404))
S.AVATAR_MAX = 2
avatar("codex", fid)
check("超過上限回 400、不寫", avatar("hermes", fid).status_code == 400 and set(S._avatars()) == {"claude", "codex"})
S.AVATAR_MAX = 64
with open(os.path.join(UP, "avatars.json"), "w", encoding="utf-8") as fh:
    fh.write('{"claude": "../../etc/passwd", "ok": "' + fid + '", "Bad Key": "' + fid + '"}')
check("檔案裡不合格式的項目不給出去", S._avatars() == {"ok": fid})
with open(os.path.join(UP, "avatars.json"), "w", encoding="utf-8") as fh:
    fh.write("壞掉了{")
check("檔案壞了 feed 回 {}，不會整個 feed 出錯", S._feed(10)["avatars"] == {})
check("檔案壞了照樣能重設", avatar("claude", fid).json() == {"avatars": {"claude": fid}})

fail = [n for n, ok in results if not ok]
for n, ok in results:
    print(("通過 " if ok else "失敗 ") + n)
print(f"共 {len(results)} 項，失敗 {len(fail)} 項")
sys.exit(1 if fail else 0)
