"""本機跑真的公告欄後端（board_app），資料是假的，用來在瀏覽器實測頁面和後端一起的行為。
不連 Mem0：寄信、結案只記在記憶體，feed 用 whiteboard-v2/tools/feed.json。
用法：uv run --no-project --with starlette --with httpx --with uvicorn python run_board_local.py <server.py 目錄> <UI 資料夾> [埠，預設 8905]
UI 資料夾裡放 stable/ 和 preview/ 各三頁；換掉 stable/ 的檔案就等於「採用了新版」。"""
import importlib.util
import json
import os
import sys
import types

SRC, UI = sys.argv[1], os.path.abspath(sys.argv[2])
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8905


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
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "UI_DIR": UI, "BOARD_USER": "u", "BOARD_PASSWORD": "p" * 16})
if os.environ.get("FAKE_AGENTS") == "1":
    # v2.2.0 寄給欄測試：假的代理身分（值是假的，只為了讓收件人清單有 claude／codex 各台／hermes 各位）
    for i, n in enumerate(["CLAUDE_LAPTOP", "CODEX_LAPTOP", "CODEX_PC", "CODEX_MAX", "CODEX_MINI", "HERMES_ALPHA", "HERMES_BETA", "HERMES_GAMMA"]):
        os.environ["MCP_TOKEN_" + n] = "fake-token-%d" % i
sys.path.insert(0, SRC)
spec = importlib.util.spec_from_file_location("srv", os.path.join(SRC, "server.py"))
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

FEED = json.load(open(os.path.join(os.path.dirname(__file__), "../../whiteboard-v2/tools/feed.json"), encoding="utf-8"))
SENT = []
S._cookie_ok = lambda request: True
S._send = lambda *a, **k: (SENT.append(a), print("[假站] 收到寄信：", a[1:3], "專案=", a[5] if len(a) > 5 else "", flush=True))[0] or {"id": "m%d" % len(SENT)}
S._list_all = lambda: []
_REAL_FEED = S._feed
S._feed = lambda n: {"messages": [], "total": 0, "todo": 0, "todo_by": {}, "claims": [], "recipients": ["all", "claude", "codex"],
                     "aliases": {}, "sessions": {}, "statuses": FEED.get("statuses", []), "research": FEED.get("research", []), "sent_count": len(SENT)}

if os.environ.get("FAKE_MSGS") == "1":
    # 每個代理各一則假留言，用真的 _feed 組出來，看頭像在留言列表的樣子
    _who_list = ["claude", "codex-laptop", "codex-pc", "codex-max", "codex-mini", "hermes-alpha", "hermes-beta", "hermes-gamma", "user"]
    _fake = [{"id": "00000000-0000-4000-8000-%012d" % i, "agent_id": w, "created_at": "2026-10-19T0%d:0%d:00+08:00" % (1 + i // 6, i % 6),
              "memory": "[訊息] %s→all｜頭像測試 %d" % (w, i),
              "metadata": {"type": "message", "to": "all", "from": w, "subject": "頭像測試：%s" % w, "body": "", "needs_action": "no",
                           "read_by": "", "project": "general", "ref": "", "attachments": ""}} for i, w in enumerate(_who_list)]
    S._list_all = lambda: list(_fake)
    S._feed = lambda n: {**_REAL_FEED(n), "statuses": FEED.get("statuses", []), "research": FEED.get("research", [])}

if os.environ.get("FAKE_STORE") == "1":
    # 整條走真的後端：專案卡（status 記憶）也放記憶體，Mem0 的 HTTP 用 MockTransport 接住，PUT 會真的改到這份資料
    import httpx
    _store = list(S._list_all())
    for i, (p, stage, pp) in enumerate([("mem0", "implementing", "yes"), ("whiteboard-v2", "verifying", ""), ("pingpong", "implementing", "no")]):
        _store.append({"id": "11111111-0000-4000-8000-%012d" % i, "agent_id": "claude", "created_at": "2026-10-18T09:00:00+08:00",
                       "updated_at": "2026-10-19T0%d:00:00+08:00" % (i + 1),
                       "memory": "名稱：%s 測試卡\n現在：本機測試\n- [x] 第一步\n- [ ] 第二步" % p,
                       "metadata": {"type": "status", "project": p, "stage": stage, **({"pingpong": pp} if pp else {})}})
    if os.environ.get("FAKE_STORE_WAITING") == "1":
        # 用真的後端算 blocked_on_user：pingpong 的「現在」在等使用者（含要跳脫的 <>），whiteboard-v2 是第一個沒勾的小目標在等使用者
        _card = {m["metadata"]["project"]: m for m in _store if m["metadata"].get("type") == "status"}
        _card["pingpong"]["memory"] = "名稱：pingpong 測試卡\n現在：等使用者決定 <乒乓> 要不要先只開 mem0\n- [x] 第一步\n- [ ] 第二步"
        _card["whiteboard-v2"]["memory"] = "名稱：whiteboard-v2 測試卡\n現在：本機測試\n- [x] 第一步\n- [ ] 等使用者批准上線"
    for i in range(int(os.environ.get("FAKE_STORE_MANY") or 0)):
        # v2.3.0：卡片多到白板要捲動，才看得出更新時捲動位置有沒有跑掉
        _store.append({"id": "22222222-0000-4000-8000-%012d" % i, "agent_id": "codex-pc", "created_at": "2026-10-17T09:00:00+08:00",
                       "updated_at": "2026-10-18T%02d:00:00+08:00" % (i % 24), "memory": "名稱：測試專案 %d\n主管：使用者\n做事：Codex PC\n現在：本機測試\n- [x] 第一步\n- [ ] 第二步\n- [ ] 第三步" % (i + 1),
                       "metadata": {"type": "status", "project": "demo-%02d" % (i + 1), "stage": "implementing"}})
    S._list_all = lambda: list(_store)

    def _mock(req):
        mid = req.url.path.split("/")[2] if req.url.path.startswith("/memories/") else ""
        if req.method == "PUT":
            d = json.loads(req.content)
            for m in _store:
                if m["id"] == mid:
                    m.update(memory=d["text"], metadata=d["metadata"])
                    print("[假站] Mem0 PUT", mid[-4:], {k: v for k, v in d["metadata"].items() if k.startswith(("pingpong", "status_at"))}, flush=True)
        return httpx.Response(200, json={"results": []})
    S.STORE.http = lambda: httpx.Client(base_url="http://fake", transport=httpx.MockTransport(_mock))
    S._feed = _REAL_FEED
    if os.environ.get("FAKE_BLOCKED") == "1":
        # v2.1.0：後端還沒算 blocked_on_user 時，先假裝 pingpong 卡在使用者（有那一句）、whiteboard-v2 卡住但沒那一句
        _wb = S._whiteboard

        def _whiteboard_blocked(items):
            st, rs = _wb(items)
            why = {"pingpong": "等使用者決定 &lt;乒乓&gt; 要不要先只開 mem0", "whiteboard-v2": ""}
            for s in st:
                s.setdefault("blocked_on_user", s["project"] in why)
                s.setdefault("blocked_why", why.get(s["project"], ""))
            return st, rs
        S._whiteboard = _whiteboard_blocked

if os.environ.get("FAKE_SEND_FAIL"):
    # v2.2.0：寄給這個地址時假裝後端拒收，看多位收件人有人失敗時頁面怎麼處理
    _fake_send, _bad = S._send, os.environ["FAKE_SEND_FAIL"]

    def _send_maybe_fail(*a, **k):
        if a[1] == _bad:
            print("[假站] 拒收：", a[1], flush=True)
            raise ValueError("假的失敗：" + _bad)
        return _fake_send(*a, **k)
    S._send = _send_maybe_fail

app = S.board_app
if os.environ.get("FAKE_PRIO_AVATAR") == "1":
    # v2.3.0：後端還沒做的兩個欄位先用假的——專案卡優先序（priority 0–5）、AI 頭像（avatars: {代號: 圖檔}），都只記在記憶體
    # 圖片走真的 /board/upload 和 /board/files（UPLOAD_DIR 要指到本機暫存資料夾）
    import re as _re
    PRIO, AV = {}, {}
    _wb2 = S._whiteboard

    def _whiteboard_prio(items):
        st, rs = _wb2(items)
        for s in st:
            s["priority"] = PRIO.get(s["project"], 0)
        return st, rs
    S._whiteboard = _whiteboard_prio
    _feed2 = S._feed
    S._feed = lambda n: {**_feed2(n), "avatars": dict(AV)}

    def _prio(d):
        p, v = str(d.get("project") or ""), d.get("priority")
        if not any(m["metadata"].get("project") == p and m["metadata"].get("type") == "status" for m in S._list_all()):
            return 404, {"error": "這個專案沒有進度卡"}
        if isinstance(v, bool) or v not in (0, 1, 2, 3, 4, 5):
            return 400, {"error": "priority 要是 0 到 5"}
        if v:
            PRIO[p] = v
        else:
            PRIO.pop(p, None)
        print("[假站] 優先序：", p, v, flush=True)
        return 200, {"project": p, "priority": v}

    def _avatar(d):
        who, f = str(d.get("who") or ""), str(d.get("file") or "")
        if not _re.match(r"^[a-z0-9-]{1,24}$", who):
            return 400, {"error": "頭像代號格式不對"}
        if f:
            S._file_path(f)  # 跟真的一樣：圖檔要已經上傳過
            AV[who] = f
        else:
            AV.pop(who, None)
        print("[假站] 頭像：", who, f or "（換回預設）", flush=True)
        return 200, {"avatars": dict(AV)}
    _ROUTES = {"/board/api/project/priority": _prio, "/board/api/avatar": _avatar}

    async def app(scope, receive, send, _inner=app):
        fn = _ROUTES.get(scope.get("path")) if scope["type"] == "http" and scope["method"] == "POST" else None
        if not fn:
            return await _inner(scope, receive, send)
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        try:
            status, out = fn(json.loads(body or b"{}"))
        except ValueError as e:
            status, out = 400, {"error": str(e)}
        raw = json.dumps(out, ensure_ascii=False).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json; charset=utf-8")]})
        await send({"type": "http.response.body", "body": raw})

if os.environ.get("FAKE_PINGPONG") == "1":
    # 假的乒乓開關（v2.0.0 網頁測試用）：statuses 帶 pingpong，POST /board/api/project/pingpong 切換，只記在記憶體
    PING = {}
    for s in FEED.get("statuses", []):
        s["pingpong"] = PING.setdefault(s.get("project"), False)

    async def app(scope, receive, send, _inner=S.board_app):
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == "/board/api/project/pingpong":
            body = b""
            while True:
                msg = await receive()
                body += msg.get("body", b"")
                if not msg.get("more_body"):
                    break
            d = json.loads(body or b"{}")
            p = d.get("project")
            status, out = (404, {"error": "這個專案沒有進度卡"}) if p not in PING else (200, None)
            if out is None:
                PING[p] = bool(d.get("on"))
                for s in FEED["statuses"]:
                    if s.get("project") == p:
                        s["pingpong"] = PING[p]
                out = {"project": p, "pingpong": PING[p]}
                print("[假站] 乒乓開關：", p, PING[p], flush=True)
            raw = json.dumps(out, ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json; charset=utf-8")]})
            await send({"type": "http.response.body", "body": raw})
            return
        await _inner(scope, receive, send)

import uvicorn  # noqa: E402
uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
