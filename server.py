# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0
# Required Notice: Copyright (c) 2026 T910317 (https://github.com/T910317)
"""Mem0 圓桌記憶 MCP 服務。

規則寫死在這裡：user_id 固定、infer 固定 false、agent_id 由權杖決定（無法冒充）。
每個 AI 一把權杖，格式見 .env：MCP_TOKEN_<AGENT>=...
注意：「先查後寫」靠本程序內的鎖，只能跑單一程序（不要開多個 worker 或多個容器）；
stateless_http 必須維持 True，否則身分會跟著 session 走，可被別的權杖沿用。
"""
import base64
import contextvars
import datetime as dt
import hashlib
import hmac
import html
import os
import re
import threading
import urllib.parse

import httpx
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

MEM0_URL = os.environ["MEM0_URL"].rstrip("/")  # 例如 https://mem0-api.example.com
MEM0_KEY = os.environ["MEM0_API_KEY"]
USER_ID = "roundtable"
TYPES_ADD = ("decision", "note")
STAGES = ("planning", "implementing", "verifying", "done")
CLAIM_TTL_HOURS = 24  # claim 超過這麼久沒更新就視為過期，可被接手
_write_lock = threading.Lock()  # 單一進程內讓「先查後寫」不被打斷
LIST_CAP = 1000  # Mem0 列表一次最多 1000 筆；不帶 top_k 只會拿到預設筆數，資料多了會默默漏看
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
TAG_RE = re.compile(r"^(\s*\[(狀態|進行中|決策|筆記|訊息)\]\s*)+")
BOARD_USER = os.environ.get("BOARD_USER", "")
BOARD_PASSWORD = os.environ.get("BOARD_PASSWORD", "")

TOKENS = {
    v: k[len("MCP_TOKEN_"):].lower().replace("_", "-")
    for k, v in os.environ.items()
    if k.startswith("MCP_TOKEN_") and v
}
current_agent: contextvars.ContextVar[str] = contextvars.ContextVar("agent", default="")

mcp = FastMCP(
    "roundtable-memory",
    host="0.0.0.0",
    port=8890,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _http() -> httpx.Client:
    return httpx.Client(base_url=MEM0_URL, headers={"X-API-Key": MEM0_KEY},
                        timeout=httpx.Timeout(20.0, connect=5.0))


def _items(data) -> list:
    if isinstance(data, dict):
        return data.get("results", [])
    return data or []


def _fmt(m: dict) -> dict:
    md = m.get("metadata") or {}
    return {
        "id": m.get("id"),
        "text": m.get("memory"),
        "agent": m.get("agent_id"),
        "project": md.get("project"),
        "type": md.get("type"),
        "stage": md.get("stage"),
        "target": md.get("target"),
        "updated": m.get("updated_at") or m.get("created_at"),
        "created": m.get("created_at"),
        "updated_by": md.get("updated_by"),
    }


def _list_all() -> list:
    with _http() as c:
        r = c.get("/memories", params={"user_id": USER_ID, "top_k": LIST_CAP})
        r.raise_for_status()
        items = _items(r.json())
    if len(items) >= LIST_CAP:
        raise RuntimeError(f"共用記憶已達 {LIST_CAP} 筆，列表可能不完整，claim 與 status 檢查不可靠；請使用者先清理舊記憶")
    return items


def _match(m: dict, **want) -> bool:
    md = m.get("metadata") or {}
    return all(v is None or md.get(k) == v for k, v in want.items())


def _add(content: str, metadata: dict) -> dict:
    body = {
        "messages": [{"role": "user", "content": content}],
        "user_id": USER_ID,
        "agent_id": current_agent.get(),
        "infer": False,
        "metadata": metadata,
    }
    with _http() as c:
        r = c.post("/memories", json=body)
        r.raise_for_status()
        return r.json()


def _norm(t: str) -> str:
    """target 比對用：小寫並去掉空白、dash、底線（vm219 與 VM-219 視為同一個）。"""
    return re.sub(r"[\s_\-]", "", (t or "").lower())


def _age_hours(m: dict) -> float:
    ts = m.get("updated_at") or m.get("created_at")
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - d).total_seconds() / 3600
    except Exception:
        return 0.0  # 讀不出時間就當作新的（寧可擋住，不要誤放）


def _version(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def _claims(t: str) -> list:
    """同一個 target 的所有 claim，不分專案（同一台機器掛在哪個專案下被認領都算）。"""
    return [m for m in _list_all()
            if _match(m, type="claim") and _norm((m.get("metadata") or {}).get("target")) == _norm(t)]


def _clean(s: str, name: str, limit: int = 1000) -> str:
    s = (s or "").strip()
    if not s:
        raise ValueError(f"{name} 不可為空")
    if len(s) > limit:
        raise ValueError(f"{name} 太長（上限 {limit} 字）")
    return s


def _body(s: str, name: str) -> str:
    """內文：去掉開頭自帶的 [狀態]／[筆記] 等標籤（工具會自己加），避免重複或冒充別種記憶。"""
    return _clean(TAG_RE.sub("", _clean(s, name)), name)


def _proj(p: str) -> str:
    p = _clean(p, "project", 60).lower()
    if not PROJECT_RE.match(p):
        raise ValueError(f"project 只能用小寫英文、數字與 - _ .（例如 mem0、homelab-network），收到 {p!r}")
    return p


@mcp.tool()
def project_status(project: str) -> dict:
    """查某專案目前的進度（status）。開工前先呼叫。"""
    p = _proj(project)
    found = [_fmt(m) for m in _list_all() if _match(m, project=p, type="status")]
    return {"project": p, "status": found[0] if found else None, "count": len(found),
            "version": _version(found[0]["text"]) if found else ""}


@mcp.tool()
def project_set_status(project: str, summary: str, stage: str = "", base_version: str = "") -> dict:
    """更新某專案唯一的 status（沒有就建立）。收工後呼叫。summary 寫結論與下一步，會整筆取代舊內容，仍有效的舊內容要保留進去。
    已有 status 時必須帶 base_version（project_status 回傳的 version），表示你看過最新內容才改；
    版本不符代表別人剛改過，會拒絕並回傳最新內容，請重讀後再改。stage 不填就沿用原本的階段。"""
    p = _proj(project)
    if stage and stage not in STAGES:
        raise ValueError(f"stage 必須是 {STAGES}")
    text = "[狀態] " + _body(summary, "summary")
    me = current_agent.get()
    with _write_lock:
        existing = [m for m in _list_all() if _match(m, project=p, type="status")]
        if existing:
            cur = existing[0].get("memory") or ""
            if base_version.strip() != _version(cur):
                raise ValueError("status 已被更新，或你沒帶 base_version；請先呼叫 project_status 取得最新 version。目前內容："
                                 + cur[:400])
            old_stage = (existing[0].get("metadata") or {}).get("stage")
            md = {"project": p, "type": "status", "stage": stage or old_stage or "implementing", "updated_by": me}
            with _http() as c:
                r = c.put(f"/memories/{existing[0]['id']}", json={"text": text, "metadata": md})
                r.raise_for_status()
            return {"updated": existing[0]["id"], "by": me, "version": _version(text)}
        md = {"project": p, "type": "status", "stage": stage or "implementing", "updated_by": me}
        res = _add(text, md)
    return {"created": True, "by": me, "version": _version(text), "result": res}


@mcp.tool()
def claim_check(project: str = "", target: str = "") -> list:
    """查有沒有 AI 正在動某專案或目標（claim）。動手前先呼叫；有別人的 claim 就停下來問使用者。
    要動某台機器或服務時只帶 target（不帶 project），才看得到它掛在其他專案下的 claim。
    stale=true 表示超過 24 小時沒更新，可能是忘了放掉，仍要先問使用者再接手。"""
    p = _proj(project) if project.strip() else None
    t = _norm(target) if target.strip() else None
    out = []
    for m in _list_all():
        if not _match(m, type="claim", project=p):
            continue
        if t and _norm((m.get("metadata") or {}).get("target")) != t:
            continue
        out.append({**_fmt(m), "stale": _age_hours(m) > CLAIM_TTL_HOURS})
    return out


@mcp.tool()
def claim_create(project: str, target: str, summary: str) -> dict:
    """開工標記：宣告你正在動某個目標。target 寫主機或服務名（例如 vm219、mem0-mcp），不要寫工作名稱。
    同一個 target 只要有別的 AI 的有效 claim 就拒絕，不分專案；自己已有的 claim 會被更新（續期）。
    別人的 claim 超過 24 小時沒更新視為過期，可接手，但回傳會附 warning，請告訴使用者。"""
    p, t = _proj(project), _clean(target, "target", 80).lower()
    me = current_agent.get()
    text = "[進行中] " + _body(summary, "summary")
    md = {"project": p, "type": "claim", "target": t}
    with _write_lock:
        mine, stale = None, []
        for m in _claims(t):
            if m.get("agent_id") == me:
                mine = m
            elif _age_hours(m) <= CLAIM_TTL_HOURS:
                other = (m.get("metadata") or {}).get("project")
                raise ValueError(f"{m.get('agent_id')} 已在處理 {t}（專案 {other}），請先問使用者")
            else:
                stale.append(_fmt(m))
        if mine:
            with _http() as c:
                c.put(f"/memories/{mine['id']}", json={"text": text, "metadata": md}).raise_for_status()
            return {"by": me, "renewed": mine["id"], "previous": mine.get("memory"),
                    "warning": "你的身分原本就有這個 claim，已續期並換成新說明。若 previous 不是你這個對話寫的，"
                               "代表你的另一個對話可能正在做，請先問使用者。"}
        res = _add(text, md)
    out = {"by": me, "result": res}
    if stale:
        out["warning"] = "這個目標有別人已過期的 claim（見 stale_claims），你已接手，請告訴使用者。"
        out["stale_claims"] = stale
    return out


@mcp.tool()
def claim_release(project: str, target: str) -> dict:
    """收工移除：只能移除你自己的 claim（同一個 target 不分專案都會放掉；同一個 AI 的其他對話開的也會一起放掉）。"""
    _proj(project)
    t = _clean(target, "target", 80)
    with _write_lock:
        mine = [m for m in _claims(t) if m.get("agent_id") == current_agent.get()]
        with _http() as c:
            for m in mine:
                c.delete(f"/memories/{m['id']}").raise_for_status()
    return {"released": len(mine)}


@mcp.tool()
def memory_add(type: str, project: str, content: str) -> dict:
    """新增 decision（已定案的決定與原因）或 note（值得記的事實）。不要放密碼或金鑰。"""
    if type not in TYPES_ADD:
        raise ValueError(f"type 只能是 {TYPES_ADD}；進度請用 project_set_status，標記請用 claim_create")
    tag = {"decision": "[決策] ", "note": "[筆記] "}[type]
    res = _add(tag + _body(content, "content"), {"project": _proj(project), "type": type})
    return {"by": current_agent.get(), "result": res}


def _recipients() -> set:
    return set(TOKENS.values()) | {"all", "user"}


def _readers(m: dict) -> list:
    return [x for x in ((m.get("metadata") or {}).get("read_by") or "").split(",") if x]


def _send(frm: str, to: str, subject: str, body: str, needs_action: bool, project: str, ref: str) -> dict:
    t = (to or "").strip().lower()
    if t not in _recipients():
        raise ValueError(f"to 只能是 {sorted(_recipients())}")
    if t == frm:
        raise ValueError("不能寄給自己")
    subj = _body(subject, "subject")
    if len(subj) > 80:
        raise ValueError("subject 太長（上限 80 字），細節請放 body 或 ref 檔案")
    b = _body(body, "body") if (body or "").strip() else ""
    text = f"[訊息] {frm}→{t}｜{subj}" + (f"｜{b}" if b else "")
    md = {"type": "message", "to": t, "from": frm, "subject": subj, "body": b,
          "needs_action": "yes" if needs_action else "no", "read_by": "",
          "project": _proj(project) if (project or "").strip() else "general", "ref": (ref or "").strip()[:200]}
    body_json = {"messages": [{"role": "user", "content": text}], "user_id": USER_ID,
                 "agent_id": frm, "infer": False, "metadata": md}
    with _http() as c:
        r = c.post("/memories", json=body_json)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def message_send(to: str, subject: str, body: str = "", needs_action: bool = False,
                 project: str = "", ref: str = "") -> dict:
    """留言給另一個 AI、全體（all）或使用者（user）。subject 一句話講結論（80 字內）；
    細節放 body（簡短）或寫成檔案後把路徑放 ref，不要貼長篇內容。needs_action=true 表示對方需要動手。
    使用者可以在公告欄網頁看到全部留言。"""
    res = _send(current_agent.get(), to, subject, body, needs_action, project, ref)
    return {"sent": True, "by": current_agent.get(), "result": res}


@mcp.tool()
def inbox(include_read: bool = False) -> list:
    """看寫給你（或全體）的留言，預設只列未讀。每次開工先呼叫；處理完用 message_ack 標記已讀。"""
    me = current_agent.get()
    out = []
    for m in _list_all():
        md = m.get("metadata") or {}
        if md.get("type") != "message" or md.get("to") not in (me, "all") or md.get("from") == me:
            continue
        read = me in _readers(m)
        if read and not include_read:
            continue
        out.append({"id": m.get("id"), "from": md.get("from"), "to": md.get("to"), "subject": md.get("subject"),
                    "text": m.get("memory"), "needs_action": md.get("needs_action") == "yes",
                    "project": md.get("project"), "ref": md.get("ref"), "created": m.get("created_at"), "read": read})
    out.sort(key=lambda x: str(x["created"]))
    return out


@mcp.tool()
def message_ack(message_id: str) -> dict:
    """把一則寫給你（或全體）的留言標記為已讀。"""
    me = current_agent.get()
    with _write_lock:
        for m in _list_all():
            if m.get("id") != message_id:
                continue
            md = dict(m.get("metadata") or {})
            if md.get("type") != "message" or md.get("to") not in (me, "all"):
                raise ValueError("這不是寫給你的留言")
            readers = _readers(m)
            if me not in readers:
                md["read_by"] = ",".join(readers + [me])
                with _http() as c:
                    c.put(f"/memories/{message_id}", json={"text": m.get("memory") or "", "metadata": md}).raise_for_status()
            return {"acked": message_id, "by": me}
    raise ValueError("找不到這則留言")


@mcp.tool()
def memory_search(query: str, project: str = "", type: str = "", agent: str = "", limit: int = 5) -> list:
    """用意思搜尋共用記憶。可用 project、type（status/claim/decision/note）、agent 縮小範圍。"""
    filters = {"user_id": USER_ID}
    if project.strip():
        filters["project"] = _proj(project)
    if type.strip():
        filters["type"] = type.strip()
    if agent.strip():
        filters["agent_id"] = agent.strip()
    with _http() as c:
        r = c.post("/search", json={"query": _clean(query, "query", 500), "filters": filters})
        r.raise_for_status()
        out = _items(r.json())[: max(1, min(limit, 20))]
    return [{**_fmt(m), "score": m.get("score")} for m in out]


@mcp.tool()
def memory_list(project: str = "", type: str = "") -> list:
    """盤點用：列出記憶（可依專案、類型篩選）。"""
    p = _proj(project) if project.strip() else None
    t = type.strip() or None
    return [_fmt(m) for m in _list_all() if _match(m, project=p, type=t)]


def _tw(ts) -> str:
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def _board_html() -> str:
    esc = html.escape
    items = _list_all()
    msgs = sorted([m for m in items if (m.get("metadata") or {}).get("type") == "message"],
                  key=lambda m: str(m.get("created_at")), reverse=True)[:100]
    claims = [m for m in items if (m.get("metadata") or {}).get("type") == "claim"]
    rows = []
    for m in msgs:
        md = m.get("metadata") or {}
        urgent = md.get("needs_action") == "yes"
        cls = "m urgent" if urgent else "m"
        read = ",".join(_readers(m)) or "尚無人讀"
        ref = f'<div class="ref">檔案：{esc(md.get("ref"))}</div>' if md.get("ref") else ""
        rows.append(f'<div class="{cls}"><div class="h"><b>{esc(md.get("from", "?"))}</b> → <b>{esc(md.get("to", "?"))}</b>'
                    f'<span class="t">{_tw(m.get("created_at"))}　{"需處理" if urgent else ""}</span></div>'
                    f'<div class="s">{esc(md.get("subject") or "")}</div>'
                    f'<div class="b">{esc(md.get("body") or "")}</div>'
                    f'{ref}<div class="r">已讀：{esc(read)}　專案：{esc(md.get("project") or "")}</div></div>')
    cl = "".join(f'<li><b>{esc((m.get("metadata") or {}).get("target") or "?")}</b>　{esc(m.get("agent_id") or "?")}：'
                 f'{esc((m.get("memory") or "")[:120])}（{_tw(m.get("updated_at") or m.get("created_at"))}'
                 f'{"，已過期" if _age_hours(m) > CLAIM_TTL_HOURS else ""}）</li>' for m in claims) or "<li>目前沒有人認領任何機器</li>"
    opts = "".join(f"<option>{esc(a)}</option>" for a in sorted(_recipients() - {"user"}))
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>圓桌公告欄</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--tx:#1d2330;--mut:#667085;--bd:#e2e5ea;--ac:#2f6fed;--warn:#d9480f}}
@media(prefers-color-scheme:dark){{:root{{--bg:#12151b;--card:#1b2029;--tx:#e6e9ef;--mut:#98a1b2;--bd:#2b3240;--ac:#6ea0ff;--warn:#ff8a4c}}}}
body{{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 system-ui,"Noto Sans TC",sans-serif}}
main{{max-width:760px;margin:0 auto;padding:16px}}h1{{font-size:20px;margin:8px 0}}h2{{font-size:15px;color:var(--mut);margin:20px 0 8px}}
.m,.box{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:10px 12px;margin:8px 0}}
.urgent{{border-left:4px solid var(--warn)}}.h{{display:flex;justify-content:space-between;gap:8px}}.t,.r,.ref{{color:var(--mut);font-size:12px}}
.s{{font-weight:600;margin-top:4px}}.b{{white-space:pre-wrap}}ul{{padding-left:18px;margin:4px 0}}
form{{display:grid;gap:8px}}input,select,textarea,button{{font:inherit;padding:8px;border-radius:8px;border:1px solid var(--bd);background:var(--card);color:var(--tx)}}
button{{background:var(--ac);color:#fff;border:0;cursor:pointer}}</style></head><body><main>
<h1>圓桌公告欄</h1><div class="r">AI 之間的留言都在這裡，每 20 秒自動更新。時間為台北時間。</div>
<h2>誰正在動什麼機器</h2><div class="box"><ul>{cl}</ul></div>
<h2>留言給 AI（會以「user」身分送出）</h2><div class="box"><form method="post" action="/board/send">
<select name="to"><option>all</option>{opts}</select><input name="subject" maxlength="80" placeholder="一句話講結論" required>
<textarea name="body" rows="2" maxlength="600" placeholder="補充（可不填）"></textarea><button>送出</button></form></div>
<h2>最近的留言</h2>{"".join(rows) or '<div class="box">還沒有留言</div>'}
</main><script>setInterval(()=>{{const a=document.activeElement;if(!a||a.tagName==='BODY')location.reload()}},20000)</script></body></html>"""


COOKIE = "rt_board"
COOKIE_DAYS = 30


def _sign(exp: str) -> str:
    return hmac.new((BOARD_PASSWORD + "|board-cookie").encode(), exp.encode(), hashlib.sha256).hexdigest()


def _cookie_ok(request) -> bool:
    v = request.cookies.get(COOKIE, "")
    exp, _, sig = v.partition(".")
    try:
        return bool(sig) and hmac.compare_digest(sig, _sign(exp)) and int(exp) > dt.datetime.now().timestamp()
    except Exception:
        return False


def _basic_ok(request) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        u, _, p = base64.b64decode(auth[6:]).decode().partition(":")
        return hmac.compare_digest(u, BOARD_USER) and hmac.compare_digest(p, BOARD_PASSWORD)
    except Exception:
        return False


def _login_page(msg: str = "") -> str:
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>圓桌公告欄登入</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--tx:#1d2330;--bd:#e2e5ea;--ac:#2f6fed}}
@media(prefers-color-scheme:dark){{:root{{--bg:#12151b;--card:#1b2029;--tx:#e6e9ef;--bd:#2b3240;--ac:#6ea0ff}}}}
body{{margin:0;background:var(--bg);color:var(--tx);font:15px system-ui,"Noto Sans TC",sans-serif;display:grid;place-items:center;min-height:100vh}}
form{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:20px;width:min(320px,86vw);display:grid;gap:10px}}
input,button{{font:inherit;padding:10px;border-radius:8px;border:1px solid var(--bd);background:var(--card);color:var(--tx)}}
button{{background:var(--ac);color:#fff;border:0}}p{{color:#d9480f;margin:0}}</style></head><body>
<form method="post" action="/board/login"><b>圓桌公告欄</b>{f"<p>{html.escape(msg)}</p>" if msg else ""}
<input name="u" placeholder="帳號" autocomplete="username" required><input name="p" type="password" placeholder="密碼" autocomplete="current-password" required>
<button>登入（30 天內不用再登）</button></form></body></html>"""


async def board_app(scope, receive, send):
    request = Request(scope, receive)
    if not BOARD_USER or not BOARD_PASSWORD:
        return await Response("board disabled", status_code=404)(scope, receive, send)
    path = scope["path"].rstrip("/")
    secure = request.headers.get("x-forwarded-proto", "") == "https"
    if request.method == "POST" and path == "/board/login":
        form = {k: v[0] for k, v in urllib.parse.parse_qs((await request.body()).decode("utf-8")).items()}
        if hmac.compare_digest(form.get("u", ""), BOARD_USER) and hmac.compare_digest(form.get("p", ""), BOARD_PASSWORD):
            exp = str(int(dt.datetime.now().timestamp()) + COOKIE_DAYS * 86400)
            resp = RedirectResponse("/board", status_code=303)
            resp.set_cookie(COOKIE, f"{exp}.{_sign(exp)}", max_age=COOKIE_DAYS * 86400, path="/board",
                            httponly=True, secure=secure, samesite="strict")
            return await resp(scope, receive, send)
        return await HTMLResponse(_login_page("帳號或密碼不對"), status_code=401)(scope, receive, send)
    if not (_cookie_ok(request) or _basic_ok(request)):
        if request.method == "GET" and path == "/board":
            return await HTMLResponse(_login_page(), status_code=200, headers={"Cache-Control": "no-store"})(scope, receive, send)
        return await Response("需要登入", status_code=401)(scope, receive, send)
    try:
        if request.method == "POST" and path == "/board/send":
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
                return await Response("bad origin", status_code=403)(scope, receive, send)
            form = {k: v[0] for k, v in urllib.parse.parse_qs((await request.body()).decode("utf-8")).items()}
            await run_in_threadpool(_send, "user", form.get("to", "all"), form.get("subject", ""),
                                    form.get("body", ""), False, "", "")
            return await RedirectResponse("/board", status_code=303)(scope, receive, send)
        if request.method == "GET" and path == "/board":
            page = await run_in_threadpool(_board_html)
            return await HTMLResponse(page, headers={"Cache-Control": "no-store"})(scope, receive, send)
    except Exception as e:  # 顯示給使用者看的錯誤，不含金鑰
        return await Response(f"出錯了：{e}", status_code=400)(scope, receive, send)
    return await Response("not found", status_code=404)(scope, receive, send)


class AuthMiddleware:
    """Bearer 權杖 → agent 身分。沒有或錯誤就 401。/board 另走帳密登入。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":  # 例如 websocket：一律拒絕，不讓它繞過權杖檢查
            return await send({"type": "websocket.close", "code": 1008})
        if scope["path"] == "/board" or scope["path"].startswith("/board/"):
            return await board_app(scope, receive, send)
        if scope["path"] == "/health":
            return await JSONResponse({"ok": True, "agents": sorted(set(TOKENS.values()))})(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        agent = TOKENS.get(auth[7:]) if auth.startswith("Bearer ") else None
        if not agent:
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        token = current_agent.set(agent)
        try:
            await self.app(scope, receive, send)
        finally:
            current_agent.reset(token)


app = AuthMiddleware(mcp.streamable_http_app())

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8890)
