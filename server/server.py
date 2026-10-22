"""Mem0 圓桌記憶 MCP 服務。

規則寫死在這裡：user_id 固定、infer 固定 false、agent_id 由權杖決定（無法冒充）。
每個 AI 一把權杖，格式見 .env：MCP_TOKEN_<AGENT>=...
注意：「先查後寫」靠本程序內的鎖，只能跑單一程序（不要開多個 worker 或多個容器）；
stateless_http 必須維持 True，否則身分會跟著 session 走，可被別的權杖沿用。
"""
import asyncio
import base64
import contextvars
import contextlib
import time
import datetime as dt
import hashlib
import hmac
import html
import io
import json
import math
import os
import tarfile
import re
import secrets
import threading
import urllib.parse

import httpx
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import FileResponse, JSONResponse

MEM0_URL = os.environ.get("MEM0_URL", "http://mem0:8000").rstrip("/")
MEM0_KEY = os.environ["MEM0_API_KEY"]
USER_ID = "roundtable"      # 原則記憶層：決策、筆記、進度卡（memory_search 預設只查這層）
OPS_UID = "roundtable-ops"  # 交流層：留言、認領
OPS_TYPES = ("message", "claim")
TYPES_ADD = ("decision", "note")
STAGES = ("planning", "implementing", "verifying", "done")
TZ_HOURS = float(os.environ.get("TZ_OFFSET_HOURS", "8"))  # 所有顯示的時間都用這個時區（相對 UTC 幾小時）
LOCAL_TZ = dt.timezone(dt.timedelta(hours=TZ_HOURS))  # 留言時間戳、每日排程用的時區
CLAIM_TTL_HOURS = 24  # claim 超過這麼久沒更新就視為過期，可被接手
_write_lock = threading.Lock()  # 單一進程內讓「先查後寫」不被打斷
LIST_CAP = 10000  # Mem0 列表一次最多回這麼多筆（2026-10-16 mem0-api 的 ALL_MEMORIES_LIMIT 由 1000 調成 10000，兩邊要一致）；
                  # 不帶 top_k 只會拿到預設筆數，資料多了會默默漏看；滿了 _list_uid 會改依作者分批撈
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
TAG_RE = re.compile(r"^(\s*\[(狀態|進行中|決策|筆記|訊息)\]\s*)+")
BOARD_USER = os.environ.get("BOARD_USER", "")
BOARD_PASSWORD = os.environ.get("BOARD_PASSWORD", "")

RESERVED_AGENTS = ("user", "all")  # user＝公告欄上的使用者本人、all＝全體；給 AI 用會變成冒充使用者（結案、撤回任何人的留言）
AGENT_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")


def _load_tokens(env) -> dict:
    """MCP_TOKEN_<名字>=權杖 → {權杖: 名字}。保留名、格式不對、還是範例值（change-me）的跳過；
    兩個名字用同一把權杖時兩個都不收（不然誰拿到都分不清是誰）。"""
    out, dup = {}, set()
    for k, v in sorted(env.items()):
        if not k.startswith("MCP_TOKEN_") or not v.strip():
            continue
        name = k[len("MCP_TOKEN_"):].lower().replace("_", "-")
        if name in RESERVED_AGENTS or not AGENT_NAME_RE.fullmatch(name):
            print(f"[auth] 略過 {k}：名字是保留字或格式不對", flush=True)
            continue
        if "change-me" in v.lower():
            print(f"[auth] 略過 {k}：還是範例值，請用 ./install.sh 產生的隨機權杖", flush=True)
            continue
        if v in out:
            dup.add(v)
            print(f"[auth] {k} 跟 MCP_TOKEN_{out[v].upper().replace('-', '_')} 用同一把權杖，兩個都不收", flush=True)
            continue
        out[v] = name
    return {t: n for t, n in out.items() if t not in dup}


TOKENS = _load_tokens(os.environ)


def _agent_for(authorization: str):
    """Authorization 標頭 → agent 名字；逐把用 compare_digest 比，不用 dict 查表（比較不受時間差影響）。"""
    if not authorization.startswith("Bearer "):
        return None
    given = authorization[7:].encode()
    found = None
    for t, n in TOKENS.items():
        if hmac.compare_digest(given, t.encode()):
            found = n
    return found
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
        "updated": md.get("status_at") or m.get("updated_at") or m.get("created_at"),  # status_at：切乒乓開關前的進度時間
        "created": m.get("created_at"),
        "updated_by": md.get("updated_by"),
        "version": _version(m.get("memory")),  # memory_update／memory_delete 的 base_version
    }


def _list_uid(uid: str) -> list:
    """列出某一區（user_id）的全部記憶。Mem0 一次最多回 LIST_CAP 筆、沒有分頁：沒滿就是全部；
    滿了改成依作者（agent_id）分批撈，再用 /entities 的該區總數核對一筆不漏，對不上就報錯（寧可停也不默默漏看）。"""
    with _http() as c:
        r = c.get("/memories", params={"user_id": uid, "top_k": LIST_CAP})
        r.raise_for_status()
        items = _items(r.json())
        if len(items) < LIST_CAP:
            return items
        r = c.get("/entities")
        r.raise_for_status()
        ents = r.json()
        total = next((e.get("total_memories") for e in ents if e.get("type") == "user" and e.get("id") == uid), None)
        seen = {}
        for agent in sorted({e.get("id") for e in ents if e.get("type") == "agent" and e.get("id")}):
            r = c.get("/memories", params={"user_id": uid, "agent_id": agent, "top_k": LIST_CAP})
            r.raise_for_status()
            part = _items(r.json())
            if len(part) >= LIST_CAP:
                raise RuntimeError(f"{uid} 區裡 {agent} 一個作者就有 {LIST_CAP} 筆以上，列表會不完整，"
                                   "claim 與 status 檢查不可靠；請使用者先整理舊記憶")
            seen.update((m.get("id"), m) for m in part)
    if total is None or len(seen) != total:
        raise RuntimeError(f"{uid} 區超過 {LIST_CAP} 筆，分批撈到 {len(seen)} 筆但總數是 {total}，列表可能不完整，"
                           "claim 與 status 檢查不可靠；請使用者先整理舊記憶")
    return list(seen.values())


def _uid_for(type_: str) -> str:
    """這種記憶寫在哪一層：留言、認領寫交流層，其他寫原則記憶層。"""
    return OPS_UID if type_ in OPS_TYPES else USER_ID


def _list_all() -> list:
    """兩層合併的全部記憶（收件匣、公告欄、認領、memory_get 等都讀這個，不用管東西在哪一層）。
    先列原則記憶層再列交流層：搬舊資料（roundtable → roundtable-ops）途中，搬到一半的那則最多兩邊都列到，
    照編號去重，不會漏。"""
    seen = {}
    for m in _list_uid(USER_ID) + _list_uid(OPS_UID):
        seen.setdefault(m.get("id"), m)
    return list(seen.values())


def _match(m: dict, **want) -> bool:
    md = m.get("metadata") or {}
    return all(v is None or md.get(k) == v for k, v in want.items())


def _add(content: str, metadata: dict) -> dict:
    body = {
        "messages": [{"role": "user", "content": content}],
        "user_id": _uid_for(metadata.get("type")),
        "agent_id": current_agent.get(),
        "infer": False,
        "metadata": metadata,
    }
    with _http() as c:
        r = c.post("/memories", json=body)
        r.raise_for_status()
        return r.json()


def _norm(t: str) -> str:
    """target 比對用：小寫並去掉空白、dash、底線（vm101 與 VM-101 視為同一個）。"""
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


def _body(s: str, name: str, limit: int = 1000) -> str:
    """內文：去掉開頭自帶的 [狀態]／[筆記] 等標籤（工具會自己加），避免重複或冒充別種記憶。"""
    return _clean(TAG_RE.sub("", _clean(s, name, limit)), name, limit)


FOOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} 使用者補充：")  # 使用者在公告欄加的註腳，一行一條接在原文最後；只有伺服器能寫
FOOT_ONE_MAX = 500     # 一條註腳最多幾字
FOOT_TOTAL_MAX = 1000  # 一筆記憶的註腳加起來最多幾字（另外算，不佔原文的 1000 字）


def _foot_split(text: str) -> tuple:
    """把最後面連續的註腳行分出來：回傳（原文, [註腳行]）。"""
    lines = (text or "").rstrip().split("\n")
    i = len(lines)
    while i > 1 and FOOT_RE.match(lines[i - 1].strip()):  # 第一行一定是原文
        i -= 1
    return "\n".join(lines[:i]).rstrip(), [x.strip() for x in lines[i:]]


def _foot_strip(content: str) -> str:
    """AI 送來的內文：拿掉所有長得像註腳的行（註腳由伺服器保管，AI 不能新增、改寫或冒充使用者）。"""
    return "\n".join(x for x in (content or "").split("\n") if not FOOT_RE.match(x.strip()))


_feet_carry = contextvars.ContextVar("feet_carry", default=())  # 審核合併時，被併掉那幾筆的註腳要接到合併後那筆


def _proj(p: str) -> str:
    p = _clean(p, "project", 60).lower()
    if not PROJECT_RE.match(p):
        raise ValueError(f"project 只能用小寫英文、數字與 - _ .（例如 mem0、homelab-network），收到 {p!r}")
    return p


# 白板專案卡的「乒乓」開關，存在 status 記憶的 metadata；pingpong_flips 是切過幾次（白板「更新幾次」要扣掉）
PINGPONG_KEYS = ("pingpong", "pingpong_by", "pingpong_at", "pingpong_flips")


def _pingpong(md: dict) -> bool:
    """這個專案有沒有打開乒乓（使用者在公告欄白板右鍵切換）；沒設過就是關。"""
    return str(md.get("pingpong") or "").lower() in ("yes", "true", "1")


# 白板專案卡的優先序：1 最優先…4，5＝暫停；也存在 status 記憶的 metadata。
# 清除時寫空字串（不刪鍵）；priority_flips 是改過幾次，白板「更新幾次」跟乒乓開關一樣要扣掉。
PRIORITY_KEYS = ("priority", "priority_by", "priority_at", "priority_flips")
PAUSED = 5


def _priority(md: dict) -> int:
    """專案卡的優先序 1～5；沒設（或看不懂）回 0。"""
    v = str(md.get("priority") or "").strip()
    return int(v) if v in ("1", "2", "3", "4", "5") else 0


@mcp.tool()
def project_status(project: str) -> dict:
    """查某專案目前的進度（status）。開工前先呼叫。pingpong=true 表示使用者在白板打開了這個專案的「乒乓」。
    blocked_on_user=true 表示專案卡在等使用者（「現在」那行或第一個沒勾的小目標在等使用者決定、批准、回答），
    blocked_why 是那一句；乒乓遇到這種專案不碰，報告寫「卡在你的回答」。
    priority 是使用者在白板設的優先序（1 最優先…4；0＝沒設）；5＝暫停，paused=true，這時 pingpong 一律回 false（乒乓跳過）。"""
    p = _proj(project)
    raw = [m for m in _list_all() if _match(m, project=p, type="status")]
    found = [_fmt(m) for m in raw]
    md = (raw[0].get("metadata") or {}) if raw else {}
    blocked, why = _blocked(TAG_STRIP.sub("", raw[0].get("memory") or "")) if raw else (False, "")
    prio = _priority(md)
    return {"project": p, "status": found[0] if found else None, "count": len(found),
            "version": _version(found[0]["text"]) if found else "",
            "pingpong": _pingpong(md) and prio != PAUSED,
            "blocked_on_user": blocked, "blocked_why": why, "priority": prio, "paused": prio == PAUSED}


def _check_done(stage: str, text: str):
    """小目標沒勾完不准標完成。"""
    if stage != "done":
        return
    left = []
    for ln in text.splitlines():
        s = None if WB_FIELD.match(ln) else WB_STEP.match(ln)
        if s and s.group(1) in WB_NOT_DONE:
            left.append(s.group(2))
    if left:
        raise ValueError(f"還有 {len(left)} 個小目標沒勾（例如「{left[0][:40]}」），不能標完成；"
                         "請改 stage=verifying，或先把小目標勾完再標 done")


@mcp.tool()
def project_set_status(project: str, summary: str, stage: str = "", base_version: str = "") -> dict:
    """更新某專案唯一的 status（沒有就建立）。收工後呼叫。summary 寫結論與下一步，會整筆取代舊內容，仍有效的舊內容要保留進去。
    summary 開頭請先寫這幾行白話（公告欄白板照這個顯示給使用者看，不寫技術細節；每行都可省略，但請盡量寫）：
      名稱：給人看的中文名稱（例：弱點掃描）
      主管：誰負責、誰拍板
      做事：誰在做（例：Codex laptop）
      現在：一句話講卡在哪、在等誰（例：Codex 發起，等 Claude 回覆）
      - [x] 已完成的小目標（一行一個，照順序）
      - [ ] 還沒完成的小目標
    之後空一行，再寫技術細節與下一步。第幾天、更新幾次由伺服器自動算，不用寫。
    已有 status 時必須帶 base_version（project_status 回傳的 version），表示你看過最新內容才改；
    版本不符代表別人剛改過，會拒絕並回傳最新內容，請重讀後再改。stage 不填就沿用原本的階段。
    標完成（stage=done，或不填而原本就是 done）時小目標要全部勾完；還有「- [ ]」會被拒絕，請改 verifying 或先勾完。
    使用者在白板設的乒乓開關、優先序會原樣留著，不用帶。"""
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
            old_md = existing[0].get("metadata") or {}
            md = {"project": p, "type": "status", "stage": stage or old_md.get("stage") or "implementing", "updated_by": me}
            _check_done(md["stage"], text)
            md.update({k: old_md[k] for k in PINGPONG_KEYS + PRIORITY_KEYS if k in old_md})  # 使用者在白板切的乒乓開關、優先序不因更新進度被洗掉
            with _http() as c:
                r = c.put(f"/memories/{existing[0]['id']}", json={"text": text, "metadata": md})
                r.raise_for_status()
            return {"updated": existing[0]["id"], "by": me, "version": _version(text)}
        md = {"project": p, "type": "status", "stage": stage or "implementing", "updated_by": me}
        _check_done(md["stage"], text)
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
    """開工標記：宣告你正在動某個目標。target 寫主機或服務名（例如 vm101、mem0-mcp），不要寫工作名稱。
    同一個 target 只要有別的 AI 的有效 claim 就拒絕，不分專案；自己已有的 claim 會被更新（續期）。
    別人的 claim 超過 24 小時沒更新視為過期，可接手，但回傳會附 warning，請告訴使用者。"""
    p, t = _proj(project), _clean(target, "target", 80).lower()
    me = current_agent.get()
    text = "[進行中] " + _body(summary, "summary")
    md = {"project": p, "type": "claim", "target": t}
    with _write_lock:
        mine, stale = None, []
        for m in _claims(t):
            if m.get("agent_id") in _names(me):  # 舊名開的 claim 也算自己的
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
        mine = [m for m in _claims(t) if m.get("agent_id") in _names(current_agent.get())]
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
    body = _body(content, "content")
    if any(FOOT_RE.match(x.strip()) for x in body.split("\n")):
        raise ValueError("「YYYY-MM-DD 使用者補充：」只能由使用者在公告欄加，內文請換個寫法")
    if not re.match(r"\d{4}-\d{2}-\d{2}", body):  # 每一則開頭都要有時間戳（當地時間），已自帶日期的不重複加
        body = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M ") + body
    res = _add(tag + body, {"project": _proj(project), "type": type})
    return {"by": current_agent.get(), "result": res}


def _group(name: str) -> str:
    return (name or "").split("-")[0]


def _groups() -> set:
    """有「前綴-裝置」身分的前綴就是群組：codex＝所有 codex-*、hermes＝hermes 與 hermes-*、
    claude＝所有 claude-*（別台 Claude 上線會自動加入）。"""
    return {_group(n) for n in TOKENS.values() if "-" in n}


# 身分改名的新舊名對應：同一組裡的名字視為同一個身分。哪個名字目前在 .env 有權杖，哪個就是正式名，其他當舊名。
# 用 MCP_ALIASES 設定，例如 "claude,claude-laptop;codex,codex-desk"（分號隔組、逗號隔名）。只在比對時換算、不改寫舊資料。
ALIAS_SETS = [{x.strip().lower() for x in g.split(",") if x.strip()}
              for g in os.environ.get("MCP_ALIASES", "").split(";") if g.strip()]


def _names(who: str) -> set:
    """who 的所有名字（正式名＋舊名）。"""
    for s in ALIAS_SETS:
        if who in s:
            return set(s)
    return {who}


def _canon(name: str) -> str:
    """舊名換成正式名（'舊名:串名' 也換）；不在對應表裡的照原樣。"""
    base, sep, label = (name or "").partition(":")
    live = set(TOKENS.values())
    for s in ALIAS_SETS:
        if base in s and base not in live:
            base = next((n for n in sorted(s) if n in live), base)
            break
    return base + sep + label


def _mine(me: str) -> set:
    """寫給我的收件人名稱：我自己（含舊名）、all、以及我所屬的群組（例如 codex）。"""
    return _names(me) | {"all"} | ({_group(me)} if _group(me) in _groups() else set())


def _recipients() -> set:
    return set(TOKENS.values()) | _groups() | {"all", "user"}


# 一則寄給好幾位：to 用逗號隔開，metadata.to 也存逗號字串；只有一位時跟以前一模一樣。
TO_MAX = 8  # 一則最多寄幾位


def _tos(to) -> list:
    """收件人欄拆成一位一位（只有一位就是 [to]）。"""
    return [x for x in (y.strip() for y in str(to or "").split(",")) if x] or [str(to or "").strip()]


def _members(to: str) -> set:
    """寄到 to 會收到的 AI 身分（all＝全部；群組＝該組所有成員；身分或舊名＝那個身分；好幾位就是聯集）。"""
    parts = _tos(to)
    if len(parts) > 1:
        return set().union(*(_members(x) for x in parts))
    agents = set(TOKENS.values())
    if to == "all":
        return agents
    if to in _groups():
        return {a for a in agents if _group(a) == to}
    return {_canon(to)}


def _readers(m: dict) -> list:
    return [x for x in ((m.get("metadata") or {}).get("read_by") or "").split(",") if x]


# 結案：寄件人（同身分任何一串）或使用者把留言標成已完成／已取代／撤回／有結論。
# 只多三個欄位 closed_by／closed_at／closed_reason，不改已讀的意思；closed_by 空字串＝沒結案（取消結案也是寫空字串）。
ACTIVE_DAYS = 7  # 徵求意見只等最近這麼多天有動靜的身分回覆；從沒連線的身分顯示「未連線」，不擋結案


def _closed(m: dict) -> dict | None:
    md = m.get("metadata") or {}
    if not md.get("closed_by"):
        return None
    out = {"by": md.get("closed_by"), "at": md.get("closed_at") or "", "reason": md.get("closed_reason") or ""}
    if md.get("closed_kind") == "recall":  # 撤回只多這兩欄，一般結案的輸出不變
        out.update(kind="recall", replies=int(md.get("recall_replies") or 0))
    return out


def _recalled(m: dict) -> bool:
    return (m.get("metadata") or {}).get("closed_kind") == "recall" and bool(_closed(m))


def _is_test(m: dict) -> bool:
    """上線測試寄的信（project=inbox-test…）：公告欄不顯示、不算待辦；資料照留（留言不刪）。"""
    return ((m.get("metadata") or {}).get("project") or "").startswith("inbox-test")


def _waiting_on(to: str) -> str:
    """待辦算在誰頭上：user、all，或收件身分的前綴（claude／codex／hermes）。"""
    base = _sub(to or "")[0]
    return base if base in ("user", "all") else _group(_canon(base))


def _handled(to: str, readers: list) -> bool:
    """多位收件人的信：這一位那邊有沒有人按過已讀（群組任何一位、身分的任何一串都算；寄給某一串的，那一串或
    不帶串名的整個身分 ACK 才算）。寄給使用者的 AI 不會讀，一律算還沒處理。"""
    if _sub(to)[0] == "user":
        return False
    want = _members(to)
    bases = {_sub(x)[0] for x in want}
    for r in readers:
        rc = _canon(r)
        b, lab = _sub(rc)
        if rc in want or b in want or (not lab and b in bases):
            return True
    return False


def _waits(m: dict) -> list:
    """待辦算在誰頭上（見 _waiting_on）。多位收件人時列還沒按已讀的那幾位，都按過就列全部。"""
    md = m.get("metadata") or {}
    parts = _tos(md.get("to"))
    if len(parts) == 1:
        return [_waiting_on(md.get("to"))]
    readers = _readers(m)
    out = []
    for x in [x for x in parts if not _handled(x, readers)] or parts:
        w = _waiting_on(x)
        if w not in out:
            out.append(w)
    return out


def _is_todo(m: dict) -> bool:
    """待辦＝要對方動手、還沒結案、對方還沒按已讀。寄給使用者的信 AI 不會讀，結案（使用者按「看過了」）前都算等使用者。
    寄給好幾位的：還有任何一位沒按已讀（或其中有使用者）就算待辦。"""
    md = m.get("metadata") or {}
    if md.get("type") != "message" or md.get("needs_action") != "yes" or _closed(m) or _is_test(m):
        return False
    parts = _tos(md.get("to"))
    if len(parts) > 1:
        readers = _readers(m)
        return any(not _handled(x, readers) for x in parts)
    return md.get("to") == "user" or not _readers(m)


def _created(m: dict):
    try:
        d = dt.datetime.fromisoformat(str(m.get("created_at")).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def _active(items: list) -> set:
    """最近 ACTIVE_DAYS 天內寫過記憶、寄過信或按過已讀的身分（舊名換成正式名）。
    已讀沒有時間戳，只看最近建立的留言上的已讀者，寧可少算也不要把沒連線的算成活躍。"""
    cut = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=ACTIVE_DAYS)
    out = set()
    for m in items:
        d = _created(m)
        if d is None or d < cut:
            continue
        md = m.get("metadata") or {}
        for n in [m.get("agent_id"), md.get("from")] + _readers(m):
            if n:
                out.add(_canon(_sub(n)[0]))
    return out


def _close(actor: str, message_id: str, reason: str, undo: bool = False) -> dict:
    """actor 是「身分」或「身分:串名」或 user。寄件人（比對身分，不比串名）與使用者可以結案或取消結案。"""
    with _write_lock:
        m = _find_message(message_id)
        md = dict(m.get("metadata") or {})
        if actor != "user" and _canon(_sub(md.get("from"))[0]) != _canon(_sub(actor)[0]):
            raise ValueError("只有寄件人（同身分的任何一串）或使用者能結案；收件人處理完請用 message_ack")
        if _recalled(m):  # 結案或取消結案都會蓋掉撤回紀錄
            raise ValueError("這則已撤回；要恢復請用 message_recall(undo=true)（公告欄按「取消撤回」）")
        if undo:
            md.update(closed_by="", closed_at="", closed_reason="")
        else:
            md.update(closed_by=actor, closed_reason=_clean(reason, "reason", 200),
                      closed_at=dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"))
        with _http() as c:
            c.put(f"/memories/{message_id}", json={"text": m.get("memory") or "", "metadata": md}).raise_for_status()
        readback = _closed(_find_message(message_id))
        if (readback or {}).get("by", "") != md["closed_by"]:
            raise RuntimeError("結案尚未讀回一致，請重新整理確認")
        return {"id": message_id, "closed": readback}


# 撤回：這封作廢（寄錯、不該寄），收件者不用看也不用做。跟結案共用 closed_* 三欄，另記
# closed_kind=recall；撤回當下已讀過的人記在 recall_notify，他們的收件匣會再列一次（標撤回），ACK 後記進 recall_seen 就不再列。
# 撤回前已有幾則回覆記在 recall_replies，回覆本身不動。不限時間；軟刪，原文照留。
def _recall(actor: str, message_id: str, reason: str, undo: bool = False) -> dict:
    """actor 是「身分:串名」、「身分」或 user。寄件人要同身分，原信記了寄件串名就要同一串；使用者可撤回任何一則。"""
    with _write_lock:
        items = _list_all()
        m = _find_message(message_id, items)
        md = dict(m.get("metadata") or {})
        if actor != "user":
            base, label = _sub(actor)
            if _canon(_sub(md.get("from"))[0]) != _canon(base):
                raise ValueError("只有寄件人或使用者能撤回；收件人不想做請回信說明，看完用 message_ack")
            fs = md.get("from_session") or ""
            if fs and label != fs:
                raise ValueError(f"這則是 {_canon(md.get('from'))}:{fs} 那一串寄的，只有那一串（session={fs}）或使用者能撤回")
        if undo:
            if not _recalled(m):
                raise ValueError("這則沒有撤回")
            md.update(closed_by="", closed_at="", closed_reason="", closed_kind="",
                      recall_notify="", recall_seen="", recall_replies="")
        else:
            if _recalled(m):
                raise ValueError("這則已經撤回了")
            replies = sum(1 for x in items if (x.get("metadata") or {}).get("type") == "message"
                          and (x.get("metadata") or {}).get("reply_to") == m.get("id") and not _recalled(x))
            md.update(closed_by=actor, closed_reason=_clean(reason, "reason", 200), closed_kind="recall",
                      closed_at=dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M"),
                      recall_notify=",".join(sorted({_canon(r) for r in _readers(m)})), recall_seen="",
                      recall_replies=str(replies))
        with _http() as c:
            c.put(f"/memories/{message_id}", json={"text": m.get("memory") or "", "metadata": md}).raise_for_status()
        back = _find_message(message_id)
        if _recalled(back) == bool(undo):
            raise RuntimeError("撤回尚未讀回一致，請重新整理確認")
        notified = [x for x in (md.get("recall_notify") or "").split(",") if x]
        return {"id": message_id, "closed": _closed(back), "notified": notified}


def _hit(names: set, me: str, tag: str) -> bool:
    """名單裡有沒有「我」：帶串名時看本串或整個身分；不帶時任何一串都算（跟已讀的算法一樣）。"""
    if tag:
        return tag in names or me in names
    return me in names or any(r.startswith(me + ":") for r in names)


def _recall_notice(m: dict, me: str, tag: str) -> bool:
    """撤回前已經讀過、還沒看過撤回通知的收件者，收件匣要再列一次。"""
    if not _recalled(m):
        return False
    md = m.get("metadata") or {}
    notify = {_canon(x) for x in (md.get("recall_notify") or "").split(",") if x}
    seen = {_canon(x) for x in (md.get("recall_seen") or "").split(",") if x}
    return _hit(notify, me, tag) and not _hit(seen, me, tag)


# 同一個身分（例如 claude）底下可以有好幾個對話串，各自用 session（串名）分開已讀與收信範圍。
# 收件人寫 "claude-laptop:mem0-deploy" 就是直接寄給那一串（舊寫法 "claude:mem0-deploy" 也收）；已讀記成 read_by 裡的 "claude-laptop:mem0-deploy"。
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")


def _label(session: str) -> str:
    s = (session or "").strip().lower()
    if s and not LABEL_RE.match(s):
        raise ValueError("session 是對話串名稱，只能用小寫英數與 . _ -（例如 mem0-deploy），40 字內")
    return s


def _sub(to: str) -> tuple:
    """'claude:mem0-deploy' → ('claude', 'mem0-deploy')；沒有冒號就是 (to, '')。"""
    base, _, label = (to or "").partition(":")
    return base, label


def _for_me(to: str, me: str) -> bool:
    parts = _tos(to)
    if len(parts) > 1:  # 寄給好幾位：其中一位是我就算
        return any(_for_me(x, me) for x in parts)
    base, label = _sub(to)
    return to in _mine(me) or (bool(label) and base in _names(me))  # 串地址要寫身分（或舊名），不認群組


def _proj_filter(projects: str):
    """'mem0,evalscope' 只收這些專案；'*' 全收；'-x' 排除 x（領航員用 '*,-mem0,…' 收沒人訂閱的專案）。"""
    inc, exc, every = set(), set(), False
    for p in (projects or "").split(","):
        p = p.strip().lower()
        if p == "*":
            every = True
        elif p.startswith("-") and p[1:]:
            exc.add(p[1:])
        elif p:
            inc.add(p)
    return lambda proj: (every or proj in inc) and proj not in exc


UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/uploads")
UPLOAD_MAX = 8 * 1024 * 1024
UPLOAD_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
FILE_ID_RE = re.compile(r"^[0-9a-f]{24}\.(png|jpg|webp|gif)$")


def _file_path(fid: str) -> str:
    fid = (fid or "").strip()
    if not FILE_ID_RE.match(fid):
        raise ValueError(f"附件編號格式不對：{fid!r}")
    p = os.path.join(UPLOAD_DIR, fid)
    if not os.path.isfile(p):
        raise ValueError(f"找不到附件 {fid}")
    return p


def _save_upload(data: bytes, ctype: str) -> str:
    ext = UPLOAD_TYPES.get((ctype or "").split(";")[0].strip().lower())
    if not ext:
        raise ValueError("只收 PNG、JPG、WebP、GIF 圖片")
    if not data or len(data) > UPLOAD_MAX:
        raise ValueError("圖片是空的或超過 8 MB")
    fid = f"{hashlib.sha256(data).hexdigest()[:24]}.{ext}"
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    p = os.path.join(UPLOAD_DIR, fid)
    if not os.path.exists(p):
        with open(p + ".tmp", "wb") as f:
            f.write(data)
        os.replace(p + ".tmp", p)
    return fid


def _atts(m: dict) -> list:
    return [x for x in ((m.get("metadata") or {}).get("attachments") or "").split(",") if x]


def _find_message(message_id: str, items=None) -> dict:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", message_id or ""):
        raise ValueError("留言編號格式不對")
    for m in _list_all() if items is None else items:
        if m.get("id") == message_id and (m.get("metadata") or {}).get("type") == "message":
            return m
    raise ValueError("找不到原留言")


def _votes(m: dict, actor: str = "user") -> dict:
    votes = json.loads((m.get("metadata") or {}).get("votes") or "{}")
    return {"up": sum(v == 1 for v in votes.values()), "down": sum(v == -1 for v in votes.values()),
            "mine": next((votes[n] for n in sorted(_names(actor)) if n in votes), 0)}


def _vote(actor: str, message_id: str, vote: int) -> dict:
    if not actor or type(vote) is not int or vote not in (-1, 0, 1):
        raise ValueError("vote 只收 -1（倒讚）、0（撤回）、1（讚）")
    with _write_lock:
        m = _find_message(message_id)
        md = dict(m.get("metadata") or {})
        votes = json.loads(md.get("votes") or "{}")
        for old in _names(actor) - {actor}:  # 舊名投的票算同一人，換成正式名，不重複計
            votes.pop(old, None)
        if vote:
            votes[actor] = vote
        else:
            votes.pop(actor, None)
        md["votes"] = json.dumps(votes, sort_keys=True)
        with _http() as c:
            c.put(f"/memories/{message_id}", json={"text": m.get("memory") or "", "metadata": md}).raise_for_status()
        readback = _find_message(message_id)
        if json.loads((readback.get("metadata") or {}).get("votes") or "{}") != votes:
            raise RuntimeError("表決尚未讀回一致，請重新整理確認")
        return {"id": message_id, **_votes(readback, actor)}


@mcp.tool()
def message_vote(message_id: str, vote: int) -> dict:
    """為留言按讚(1)、倒讚(-1)或撤回(0)。每個 MCP 身分每則只留一票；不能替別人投票。表決不是使用者批准。"""
    return _vote(current_agent.get(), message_id, vote)


def _expected(to: str, frm: str) -> set:
    """徵求意見時應該回覆的人：單一對象就是他；群組是該組所有成員；all 是所有 AI（都扣掉發問者自己）。
    名字一律換成正式名（舊名 claude 的回覆算 claude-laptop 回覆了）。"""
    return _members(to) - {_canon(frm)}


def _opinions(m: dict, items: list, active: set | None = None) -> dict | None:
    """responded＝已回；pending＝活躍但還沒回；offline＝該回但最近沒連線的身分（不算欠回覆，有才給）。"""
    md = m.get("metadata") or {}
    if md.get("ask_opinion") != "yes":
        return None
    want = _expected(md.get("to") or "", md.get("from") or "")
    got = set()
    for x in items:
        xm = x.get("metadata") or {}
        if xm.get("type") == "message" and xm.get("reply_to") == m.get("id") and not _recalled(x):  # 撤回的回覆不算回了
            got.add(_canon(xm.get("from")))
            if xm.get("from_session"):  # 某一串回覆，也算「claude-laptop:那一串」回覆了
                got.add(_canon(f'{xm.get("from")}:{xm.get("from_session")}'))
    if active is None:
        active = _active(items)
    left = want - got
    off = {x for x in left if _canon(_sub(x)[0]) not in active}  # 寄給某一串（身分:串名）時看那個身分活不活躍
    out = {"responded": sorted(want & got), "pending": sorted(left - off)}
    if off:
        out["offline"] = sorted(off)
    return out


def _send(frm: str, to: str, subject: str, body: str, needs_action: bool, project: str, ref: str,
           attachments: str = "", reply_to: str = "", ask_opinion: bool = False, from_session: str = "") -> dict:
    atts = [a.strip() for a in (attachments or "").split(",") if a.strip()][:6]
    for a in atts:
        _file_path(a)
    known = set(TOKENS.values()) | {n for s in ALIAS_SETS for n in s}  # 串地址的身分可以寫正式名或舊名
    fs = _label(from_session)
    raw = _tos((to or "").lower())
    parts, seen = [], set()
    for t in raw:  # 好幾位時每一位都照一位的規則檢查，同一位（含舊名）只留第一次
        base, sub = _sub(t)
        if t not in _recipients() and not (sub and base in known and LABEL_RE.match(sub)):
            raise ValueError(f"to 只能是 {sorted(_recipients())}，或「身分:串名」直接寄給某個對話串（例如 claude-laptop:mem0-deploy）"
                             + ("；好幾位用逗號隔開" if len(raw) > 1 else ""))
        # 有串名的串可以寄給自己身分或群組（依專案分給其他串）；沒串名時寄給只有自己的地址就是寄給自己
        if (not fs and (t == frm or (not sub and _members(t) <= _names(frm)))) or (fs and _canon(t) == f"{_canon(frm)}:{fs}"):
            raise ValueError("不能寄給自己" + (f"（{t}）" if len(raw) > 1 else ""))
        if sub and not fs and _canon(base) == _canon(frm):  # 沒串名的串間信在公告欄會看起來像自己寄自己
            raise ValueError("寄給同身分的其他串（身分:串名）時請帶 session=你的串名，公告欄才看得出是哪一串寄給哪一串")
        if _canon(t) not in seen:
            seen.add(_canon(t))
            parts.append(t)
    if len(parts) > TO_MAX:
        raise ValueError(f"一則最多寄給 {TO_MAX} 位")
    t = ",".join(parts)
    subj = _body(subject, "subject")
    if len(subj) > 80:
        raise ValueError("subject 太長（上限 80 字），細節請放 body 或 ref 檔案")
    b = _body(body, "body") if (body or "").strip() else ""
    text = f"[訊息] {frm}{':' + fs if fs else ''}→{t}｜{subj}" + (f"｜{b}" if b else "")
    md = {"type": "message", "to": t, "from": frm, "subject": subj, "body": b,
          "needs_action": "yes" if needs_action else "no", "read_by": "",
          "project": _proj(project) if (project or "").strip() else "general", "ref": (ref or "").strip()[:200],
          "attachments": ",".join(atts)}
    if fs:
        md["from_session"] = fs
    if atts:
        text += f"｜附件 {len(atts)} 張（用 attachment_get 看）"
    if reply_to:
        try:
            parent = _find_message(reply_to)
        except ValueError:  # 回覆已封存的舊留言：原編號在封存區找得到就照樣接上
            rid = (reply_to or "").strip().lower()
            if not (re.fullmatch(r"[0-9a-f-]{36}", rid) and _archived_copy(rid)):
                raise
            parent = {"id": rid}
        md["reply_to"] = parent["id"]
    if ask_opinion:
        md["ask_opinion"] = "yes"
        text += "｜徵求意見：每位收件者都要用 reply_to 回覆這則"
    body_json = {"messages": [{"role": "user", "content": text}], "user_id": OPS_UID,
                 "agent_id": frm, "infer": False, "metadata": md}
    with _http() as c:
        r = c.post("/memories", json=body_json)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def message_send(to: str, subject: str, body: str = "", needs_action: bool = False,
                  project: str = "", ref: str = "", reply_to: str = "", ask_opinion: bool = False,
                  session: str = "", closes: str = "") -> dict:
    """留言給另一個 AI、一整組（codex＝所有 codex-*）、全體（all）或使用者（user）；好幾位用逗號隔開（例如
    "claude-laptop:mem0-deploy,codex-laptop"，最多 8 位），只寄出一則。subject 一句話講結論（80 字內）；
    細節放 body（簡短）或寫成檔案後把路徑放 ref，不要貼長篇內容。needs_action=true 表示對方需要動手。
    ask_opinion=true 表示徵求意見：每位收件者都要用 reply_to 回覆這則，沒回覆前 inbox 會一直列出（ACK 不算回覆）。
    回覆徵求意見時，用 message_send(to=原發問者, reply_to=那則的 id)。使用者可以在公告欄網頁看到全部留言。
    要寄給某個 Claude 對話串，to 寫 "身分:串名"（例如 claude-laptop:mem0-deploy，舊寫法 claude:串名 也收）；寄 to="claude"（所有 Claude）會依 project 分給訂閱該專案的串
    （Claude 的串帶 session 時也可以寄 to="claude"，交給負責該專案的其他串）。
    你自己是有串名的對話串時，帶 session=你的串名，對方回覆才會回到你這串；寄給同身分的其他串一定要帶。
    closes="id1,id2"：這則是追問／合併／更正，或是你徵求意見的結論時，順手把你自己先前寄的那幾則結案（只能結自己寄的）。
    寄錯、整封作廢（對方不用看也不用做）請用 message_recall 撤回，不要再補寄一封「不用回了」。"""
    me = current_agent.get()
    old = [x.strip() for x in (closes or "").split(",") if x.strip()][:10]
    for mid in old:  # 先檢查再寄，避免寄出去了才發現不能結
        om = _find_message(mid)
        if _canon(_sub((om.get("metadata") or {}).get("from"))[0]) != _canon(me):
            raise ValueError(f"closes 只能結你自己寄的留言，{mid} 不是")
        if _recalled(om):
            raise ValueError(f"{mid} 已撤回，不用再結案；請把它從 closes 拿掉")
    res = _send(me, to, subject, body, needs_action, project, ref, reply_to=reply_to,
                ask_opinion=ask_opinion, from_session=session)
    by = me + (f":{_label(session)}" if (session or "").strip() else "")
    out = {"sent": True, "by": by, "result": res}
    if old:
        new_id = next((r.get("id") for r in _items(res) if r.get("id")), "")
        out["closed"] = [_close(by, mid, f"已由 {new_id or '新留言'} 取代：{_body(subject, 'subject')[:60]}")["id"] for mid in old]
    return out


@mcp.tool()
def message_close(message_id: str, reason: str, session: str = "", undo: bool = False) -> dict:
    """把你自己寄出的留言結案：事情已完成、已被新留言取代，或徵求意見已有結論（原信仍是有效紀錄）。reason 寫一句為什麼
    （例如「IP 搬遷已完成」「已由 <id> 取代」）。只有寄件人（同身分的任何一串）能結案，使用者可在公告欄結案；
    收件人處理完請用 message_ack，不要用這個。結案後公告欄待辦不再算它、徵求意見顯示已結案，收件人的 inbox 預設不再列出。
    undo=true 取消結案。Claude 的對話串請帶 session=串名，紀錄才看得出是哪一串結的。寄錯要作廢請用 message_recall。"""
    me = current_agent.get()
    who = me + (f":{_label(session)}" if (session or "").strip() else "")
    return _close(who, message_id, reason, undo=undo)


@mcp.tool()
def message_recall(message_id: str, reason: str, session: str = "", undo: bool = False) -> dict:
    """撤回你自己寄出的留言：寄錯對象、內容錯了或不該寄，整封作廢，收件者不用看也不用做（跟結案不同：結案是事情做完，
    原信仍有效）。reason 寫一句為什麼（例如「寄錯對象」）。只有寄件人能撤回；原信帶了 session 就要同一串撤回；
    使用者可在公告欄撤回任何一則。不限時間，原文照留（公告欄摺起來顯示「已撤回」）。
    撤回後：還沒讀的收件者不會再看到；已經讀過的，他的收件匣會再列一次（標 closed.kind=recall）提醒他停手。
    已經有人回覆也能撤回，回覆不動。內容要更正就撤回後重寄。undo=true 取消撤回。Claude 的對話串請帶 session=串名。"""
    me = current_agent.get()
    who = me + (f":{_label(session)}" if (session or "").strip() else "")
    return _recall(who, message_id, reason, undo=undo)


@mcp.tool()
def message_ask_opinion(message_id: str) -> dict:
    """把你自己已經送出的一則留言改成「徵求意見」（每位收件者都要回覆）。只有原發問者能改。"""
    me = current_agent.get()
    with _write_lock:
        items = _list_all()
        m = _find_message(message_id, items)
        md = dict(m.get("metadata") or {})
        if _canon(md.get("from")) != _canon(me):
            raise ValueError("只有原發問者能把留言改成徵求意見")
        if md.get("ask_opinion") != "yes":
            md["ask_opinion"] = "yes"
            with _http() as c:
                c.put(f"/memories/{message_id}", json={"text": m.get("memory") or "", "metadata": md}).raise_for_status()
            items = _list_all()
            m = _find_message(message_id, items)
    return {"id": message_id, **(_opinions(m, items) or {})}


@mcp.tool()
def inbox(include_read: bool = False, session: str = "", projects: str = "") -> list:
    """看寫給你（或全體）的留言，預設只列未讀。每次開工先呼叫；處理完用 message_ack 標記已讀。
    留言若是回覆別則（reply_to 有值），reply_to_info 會附上原留言的主旨、內文與附件編號；
    使用者常把截圖附在被引用的原留言上，附件用 attachment_get 看。
    Claude 的對話串請帶 session=串名、projects=訂閱的專案（逗號分隔；hook 提示會告訴你該帶什麼），
    就只會看到「直接寄給本串（claude-laptop:串名，舊寫法 claude:串名 也算）」、「回覆本串留言」與「寄給 claude/all
    且專案在訂閱內」的信，已讀也只算本串的；ACK 時記得一樣帶 session。不帶參數＝舊行為（整個身分共用一個收件匣）。
    改名前用舊名（claude）寄的信、標的已讀，都算同一個身分的。
    寄件人已結案（closed 有值：已完成、被取代）的信預設不列，include_read=true 才列。
    寄件人撤回（closed.kind=recall，作廢）的信：沒讀過的不列；你之前讀過的會再列一次，表示那封作廢、照它做的請停手，
    看完 message_ack 就不再列。超過 14 天的舊留言會自動封存、不再列出（memory_search 帶 archived=true 找）。"""
    me = _canon(current_agent.get())
    label = _label(session)
    tag = f"{me}:{label}" if label else ""
    want = _proj_filter(projects) if (label or (projects or "").strip()) else (lambda proj: True)
    out = []
    items = _list_all()
    by_id = {m.get("id"): m for m in items}
    old = {}

    def parent_of(rid):  # 原信已經封存（回覆是封存後才寄的）就去封存區找，同一次收信只找一次
        if not rid or rid in by_id:
            return by_id.get(rid or "")
        if rid not in old:
            try:
                a = _archived_copy(rid) if re.fullmatch(r"[0-9a-f-]{36}", rid) else None
            except Exception:
                a = None
            old[rid] = _unarchived(a) if a else None
        return old[rid]

    active = _active(items) | {me}  # 正在收信的自己一定算活躍，久沒上線回來也看得到自己欠的回覆
    for m in items:
        md = m.get("metadata") or {}
        to = md.get("to") or ""
        if md.get("type") != "message" or not _for_me(to, me):
            continue
        readers = [_canon(r) for r in _readers(m)]  # 舊名的已讀換成正式名再比
        sender = _canon(md.get("from"))
        if label:
            subs = [_sub(x)[1] for x in _tos(to) if _for_me(x, me)]  # 寄給好幾位時只看寫給我的那幾位
            # 直接寄給某一串：只有那一串看得到
            direct = label in subs and not (sender == me and md.get("from_session") == label)
            if not direct:
                if "" not in subs or (sender == me and (md.get("from_session") or label) == label):  # 本串（或舊版）自己寄的
                    continue
                pm = (parent_of(md.get("reply_to")) or {}).get("metadata") or {}
                my_thread = _canon(pm.get("from")) == me and pm.get("from_session") == label  # 回覆本串發的留言
                if not my_thread and not want(md.get("project") or "general"):
                    continue
            # 改版前用整個身分 ACK 過的舊留言仍算已讀，避免各串重新冒出一堆舊信
            read = tag in readers or me in readers
        else:
            if sender == me or not want(md.get("project") or "general"):
                continue
            # 舊行為：整個身分共用，任何一串 ACK 過就算讀過
            read = me in readers or any(r.startswith(me + ":") for r in readers)
        op = _opinions(m, items, active)
        closed = _closed(m)
        owe = bool(op) and not closed and (me in op["pending"] or (bool(tag) and tag in op["pending"]))  # 徵求意見還沒回：就算 ACK 過也繼續列出
        notice = _recall_notice(m, me, tag)  # 讀過才被撤回：再列一次叫他停手
        if (read or closed) and not include_read and not owe and not notice:
            continue
        info = None
        parent = parent_of(md.get("reply_to"))
        if parent is not None:
            pm = parent.get("metadata") or {}
            info = {"id": parent.get("id"), "from": pm.get("from"), "to": pm.get("to"), "subject": pm.get("subject"),
                    "body": (pm.get("body") or "")[:1500], "attachments": _atts(parent), "ref": pm.get("ref")}
            if _closed(parent):  # 回覆的那則已結案／撤回，也讓收件者知道
                info["closed"] = _closed(parent)
        out.append({"id": m.get("id"), "from": md.get("from"), "from_session": md.get("from_session") or "",
                    "to": md.get("to"), "subject": md.get("subject"),
                    "text": m.get("memory"), "needs_action": md.get("needs_action") == "yes",
                    "project": md.get("project"), "ref": md.get("ref"), "attachments": _atts(m),
                    "created": m.get("created_at"), "read": read, "reply_to": md.get("reply_to") or "",
                    "reply_to_info": info, "ask_opinion": bool(op), "you_owe_opinion": owe,
                    "opinion": op})
        if closed:  # 只在結案時多這一欄，沒結案的輸出跟改版前一樣
            out[-1]["closed"] = closed
    out.sort(key=lambda x: str(x["created"]))
    return out


@mcp.tool()
def message_ack(message_id: str, session: str = "") -> dict:
    """把一則寫給你（或全體）的留言標記為已讀。Claude 的對話串請帶 session=串名，只標本串已讀、不影響別串；
    不帶 session 是舊行為（整個身分一起標已讀，會讓其他串也看不到）。"""
    me = _canon(current_agent.get())
    label = _label(session)
    who = f"{me}:{label}" if label else me
    with _write_lock:
        for m in _list_all():
            if m.get("id") != message_id:
                continue
            md = dict(m.get("metadata") or {})
            if md.get("type") != "message" or not _for_me(md.get("to") or "", me):
                raise ValueError("這不是寫給你的留言")
            subs = [_sub(x)[1] for x in _tos(md.get("to")) if _for_me(x, me)]
            if label and label not in subs and "" not in subs:
                raise ValueError(f"這則是直接寄給 {md.get('to')} 那一串的，不要替別串 ACK")
            readers = _readers(m)
            changed = False
            if who not in {_canon(r) for r in readers}:  # 舊名已標過也算，不重複寫
                md["read_by"] = ",".join(readers + [who])
                changed = True
            if _recalled(m):  # 看過撤回通知：記進 recall_seen，收件匣就不再列
                seen = [x for x in (md.get("recall_seen") or "").split(",") if x]
                if who not in {_canon(x) for x in seen}:
                    md["recall_seen"] = ",".join(seen + [who])
                    changed = True
            if changed:
                with _http() as c:
                    c.put(f"/memories/{message_id}", json={"text": m.get("memory") or "", "metadata": md}).raise_for_status()
            return {"acked": message_id, "by": who}
    raise ValueError("找不到這則留言")


@mcp.tool()
def attachment_get(file_id: str) -> Image:
    """看留言附的圖片（例如使用者貼的截圖）。file_id 來自 inbox 回傳的 attachments。"""
    p = _file_path(file_id)
    with open(p, "rb") as f:
        return Image(data=f.read(), format=file_id.rsplit(".", 1)[1].replace("jpg", "jpeg"))


SKILLS_ROOT = os.environ.get("SKILLS_ROOT", "/skills-repo")
EMB_URL = os.environ.get("EMB_URL", "http://embed:8080").rstrip("/")
EMB_MODEL = os.environ.get("EMB_MODEL", "qwen3-embedding-0.6b")
_emb_cache: dict = {}


def _skill_dir(name: str) -> str:
    n = (name or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,80}", n):
        raise ValueError("技能名稱只能用小寫英文、數字與 . _ -")
    d = os.path.join(SKILLS_ROOT, "skills", n)
    if not os.path.isfile(os.path.join(d, "SKILL.md")):
        raise ValueError(f"共用技能庫沒有 {n}")
    return d


def _manifest() -> list:
    import json
    with open(os.path.join(SKILLS_ROOT, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)["skills"]


def _embed(texts: list) -> list:
    todo = [t for t in texts if t not in _emb_cache]
    if todo:
        r = httpx.post(EMB_URL + "/v1/embeddings", json={"model": EMB_MODEL, "input": todo}, timeout=60)
        r.raise_for_status()
        for t, d in zip(todo, r.json()["data"]):
            _emb_cache[t] = d["embedding"]
    return [_emb_cache[t] for t in texts]


@mcp.tool()
def skill_list(query: str = "", limit: int = 20) -> list:
    """列出圓桌共用技能庫（所有 AI 共用的自訂技能唯一正本）。
    新增或修改技能前，先用 query 描述你想做的技能，查有沒有相似的已存在（score 越高越像，>0.8 多半重複）。"""
    rows = _manifest()
    if not query.strip():
        return [{k: r[k] for k in ("name", "description", "sha256")} for r in rows][: max(1, min(limit, 200))]
    descs = [f"{r['name']}: {r['description']}" for r in rows]
    vecs = _embed(descs + [_clean(query, "query", 500)])
    q = vecs[-1]
    qn = sum(x * x for x in q) ** 0.5 or 1.0
    scored = []
    for r, v in zip(rows, vecs[:-1]):
        vn = sum(x * x for x in v) ** 0.5 or 1.0
        scored.append({"name": r["name"], "description": r["description"], "sha256": r["sha256"],
                       "score": round(sum(a * b for a, b in zip(q, v)) / (qn * vn), 3)})
    scored.sort(key=lambda x: -x["score"])
    return scored[: max(1, min(limit, 50))]


@mcp.tool()
def skill_get(name: str, file: str = "SKILL.md") -> dict:
    """讀共用技能庫裡某個技能的檔案（預設 SKILL.md）。要整包安裝請用 HTTPS GET /skills/<name>.tar.gz（同一把權杖）。"""
    d = _skill_dir(name)
    p = os.path.normpath(os.path.join(d, file))
    if not p.startswith(d + os.sep) or not os.path.isfile(p):
        raise ValueError(f"{name} 裡沒有 {file}")
    files = sorted(os.path.relpath(os.path.join(r, f), d) for r, _, fs in os.walk(d) for f in fs)
    with open(p, encoding="utf-8", errors="replace") as f:
        text = f.read()
    return {"name": name, "file": file, "files": files, "text": text[:60000], "truncated": len(text) > 60000}


PERSONA_UID = "personas"  # 人格另放一區，一般工具都碰不到


@mcp.tool()
def persona_get(name: str, kind: str = "soul") -> dict:
    """取得某個人格的備份（PERSONA_NAMES 設定的名字，另有 default）。
    只有使用者明確說「扮演某某」「用某某人格」時才可以呼叫，平常做事一律不要呼叫。
    kind：soul＝說話方式與個性；notes＝該人格自己的筆記。"""
    n, k = _clean(name, "name", 40).lower(), (kind or "soul").strip().lower()
    if k not in ("soul", "notes"):
        raise ValueError("kind 只能是 soul 或 notes")
    items = _list_uid(PERSONA_UID)
    pick =lambda who: [m for m in items if (m.get("metadata") or {}).get("persona") == who
                        and (m.get("metadata") or {}).get("kind") == k]
    parts = pick(n)
    if not parts and k == "notes":
        parts = pick("default")  # 人格沒有自己的筆記時用 default 的
    if not parts:
        names = sorted({(m.get("metadata") or {}).get("persona") for m in items} - {None})
        raise ValueError(f"找不到 {n} 的 {k}；有的人格：{names}")
    parts.sort(key=lambda m: int((m.get("metadata") or {}).get("part") or 0))
    md = parts[0].get("metadata") or {}
    return {"persona": n, "kind": k, "source": md.get("source"), "text": "\n\n".join(m.get("memory") or "" for m in parts)}


PERSONA_WRITERS = {x.strip().lower() for x in os.environ.get("PERSONA_WRITERS", "").split(",") if x.strip()}  # 只有這些身分能寫人格區
PERSONA_NAMES = {x.strip().lower() for x in os.environ.get("PERSONA_NAMES", "").split(",") if x.strip()}  # 可寫的人格名字；default 不自動同步
PERSONA_MAX_CHARS = 20000
PERSONA_CHUNK = 800
SECRET_PATTERNS = [
    ("私鑰區塊", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("sk- 類金鑰", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("GitHub 權杖", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("AWS 金鑰", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Bearer 權杖", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    ("密碼／金鑰賦值", re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*\S{6,}")),
    ("長十六進位字串", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
]


def _persona_chunks(text: str) -> list:
    """依空行分段後合併到約 PERSONA_CHUNK 字；過長的段落硬切。結果用 \\n\\n 接回即為標準形。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces = []
    for p in paras:
        while len(p) > PERSONA_CHUNK:
            pieces.append(p[:PERSONA_CHUNK])
            p = p[PERSONA_CHUNK:]
        pieces.append(p)
    out, cur = [], ""
    for p in pieces:
        if cur and len(cur) + 2 + len(p) > PERSONA_CHUNK:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return out


def _persona_parts(items: list, name: str, kind: str) -> list:
    return sorted([m for m in items if (m.get("metadata") or {}).get("persona") == name
                   and (m.get("metadata") or {}).get("kind") == kind],
                  key=lambda m: int((m.get("metadata") or {}).get("part") or 0))


@mcp.tool()
def persona_put(name: str, kind: str, text: str, source: str = "") -> dict:
    """（只有被授權的 Hermes 人格可用）更新某個人格的備份：name＝PERSONA_NAMES 裡的名字，kind＝soul 或 notes。
    text 是該檔完整內容（整份取代，不是追加）。寫前會檢查機密、內容沒變就不寫、寫後讀回比對。
    不收 USER.md、聊天紀錄、私人筆記。"""
    me = current_agent.get()
    if not (_names(me) & PERSONA_WRITERS):  # 改名後用舊名登記的權限照樣有效
        raise ValueError("你的身分沒有寫入人格區的權限")
    n, k = _clean(name, "name", 40).lower(), (kind or "").strip().lower()
    if n not in PERSONA_NAMES:
        raise ValueError(f"name 只能是 {sorted(PERSONA_NAMES)}")
    if k not in ("soul", "notes"):
        raise ValueError("kind 只能是 soul 或 notes")
    t = _clean(text, "text", PERSONA_MAX_CHARS)
    hits = [label for label, rx in SECRET_PATTERNS if rx.search(t)]
    if hits:
        raise ValueError(f"內容疑似含機密（{'、'.join(hits)}），已拒絕寫入；請移除後再送，不要把機密放進記憶")
    chunks = _persona_chunks(t)
    canon = "\n\n".join(chunks)
    sha = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    src = (source or "").strip()[:200]
    with _write_lock:
        with _http() as c:
            def listing():
                return _list_uid(PERSONA_UID)
            old = _persona_parts(listing(), n, k)
            if old and "\n\n".join(m.get("memory") or "" for m in old) == canon:
                return {"persona": n, "kind": k, "changed": False, "parts": len(old), "sha256": sha}
            src_final = src or ((old[0].get("metadata") or {}).get("source") or "") if old else src
            for i, ch in enumerate(chunks, 1):
                r = c.post("/memories", json={
                    "messages": [{"role": "user", "content": ch}], "user_id": PERSONA_UID,
                    "agent_id": f"hermes-{n}", "infer": False,
                    "metadata": {"type": "persona", "persona": n, "kind": k, "part": i, "parts": len(chunks),
                                 "source": src_final,
                                 "sha256": sha, "written_by": me}})
                r.raise_for_status()
            now = listing()
            fresh = [m for m in _persona_parts(now, n, k) if (m.get("metadata") or {}).get("sha256") == sha]
            if "\n\n".join(m.get("memory") or "" for m in fresh) != canon or len(fresh) != len(chunks):
                for m in fresh:  # 讀回不一致：撤掉剛寫的，保留舊的
                    c.delete(f"/memories/{m['id']}")
                raise RuntimeError("寫後讀回比對不一致，已撤回這次寫入，舊內容保持不變")
            for m in old:
                c.delete(f"/memories/{m['id']}")
    return {"persona": n, "kind": k, "changed": True, "parts": len(chunks), "sha256": sha, "by": me}


# 搜尋的混合排序：
# Mem0 內建的關鍵字比對照英文斷詞，中文整句黏成一個詞、IP 因為有小數點被丟掉，而且只幫向量已撈到的加分，
# 所以設備名、IP、VM 編號這類短詞幾乎搜不到。改成在這裡排：向 Mem0 多要一些向量結果，
# 拿整區快照一字不差比對問句裡的英數詞（少見的詞加比較多），向量沒撈到但關鍵字中的也補進來，最後依類型微調。
# 參數用報告的 20 題加 8 題短詞題掃過；改參數前先重跑那份評測。
SEARCH_POOL = 60     # 向 Mem0 要幾筆向量結果（它內部本來就先撈 60 筆再截）
KW_WEIGHT = 0.3      # 關鍵字分（0～1）乘上這個加進相似度；相似度大約落在 0.25～0.8
KW_POWER = 2.0       # 詞的稀有度（0～1）再平方：IP、CT42 這種少見的詞幾乎全加，PVE、IP、Mem0 這種常見詞只加一點
KW_MIN = 0.3         # 向量沒撈到、只靠關鍵字補進來的，關鍵字分至少要這麼高
KW_EXTRA = 20        # 只靠關鍵字補進來的最多幾則
TYPE_ADJ = {"decision": 0.2, "status": 0.2, "message": -0.2}  # 決策、狀態往前，留言往後（按相關程度的比例，見 final）
KW_TTL = 30          # 關鍵字比對用的整區快照留幾秒（672 則時列一次約 0.12 秒）
_KW_RE = re.compile(r"[a-z0-9](?:[a-z0-9._:/-]*[a-z0-9])?")
_kw_snap: dict = {}  # 區（user_id）→ (列出的時間, [(記憶, 小寫內文)])


def _kw_terms(query: str) -> list:
    """問句裡要一字不差比對的詞：英數字串（設備名、IP、VM 編號、網址、代號）。中文交給向量。"""
    out = []
    for t in _KW_RE.findall(query.lower()):
        if len(t) >= 2 and t not in out:
            out.append(t)
    return out[:12]


def _kw_corpus(uid: str = USER_ID) -> list:
    at, docs = _kw_snap.get(uid, (0.0, []))
    if not docs or time.monotonic() - at > KW_TTL:
        docs = [(m, (m.get("memory") or "").lower()) for m in _list_uid(uid)]
        _kw_snap[uid] = (time.monotonic(), docs)
    return docs


def _kw_scores(terms: list, docs: list) -> dict:
    """每則記憶的關鍵字分（0～1）：問句的英數詞中了幾個，越少見的詞越重（用整區算）；整區都沒有的詞不算。"""
    if not terms or not docs:
        return {}
    pats = [re.compile(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])") for t in terms]
    hit = [[t in text and p.search(text) is not None for t, p in zip(terms, pats)] for _, text in docs]
    n = len(docs)
    df = [sum(h[i] for h in hit) for i in range(len(terms))]
    w = [(math.log((n + 1) / (d + 1)) / math.log(n + 1)) ** KW_POWER if d else 0.0 for d in df]
    used = sum(1 for d in df if d)
    out = {}
    for (m, _), h in zip(docs, hit):
        s = sum(wi for wi, hi in zip(w, h) if hi)
        if s > 0:
            out[m.get("id")] = (s / used, m)
    return out


def _in_scope(m: dict, filters: dict) -> bool:
    md = m.get("metadata") or {}
    return all((m.get("agent_id") if k == "agent_id" else md.get(k)) == v
               for k, v in filters.items() if k != "user_id")


# 精簡版：搜尋、列表預設每則只回開頭一段，
# 要全文用 memory_get。10 則精簡版約是改版前 5 則全文的一半 token。
SNIP_CHARS = 100     # 精簡版每則給幾個字
SNIP_SLACK = 30      # 全文只比這多一點就整段給，不切
LIST_PAGE = 50       # memory_list 一頁預設幾則（最多 200）


def _snip(text: str, terms: list) -> str:
    """精簡版內文：去掉 [筆記] 這類標籤、空白併成一格，取開頭 SNIP_CHARS 字；
    問句的英數詞在後面才出現時，補上它前後一小段，看得出為什麼搜到這則。"""
    t = re.sub(r"\s+", " ", TAG_RE.sub("", text or "")).strip()
    if len(t) <= SNIP_CHARS + SNIP_SLACK:
        return t
    out = t[:SNIP_CHARS] + "…"
    for w in terms:
        hit = re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t, re.I)
        if hit and hit.start() >= SNIP_CHARS:
            out += t[max(SNIP_CHARS, hit.start() - 30): hit.end() + 40] + "…"
            break
    return out


def _brief(m: dict, terms: list) -> dict:
    """精簡版一則：date 是留言的寄出日、其他類型的最後修改日；chars 是全文字數。"""
    md = m.get("metadata") or {}
    ts = m.get("created_at") if md.get("type") == "message" else (m.get("updated_at") or m.get("created_at"))
    return {"id": m.get("id"), "type": md.get("type"), "project": md.get("project"), "agent": m.get("agent_id"),
            "date": str(ts or "")[:10], "text": _snip(m.get("memory"), terms), "chars": len(m.get("memory") or "")}


@mcp.tool()
def memory_search(query: str, project: str = "", type: str = "", agent: str = "", limit: int = 10,
                  full: bool = False, archived: bool = False, messages: bool = False) -> list:
    """搜尋共用記憶：比意思，也一字不差比對問句裡的英數詞（設備名、IP、VM 編號、網址、代號），
    只打「10.0.0.5」「VM101」也搜得到。預設回 10 則（最多 20）。
    預設只搜決策、筆記、進度卡，不含留言和認領；要連留言一起搜帶 messages=true（決策、狀態排前面，留言排後面），
    只搜留言帶 type=message，只搜認領帶 type=claim。
    可用 project、type（status/claim/decision/note/message）、agent 縮小範圍；不確定放在哪個 project 就先不帶。
    每則只回精簡版（開頭約 100 字，chars＝全文字數）；要全文（含 version）用 memory_get(id)、帶 full=true，
    或問句只打記憶編號（前 8 碼以上）。超過 14 天的留言會自動封存、平常搜不到；找舊留言帶 archived=true。"""
    q = _clean(query, "query", 500)
    key = q.strip().lower()
    if re.fullmatch(r"[0-9a-f]{8}(?:[0-9a-f-]{0,28})", key):  # 只打編號：直接回那則全文（還沒拿到 memory_get 的舊連線也能用）
        got = [r for r in memory_get(key) if "error" not in r]
        if got:
            return [{**r, "score": 1.0} for r in got]
    filters = {}
    if project.strip():
        filters["project"] = _proj(project)
    if type.strip():
        filters["type"] = type.strip()
    if agent.strip():
        filters["agent_id"] = agent.strip()
    if archived:
        uids = [MSG_ARCHIVE_UID]
    elif filters.get("type") in OPS_TYPES:
        uids = [OPS_UID]
    else:  # 預設只搜原則記憶層，答案不會被大量留言擠掉
        uids = [USER_ID, OPS_UID] if messages else [USER_ID]
    pool = []
    with _http() as c:
        for uid in uids:
            r = c.post("/search", json={"query": q, "filters": {"user_id": uid, **filters}, "top_k": SEARCH_POOL})
            r.raise_for_status()
            pool += _items(r.json())
    cand = {m.get("id"): (m.get("score") or 0.0, m) for m in pool}
    floor = min((s for s, _ in cand.values()), default=0.0)  # 這次撈到最不相關那則的相似度
    kw = {}
    terms = _kw_terms(q)
    if terms:
        try:
            kw = _kw_scores(terms, [d for uid in uids for d in _kw_corpus(uid)])
        except Exception:  # 列整區失敗（例如超過上限）就只用向量結果，搜尋本身不能壞
            kw = {}
        extra = sorted(((k, i, m) for i, (k, m) in kw.items()
                        if i not in cand and k >= KW_MIN and _in_scope(m, filters)), key=lambda x: -x[0])
        for _, i, m in extra[:KW_EXTRA]:  # 向量沒撈到、只靠關鍵字補進來的，相似度當成墊底那則
            cand[i] = (floor, m)

    def final(i):  # 類型只按「比墊底那則多相關多少」的比例加減，本來就不相關的不會被類型抬上來
        s, m = cand[i]
        adj = TYPE_ADJ.get((m.get("metadata") or {}).get("type"), 0.0)
        return s + (s - floor) * adj + KW_WEIGHT * kw.get(i, (0.0,))[0]

    out, seen = [], set()
    for i in sorted(cand, key=final, reverse=True):
        m = _unarchived(cand[i][1]) if archived else cand[i][1]
        if m.get("id") in seen:  # 封存搬到一半重來過，同一則可能有兩份副本
            continue
        seen.add(m.get("id"))
        row = _fmt(m) if full else _brief(m, terms)
        row["score"] = round(final(i), 4)
        if archived:
            row["archived"] = str((cand[i][1].get("metadata") or {}).get("archived_at") or "")[:10]
        out.append(row)
        if len(out) >= max(1, min(limit, 20)):
            break
    return out


@mcp.tool()
def memory_list(project: str = "", type: str = "", limit: int = LIST_PAGE, offset: int = 0, full: bool = False) -> dict:
    """盤點用：依專案、類型列出記憶，最近有動靜的在前，一次最多 50 則（limit 最多 200），用 offset 往後翻頁
    （回傳的 next_offset 是下一頁；沒有下一頁就是 null）。每則只回精簡版；要全文用 memory_get，或帶 full=true。
    找特定的事請用 memory_search，比較省。超過 14 天的留言已自動封存，不會列出。"""
    p = _proj(project) if project.strip() else None
    t = type.strip() or None
    rows = [m for m in _list_all() if _match(m, project=p, type=t)]
    rows.sort(key=lambda m: str(m.get("updated_at") or m.get("created_at") or ""), reverse=True)
    n, o = max(1, min(limit, 200)), max(0, offset)
    page = rows[o:o + n]
    return {"total": len(rows), "offset": o, "next_offset": o + n if o + n < len(rows) else None,
            "items": [_fmt(m) if full else _brief(m, []) for m in page]}


@mcp.tool()
def memory_get(ids: str) -> list:
    """讀記憶全文（含 version：memory_update／memory_delete 要帶）。ids 是 memory_search／memory_list 回傳的 id，
    一次最多 10 個、逗號隔開；只給前 8 碼也可以。已封存的舊留言用原編號照樣拿得到（多一欄 archived＝封存日）。"""
    want = [x for x in re.split(r"[\s,，、]+", (ids or "").strip().lower()) if x]
    if not want:
        raise ValueError("ids 至少給一個記憶編號")
    if len(want) > 10:
        raise ValueError("一次最多 10 個")
    bad = [x for x in want if not re.fullmatch(r"[0-9a-f]{8}(?:[0-9a-f-]{0,28})", x)]
    if bad:
        raise ValueError(f"記憶編號格式不對：{'、'.join(bad)}（至少前 8 碼）")
    items = _list_all()
    out = []
    for w in want:
        hits = [m for m in items if (m.get("id") or "").lower().startswith(w)]
        if len(hits) > 1:
            out.append({"id": w, "error": f"前幾碼對到 {len(hits)} 筆，請多給幾碼"})
            continue
        if hits:
            out.append(_fmt(hits[0]))
            continue
        try:
            a = _archived_copy(w)
        except ValueError as e:
            out.append({"id": w, "error": str(e)})
            continue
        if a is None:
            out.append({"id": w, "error": "找不到：可能已刪除（memory_trash 查）或編號打錯"})
            continue
        row = _fmt(_unarchived(a))
        row["archived"] = str((a.get("metadata") or {}).get("archived_at") or "")[:10]
        out.append(row)
    return out


# 整理記憶用：改、刪都先把完整舊內容存進另一個區（roundtable-archive），讀回確認才動原記憶。
# 封存區不在 roundtable 底下，所以 inbox、搜尋、列表、公告欄都看不到；要查用 memory_trash／memory_history。
ARCHIVE_UID = "roundtable-archive"
EDIT_TYPES = ("decision", "note")              # status 用 project_set_status 改；claim、留言有各自的工具
DELETE_TYPES = ("decision", "note", "status")  # 已結束專案的 status 也可以刪


def _now_tw() -> str:
    return dt.datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def _archive_list() -> list:
    return _list_uid(ARCHIVE_UID)


def _snap(a: dict) -> dict:
    try:
        return json.loads(a.get("memory") or "{}")
    except ValueError:
        return {}


def _find_memory(memory_id: str, items=None) -> dict:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", memory_id or ""):
        raise ValueError("記憶編號格式不對（用 memory_list／memory_search 回傳的 id）")
    for m in _list_all() if items is None else items:
        if m.get("id") == memory_id:
            return m
    raise ValueError("找不到這筆記憶（可能已被刪除：用 memory_trash 查、memory_restore 還原）")


def _check_base(m: dict, base_version: str) -> None:
    cur = m.get("memory") or ""
    if (base_version or "").strip() != _version(cur):
        raise ValueError("這筆記憶已被改過，或你沒帶 base_version（memory_get 回傳的 version）；"
                         "請重讀後再做。目前內容：" + cur[:400])


def _archive(m: dict, kind: str, reason: str, me: str) -> str:
    """把一筆記憶完整存進封存區，讀回逐字核對後才回傳封存編號；失敗就丟錯，原記憶不會被動到。"""
    snap = {k: m.get(k) for k in ("id", "memory", "metadata", "agent_id", "created_at", "updated_at")}
    text = json.dumps(snap, ensure_ascii=False, sort_keys=True)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    md0 = m.get("metadata") or {}
    md = {"type": kind, "memory_id": m["id"], "project": md0.get("project") or "", "orig_type": md0.get("type") or "",
          "author": m.get("agent_id") or "", "by": me, "at": _now_tw(), "reason": reason,
          "base_version": _version(m.get("memory")), "sha256": sha}
    with _http() as c:
        c.post("/memories", json={"messages": [{"role": "user", "content": text}], "user_id": ARCHIVE_UID,
                                  "agent_id": me, "infer": False, "metadata": md}).raise_for_status()
    for a in _archive_list():
        am = a.get("metadata") or {}
        if (am.get("memory_id") == m["id"] and am.get("type") == kind and am.get("at") == md["at"]
                and hashlib.sha256((a.get("memory") or "").encode("utf-8")).hexdigest() == sha):
            return a["id"]
    raise RuntimeError("存進封存區後讀不回一樣的內容，已停止（原記憶沒有動）")


@mcp.tool()
def memory_update(memory_id: str, content: str, base_version: str, reason: str = "") -> dict:
    """修改一筆 decision／note（例如把重複的幾筆歸納成一筆、更正過時的內容）。content 是整筆新內容，會取代舊內容
    （開頭沒寫日期會自動補上當地時間）。必須帶 base_version（memory_get 回傳的 version），版本不符會拒絕。
    改之前會把完整舊版存進封存區：用 memory_history 查舊版，要改回就把舊內容再 memory_update 回去。
    改別人寫的記憶 reason 必填。任何身分都能改，全部留紀錄。status 用 project_set_status；claim、留言不能用這個改。
    結尾一行行的「YYYY-MM-DD 使用者補充：…」是使用者在公告欄加的註腳，由伺服器保管：content 不用帶（帶了也會被拿掉），
    改完會自動接回最後面，不佔 1000 字上限。"""
    me = current_agent.get()
    body = _body(_foot_strip(content), "content")  # 註腳行不收，下面用舊記錄的接回去
    why = (reason or "").strip()[:300]
    with _write_lock:
        m = _find_memory(memory_id)
        md = dict(m.get("metadata") or {})
        if md.get("type") not in EDIT_TYPES:
            raise ValueError(f"memory_update 只能改 {EDIT_TYPES}，這筆是 {md.get('type')}"
                             "（status 用 project_set_status；claim、留言有各自的工具）")
        _check_base(m, base_version)
        if m.get("agent_id") not in _names(me) and not why:
            raise ValueError("改別人寫的記憶要填 reason（為什麼改），會記在封存紀錄裡")
        if not re.match(r"\d{4}-\d{2}-\d{2}", body):
            body = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M ") + body
        feet = _foot_split(m.get("memory") or "")[1]
        feet += [x for x in _feet_carry.get() if x not in feet]
        text = {"decision": "[決策] ", "note": "[筆記] "}[md["type"]] + body + "".join("\n" + x for x in feet)
        if text == (m.get("memory") or ""):
            return {"changed": False, "id": memory_id, "version": _version(text)}
        archive_id = _archive(m, "memory_revision", why, me)
        md.update({"updated_by": me, "revision": int(md.get("revision") or 1) + 1})
        with _http() as c:
            c.put(f"/memories/{memory_id}", json={"text": text, "metadata": md}).raise_for_status()
        if (_find_memory(memory_id).get("memory") or "") != text:
            raise RuntimeError(f"改完讀回內容不一致；舊版在封存區 {archive_id}，請用 memory_history 核對")
    return {"changed": True, "id": memory_id, "by": me, "version": _version(text), "archive_id": archive_id}


@mcp.tool()
def memory_delete(memory_id: str, reason: str, base_version: str) -> dict:
    """刪除一筆重複或已失效的 decision／note／status（軟刪除，可還原）。reason 必填，例如「已併入 <id>」「專案已結束」。
    必須帶 base_version（memory_get 回傳的 version），表示你看過最新內容。
    會先把完整內容、誰刪、何時、為什麼存進封存區並讀回核對，才從共用記憶移除；之後搜尋、列表、公告欄都看不到。
    刪錯了用 memory_trash 找到 trash_id，再 memory_restore 還原。任何身分都能刪，全部留紀錄。"""
    me = current_agent.get()
    why = _clean(reason, "reason", 300)
    with _write_lock:
        m = _find_memory(memory_id)
        t = (m.get("metadata") or {}).get("type")
        if t not in DELETE_TYPES:
            raise ValueError(f"memory_delete 只能刪 {DELETE_TYPES}，這筆是 {t}（claim 用 claim_release，留言不刪）")
        _check_base(m, base_version)
        trash_id = _archive(m, "memory_trash", why, me)
        with _http() as c:
            c.delete(f"/memories/{memory_id}").raise_for_status()
        if any(x.get("id") == memory_id for x in _list_all()):
            raise RuntimeError(f"已存進封存區（{trash_id}），但原記憶還在；請重試或告訴使用者")
    return {"deleted": memory_id, "trash_id": trash_id, "by": me, "reason": why,
            "restore": f'memory_restore(trash_id="{trash_id}")'}


@mcp.tool()
def memory_trash(project: str = "", include_restored: bool = False, limit: int = 50) -> list:
    """回收桶：列出被 memory_delete 刪掉的記憶（原內容、作者、誰刪、何時、為什麼），新的在前。
    已還原的預設不列（include_restored=true 才列）。要還原就用 memory_restore(trash_id)。"""
    p = _proj(project) if project.strip() else None
    out = []
    for a in _archive_list():
        am = a.get("metadata") or {}
        if am.get("type") != "memory_trash" or (p and am.get("project") != p):
            continue
        if am.get("restored_id") and not include_restored:
            continue
        out.append({"trash_id": a.get("id"), "memory_id": am.get("memory_id"), "type": am.get("orig_type"),
                    "project": am.get("project"), "author": am.get("author"), "text": _snap(a).get("memory"),
                    "deleted_by": am.get("by"), "deleted_at": am.get("at"), "reason": am.get("reason"),
                    "restored_id": am.get("restored_id") or "", "restored_by": am.get("restored_by") or ""})
    out.sort(key=lambda x: x["deleted_at"] or "", reverse=True)
    return out[: max(1, min(limit, 200))]


@mcp.tool()
def memory_restore(trash_id: str, reason: str = "") -> dict:
    """把 memory_delete 刪掉的記憶原樣還原（內容、原作者、專案、類型都照舊；Mem0 會給新的 id，舊 id 記在 orig_id）。
    trash_id 用 memory_trash 查。同一筆只能還原一次；專案已有新的 status 時不能還原舊 status（請用 project_set_status 合併）。"""
    me = current_agent.get()
    with _write_lock:
        a = next((x for x in _archive_list() if x.get("id") == trash_id), None)
        am = dict((a or {}).get("metadata") or {})
        if not a or am.get("type") != "memory_trash":
            raise ValueError("找不到這筆刪除紀錄（trash_id 用 memory_trash 查）")
        if am.get("restored_id"):
            raise ValueError(f"這筆已經還原過了（新 id {am['restored_id']}，{am.get('restored_by')} 還原）")
        if hashlib.sha256((a.get("memory") or "").encode("utf-8")).hexdigest() != am.get("sha256"):
            raise RuntimeError("封存內容和當時的指紋對不上，不還原；請告訴使用者")
        snap = _snap(a)
        md = dict(snap.get("metadata") or {})
        if md.get("type") == "status" and any(_match(m, project=md.get("project"), type="status") for m in _list_all()):
            raise ValueError("這個專案已經有新的 status，不能直接還原舊的；請把需要的內容用 project_set_status 併進去")
        md.update({"restored_from": trash_id, "orig_id": snap.get("id") or "", "orig_created": snap.get("created_at") or "",
                   "restored_by": me})
        with _http() as c:
            c.post("/memories", json={"messages": [{"role": "user", "content": snap.get("memory") or ""}],
                                      "user_id": _uid_for(md.get("type")), "agent_id": snap.get("agent_id") or me, "infer": False,
                                      "metadata": md}).raise_for_status()
        back = [m for m in _list_all() if (m.get("metadata") or {}).get("restored_from") == trash_id]
        if len(back) != 1 or (back[0].get("memory") or "") != (snap.get("memory") or ""):
            raise RuntimeError("還原後讀回不一致，請用 memory_list 核對並告訴使用者")
        am.update({"restored_id": back[0]["id"], "restored_by": me, "restored_at": _now_tw(),
                   "restore_reason": (reason or "").strip()[:300]})
        with _http() as c:
            c.put(f"/memories/{trash_id}", json={"text": a.get("memory") or "", "metadata": am}).raise_for_status()
    return {"restored": back[0]["id"], "orig_id": snap.get("id"), "from_trash": trash_id, "by": me,
            "text": (snap.get("memory") or "")[:200]}


@mcp.tool()
def memory_history(memory_id: str) -> list:
    """查一筆記憶被改過、刪過的紀錄（每次改之前的完整舊版、誰、何時、為什麼），舊的在前。
    還原過的記憶會一併列出還原前那個 id 的紀錄。要改回舊版：把舊內容用 memory_update 寫回去。"""
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", memory_id or ""):
        raise ValueError("記憶編號格式不對")
    ids = {memory_id}
    for m in _list_all():
        if m.get("id") == memory_id and (m.get("metadata") or {}).get("orig_id"):
            ids.add(m["metadata"]["orig_id"])
    out = []
    for a in _archive_list():
        am = a.get("metadata") or {}
        if am.get("memory_id") not in ids or am.get("type") not in ("memory_revision", "memory_trash"):
            continue
        out.append({"archive_id": a.get("id"), "memory_id": am.get("memory_id"),
                    "kind": "改之前的舊版" if am["type"] == "memory_revision" else "刪除",
                    "text": _snap(a).get("memory"), "version": am.get("base_version"), "by": am.get("by"),
                    "at": am.get("at"), "reason": am.get("reason"), "restored_id": am.get("restored_id") or ""})
    out.sort(key=lambda x: x["at"] or "")
    return out


# 舊留言自動封存：
# 留言佔共用區六成，越積越多會把真正的答案擠下去、也會碰到列表上限。每天凌晨把「整串（原信和所有回覆）都超過 14 天、
# 而且沒有還在等人做的待辦、沒有沒結案的徵求意見」的留言搬到另一區：先存副本、讀回逐字核對，才刪共用區的原留言。
# 搬走後收件匣、公告欄、一般搜尋、列表都看不到；memory_search(archived=true) 搜得到，memory_get(原編號) 拿得到。
# 另開一區、不跟改刪紀錄（roundtable-archive）混在一起，回收桶和改版紀錄才不會被大量留言拖慢。
MSG_ARCHIVE_UID = "roundtable-msg-archive"
MSG_ARCHIVE_DAYS = 14
MSG_ARCHIVE_AT = (4, 30)    # 每天當地時間幾點跑（避開週一 02:00 的記憶盤點、03:30 的 PBS 備份）
MSG_ARCHIVE_BATCH = 25      # 每批搬幾則；搬的時候拿著寫入鎖，批跟批之間放開，別的寫入不會卡太久
MSG_ARCHIVE_MAX = 500       # 一次最多搬幾則，剩下的隔天再搬
MSG_ARCHIVE_ON = os.environ.get("MSG_ARCHIVE", "1") != "0"  # 設 0 就不排程（搜尋、讀取封存區照常）
_archive_last: dict = {}    # 最近一次的結果，也會印進 docker logs


def _unarchived(a: dict) -> dict:
    """封存區的副本換回原留言的樣子：原編號、原建立與修改時間（副本自己的編號和時間只在封存區內用）。"""
    md = dict(a.get("metadata") or {})
    return {**a, "id": md.pop("orig_id", None) or a.get("id"),
            "created_at": md.pop("orig_created", None) or a.get("created_at"),
            "updated_at": md.pop("orig_updated", None) or a.get("updated_at"), "metadata": md}


def _archived_copies(orig_id: str) -> list:
    """封存區裡原編號是 orig_id 的副本（正常只有一份）。用 Mem0 搜尋的欄位篩選找，不用列整區。"""
    with _http() as c:
        r = c.post("/search", json={"query": orig_id, "filters": {"user_id": MSG_ARCHIVE_UID, "orig_id": orig_id},
                                    "top_k": 10, "threshold": 0})
        r.raise_for_status()
        return [a for a in _items(r.json()) if (a.get("metadata") or {}).get("orig_id") == orig_id]


def _archived_copy(oid: str) -> dict | None:
    """用原編號（完整或前 8 碼以上）找封存的舊留言；有好幾份副本取最早那份，找不到回 None。"""
    if len(oid) == 36:
        hits = _archived_copies(oid)
    else:  # 只有前幾碼：列整個封存區比對
        hits = [a for a in _list_uid(MSG_ARCHIVE_UID) if ((a.get("metadata") or {}).get("orig_id") or "").startswith(oid)]
        if len({(a.get("metadata") or {}).get("orig_id") for a in hits}) > 1:
            raise ValueError("前幾碼對到好幾則封存的留言，請多給幾碼")
    return min(hits, key=lambda a: str(a.get("created_at"))) if hits else None


def _archive_due_one(m: dict, now) -> bool:
    if (m.get("metadata") or {}).get("type") != "message":
        return False
    d = _created(m)
    if d is None or now - d < dt.timedelta(days=MSG_ARCHIVE_DAYS):
        return False
    if _is_todo(m):  # 還在等人動手
        return False
    if (m.get("metadata") or {}).get("ask_opinion") == "yes" and not _closed(m):  # 徵求意見還沒結案
        return False
    return True


def _archive_due(items: list, now) -> list:
    """可以封存的留言，舊的在前。原信和回覆算一整串，整串都可以封存才一起搬：
    收件匣、公告欄靠原信判斷回覆是給誰的，徵求意見也要數回覆，不能只搬走一半。"""
    msgs = {m.get("id"): m for m in items if (m.get("metadata") or {}).get("type") == "message"}
    up = {}

    def root(i):
        while up.get(i, i) != i:
            i = up[i]
        return i

    for i, m in msgs.items():
        p = (m.get("metadata") or {}).get("reply_to")
        if p in msgs and root(p) != root(i):
            up[root(i)] = root(p)
    threads = {}
    for i, m in msgs.items():
        threads.setdefault(root(i), []).append(m)
    out = []
    for ms in threads.values():
        if all(_archive_due_one(m, now) for m in ms):
            out += ms
    out.sort(key=lambda m: str(m.get("created_at")))
    return out


def _archive_msg(m: dict) -> str:
    """把一則留言搬進封存區：先存副本（內文、作者、所有欄位照抄，另記原編號與原時間），讀回逐字核對，
    才從共用區刪掉原留言、再確認刪掉了。哪一步不對就丟錯，原留言不會在副本核對前被刪。回傳副本編號。"""
    oid = m["id"]
    md = dict(m.get("metadata") or {})
    md.update(orig_id=oid, orig_created=m.get("created_at") or "", orig_updated=m.get("updated_at") or "",
              archived_at=_now_tw())

    def same(a):
        am = a.get("metadata") or {}
        return ((a.get("memory") or "") == (m.get("memory") or "") and (a.get("agent_id") or "") == (m.get("agent_id") or "")
                and all(am.get(k) == v for k, v in md.items() if k != "archived_at"))

    ok = next((a for a in _archived_copies(oid) if same(a)), None)  # 上次搬到一半（副本存了、原留言沒刪）就不再存一份
    if ok is None:
        body = {"messages": [{"role": "user", "content": m.get("memory") or ""}], "user_id": MSG_ARCHIVE_UID,
                "infer": False, "metadata": md}
        if m.get("agent_id"):
            body["agent_id"] = m["agent_id"]
        with _http() as c:
            c.post("/memories", json=body).raise_for_status()
        ok = next((a for a in _archived_copies(oid) if same(a)), None)
        if ok is None:
            raise RuntimeError("副本讀回跟原留言對不上，原留言沒刪")
    with _http() as c:
        c.delete(f"/memories/{oid}").raise_for_status()
        r = c.get(f"/memories/{oid}")
        r.raise_for_status()
        if r.json():
            raise RuntimeError("刪了原留言但還讀得到")
    return ok["id"]


def _archive_old_messages(now=None, limit: int = MSG_ARCHIVE_MAX) -> dict:
    """搬一輪：每批 MSG_ARCHIVE_BATCH 則，最多 limit 則；出錯就停（已搬的不受影響，隔天再試）。"""
    now = now or dt.datetime.now(dt.timezone.utc)
    moved, err = 0, ""
    while moved < limit and not err:
        with _write_lock:
            due = _archive_due(_list_all(), now)[: min(MSG_ARCHIVE_BATCH, limit - moved)]
            if not due:
                break
            for m in due:
                try:
                    _archive_msg(m)
                except Exception as e:
                    err = f"{m.get('id')}：{e}"
                    break
                moved += 1
    res = {"at": _now_tw(), "moved": moved, "left": len(_archive_due(_list_all(), now)), "error": err}
    _archive_last.clear()
    _archive_last.update(res)
    print("[舊留言封存] " + json.dumps(res, ensure_ascii=False), flush=True)
    return res


def _archive_loop() -> None:
    while True:
        now = dt.datetime.now(LOCAL_TZ)
        nxt = now.replace(hour=MSG_ARCHIVE_AT[0], minute=MSG_ARCHIVE_AT[1], second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        time.sleep((nxt - now).total_seconds())
        try:
            _archive_old_messages()
        except Exception as e:  # 列整區失敗之類：記下來，隔天再試，不能讓排程停掉
            print(f"[舊留言封存] 這輪失敗：{e}", flush=True)


RICH_RE = re.compile(
    r"(?P<fence>```.*?```)"
    r"|(?P<tick>`[^`\n]+`)"
    r"|(?P<url>https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]+)"
    r"|(?P<path>[A-Za-z]:\\[^\s<>\"'，。；、）」』】`]+)"
    r"|(?P<ident>(?<![A-Za-z0-9_])(?:[a-z][a-z0-9]*(?:_[a-z0-9]+)+|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?![A-Za-z0-9_]))",
    re.S)


def _rich(s: str) -> str:
    """跳脫 HTML 後：網址變可點連結（只認 http/https）；路徑、`反引號`、函式或變數名變等寬字；```圍欄```變程式碼框。"""
    esc, out, pos = html.escape, [], 0
    s = s or ""
    for m in RICH_RE.finditer(s):
        out.append(esc(s[pos:m.start()]))
        pos, g, v = m.end(), m.lastgroup, m.group()
        if g == "fence":
            out.append(f'<pre class="blk"><code>{esc(v[3:-3].strip(chr(10)))}</code></pre>')
        elif g == "tick":
            out.append(f"<code>{esc(v[1:-1])}</code>")
        elif g == "url":
            url = v.rstrip(".,;:!?")
            u = esc(url, quote=True)
            out.append(f'<a href="{u}" target="_blank" rel="noopener noreferrer">{u}</a>{esc(v[len(url):])}')
        else:
            out.append(f"<code>{esc(v)}</code>")
    out.append(esc(s[pos:]))
    return "".join(out)


def _breaks(s: str) -> str:
    """讓長段落好讀：編號項目(1)(2)各自換行，長文在句號後換行。"""
    s = re.sub(r"\s*([（(]\d{1,2}[)）])", r"\n\1", s or "").strip()
    if len(s) > 80:
        s = re.sub(r"。(?=[^\n」』）)])", "。\n", s)
    return s


def _tw(ts) -> str:
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(LOCAL_TZ).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def _rel(ts) -> str:
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        s = (dt.datetime.now(dt.timezone.utc) - d).total_seconds()
        if s < 90:
            return "剛剛"
        if s < 3600:
            return f"{int(s // 60)} 分鐘前"
        if s < 86400:
            return f"{int(s // 3600)} 小時前"
    except Exception:
        pass
    return _tw(ts)


def _day(ts) -> str:
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        d = d.astimezone(LOCAL_TZ)
        today = dt.datetime.now(LOCAL_TZ).date()
        if d.date() == today:
            return "今天"
        if d.date() == today - dt.timedelta(days=1):
            return "昨天"
        return d.strftime("%m 月 %d 日")
    except Exception:
        return ""


def _who(name: str) -> str:
    n = (name or "?").lower()
    for key in ("claude", "codex", "hermes", "user"):
        if n.startswith(key):
            return key
    return "other"


CLAIM_PREFIX = re.compile(r"^\[進行中\]\s*")


def _board_html() -> str:
    esc = html.escape
    items = _list_all()
    msgs = sorted([m for m in items if (m.get("metadata") or {}).get("type") == "message" and not _is_test(m)],
                  key=lambda m: str(m.get("created_at")), reverse=True)[:80]
    claims = [m for m in items if (m.get("metadata") or {}).get("type") == "claim"]
    todo = [m for m in msgs if _is_todo(m)]
    agents = sorted({(m.get("metadata") or {}).get("from", "?") for m in msgs})

    def badge(name):
        return f'<span class="who w-{_who(name)}">{esc(str(name))}</span>'

    cl = []
    for m in claims:
        md = m.get("metadata") or {}
        old = _age_hours(m) > CLAIM_TTL_HOURS
        ts = m.get("updated_at") or m.get("created_at")
        text = CLAIM_PREFIX.sub("", m.get("memory") or "")
        cl.append(f'<li class="claim{" old" if old else ""}"><div class="ct"><b class="tg">{esc(md.get("target") or "?")}</b>'
                  f'{badge(m.get("agent_id") or "?")}<span class="when" title="{_tw(ts)}">'
                  f'{_rel(ts)}{"．已過期" if old else ""}</span></div><div class="cs">{_rich(text)}</div></li>')
    rows, last_day = [], None
    for m in msgs:
        md = m.get("metadata") or {}
        d = _day(m.get("created_at"))
        if d != last_day:
            rows.append(f'<div class="day">{d}</div>')
            last_day = d
        urgent = md.get("needs_action") == "yes"
        readers = _readers(m)
        sender = md.get("from", "?")
        tags = ""
        if urgent:
            tags += '<span class="tag">已處理</span>' if readers else '<span class="tag hot">需處理</span>'
        tags += '<span class="tag">已讀：' + esc("、".join(readers)) + "</span>" if readers else '<span class="tag">尚無人讀</span>'
        if md.get("project") and md.get("project") != "general":
            tags += f'<span class="tag">{esc(md.get("project"))}</span>'
        body = f'<div class="b">{_rich(md.get("body") or "")}</div>' if md.get("body") else ""
        ref = f'<div class="ref">檔案　{esc(md.get("ref"))}</div>' if md.get("ref") else ""
        cls = "msg urgent" if urgent and not readers else "msg"
        rows.append(f'<article class="{cls}" data-from="{esc(sender)}" data-to="{esc(md.get("to", ""))}">'
                    f'<div class="av w-{_who(sender)}">{esc(sender[:1].upper())}</div><div class="mc">'
                    f'<div class="line">{badge(sender)}<span class="arrow">→</span>{badge(md.get("to", "?"))}'
                    f'<span class="when" title="{_tw(m.get("created_at"))}">{_rel(m.get("created_at"))}</span></div>'
                    f'<h3>{_rich(md.get("subject") or "")}</h3>{body}{ref}<div class="tags">{tags}</div></div></article>')
    chips = '<button class="chip on" data-f="">全部</button>' + "".join(
        f'<button class="chip" data-f="{esc(a)}">{esc(a)}</button>' for a in agents)
    opts = "".join(f"<option>{esc(a)}</option>" for a in sorted(_recipients() - {"user", "all"}))
    claims_html = "".join(cl) or '<li class="none">目前沒有人認領任何機器</li>'
    rows_html = "".join(rows) or '<div class="panel none">還沒有留言</div>'
    return BOARD_TEMPLATE.format(n_claims=len(claims), n_todo=len(todo), n_msgs=len(msgs), claims=claims_html,
                                 chips=chips, rows=rows_html, opts=opts)


UUID_RE = re.compile(r"[0-9a-fA-F-]{36}")
_reply_cache: dict = {}  # 原編號 →（查的時間, 封存副本或 None）；找到的一直記著，找不到的 10 分鐘後再查


def _archived_reply(oid: str) -> dict:
    """回覆的原留言不在共用區：多半是 14 天封存搬走了（公告欄串 10/19 回報，寫「不存在」使用者會以為資料丟了）。
    從封存區拿原主旨，帶 archived＝封存日；封存區也沒有才寫「原留言已不存在」。公告欄每次刷新都會跑，所以記住結果。"""
    if not UUID_RE.fullmatch(oid or ""):
        return {"id": oid, "from": "?", "subject": "原留言已不存在"}
    hit = _reply_cache.get(oid)
    if hit is None or (hit[1] is None and time.time() - hit[0] > 600):
        try:
            hit = (time.time(), _archived_copy(oid))
        except Exception:  # 封存區查不到（連線、編號不對）不能讓整個公告欄壞掉；不記，下次再查
            hit = (0.0, None)
        else:
            if len(_reply_cache) > 2000:
                _reply_cache.clear()
            _reply_cache[oid] = hit
    if hit[1] is None:
        return {"id": oid, "from": "?", "subject": "原留言已不存在"}
    amd = hit[1].get("metadata") or {}
    return {"id": oid, "from": amd.get("from", "?"), "from_session": amd.get("from_session") or "",
            "subject": _rich(amd.get("subject") or ""), "archived": str(amd.get("archived_at") or "")[:10]}


def _message_feed(m: dict, items: list, active: set | None = None) -> dict:
    md = m.get("metadata") or {}
    reply = None
    if md.get("reply_to"):
        try:
            parent = _find_message(md["reply_to"], items)
            pmd = parent.get("metadata") or {}
            reply = {"id": parent["id"], "from": pmd.get("from", "?"), "from_session": pmd.get("from_session") or "",
                     "subject": _rich(pmd.get("subject") or "")}
        except ValueError:
            reply = _archived_reply(md["reply_to"])
    return {"id": m.get("id"), "from": md.get("from", "?"), "from_session": md.get("from_session") or "",
            "to": md.get("to", "?"), "who": _who(md.get("from")),
            "created": m.get("created_at"), "tw": _tw(m.get("created_at")), "day": _day(m.get("created_at")),
            "subject": _rich(md.get("subject") or ""), "body": _rich(md.get("body") or ""),
            "ref": md.get("ref") or "", "project": md.get("project") or "general",
            "needs_action": md.get("needs_action") == "yes", "readers": _readers(m), "attachments": _atts(m),
            "reply": reply, "votes": _votes(m), "opinion": _opinions(m, items, active),
            "closed": _closed(m), "todo": _is_todo(m), "waiting_on": _waits(m)[0],
            **({"waiting_all": _waits(m)} if len(_tos(md.get("to"))) > 1 else {})}


_session_names_cache = {"mtime": None, "names": {}}


def _session_names() -> dict:
    """公告欄把 claude:串名 顯示成中文名稱用。路由表放在 UI_DIR/session-routes.json（格式見 docs/sessions.md）；檔案不在就回空的，頁面會直接顯示串名。
    只給 label→title，不給 local_ 編號；inbox-test 開頭的測試串不給。"""
    path = os.path.join(UI_DIR, "session-routes.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if mtime != _session_names_cache["mtime"]:
        names = {}
        try:
            with open(path, encoding="utf-8") as f:
                routes = json.load(f)
            for v in routes.values():
                label = v.get("label", "") if isinstance(v, dict) else ""
                if LABEL_RE.match(label) and not label.startswith("inbox-test"):
                    names[label] = str(v.get("title") or label)[:80]
        except (OSError, ValueError, AttributeError):
            names = {}
        _session_names_cache.update(mtime=mtime, names=names)
    return dict(_session_names_cache["names"])


def _feed(limit: int, multi: bool = False) -> dict:
    """新版公告欄的資料：最近 limit 則留言（舊到新）與目前的 claim。文字已在伺服器端跳脫並轉好格式。
    multi＝頁面自己說看得懂逗號收件人（feed 帶 ?multi_to=1）才回 multi_to=true：2.2.0 正式版一看到就會寄逗號 to，
    但顯示還不會拆（公告欄串 10/19），所以要等拆得開的版本自己來要。"""
    items = _list_all()
    msgs = sorted([m for m in items if (m.get("metadata") or {}).get("type") == "message" and not _is_test(m)],
                  key=lambda m: str(m.get("created_at")))
    todo_by, todo = {}, 0  # 待辦依「等誰」分：user／claude／codex／hermes／all；寄給好幾位的每位各算一次，總數只算一則
    for m in msgs:
        if _is_todo(m):
            todo += 1
            for w in _waits(m):
                todo_by[w] = todo_by.get(w, 0) + 1
    active = _active(items)
    out = []
    for m in msgs[-max(10, min(limit, 500)):]:
        out.append(_message_feed(m, items, active))
    claims = []
    for m in items:
        md = m.get("metadata") or {}
        if md.get("type") == "claim":
            ts = m.get("updated_at") or m.get("created_at")
            claims.append({"target": md.get("target") or "?", "agent": m.get("agent_id") or "?",
                           "who": _who(m.get("agent_id")), "when": _rel(ts), "old": _age_hours(m) > CLAIM_TTL_HOURS,
                           "text": _rich(CLAIM_PREFIX.sub("", m.get("memory") or ""))})
    statuses, research = _whiteboard(items)
    return {"messages": out, "total": len(msgs), "todo": todo, "todo_by": todo_by, "claims": claims,
            "recipients": sorted(_recipients() - {"user"}), "statuses": statuses, "research": research,
            "sessions": _session_names(),
            "aliases": {n: _canon(n) for s in ALIAS_SETS for n in s if _canon(n) != n},
            **({"multi_to": True} if multi else {}), "avatars": _avatars()}


STAGE_LABEL = {"planning": "規劃", "implementing": "進行中", "verifying": "驗證", "done": "完成"}
RESEARCH_ITEM = re.compile(r"^\s*(\d{1,3})[.、．)）]\s*(.+)$")
# 白板專案卡；格式寫在 project_set_status 說明
WB_FIELD = re.compile(r"^\s*(名稱|主管|做事|現在)\s*[:：]\s*(.+?)\s*$")
WB_STEP = re.compile(r"^\s*(?:[-*•]\s*)?(\[[ xXvV✓]\]|[✓✔☑✗✘☐])\s*(.+?)\s*$")
WB_NOT_DONE = ("[ ]", "✗", "✘", "☐")
# 專案卡卡在等使用者：「現在」那行的某一句，或第一個還沒勾的小目標，寫著在等使用者／本人。
# 只看第一個沒勾的：後面幾步寫「（要使用者批准）」是之後的關卡，眼前還有事可做，不算卡住。
WB_WAIT = re.compile(r"等(?:待)?\s*(?:使用者|本人|你)"
                     r"|(?:要|需要?|請|待)\s*(?:使用者|本人)\s*(?:決定|批准|核准|批|回答|回覆|確認|拍板|同意|選|看|按|動手)"
                     r"|(?:使用者|本人)\s*(?:決定|回答|批准|核准)(?![的了過後])")
WB_USER_STEP = re.compile(r"^\s*(?:使用者|本人)")  # 小目標的主詞就是使用者，例：「使用者回答 6 個問題」


def _blocked(text: str) -> tuple:
    """（卡在使用者？, 那一句）。那一句是純文字：去掉「現在：」和勾選框，80 字內。"""
    now, first = "", None
    for ln in text.splitlines():
        f, s = WB_FIELD.match(ln), WB_STEP.match(ln)
        if f:
            if f.group(1) == "現在" and not now:
                now = f.group(2)
        elif s and first is None and s.group(1) in WB_NOT_DONE:
            first = s.group(2)
    hits = [x.strip() for x in re.split(r"[；;。]", now) if WB_WAIT.search(x)]
    if not hits and first and (WB_WAIT.search(first) or WB_USER_STEP.match(first)):
        hits = [first.strip()]
    if not hits:
        return False, ""
    return True, hits[0] if len(hits[0]) <= 80 else hits[0][:79] + "…"


TPE = LOCAL_TZ
_status_hist = {}  # status 記憶 id → (updated_at, 更新次數)；白板每次輪詢都會用，內容沒變就不重查


def _tpe_date(ts):
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(TPE).date()
    except Exception:
        return None


def _status_updates(m: dict):
    """這筆 status 建立後被 project_set_status 改過幾次（數 Mem0 歷史裡的 UPDATE）；查不到回 None，頁面就不顯示。"""
    mid, ts = m.get("id"), m.get("updated_at") or m.get("created_at")
    hit = _status_hist.get(mid)
    if hit and hit[0] == ts:
        return hit[1]
    try:
        with _http() as c:
            r = c.get(f"/memories/{mid}/history")
            r.raise_for_status()
            n = sum(1 for e in _items(r.json()) if str(e.get("event")).upper() == "UPDATE")
        md = m.get("metadata") or {}
        n = max(0, n - int(md.get("pingpong_flips") or 0) - int(md.get("priority_flips") or 0))  # 白板切乒乓開關、改優先序也是一次 UPDATE，不算進度更新
    except Exception:
        return hit[1] if hit else None
    _status_hist[mid] = (ts, n)
    return n


def _whiteboard(items: list) -> tuple:
    """白板：各專案 status（完成的排後面），以及 research 專案狀態裡「1. …」開頭的每一行當成一個研究主題。"""
    statuses, research = [], {"intro": "", "items": [], "when": "", "who": ""}
    for m in items:
        md = m.get("metadata") or {}
        if md.get("type") != "status":
            continue
        ts = md.get("status_at") or m.get("updated_at") or m.get("created_at")  # 切乒乓開關不算進度更新
        text = TAG_STRIP.sub("", m.get("memory") or "")
        who = md.get("updated_by") or m.get("agent_id") or "?"
        if md.get("project") == "research":
            lines = text.splitlines()
            research.update(when=_rel(ts), who=who)
            intro = []
            for ln in lines:
                hit = RESEARCH_ITEM.match(ln)
                if not hit:
                    if not research["items"]:
                        intro.append(ln)
                    elif ln.strip():  # 項目底下的續行併進上一項
                        research["items"][-1]["raw"] += "\n" + ln.strip()
                    continue
                research["items"].append({"n": int(hit.group(1)), "raw": hit.group(2).strip()})
            for it in research["items"]:
                raw = it.pop("raw")
                title = re.split(r"[（(—]|\s-\s", raw, 1)[0].strip() or raw[:40]
                it.update(title=html.escape(title[:80]), detail=_rich(raw),
                          done=bool(re.search(r"狀態[:：]\s*(已)?完成|［完成］|\[完成\]", raw)))
            research["intro"] = _rich(" ".join(x.strip() for x in intro if x.strip()))
            continue
        stage = md.get("stage") or "implementing"
        try:
            epoch = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except Exception:
            epoch = 0
        card, rest = {"名稱": "", "主管": "", "做事": "", "現在": ""}, []
        steps = []
        for ln in text.splitlines():
            f, s = WB_FIELD.match(ln), WB_STEP.match(ln)
            if f and not card[f.group(1)]:
                card[f.group(1)] = f.group(2)[:120]
            elif s:
                steps.append({"t": _rich(s.group(2)[:120]), "done": s.group(1) not in WB_NOT_DONE})
            else:
                rest.append(ln)
        detail = "\n".join(rest).strip()
        first = detail.splitlines()[0] if detail else ""
        blocked, why = _blocked(text)
        start = _tpe_date(m.get("created_at") or ts)
        end = _tpe_date(ts) if stage == "done" else dt.datetime.now(TPE).date()
        statuses.append({"project": md.get("project") or "?", "stage": stage, "stage_label": STAGE_LABEL.get(stage, stage),
                         "who": who, "whoc": _who(who), "when": _rel(ts), "tw": _tw(ts), "epoch": epoch,
                         "summary": _rich(first[:140] + ("…" if len(first) > 140 else "")), "text": _rich(_breaks(text)),
                         "name": html.escape(card["名稱"]), "lead": html.escape(card["主管"]), "doers": html.escape(card["做事"]),
                         "now": _rich(card["現在"]), "steps": steps[:30], "detail": _rich(_breaks(detail)),
                         "day": (end - start).days + 1 if start and end else 0,
                         "since": start.strftime("%m-%d") if start else "", "updates": _status_updates(m),
                         "pingpong": _pingpong(md), "blocked_on_user": blocked, "blocked_why": html.escape(why),
                         "priority": _priority(md)})
    statuses.sort(key=lambda s: (s["stage"] == "done", -s["epoch"]))
    return statuses, research


UI_DIR = os.environ.get("UI_DIR", "/ui")
_ui_lock = threading.RLock()
_UI_FILES = ("board.html", "memory.html", "versions.html")
_last_rollback = {}


def _ui_file(which: str) -> str:
    return os.path.join(UI_DIR, which, "board.html")


def _ui_load_snapshot(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("schema") != 1 or set(data.get("files", {})) != set(_UI_FILES):
        raise ValueError("整批版本備份格式不符")
    return {n: None if data["files"][n] is None else base64.b64decode(data["files"][n], validate=True)
            for n in _UI_FILES}


def _ui_save_snapshot(path, snapshot):
    data = {"schema": 1, "files": {n: None if v is None else base64.b64encode(v).decode("ascii")
                                  for n, v in snapshot.items()}}
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(path + ".tmp", path)
    finally:
        if os.path.exists(path + ".tmp"):
            os.unlink(path + ".tmp")


def _ui_write_snapshot(snapshot):
    for name in _UI_FILES:
        path = os.path.join(UI_DIR, "stable", name)
        content = snapshot[name]
        try:
            if content is None:
                if os.path.exists(path):
                    os.unlink(path)
            else:
                with open(path + ".tmp", "wb") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(path + ".tmp", path)
        finally:
            if os.path.exists(path + ".tmp"):
                os.unlink(path + ".tmp")


def _ui_recover():
    journal = os.path.join(UI_DIR, "stable", "ui.transaction.json")
    if os.path.isfile(journal):
        _ui_write_snapshot(_ui_load_snapshot(journal))
        os.unlink(journal)


def _ui_snapshot(which):
    with _ui_lock:
        if which == "stable":
            _ui_recover()
        result = {}
        for name in _UI_FILES:
            path = os.path.join(UI_DIR, which, name)
            if os.path.exists(path) and not os.path.isfile(path):
                raise ValueError("版本頁面不是一般檔案")
            with open(path, "rb") if os.path.isfile(path) else contextlib.nullcontext(None) as f:
                result[name] = f.read() if f else None
        return result


def _ui_release_id(snapshot):
    identities = []
    for name, kind in zip(_UI_FILES, ("board", "memory", "release")):
        content = snapshot[name]
        if content is None:
            raise ValueError("公告欄、記憶頁與版本紀錄須一起備齊")
        version = re.search(rb'<meta name="' + kind.encode() + rb'-version" content="([^"]+)"', content)
        revision = re.search(rb'<meta name="' + kind.encode() + rb'-revision" content="([^"]+)"', content)
        if not version or not revision:
            raise ValueError("版本頁面缺少版號或修訂編號")
        identities.append((version[1], revision[1]))
    if len(set(identities)) != 1:
        raise ValueError("三個頁面的版號或修訂編號不一致")
    return identities[0]


def _ui_backup_entries():
    folder = os.path.join(UI_DIR, "stable")
    if not os.path.isdir(folder):
        return []
    pattern = r"(?:ui\.(?:bak|rolledback)-\d{8}-\d{6}-\d{6}-[a-f0-9]{8}|board\.html\.(?:bak|rolledback)-\d{8}-\d{6}(?:-\d{6})?)"
    return sorted((e.name for e in os.scandir(folder)
                   if e.is_file(follow_symlinks=False) and re.fullmatch(pattern, e.name)),
                  key=lambda n: n.split("-", 1)[1])


def _ui_prune_backups():
    # Prefer complete rollback history; retain newest entries within each priority.
    entries = sorted(_ui_backup_entries(), key=lambda name: name.startswith("ui.bak-"))
    for name in entries[:-20]:
        os.unlink(os.path.join(UI_DIR, "stable", name))
        _ui_bak_meta.pop(name, None)


_ui_bak_meta = {}  # 備份檔名 → (內部流水號, 版號)；備份檔寫好就不會再改，讀一次就記住


def _ui_bak_rev(name):
    if name not in _ui_bak_meta:
        try:
            version, revision = _ui_release_id(_ui_load_snapshot(os.path.join(UI_DIR, "stable", name)))
            _ui_bak_meta[name] = (int(revision), re.sub(r"-rc\.\d+$", "", version.decode("utf-8")))
        except Exception:
            _ui_bak_meta[name] = None
    return _ui_bak_meta[name]


def _ui_stable_rev(stable):
    try:
        return int(_ui_release_id(stable)[1])
    except Exception:
        return None


def _ui_targets(current):
    """可以回滾到的版本：內部流水號 → (備份檔名, 版號)。同一號有好幾份就用最新那份，不含現在的正式版。"""
    out = {}
    for name in _ui_backup_entries():  # 依時間排，後面的蓋掉前面的
        meta = _ui_bak_rev(name) if name.startswith("ui.bak-") else None
        if meta and meta[0] != current:
            out[meta[0]] = (name, meta[1])
    return out


def _ui_dedupe_backups():
    """同一版（內部流水號一樣、內容也一樣）的備份只留最新那份，免得 20 份的額度被重複的吃掉。"""
    keep = {}
    for name in reversed([n for n in _ui_backup_entries() if n.startswith("ui.bak-")]):
        meta = _ui_bak_rev(name)
        if not meta:
            continue
        if meta[0] not in keep:
            keep[meta[0]] = name
            continue
        path, newer = (os.path.join(UI_DIR, "stable", n) for n in (name, keep[meta[0]]))
        if _ui_load_snapshot(path) == _ui_load_snapshot(newer):
            os.unlink(path)
            _ui_bak_meta.pop(name, None)


def _ui_stamp():
    return dt.datetime.now(LOCAL_TZ).strftime("%Y%m%d-%H%M%S-%f") + "-" + secrets.token_hex(4)


def _ui_apply(snapshot, previous):
    journal = os.path.join(UI_DIR, "stable", "ui.transaction.json")
    _ui_save_snapshot(journal, previous)
    try:
        _ui_write_snapshot(snapshot)
    except Exception:
        # A failed restore leaves the journal in place; readers retry recovery before serving a page.
        _ui_write_snapshot(previous)
        os.unlink(journal)
        raise
    os.unlink(journal)


def _ui_fingerprint(snapshot):
    return hashlib.sha256(json.dumps({n: None if v is None else base64.b64encode(v).decode("ascii")
                                     for n, v in snapshot.items()}, sort_keys=True).encode()).hexdigest()


def _ui_unrecorded(preview) -> str:
    """放預覽要走 tools/publish_preview.py（加修訂號、寫更新紀錄、記下三頁 SHA）；
    之後有人直接改預覽檔，SHA 就對不上，回傳給使用者看的原因。沒有登記檔時不檢查。"""
    path = os.path.join(UI_DIR, "preview", "publish-record.json")
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            want = json.load(f)["sha256"]
    except Exception:
        return "放預覽登記檔讀不到，請 AI 用 publish_preview.py 重放一次"
    for name in _UI_FILES:
        have = None if preview[name] is None else hashlib.sha256(preview[name]).hexdigest()
        if want.get(name) != have:
            return "預覽版有改動還沒寫進版本更新紀錄，請 AI 用 publish_preview.py 補上後再採用"
    return ""


def _ui_blocked(preview) -> str:
    try:
        _ui_release_id(preview)
    except ValueError as e:
        return str(e)
    return _ui_unrecorded(preview)


def _ui_state() -> dict:
    with _ui_lock:
        preview, stable = _ui_snapshot("preview"), _ui_snapshot("stable")
        current = preview == stable
        blocked = "" if current else _ui_blocked(preview)
        rev = _ui_stable_rev(stable)
        targets = _ui_targets(rev)
        return {"has_stable": any(v is not None for v in stable.values()),
                "can_promote": not current and not blocked, "current": current, "blocked": blocked,
                "can_rollback": any(rev is None or r < rev for r in targets), "stable_rev": rev,
                "rollback_to": [{"rev": r, "version": targets[r][1]} for r in sorted(targets, reverse=True)]}


def _ui_promote() -> str:
    """One release includes the board, rendered-memory template, and release history."""
    with _ui_lock:
        preview, stable = _ui_snapshot("preview"), _ui_snapshot("stable")
        _ui_release_id(preview)
        if preview == stable:
            return "already-current"
        unrecorded = _ui_unrecorded(preview)
        if unrecorded:
            raise ValueError(unrecorded)
        os.makedirs(os.path.join(UI_DIR, "stable"), exist_ok=True)
        stamp = _ui_stamp()
        backup = os.path.join(UI_DIR, "stable", "ui.bak-" + stamp)
        _ui_save_snapshot(backup, stable)
        try:
            _ui_apply(preview, stable)
        except Exception:
            if not os.path.exists(os.path.join(UI_DIR, "stable", "ui.transaction.json")):
                os.unlink(backup)
            raise
        _last_rollback.clear()
        _ui_dedupe_backups()
        _ui_prune_backups()
        return stamp


def _ui_rollback(rev=None) -> str:
    """把正式版三頁整批換成某一版的備份（rev＝那一版的內部流水號）；沒指定就退到比現在舊的最新一版。
    換之前把現在的正式版也存成備份，退錯了可以從更新紀錄再換回來。只換網頁，不動伺服器程式和資料。"""
    with _ui_lock:
        stable = _ui_snapshot("stable")
        current = _ui_stable_rev(stable)
        if rev is not None and rev == current:
            return "already-current"
        identity = _ui_fingerprint(stable)
        now, root = time.monotonic(), os.path.realpath(UI_DIR)
        if (rev is None and _last_rollback.get("root") == root and _last_rollback.get("fingerprint") == identity
                and now - _last_rollback.get("at", 0) < 5):
            return "already-current"
        targets = _ui_targets(current)
        if rev is None:
            older = [r for r in targets if current is None or r < current]
            if not older:
                raise ValueError("沒有比現在更舊、可以回滾的備份")
            rev = max(older)
        if rev not in targets:
            raise ValueError("找不到這一版的備份（備份最多留 20 份），沒辦法回滾到這一版")
        target = _ui_load_snapshot(os.path.join(UI_DIR, "stable", targets[rev][0]))
        stamp = _ui_stamp()
        keep = os.path.join(UI_DIR, "stable", "ui.bak-" + stamp)
        _ui_save_snapshot(keep, stable)
        try:
            _ui_apply(target, stable)
        except Exception:
            if not os.path.exists(os.path.join(UI_DIR, "stable", "ui.transaction.json")):
                os.unlink(keep)
            raise
        _last_rollback.update(root=root, at=time.monotonic(), fingerprint=_ui_fingerprint(target))
        _ui_dedupe_backups()
        _ui_prune_backups()
        return stamp


def _ui_html(which, name):
    content = _ui_snapshot(which)[name]
    if content is None:
        return None
    text = content.decode("utf-8")
    text = text.replace('<meta charset="utf-8">', '<meta charset="utf-8"><meta name="rt-tz-offset" content="%g">' % TZ_HOURS, 1)
    if which == "stable":
        text = text.replace('href="/board/preview/versions"', 'href="/board/versions"')
        text = text.replace('本次候選', '目前版本').replace('候選・尚未部署', '已共同採用', 2)
        if name != "board.html":
            text = text.replace('href="/board/preview/memory"', 'href="/board/memory"').replace('href="/board/preview"', 'href="/board"')
            text = text.replace('記憶頁預覽', '記憶頁正式版').replace('公告欄預覽', '公告欄')
            text = text.replace('href="/board/memory">看正式記憶頁', 'href="/board/preview/memory">看記憶頁預覽')
            text = text.replace('href="/board/memory">正式版', 'href="/board/preview/memory">預覽版')
            text = text.replace('<title>圓桌記憶｜預覽</title>', '<title>圓桌記憶｜正式版</title>')
            text = text.replace('href="/board">正式公告欄', 'href="/board/preview">公告欄預覽')
            text = text.replace('只改顯示，不改記憶內容。採用與回滾會和公告欄一起進行。', '公告欄、記憶頁與版本紀錄已共同採用。')
    return text


# 換版後還開著的舊頁面不能再寫入。
# 送頁面時在 <head> 塞一段小程式，頁面之後送出的每個寫入都帶上「哪一種頁面:內部流水號」；
# 伺服器拿去比對那一種頁面現在的流水號，對不上就回 409、什麼都不寫。部署前就開著的分頁沒有這段程式、
# 帶不出版號，一樣擋下。伺服器自己產生的舊式頁面沒有流水號，改用這次啟動的代號，重啟後也算舊版。
BOARD_REV_CHECK = os.environ.get("BOARD_REV_CHECK", "1") != "0"  # 設 0 整個關掉，回到不檢查
_BOOT = secrets.token_hex(4)
_REV_WRITE_PATHS = ("/board/send", "/board/upload", "/board/promote", "/board/rollback", "/board/api/avatar",
                    "/board/preview/vote", "/board/preview/close", "/board/preview/recall")
_REV_WRITE_PREFIXES = ("/board/api/review/", "/board/api/memory/", "/board/api/experience/", "/board/api/project/")
_REV_META = re.compile(r'<meta name="(?:board|memory|release)-revision" content="(\d+)"')
_REV_GUARD_JS = """<script>(function(){var T=%s,F=window.fetch;
function stale(){if(document.getElementById('board-stale'))return;var d=document.createElement('div');d.id='board-stale';d.setAttribute('role','alert');
d.style.cssText='position:fixed;top:0;left:0;right:0;z-index:2147483647;padding:10px 16px;background:#b42318;color:#fff;font:15px/1.5 system-ui,sans-serif;display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;justify-content:center';
d.appendChild(document.createTextNode('這個頁面是舊版，剛才的動作沒有送出。寫到一半的字先複製起來，再重新載入。'));
var b=document.createElement('button');b.type='button';b.textContent='重新載入';b.style.cssText='font:inherit;padding:4px 14px;border:0;border-radius:8px;cursor:pointer';
b.onclick=function(){location.reload()};d.appendChild(b);(document.body||document.documentElement).appendChild(d);b.focus()}
window.fetch=function(u,o){o=o||{};var m=String(o.method||(u&&u.method)||'GET').toUpperCase();
if(m!=='GET'&&m!=='HEAD'){var h=new Headers(o.headers||(u&&u.headers)||{});h.set('X-Board-Rev',T);o=Object.assign({},o,{headers:h})}
return F.call(this,u,o).then(function(r){if(r.status===409&&r.headers.get('X-Board-Stale'))stale();return r})};
document.addEventListener('submit',function(e){var f=e.target;if(!f||String(f.method).toLowerCase()!=='post')return;
var i=f.querySelector('input[name=_board_rev]');if(!i){i=document.createElement('input');i.type='hidden';i.name='_board_rev';f.appendChild(i)}i.value=T},true)})();</script>"""


def _page_token(which, text):
    """這個頁面的版號：正式版／預覽版的頁面用內部流水號，伺服器自己產生的舊式頁面用這次啟動的代號。"""
    m = _REV_META.search(text)
    return f"{which}:{m[1]}" if m and which in ("stable", "preview") else "legacy:" + _BOOT


def _rev_guard(text, which):
    """把帶版號的小程式塞進 <head> 最前面，要比頁面自己的程式先跑。"""
    script = _REV_GUARD_JS % json.dumps(_page_token(which, text))
    m = re.search(r"<head[^>]*>", text, re.I)
    return text[:m.end()] + script + text[m.end():] if m else script + text


def _rev_stale(token) -> str:
    """寫入請求帶的版號是不是現在的版本；是就回空字串，不是就回給使用者看的原因。"""
    which, _, rev = (token or "").partition(":")
    if which in ("stable", "preview"):
        snap = _ui_snapshot(which)
        now = {m[1] for c in snap.values() if c is not None for m in [_REV_META.search(c.decode("utf-8", "replace"))] if m}
        if rev in now:
            return ""
        ver = re.search(rb'<meta name="board-version" content="([^"]+)"', snap.get("board.html") or b"")
        label = "正式版" if which == "stable" else "預覽版"
        return (f"這個頁面是舊版，{label}已經換成 {ver[1].decode() if ver else '新版'}。"
                "剛才的動作沒有送出，請重新載入後再做一次（寫到一半的字先複製起來）。")
    if which == "legacy" and rev == _BOOT:
        return ""
    return "這個頁面是更新前打開的，剛才的動作沒有送出。請重新載入後再做一次（寫到一半的字先複製起來）。"


BOARD_TEMPLATE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>圓桌公告欄</title><style>
:root{{--bg:#f4f5f7;--card:#fff;--tx:#161b26;--mut:#5b6577;--bd:#dde1e8;--ac:#2456d6;--hot:#c2410c;--hotbg:#fff3ea;
--c-claude:#2456d6;--c-codex:#0f7a4b;--c-hermes:#7a3fc4;--c-user:#b45309;--c-other:#5b6577}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0f131a;--card:#171c26;--tx:#e8ebf1;--mut:#a0a9ba;--bd:#2a3342;--ac:#7aa2ff;--hot:#ff9a5c;--hotbg:#2a1d14;
--c-claude:#7aa2ff;--c-codex:#4fd39b;--c-hermes:#c39bff;--c-user:#ffb454;--c-other:#a0a9ba}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--tx);font:16px/1.65 system-ui,-apple-system,"Segoe UI","Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif}}
main{{max-width:760px;margin:0 auto;padding:20px 16px 80px}}
header{{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 14px;margin-bottom:6px}}h1{{font-size:24px;margin:0;letter-spacing:.02em}}
.sum{{color:var(--mut);font-size:14px}}.sum .hot{{color:var(--hot);font-weight:700}}
h2{{font-size:13px;font-weight:700;color:var(--mut);letter-spacing:.12em;margin:28px 0 10px}}
.panel{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:6px 14px}}
ul{{list-style:none;margin:0;padding:0}}.claim{{padding:10px 0;border-top:1px solid var(--bd)}}.claim:first-child{{border-top:0}}
.claim.old{{opacity:.6}}.ct{{display:flex;flex-wrap:wrap;align-items:center;gap:8px}}.tg{{font-size:17px;font-family:ui-monospace,Consolas,monospace}}
.cs{{color:var(--mut);font-size:14px;margin-top:2px;overflow-wrap:anywhere}}.none{{padding:12px 0;color:var(--mut)}}
.who{{display:inline-block;font-size:13px;font-weight:700;padding:1px 9px;border-radius:99px;border:1px solid currentColor}}
.w-claude{{color:var(--c-claude)}}.w-codex{{color:var(--c-codex)}}.w-hermes{{color:var(--c-hermes)}}.w-user{{color:var(--c-user)}}.w-other{{color:var(--c-other)}}
.when{{margin-left:auto;color:var(--mut);font-size:13px;white-space:nowrap}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 4px}}.chip{{font:inherit;font-size:14px;padding:5px 14px;border-radius:99px;border:1px solid var(--bd);background:var(--card);color:var(--tx);cursor:pointer}}
.chip.on{{background:var(--ac);border-color:var(--ac);color:#fff}}.chip:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,summary:focus-visible{{outline:2px solid var(--ac);outline-offset:2px}}
.day{{font-size:13px;color:var(--mut);margin:22px 0 8px;display:flex;align-items:center;gap:10px}}.day:after{{content:"";flex:1;height:1px;background:var(--bd)}}
.msg{{display:flex;gap:12px;background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:14px;margin:10px 0}}
.msg.urgent{{border-color:var(--hot);background:var(--hotbg)}}.av{{flex:none;width:38px;height:38px;border-radius:50%;display:grid;place-items:center;font-weight:800;border:2px solid currentColor}}
.mc{{min-width:0;flex:1}}.line{{display:flex;flex-wrap:wrap;align-items:center;gap:6px}}.arrow{{color:var(--mut)}}
h3{{font-size:17px;line-height:1.45;margin:6px 0 2px;overflow-wrap:anywhere}}.b{{white-space:pre-wrap;overflow-wrap:anywhere;opacity:.88;margin-top:4px}}
.ref{{font-size:13px;color:var(--mut);margin-top:6px;overflow-wrap:anywhere}}.tags{{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}}
.tag{{font-size:12.5px;color:var(--mut);border:1px solid var(--bd);border-radius:6px;padding:0 8px}}.tag.hot{{color:var(--hot);border-color:var(--hot);font-weight:700}}
details{{margin-top:22px}}summary{{cursor:pointer;font-weight:700;padding:10px 14px;background:var(--card);border:1px solid var(--bd);border-radius:12px}}
form{{display:grid;gap:10px;padding:14px;background:var(--card);border:1px solid var(--bd);border-top:0;border-radius:0 0 12px 12px}}
input,select,textarea,.send{{font:inherit;padding:10px;border-radius:8px;border:1px solid var(--bd);background:var(--bg);color:var(--tx)}}
.send{{background:var(--ac);border-color:var(--ac);color:#fff;font-weight:700;cursor:pointer}}[hidden]{{display:none!important}}
@media(max-width:480px){{.msg{{padding:12px;gap:10px}}.av{{width:32px;height:32px}}.when{{margin-left:0}}}}
nav{{display:flex;gap:6px;margin-bottom:10px}}nav a{{padding:6px 16px;border-radius:99px;border:1px solid var(--bd);background:var(--card);color:var(--tx);text-decoration:none;font-size:15px}}
nav a.on{{background:var(--ac);border-color:var(--ac);color:#fff}}
.b a,.cs a,h3 a{{color:var(--ac);text-decoration:underline;overflow-wrap:anywhere}}
.b{{line-height:1.85}}pre.blk{{margin:10px 0 2px;padding:10px 12px;background:var(--bg);border:1px solid var(--bd);border-left:4px solid var(--ac);border-radius:8px;overflow-x:auto;white-space:pre}}
pre.blk code{{border:0;padding:0;background:none}}
code{{font:13.5px ui-monospace,Consolas,monospace;background:var(--bg);border:1px solid var(--bd);border-radius:5px;padding:0 5px;overflow-wrap:anywhere}}
.fab{{position:fixed;right:max(16px,env(safe-area-inset-right,0px));bottom:max(16px,env(safe-area-inset-bottom,0px));z-index:6;background:var(--ac);color:#fff;border:0;border-radius:99px;padding:12px 20px;font:inherit;font-weight:700;box-shadow:0 4px 16px rgba(0,0,0,.35);cursor:pointer}}
.drawer{{position:fixed;top:0;right:0;bottom:0;z-index:7;width:min(400px,100vw);background:var(--card);border-left:1px solid var(--bd);box-shadow:-8px 0 28px rgba(0,0,0,.35);padding:calc(14px + env(safe-area-inset-top,0px)) 16px 16px;overflow-y:auto}}
.dh{{display:flex;align-items:baseline;gap:8px;margin-bottom:12px}}.dsub{{color:var(--mut);font-size:13px}}.dx{{margin-left:auto;font:inherit;background:none;border:1px solid var(--bd);border-radius:8px;color:var(--tx);padding:2px 10px;cursor:pointer}}
.drawer form{{padding:0;border:0;background:none;border-radius:0}}
</style></head><body><main>
<nav><a href="/board" class="on">公告欄</a><a href="/board/memory">記憶總覽</a><a href="/board/preview">預覽新版</a></nav>
<header><h1>圓桌公告欄</h1><div class="sum">{n_claims} 台機器有人在動　<span class="hot">{n_todo} 則待處理</span>　共 {n_msgs} 則留言　當地時間，每 20 秒自動更新</div></header>
<h2>誰正在動哪台機器</h2><div class="panel"><ul>{claims}</ul></div>
<h2>留言</h2><div class="chips">{chips}</div>
{rows}
<button type="button" class="fab" id="fab">✉ 留言給 AI</button>
<aside class="drawer" id="drawer" hidden aria-label="留言給 AI"><div class="dh"><b>留言給 AI</b><span class="dsub">以 user 身分送出</span><button type="button" class="dx" id="dx" aria-label="關閉">✕</button></div>
<form method="post" action="/board/send">
<select name="to" id="to" aria-label="收件人"><option>all</option>{opts}</select>
<input name="subject" id="subject" maxlength="80" placeholder="一句話講結論（80 字內）" required>
<textarea name="body" id="body" rows="6" maxlength="600" placeholder="補充（可不填）"></textarea><button class="send">送出</button></form></aside>
</main><script>
const chips=document.querySelectorAll('.chip');chips.forEach(c=>c.onclick=()=>{{chips.forEach(x=>x.classList.remove('on'));c.classList.add('on');
const f=c.dataset.f;document.querySelectorAll('.msg').forEach(m=>m.hidden=!!f&&m.dataset.from!==f&&m.dataset.to!==f)}});
const dr=document.getElementById('drawer'),fab=document.getElementById('fab');
function openDr(){{dr.hidden=false;fab.hidden=true;document.getElementById('subject').focus()}}
function closeDr(){{dr.hidden=true;fab.hidden=false}}
fab.onclick=openDr;document.getElementById('dx').onclick=closeDr;
document.addEventListener('keydown',e=>{{if(e.key==='Escape'&&!dr.hidden)closeDr()}});
setInterval(()=>{{const a=document.activeElement;if((!a||a.tagName==='BODY'||a.classList.contains('chip'))&&dr.hidden)location.reload()}},20000)
</script></body></html>"""


TYPE_LABEL = {"message": "訊息", "decision": "決策", "note": "筆記", "status": "進度", "claim": "告示牌"}
TYPE_ORDER = ["message", "decision", "note", "status", "claim"]
TAG_STRIP = re.compile(r"^(\s*\[[^\]]{1,6}\]\s*)+")


def _avatar_info(name: str) -> tuple[str, int]:
    labels = {"user": "人"}
    hue = 0
    for char in name:
        hue = (hue * 31 + ord(char)) % 360
    return labels.get(name, name[:2].upper() or "?"), hue


def _preview_tw(ts) -> str:
    try:
        value = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "時間未記錄"


LEGACY_CODEX_DEVICE: dict = {}  # 舊資料作者更正表（記憶編號 → 身分），只改顯示、不改資料


def _group_label(to: str) -> str:
    """群組地址（codex＝所有 codex-*）顯示成「全部 Codex」，其他照原樣；好幾位用「、」接起來。"""
    if len(_tos(to)) > 1:
        return "、".join(_group_label(x) for x in _tos(to))
    return f"全部 {to[:1].upper()}{to[1:]}" if to in _groups() and to not in set(TOKENS.values()) else to


FOOT_PARTS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) 使用者補充：(.*)$")


def _feet_html(feet: list) -> str:
    """記憶底下的註腳：日期＋「使用者補充」，小字排在原文下面；沒有註腳也留一個空框給頁面加。"""
    out = []
    for x in feet:
        p = FOOT_PARTS_RE.match(x)
        if p:
            out.append(f'<div class="foot"><span class="fd">{int(p[2])}/{int(p[3])} 使用者補充</span>'
                       f'<span class="ft">{_rich(p[4])}</span></div>')
    return f'<div class="feet">{"".join(out)}</div>'


def _memory_html(preview: bool = False) -> str:
    esc = html.escape
    template = _ui_html("preview" if preview else "stable", "memory.html")
    if preview and template is None:
        raise ValueError("還沒有記憶頁預覽版")
    modern = template is not None
    template = template if modern else MEMORY_TEMPLATE
    feet_ok = modern and 'name="memory-footnotes"' in template  # 頁面版本支援註腳才分開顯示（舊的正式版照舊）
    items = _list_all()
    rows, agents, projects = [], set(), set()
    counts = {t: 0 for t in TYPE_ORDER}
    for m in items:
        md = m.get("metadata") or {}
        t = md.get("type") or "note"
        if t not in TYPE_LABEL:
            t = "note"
        counts[t] += 1
        who = md.get("from") or m.get("agent_id") or "?"
        legacy = who == "codex" and m.get("id") in LEGACY_CODEX_DEVICE
        if legacy:
            who = LEGACY_CODEX_DEVICE[m["id"]]
        old_name = who if _canon(who) != who else ""  # 改名前的舊名（claude）顯示成正式名，同一台機器一個代表
        who = _canon(who)
        proj = md.get("project") or "general"
        if who != "codex":  # 判斷不出是哪一台的舊 codex 不進作者清單，搜尋仍找得到
            agents.add(who)
        projects.add(proj)
        ts = m.get("updated_at") or m.get("created_at")
        try:
            epoch = int(dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp())
        except Exception:
            epoch = 0
        text = TAG_STRIP.sub("", m.get("memory") or "")
        if t == "message" and md.get("subject"):
            body_html = f'<div class="sub">{_rich(md.get("subject"))}</div>'
            if md.get("body"):
                body_html += f'<div class="bd">{_rich(_breaks(md.get("body")))}</div>'
            text = f'{md.get("subject")} {md.get("body") or ""}'
        elif feet_ok and t in EDIT_TYPES:  # 原文照常，使用者加的註腳排在底下
            main, feet = _foot_split(text)
            body_html = f'<div class="bd">{_rich(_breaks(main))}</div>{_feet_html(feet)}'
        else:
            body_html = f'<div class="bd">{_rich(_breaks(text))}</div>'
        extra = ""
        if t == "message":
            extra = f'<span class="tag">寄給 {esc(_group_label(md.get("to") or "?"))}</span>'
            readers = _readers(m)
            if modern:
                extra += '<span class="tag ack">' + ("AI已確認：" + esc("、".join(readers)) if readers else "尚無AI確認") + '</span>'
        if t == "claim":
            extra = f'<span class="tag">機器 {esc(md.get("target") or "?")}</span>'
        if t == "status" and md.get("stage"):
            extra = f'<span class="tag">階段 {esc(md.get("stage"))}</span>'
        stamp = ""
        if modern:
            stamp = f'<div class="stamp" style="color:var(--mut);font-size:12.5px;font-variant-numeric:tabular-nums;margin-bottom:2px">{esc(_preview_tw(ts))}</div>'
        avatar, content_open, content_close = "", "", ""
        if modern:
            display_who = "codex（裝置未記錄）" if who == "codex" else who + ("（10/13 舊身分）" if legacy else "") + (f"（舊名 {old_name}）" if old_name else "")
            if t in ("status", "claim") and md.get("updated_by"):
                display_who += " · 更新者 " + str(md["updated_by"])
            label, hue = _avatar_info(who)
            avatar = f'<div class="avatar" style="background:hsl({hue},55%,38%)" aria-hidden="true">{esc(label)}</div>'
            content_open, content_close = '<div class="content">', '</div>'
        mid_attr = f'data-id="{esc(m.get("id") or "")}" ' if feet_ok and t in EDIT_TYPES else ""
        rows.append(
            f'<article class="mem t-{t}" {mid_attr}data-type="{t}" data-agent="{esc(who)}" data-project="{esc(proj)}" data-ts="{epoch}" '
            f'data-text="{esc((text + " " + proj + " " + who).lower())}">{avatar}{content_open}{stamp}<div class="mh">'
            f'<span class="ty">{TYPE_LABEL[t]}</span><span class="who w-{_who(who)}">{esc(display_who if modern else who)}</span>'
            f'<span class="tag">{esc(proj)}</span>{extra}<span class="when" title="{_tw(ts)}">{_rel(ts)}</span></div>'
            f'<div class="mt">{body_html}</div>{content_close}</article>')
    chips = "".join(
        f'<button type="button" class="tchip t-{t} on" data-t="{t}">{TYPE_LABEL[t]} <b>{counts[t]}</b></button>' for t in TYPE_ORDER)
    ag = "".join(f"<option>{esc(a)}</option>" for a in sorted(agents))
    pj = "".join(f"<option>{esc(p)}</option>" for p in sorted(projects))
    return (template.replace("@@CHIPS@@", chips).replace("@@AGENTS@@", ag).replace("@@PROJECTS@@", pj)
            .replace("@@ROWS@@", "".join(rows)).replace("@@TOTAL@@", str(len(items))))


MEMORY_TEMPLATE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>圓桌記憶總覽</title><style>
:root{--bg:#f4f5f7;--card:#fff;--tx:#161b26;--mut:#5b6577;--bd:#dde1e8;--ac:#2456d6;
--c-claude:#2456d6;--c-codex:#0f7a4b;--c-hermes:#7a3fc4;--c-user:#b45309;--c-other:#5b6577;
--m-bg:#e8f0ff;--m-bd:#9db8f2;--d-bg:#fff4d6;--d-bd:#e5c25a;--n-bg:#e6f6ec;--n-bd:#8fcfa6;--s-bg:#f1e8fb;--s-bd:#c3a3e6;--c-bg:#ffe9e0;--c-bd:#f0a98a}
@media(prefers-color-scheme:dark){:root{--bg:#0f131a;--card:#171c26;--tx:#e8ebf1;--mut:#a0a9ba;--bd:#2a3342;--ac:#7aa2ff;
--c-claude:#7aa2ff;--c-codex:#4fd39b;--c-hermes:#c39bff;--c-user:#ffb454;--c-other:#a0a9ba;
--m-bg:#16233f;--m-bd:#2f4f94;--d-bg:#33290f;--d-bd:#7a6420;--n-bg:#12301f;--n-bd:#2d6b47;--s-bg:#2a1d3d;--s-bd:#5d3f8a;--c-bg:#3a1f14;--c-bd:#8a4a2e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:16px/1.65 system-ui,-apple-system,"Segoe UI","Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 80px}nav{display:flex;gap:6px;margin-bottom:10px}
nav a{padding:6px 16px;border-radius:99px;border:1px solid var(--bd);background:var(--card);color:var(--tx);text-decoration:none;font-size:15px}
nav a.on{background:var(--ac);border-color:var(--ac);color:#fff}h1{font-size:24px;margin:6px 0 2px}.sum{color:var(--mut);font-size:14px}
.bar{display:grid;gap:10px;margin:16px 0;padding:12px;background:var(--card);border:1px solid var(--bd);border-radius:12px;position:sticky;top:env(safe-area-inset-top,0px);z-index:2}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input,select,button{font:inherit;padding:8px 10px;border-radius:8px;border:1px solid var(--bd);background:var(--bg);color:var(--tx)}
input[type=search]{flex:1;min-width:160px}button{cursor:pointer}
.tchip{border-radius:99px;padding:5px 14px;font-size:14px;opacity:.45}.tchip.on{opacity:1}
.tchip.t-message{background:var(--m-bg);border-color:var(--m-bd)}.tchip.t-decision{background:var(--d-bg);border-color:var(--d-bd)}
.tchip.t-note{background:var(--n-bg);border-color:var(--n-bd)}.tchip.t-status{background:var(--s-bg);border-color:var(--s-bd)}.tchip.t-claim{background:var(--c-bg);border-color:var(--c-bd)}
.mem{border:1px solid var(--bd);border-radius:12px;padding:12px 14px;margin:10px 0}
.t-message.mem{background:var(--m-bg);border-color:var(--m-bd)}.t-decision.mem{background:var(--d-bg);border-color:var(--d-bd)}
.t-note.mem{background:var(--n-bg);border-color:var(--n-bd)}.t-status.mem{background:var(--s-bg);border-color:var(--s-bd)}.t-claim.mem{background:var(--c-bg);border-color:var(--c-bd)}
.mh{display:flex;flex-wrap:wrap;align-items:center;gap:6px}.ty{font-weight:800;font-size:14px;letter-spacing:.08em}
.who{font-size:13px;font-weight:700;padding:1px 9px;border-radius:99px;border:1px solid currentColor}
.w-claude{color:var(--c-claude)}.w-codex{color:var(--c-codex)}.w-hermes{color:var(--c-hermes)}.w-user{color:var(--c-user)}.w-other{color:var(--c-other)}
.tag{font-size:12.5px;color:var(--mut);border:1px solid var(--bd);border-radius:6px;padding:0 8px;background:var(--card)}
.when{margin-left:auto;color:var(--mut);font-size:13px;white-space:nowrap}.mt{margin-top:6px;white-space:pre-wrap;overflow-wrap:anywhere}
[hidden]{display:none!important}:focus-visible{outline:2px solid var(--ac);outline-offset:2px}.cnt{color:var(--mut);font-size:14px}
@media(max-width:480px){.when{margin-left:0}}
.mt a{color:var(--ac);text-decoration:underline;overflow-wrap:anywhere}
code{font:13.5px ui-monospace,Consolas,monospace;background:var(--card);border:1px solid var(--bd);border-radius:5px;padding:0 5px;overflow-wrap:anywhere}
.mt .sub{font-weight:700;font-size:17px;line-height:1.5}.mt .bd{margin-top:8px;line-height:1.85}.mt .sub+.bd{padding-top:8px;border-top:1px dashed var(--bd)}
pre.blk{margin:10px 0 2px;padding:10px 12px;background:var(--card);border:1px solid var(--bd);border-left:4px solid var(--ac);border-radius:8px;overflow-x:auto;white-space:pre}
pre.blk code{border:0;padding:0;background:none;font-size:13.5px}
</style></head><body><main>
<nav><a href="/board">公告欄</a><a href="/board/memory" class="on">記憶總覽</a></nav>
<h1>圓桌記憶總覽</h1><div class="sum">共 @@TOTAL@@ 筆。底色代表類型：藍＝訊息、黃＝決策、綠＝筆記、紫＝進度、橘＝告示牌。</div>
<div class="bar"><div class="row" id="chips">@@CHIPS@@</div>
<div class="row"><input type="search" id="q" placeholder="搜尋關鍵字" aria-label="搜尋關鍵字">
<select id="ag" aria-label="依人名"><option value="">全部人名</option>@@AGENTS@@</select>
<select id="pj" aria-label="依專案"><option value="">全部專案</option>@@PROJECTS@@</select>
<select id="so" aria-label="排序"><option value="desc">時間：新到舊</option><option value="asc">時間：舊到新</option></select>
<button type="button" id="rs">重設</button><span class="cnt" id="cnt"></span></div></div>
<div id="list">@@ROWS@@</div>
</main><script>
const list=document.getElementById('list'),all=[...list.children],q=document.getElementById('q'),ag=document.getElementById('ag'),
pj=document.getElementById('pj'),so=document.getElementById('so'),cnt=document.getElementById('cnt'),chips=[...document.querySelectorAll('.tchip')];
function apply(){const on=new Set(chips.filter(c=>c.classList.contains('on')).map(c=>c.dataset.t)),k=q.value.trim().toLowerCase();let n=0;
all.sort((a,b)=>so.value==='asc'?a.dataset.ts-b.dataset.ts:b.dataset.ts-a.dataset.ts).forEach(e=>{list.appendChild(e);
const ok=on.has(e.dataset.type)&&(!ag.value||e.dataset.agent===ag.value)&&(!pj.value||e.dataset.project===pj.value)&&(!k||e.dataset.text.includes(k));e.hidden=!ok;if(ok)n++});
cnt.textContent='顯示 '+n+' / '+all.length+' 筆'}
chips.forEach(c=>c.onclick=()=>{c.classList.toggle('on');apply()});[q,ag,pj,so].forEach(e=>e.oninput=apply);
document.getElementById('rs').onclick=()=>{q.value='';ag.value='';pj.value='';so.value='desc';chips.forEach(c=>c.classList.add('on'));apply()};apply();
</script></body></html>"""


"""Pending approval: persistent verifier for one board account; no network."""
import hashlib,hmac,json,os,secrets,stat,tempfile,threading
from pathlib import Path

class BoardPasswordStore:
    def __init__(self, filename, fallback):
        self.path=Path(filename) if filename else None
        self.fallback=fallback
        self.lock=threading.RLock()

    def _record(self):
        if self.path is None:return None
        try:
            st=self.path.lstat()
        except FileNotFoundError:return None
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid!=os.geteuid() or st.st_size>8192:
            raise ValueError('credential store unavailable')
        try:
            fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd,'r',encoding='utf-8') as f:record=json.load(f)
            if set(record)!={'schema','salt','verifier','session_key'} or record['schema']!=1:raise ValueError()
            for key,size in [('salt',16),('verifier',32),('session_key',32)]:
                if len(record[key])!=size*2 or len(bytes.fromhex(record[key]))!=size:raise ValueError()
            return record
        except Exception:raise ValueError('credential store unavailable') from None

    @staticmethod
    def _derive(password,salt):
        return hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1,dklen=32,maxmem=64*1024*1024)

    def verify(self,password):
        if not isinstance(password,str) or len(password.encode())>512:return False
        with self.lock:  # 只在讀檔時持鎖；scrypt 在鎖外算，cookie 檢查不必排在慢的密碼驗證後面
            record=self._record()
        if record is None:return hmac.compare_digest(password.encode(),self.fallback.encode())
        return hmac.compare_digest(self._derive(password,bytes.fromhex(record['salt'])),bytes.fromhex(record['verifier']))

    def sign(self,expiry):
        with self.lock:
            record=self._record()
            key=bytes.fromhex(record['session_key']) if record else (self.fallback+'|board-cookie').encode()
            return hmac.new(key,expiry.encode(),hashlib.sha256).hexdigest()

    def available(self):
        if self.path is None:return False
        try:
            st=self.path.parent.lstat()
            return stat.S_ISDIR(st.st_mode) and not st.st_mode & 0o077 and st.st_uid==os.geteuid()
        except OSError:return False

    def change(self,current,new,confirm):
        with self.lock:
            if not self.available():raise ValueError('credential storage not configured')
            if not self.verify(current):raise ValueError('目前密碼不正確')
            if not isinstance(new,str) or not 12<=len(new.encode())<=128:raise ValueError('新密碼須為12至128個UTF-8位元組')
            if not hmac.compare_digest(new.encode(),confirm.encode()):raise ValueError('兩次新密碼不一致')
            if self.verify(new):raise ValueError('請使用不同的新密碼')
            salt=secrets.token_bytes(16)
            record={'schema':1,'salt':salt.hex(),'verifier':self._derive(new,salt).hex(),'session_key':secrets.token_hex(32)}
            fd,name=tempfile.mkstemp(prefix='.credential-',dir=self.path.parent)
            try:
                os.fchmod(fd,0o600)
                with os.fdopen(fd,'w',encoding='utf-8') as f:
                    json.dump(record,f);f.flush();os.fsync(f.fileno())
                os.replace(name,self.path)
                dfd=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(dfd)
                finally:os.close(dfd)
            finally:
                if os.path.exists(name):os.unlink(name)


_board_auth = BoardPasswordStore(os.environ.get("BOARD_AUTH_FILE", ""), BOARD_PASSWORD)


# ---- 記憶審核：公告欄記憶頁的「審核」分頁----
# 記憶整理人上傳清單；使用者按「照做／不要／改成…」，伺服器當場以 user 身分改記憶，短時間內可復原。
# 每份三個檔：<id>.json（清單）、<id>.state.json（每一項目前狀態＋復原步驟）、<id>.log.jsonl（每一次按鍵）。
REVIEW_DIR = os.environ.get("REVIEW_DIR", "/reviews")
REVIEW_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
ITEM_ID_RE = re.compile(r"^[0-9A-Za-z_-]{1,32}$")
REVIEW_UNDO_SECONDS = 60  # 頁面只給 5 秒的復原鈕；伺服器多留一點，網路慢也復原得了
REVIEW_FINAL = ("done", "skipped")
_review_lock = threading.Lock()


def _review_path(rid: str, ext: str) -> str:
    if not REVIEW_ID_RE.match(rid or ""):
        raise ValueError("審核編號格式不對")
    return os.path.join(REVIEW_DIR, rid + ext)


def _review_read(rid: str, ext: str, default=None):
    try:
        with open(_review_path(rid, ext), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _review_write(rid: str, ext: str, obj) -> None:
    p = _review_path(rid, ext)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(p + ".tmp", p)


def _review_log(rid: str, entry: dict) -> None:
    with open(_review_path(rid, ".log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": _now_tw(), **entry}, ensure_ascii=False) + "\n")


def _review_items(data: dict) -> list:
    """要使用者決定的項目 [(分頁, 分組, 項目)]；Codex 回覆分頁、唯讀分組不算。"""
    out = []
    for t in data.get("tabs") or []:
        if t.get("key") == "codex":
            continue
        for g in t.get("groups") or []:
            if not g.get("readonly"):
                out.extend((t, g, it) for it in g.get("items") or [])
    return out


def _review_list() -> list:
    out = []
    if not os.path.isdir(REVIEW_DIR):
        return out
    for f in os.listdir(REVIEW_DIR):
        rid = f[:-5]
        if not f.endswith(".json") or f.endswith((".refs.json", ".state.json")) or not REVIEW_ID_RE.match(rid):
            continue
        data = _review_read(rid, ".json", {}) or {}
        state = _review_read(rid, ".state.json", {}) or {}
        n = sum(1 for _, _, it in _review_items(data)
                if (state.get(it.get("id")) or {}).get("status") is None and not it.get("exists"))  # 經歷區已有的不用選
        out.append({"id": rid, "title": data.get("title") or rid, "created": data.get("created") or "",
                    "open": n, "dry_run": bool(data.get("dry_run"))})
    out.sort(key=lambda x: x["created"], reverse=True)
    return out


def _review_get(rid: str) -> dict:
    data = _review_read(rid, ".json")
    if not data:
        raise ValueError("找不到這份審核")
    state = _review_read(rid, ".state.json", {}) or {}
    view = {k: {x: y for x, y in v.items() if x != "undo"} for k, v in state.items()}  # 復原步驟只留在伺服器
    asked = {v["asked"]: k for k, v in view.items() if v.get("asked") and v.get("status") == "waiting"}
    if asked:  # 問過的人用 reply_to 回了，就把回答帶到卡片上
        for m in _list_all():
            md = m.get("metadata") or {}
            if md.get("type") == "message" and md.get("reply_to") in asked:
                view[asked[md["reply_to"]]].setdefault("replies", []).append({
                    "from": _canon(md.get("from") or m.get("agent_id") or ""), "at": _tw(m.get("created_at")),
                    "text": (md.get("subject") or "") + (f"｜{md['body']}" if md.get("body") else "")})
    names = {a: _review_who(a) for a in {str(x[2].get("needs_to") or "").strip() for x in _review_items(data)} if a}
    names.update({r["from"]: _review_who(r["from"]) for v in view.values() for r in v.get("replies") or []})
    cur = {m.get("id"): m.get("memory") or "" for m in _list_all()}
    feet = {o["id"]: _foot_split(cur[o["id"]])[1] for _, _, it in _review_items(data)
            for o in it.get("originals") or [] if o.get("id") in cur}  # 原文底下現在有哪些註腳（清單做好之後加的也算）
    return {"data": data, "refs": _review_read(rid, ".refs.json"), "state": view, "names": names,
            "feet": feet, "dry_run": bool(data.get("dry_run")),
            "import_target": "experience"}  # 匯入草稿寫到哪裡；舊伺服器沒有這欄（寫 user-profile），頁面就不讓按


def _review_who(addr: str) -> str:
    """圓桌地址顯示成中文短名：claude-laptop:mem0-deploy → Claude・Mem0 部署（跟公告欄一樣讀路由表）。"""
    base, sub = _sub((addr or "").strip().lower())
    if sub:
        return f"{_group(_canon(base)).capitalize()}・" + str(_session_names().get(sub) or sub).split("（")[0]
    return _group_label(base) if base in _groups() else base


def _as_user(fn, *args):
    """公告欄上的動作一律算使用者做的（跟留言、結案一樣用 user 身分）。"""
    token = current_agent.set("user")
    try:
        return fn(*args)
    finally:
        current_agent.reset(token)


def _project_pingpong(project: str, on) -> dict:
    """白板專案卡右鍵切換「乒乓」：只改那個專案 status 記憶的 metadata，內文不動，寫完讀回確認。
    第一次切時把原本的更新時間記進 status_at，白板的「幾分鐘前」和排序就不會因為切開關而變；
    pingpong_flips 記切過幾次，白板的「更新幾次」會扣掉。下次 project_set_status 會留下開關、清掉 status_at。"""
    p = _proj(project)
    if not isinstance(on, bool):
        raise ValueError("on 要是 true 或 false")
    with _write_lock:
        cur = [m for m in _list_all() if _match(m, project=p, type="status")]
        if not cur:
            raise LookupError(f"專案 {p} 還沒有進度卡")
        m = cur[0]
        text = m.get("memory") or ""
        md = dict(m.get("metadata") or {})
        md.setdefault("status_at", m.get("updated_at") or m.get("created_at") or "")
        md.update(pingpong="yes" if on else "no", pingpong_by=current_agent.get(), pingpong_at=_now_tw(),
                  pingpong_flips=str(int(md.get("pingpong_flips") or 0) + 1))
        with _http() as c:
            c.put(f"/memories/{m['id']}", json={"text": text, "metadata": md}).raise_for_status()
        back = _find_memory(m["id"])
        if _pingpong(back.get("metadata") or {}) is not on or (back.get("memory") or "") != text:
            raise RuntimeError("寫完讀回對不上，請重新整理再試")
    return {"project": p, "pingpong": on}


def _project_priority(project: str, priority) -> dict:
    """白板專案卡右鍵設優先序（1 最優先…4，5＝暫停，0＝清除）：寫法跟 _project_pingpong 一樣，只改 metadata、內文不動、
    status_at 留住原本的更新時間、priority_flips 記改過幾次（白板「更新幾次」扣掉）；跟原本一樣就不寫。"""
    p = _proj(project)
    if isinstance(priority, str) and priority.strip().isdigit():
        priority = int(priority.strip())
    if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 5:
        raise ValueError("priority 要是 0～5 的整數（1 最優先，5＝暫停，0＝清除）")
    with _write_lock:
        cur = [m for m in _list_all() if _match(m, project=p, type="status")]
        if not cur:
            raise LookupError(f"專案 {p} 還沒有進度卡")
        m = cur[0]
        md = dict(m.get("metadata") or {})
        if _priority(md) == priority:
            return {"project": p, "priority": priority}
        text = m.get("memory") or ""
        md.setdefault("status_at", m.get("updated_at") or m.get("created_at") or "")
        md.update(priority=str(priority) if priority else "", priority_by=current_agent.get(), priority_at=_now_tw(),
                  priority_flips=str(int(md.get("priority_flips") or 0) + 1))
        with _http() as c:
            c.put(f"/memories/{m['id']}", json={"text": text, "metadata": md}).raise_for_status()
        back = _find_memory(m["id"])
        if _priority(back.get("metadata") or {}) != priority or (back.get("memory") or "") != text:
            raise RuntimeError("寫完讀回對不上，請重新整理再試")
    return {"project": p, "priority": priority}


# AI 自訂頭像（公告欄 2.3.0）：{短代號: 附件編號}，存在 UPLOAD_DIR/avatars.json（跟附件同一個掛載目錄，換映像不會掉）。
# 圖片先走 /board/upload 拿附件編號；檔名不合 FILE_ID_RE，/board/files/ 讀不到這個 JSON。
AVATAR_FILE = "avatars.json"
AVATAR_WHO_RE = re.compile(r"^[a-z0-9-]{1,64}$")
AVATAR_MAX = 64
_avatar_lock = threading.Lock()


def _avatars() -> dict:
    """目前設過的頭像；檔案不在或壞了回 {}（頁面就顯示預設字母）。"""
    try:
        with open(os.path.join(UPLOAD_DIR, AVATAR_FILE), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in sorted(d.items()) if isinstance(k, str) and AVATAR_WHO_RE.match(k)
            and isinstance(v, str) and FILE_ID_RE.match(v)}


def _set_avatar(who, file) -> dict:
    """設定（file＝附件編號）或換回預設（file 空字串）某個 AI 的頭像；寫完讀回確認，回全部頭像。"""
    who = str(who or "").strip().lower()
    if not AVATAR_WHO_RE.match(who):
        raise ValueError("who 要是頭像代號，就是完整的身分名（小寫英數與 -，64 字內，例如 claude、codex-laptop）")
    fid = str(file or "").strip()
    if fid:
        _file_path(fid)  # 格式要對、圖片要真的在
    with _avatar_lock:
        cur = _avatars()
        if fid:
            cur[who] = fid
        else:
            cur.pop(who, None)
        if len(cur) > AVATAR_MAX:
            raise ValueError(f"頭像最多設 {AVATAR_MAX} 個")
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        path = os.path.join(UPLOAD_DIR, AVATAR_FILE)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, sort_keys=True)
        os.replace(path + ".tmp", path)
        back = _avatars()
    if back.get(who, "") != fid:
        raise RuntimeError("寫完讀回對不上，請重新整理再試")
    return {"avatars": back}


def _memory_busy() -> None:
    """有別的 AI 正在整理共用記憶（claim mem0-memory）就先不改。"""
    busy = [m for m in _claims("mem0-memory") if m.get("agent_id") != "user" and _age_hours(m) <= CLAIM_TTL_HOURS]
    if busy:
        raise ValueError(f"{busy[0].get('agent_id')} 正在整理共用記憶，請等它做完再按")


MEMORY_ID_RE = re.compile(r"^[0-9a-f-]{8,40}$")
_foot_undo = {}  # 復原權杖 → {id, version, old, until}；只放在伺服器記憶體，容器重開就失效


def _memory_footnote(mid: str, text: str) -> dict:
    """使用者在某筆決策／筆記底下加註腳：原文不動，最後面換行接「YYYY-MM-DD 使用者補充：…」，AI 搜尋讀得到。
    紀錄檔只記哪一筆、何時、成不成功，不記註腳內容。"""
    note = " ".join((text or "").split())
    if not MEMORY_ID_RE.match(mid or ""):
        raise ValueError("記憶代號不對")
    if not note:
        raise ValueError("註腳不能是空的")
    if len(note) > FOOT_ONE_MAX:
        raise ValueError(f"註腳太長（一次最多 {FOOT_ONE_MAX} 字）")
    _memory_busy()
    line = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d") + " 使用者補充：" + note
    try:
        with _write_lock:
            m = _find_memory(mid)
            md = dict(m.get("metadata") or {})
            if md.get("type") not in EDIT_TYPES:
                raise ValueError("只有決策和筆記可以加註腳")
            old = m.get("memory") or ""
            if sum(len(x) + 1 for x in _foot_split(old)[1]) + len(line) > FOOT_TOTAL_MAX:
                raise ValueError(f"這筆的註腳加起來超過 {FOOT_TOTAL_MAX} 字了，沒辦法再加")
            new = old.rstrip() + "\n" + line
            archive_id = _archive(m, "memory_revision", "使用者追加補充（公告欄）", "user")
            with _http() as c:
                c.put(f"/memories/{mid}", json={"text": new, "metadata": dict(
                    md, updated_by="user", revision=int(md.get("revision") or 1) + 1)}).raise_for_status()
            if (_find_memory(mid).get("memory") or "") != new:
                raise RuntimeError(f"加完讀回內容不一致；舊版在封存區 {archive_id}")
    except Exception as e:
        _review_log("footnotes", {"act": "add", "id": mid, "ok": False, "err": str(e)[:120]})
        raise
    token = secrets.token_hex(12)
    with _review_lock:
        now = time.time()
        for k in [k for k, v in _foot_undo.items() if v["until"] < now]:
            del _foot_undo[k]
        _foot_undo[token] = {"id": mid, "version": _version(new), "old": {"text": old, "metadata": md},
                             "until": now + REVIEW_UNDO_SECONDS}
    _review_log("footnotes", {"act": "add", "id": mid, "ok": True})
    return {"id": mid, "version": _version(new), "feet": _foot_split(new)[1], "undo_token": token}


def _memory_footnote_undo(token: str) -> dict:
    """五秒內按了復原：把那筆改回加註腳之前的樣子（之後又被改過就不動）。"""
    with _review_lock:
        u = _foot_undo.pop(token or "", None)
    if not u or u["until"] < time.time():
        raise ValueError("超過可以復原的時間了；要拿掉這條註腳，請跟記憶整理人說")
    try:
        with _write_lock:
            m = _find_memory(u["id"])
            if _version(m.get("memory")) != u["version"]:
                raise ValueError("這筆之後又被改過，沒有拿掉註腳")
            with _http() as c:
                c.put(f"/memories/{u['id']}", json={"text": u["old"]["text"], "metadata": u["old"]["metadata"]}).raise_for_status()
    except Exception as e:
        _review_log("footnotes", {"act": "undo", "id": u["id"], "ok": False, "err": str(e)[:120]})
        raise
    _review_log("footnotes", {"act": "undo", "id": u["id"], "ok": True})
    return {"id": u["id"], "version": _version(u["old"]["text"]), "feet": _foot_split(u["old"]["text"])[1]}


# ---- 經歷區：記憶頁「經歷區」分頁----
# 頁面只拿整理後的內容和分類、敏感度、時間；原句（source_text）和出處（source_refs）不給頁面。
# 補充跟共用記憶的註腳一樣：原文不動，最後面接「YYYY-MM-DD 使用者補充：…」；經歷區的舊版由 experience_update 自己封存。
EXPERIENCE = None  # 檔案最後面載入經歷區工具時設定
EXP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
_exp_undo = {}  # 復原權杖 → {id, version, old, until}；只放在伺服器記憶體


def _exp_store():
    if EXPERIENCE is None:
        raise ValueError("經歷區工具沒有載入")
    return EXPERIENCE


def _exp_rows() -> list:
    """經歷區目前每一筆（ExperienceStore 的格式，含 version）。"""
    s = _exp_store()
    return [s._format(r) for r in s._current()]


def _exp_find(record_id: str) -> dict:
    hit = [r for r in _exp_rows() if r.get("record_id") == record_id]
    if not hit:
        raise ValueError("經歷區找不到這一條，可能剛被改過，請重新整理")
    return hit[0]


def _exp_error(e: Exception) -> str:
    """經歷區工具的英文錯誤轉成白話；不帶補充內容。"""
    s = str(e)
    if "Prohibited input" in s:
        return "內容裡有看起來像密碼、金鑰、身分證字號或住址的字，經歷區不收，沒有寫入"
    if "Version conflict" in s:
        return "這一條剛被改過，請重新整理再補"
    if "too large" in s:
        return "這一條加起來太長了，沒辦法再補"
    return s[:200]


def _experience_browse() -> dict:
    items = []
    for r in _exp_rows():
        main, feet = _foot_split(r.get("content") or "")
        items.append({"id": r["record_id"], "text": main, "feet": feet, "category": r.get("category") or "",
                      "sensitivity": r.get("sensitivity") or "", "kind": r.get("record_kind") or "",
                      "confidence": r.get("confidence") or "", "when": r.get("when") or "",
                      "sort_date": r.get("sort_date") or "", "revision": r.get("record_revision") or 1,
                      "version": r.get("version") or ""})
    items.sort(key=lambda x: x["sort_date"] or "", reverse=True)  # 有日期的新到舊，不知道日期的排最後
    return {"items": items, "total": len(items)}


def _experience_footnote(record_id: str, text: str) -> dict:
    note = " ".join((text or "").split())
    if not EXP_ID_RE.match(record_id or ""):
        raise ValueError("經歷代號不對")
    if not note:
        raise ValueError("補充不能是空的")
    if len(note) > FOOT_ONE_MAX:
        raise ValueError(f"補充太長（一次最多 {FOOT_ONE_MAX} 字）")
    _memory_busy()
    line = dt.datetime.now(LOCAL_TZ).strftime("%Y-%m-%d") + " 使用者補充：" + note
    try:
        r = _exp_find(record_id)
        old = r.get("content") or ""
        if sum(len(x) + 1 for x in _foot_split(old)[1]) + len(line) > FOOT_TOTAL_MAX:
            raise ValueError(f"這一條的補充加起來超過 {FOOT_TOTAL_MAX} 字了，沒辦法再加")
        new = old.rstrip() + "\n" + line
        try:
            out = _as_user(_exp_store().update, record_id, {"content": new}, r["version"])
        except (ValueError, KeyError, RuntimeError) as e:
            raise ValueError(_exp_error(e)) from None
        if (out.get("content") or "") != new:
            raise RuntimeError("加完讀回內容不一致；舊版在經歷歷史裡")
    except Exception as e:
        _review_log("footnotes", {"act": "exp-add", "id": record_id, "ok": False, "err": str(e)[:120]})
        raise
    token = secrets.token_hex(12)
    with _review_lock:
        now = time.time()
        for k in [k for k, v in _exp_undo.items() if v["until"] < now]:
            del _exp_undo[k]
        _exp_undo[token] = {"id": record_id, "version": out["version"], "old": old, "until": now + REVIEW_UNDO_SECONDS}
    _review_log("footnotes", {"act": "exp-add", "id": record_id, "ok": True})
    return {"id": record_id, "version": out["version"], "feet": _foot_split(new)[1], "undo_token": token}


def _experience_footnote_undo(token: str) -> dict:
    """五秒內按了復原：把那一條改回補充之前的內容（又是一次 experience_update，舊版照樣封存）。"""
    with _review_lock:
        u = _exp_undo.pop(token or "", None)
    if not u or u["until"] < time.time():
        raise ValueError("超過可以復原的時間了；要拿掉這條補充，請跟記憶整理人說")
    try:
        r = _exp_find(u["id"])
        if r["version"] != u["version"]:
            raise ValueError("這一條之後又被改過，沒有拿掉補充")
        try:
            _as_user(_exp_store().update, u["id"], {"content": u["old"]}, r["version"])
        except (ValueError, KeyError, RuntimeError) as e:
            raise ValueError(_exp_error(e)) from None
    except Exception as e:
        _review_log("footnotes", {"act": "exp-undo", "id": u["id"], "ok": False, "err": str(e)[:120]})
        raise
    _review_log("footnotes", {"act": "exp-undo", "id": u["id"], "ok": True})
    return {"id": u["id"], "feet": _foot_split(u["old"])[1]}


def _import_record(rid: str, tab: dict, group: dict, item: dict, body: str, own_words: bool) -> dict:
    """匯入審核的一條草稿 → 經歷區一筆。分類、敏感度、時間照清單寫的；沒寫就用分組名稱和「一般」。"""
    sens = str(item.get("sensitivity") or ("敏感" if group.get("sensitive") else "一般"))
    kind = str(item.get("kind") or "")
    sort = str(item.get("sort_date") or "")
    rec = {"record_id": re.sub(r"[^a-z0-9._-]", "-", f"rv-{rid}-{item.get('id')}".lower())[:120],
           "content": body.strip(), "when": str(item.get("when") or "時間不明"),
           "sort_date": sort if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", sort) else None,
           "category": str(item.get("category") or group.get("label") or "其他"),
           "sensitivity": sens if sens in ("一般", "敏感", "高度敏感") else "敏感",
           # 照草稿寫：草稿是 AI 整理的摘要，還沒核對過原句；使用者自己改寫的才算本人自述
           "confidence": "自述" if own_words else "摘要待核查",
           "record_kind": kind if kind in ("event", "state", "preference", "plan") else "state",
           "source_refs": [{"type": "board-review", "review": rid, "item": str(item.get("id") or ""),
                            "draft": str(tab.get("draft") or tab.get("label") or "")}],
           "import_batch_id": f"board-review-{rid}"[:120]}
    return rec


def _review_revert(steps: list) -> list:
    """把做過的步驟倒著退回；回傳沒退成的原因（空的就是全部退回了）。"""
    errs = []
    for s in reversed(steps):
        try:
            if s["op"] == "update":  # 改回改之前的原文與欄位（舊版也還在封存區）
                with _write_lock:
                    m = _find_memory(s["id"])
                    if _version(m.get("memory")) != s["version"]:
                        raise ValueError(f"{s['id'][:8]} 之後又被改過，沒有改回")
                    with _http() as c:
                        c.put(f"/memories/{s['id']}", json={"text": s["old"]["text"], "metadata": s["old"]["metadata"]}).raise_for_status()
            elif s["op"] == "delete":
                memory_restore(s["trash_id"], "記憶審核：使用者按了復原")
            elif s["op"] == "add" and s.get("id"):
                m = _find_memory(s["id"])
                memory_delete(s["id"], "記憶審核：使用者按了復原，撤回剛寫進的這筆", _version(m.get("memory")))
            elif s["op"] == "ask" and s.get("id"):
                _close("user", s["id"], "使用者在記憶審核按了復原")
            elif s["op"] == "exp_add":  # 經歷區沒有刪除；頁面在送出前先給 5 秒反悔，送出後就退不回來
                raise ValueError(f"經歷區沒有刪除功能，{s.get('id')} 已經寫進去了；要更正請在經歷區分頁用補充")
        except Exception as e:
            errs.append(str(e)[:200])
    return errs


def _review_run_ops(rid: str, item: dict, ops: list, reason: str) -> list:
    """照清單的 ops 依序改記憶；任何一步失敗就把前面做過的退回，不做一半。"""
    _memory_busy()
    why = f"記憶審核 {rid}／{item.get('id')}" + (f"：{reason}" if reason else "")
    orig = {o["id"]: o for o in item.get("originals") or [] if o.get("id")}

    def full(x):  # ops 可以寫完整 id 或前 8 碼；版本用清單裡那筆原文的 version
        hit = [k for k in orig if x and k.startswith(x)]
        if len(hit) != 1:
            raise ValueError(f"清單裡找不到記憶 {x} 的原文，沒辦法照做")
        return hit[0]

    def now_ver(mid, listed):
        """清單做好之後使用者只加了註腳：照現在的版本做，回傳（版本, 現在的註腳行）；其他改動照樣擋下。"""
        base = str(orig[mid].get("text") or "").rstrip()
        cur = _find_memory(mid).get("memory") or ""
        feet = _foot_split(cur)[1]
        if _version(cur) != listed and base and _version(orig[mid].get("text") or "") == listed and cur.startswith(base):
            extra = [x.strip() for x in cur[len(base):].split("\n") if x.strip()]
            if extra and all(FOOT_RE.match(x) for x in extra):
                return _version(cur), feet
        return listed, feet

    merged = next((full(o.get("id")) for o in ops if o.get("op") == "update"), "")  # 合併：刪掉的那幾筆理由寫「已併入」
    plan, carry = [], []  # 先對好每一筆的版本；併掉的那幾筆的註腳，搬到合併後那筆的最後
    for op in ops:
        mid = full(op.get("id")) if op.get("op") in ("update", "delete") else ""
        ver, extra = now_ver(mid, op.get("version") or orig[mid].get("version") or "") if mid else ("", [])
        plan.append((op, mid, ver, extra))
        if op.get("op") == "delete" and merged:
            carry += extra
    done = []
    try:
        for op, mid, ver, extra in plan:
            kind = op.get("op")
            if kind == "delete" and merged:
                r = memory_delete(mid, f"{why}：已併入 {merged[:8]}", ver)
                done.append({"op": "delete", "id": mid, "trash_id": r["trash_id"]})
            elif kind == "update":
                cur = _find_memory(mid)
                carried = _feet_carry.set(tuple(carry))  # 這筆自己的註腳 memory_update 會自動接回
                try:
                    r = memory_update(mid, op.get("content") or "", ver, why)
                finally:
                    _feet_carry.reset(carried)
                done.append({"op": "update", "id": mid, "version": r["version"],
                             "old": {"text": cur.get("memory"), "metadata": cur.get("metadata")}})
            elif kind == "delete":
                r = memory_delete(mid, why, ver)
                done.append({"op": "delete", "id": mid, "trash_id": r["trash_id"]})
            elif kind == "add":
                r = memory_add(op.get("type") or "note", op.get("project") or "general", op.get("content") or "")
                done.append({"op": "add", "id": next((x.get("id") for x in _items(r["result"]) if x.get("id")), "")})
            else:
                raise ValueError(f"清單裡有看不懂的步驟 {kind!r}")
    except Exception as e:
        left = _review_revert(done)
        msg = "這筆剛被改過，請重新整理再決定" if "已被改過" in str(e) else str(e)[:300]
        raise ValueError(msg + (f"（前面做過的沒有全部退回：{'；'.join(left)}）" if left else "")) from None
    return done


def _review_steps_text(steps: list) -> str:
    words = {"update": "已改 {id}", "delete": "已刪 {id}（回收桶 {trash}）", "add": "已新增 {id}"}
    return "、".join(words[s["op"]].format(id=s["id"][:8], trash=s.get("trash_id", "")[:8]) for s in steps if s["op"] in words)


def _review_ops_text(ops: list) -> str:
    words = {"update": "改 {id}", "delete": "刪 {id}", "add": "新增一筆"}
    return "、".join(words.get(o.get("op"), "?").format(id=str(o.get("id") or "")[:8]) for o in ops)


def _review_decide(rid: str, data: dict, tab: dict, group: dict, item: dict, choice: str, text: str, prev: dict) -> tuple:
    """決定這一按要做什麼；回傳（新狀態, 做過的步驟）。dry_run 的測試清單什麼都不動。"""
    dry, iid, title = bool(data.get("dry_run")), item.get("id"), str(item.get("title") or item.get("text") or "")[:40]
    test = "（測試清單，沒有真的動）" if dry else ""
    if choice == "skip":
        return {"status": "skipped", "note": "沒動"}, []
    if tab.get("key") == "chatgpt":  # 大量匯入的草稿：一條＝經歷區一筆（10/16 寫進 user-profile 變成重複，改寫經歷區）
        if item.get("exists"):
            raise ValueError(f"經歷區已經有了（{item['exists']}），不用再寫")
        rec = _import_record(rid, tab, group, item, text if choice == "change" else str(item.get("text") or ""), choice == "change")
        if not rec["content"]:
            raise ValueError("這一條沒有內容")
        try:
            validate_experience(rec)
        except ValueError as e:
            raise ValueError(_exp_error(e)) from None
        where = f"經歷區「{rec['category']}」（{rec['sensitivity']}）"
        if dry:
            return {"status": "done", "note": test + f"會寫進{where}：" + rec["content"][:60]}, []
        _memory_busy()
        same = next((r["record_id"] for r in _exp_rows() if r["record_id"] != rec["record_id"]
                     and _foot_split(r.get("content") or "")[0].strip() == rec["content"]), "")
        if same:  # 一字不差的已經在經歷區：不再寫一份
            return {"status": "skipped", "note": f"經歷區已經有一樣的（{same}），沒有再寫"}, []
        try:
            out = _as_user(_exp_store().add, rec)
        except (ValueError, KeyError, RuntimeError) as e:
            raise ValueError(_exp_error(e)) from None
        return {"status": "done", "note": f"已寫進{where}，代號 {out['record_id']}"}, [{"op": "exp_add", "id": out["record_id"]}]
    ask_to = (item.get("needs_to") or "").strip()
    if ask_to and prev.get("status") != "waiting":  # 要先問別人：第一次按只發留言
        subj = f"記憶審核 {rid}／{iid}：{title}，你同意嗎？"[:80]
        body = f"建議：{item.get('suggestion') or ''}\n請用 reply_to 回覆這則；使用者看到回答後才會決定要不要照做。"
        mid = "" if dry else next((x.get("id") for x in _items(_send("user", ask_to, subj, body, True, "mem0", "")) if x.get("id")), "")
        return {"status": "waiting", "waiting_on": ask_to, "asked": mid,
                "note": test + f"已在公告欄問 {_review_who(ask_to)}；對方回了，再按一次「照做」才會真的做"}, [{"op": "ask", "id": mid}]
    ops, reason = list(item.get("ops") or []), ""
    if choice == "change":  # 使用者的字不猜：刪除類當理由、單筆更正當新內容，其他交給記憶整理人
        if ops and all(o.get("op") == "delete" for o in ops):
            reason = text
        elif sum(o.get("op") == "update" for o in ops) == 1:
            ops = [dict(o, content=text) if o.get("op") == "update" else o for o in ops]
        else:
            ops = []
    if not ops or item.get("local"):  # 伺服器做不了（沒寫步驟、要改使用者電腦上的檔案）：交給記憶整理人
        ex = data.get("executor") or {}
        name, to = ex.get("name") or "記憶整理人", (ex.get("to") or "").strip()
        mid = ""
        if to and not dry:
            subj = f"記憶審核 {rid}／{iid} 請照做：{title}"[:80]
            body = ("使用者要改成：" + text if choice == "change" else "使用者選了照做。") + f"\n建議：{item.get('suggestion') or ''}"
            mid = next((x.get("id") for x in _items(_send("user", to, subj, body[:1000], True, "mem0", "")) if x.get("id")), "")
        return {"status": "waiting", "waiting_on": name, "asked": mid, "forwarded": True,
                "note": test + f"這一項要{name}動手，已經交給它；做完會在這裡標「已完成」"}, [{"op": "ask", "id": mid}]
    if dry:
        return {"status": "done", "note": test + "照做會：" + _review_ops_text(ops)}, []
    steps = _review_run_ops(rid, item, ops, reason)
    return {"status": "done", "note": _review_steps_text(steps)}, steps


def _review_act(rid: str, iid: str, choice: str, text: str = "") -> dict:
    if choice not in ("do", "skip", "change"):
        raise ValueError("choice 只能是 do／skip／change")
    if not ITEM_ID_RE.match(iid or ""):
        raise ValueError("項目編號格式不對")
    text = (text or "").strip()[:1000]
    if choice == "change" and not text:
        raise ValueError("「改成…」要寫內容")
    with _review_lock:
        data = _review_read(rid, ".json")
        if not data:
            raise ValueError("找不到這份審核")
        hit = [x for x in _review_items(data) if x[2].get("id") == iid]
        if not hit:
            raise ValueError("這份審核沒有這一項，或這一項不用你決定")
        state = _review_read(rid, ".state.json", {}) or {}
        prev = state.get(iid) or {}
        if prev.get("status") in REVIEW_FINAL:
            raise ValueError("這一項已經處理過了；要反悔請到回收桶還原，或用記憶歷史改回")
        if prev.get("forwarded"):
            raise ValueError("這一項已經交給記憶整理人了，等它做完")
        st, steps = _review_decide(rid, data, *hit[0], choice, text, prev)
        token = secrets.token_hex(12)
        prev.pop("undo", None)
        st.update({"choice": choice, "text": text, "at": _now_tw(),
                   "undo": {"token": token, "until": time.time() + REVIEW_UNDO_SECONDS, "steps": steps, "prev": prev}})
        state[iid] = st
        _review_write(rid, ".state.json", state)
        _review_log(rid, {"item": iid, "choice": choice, "text": text, "status": st["status"], "note": st["note"],
                          "steps": steps, "dry_run": bool(data.get("dry_run"))})
    return {"item": iid, "status": st["status"], "note": st["note"], "undo_token": token}


def _review_undo(rid: str, iid: str, token: str) -> dict:
    with _review_lock:
        state = _review_read(rid, ".state.json", {}) or {}
        st = state.get(iid) or {}
        u = st.get("undo") or {}
        if not u or not hmac.compare_digest(str(token or ""), str(u.get("token") or "")):
            raise ValueError("這一項已經復原過，或復原碼不對；請重新整理")
        if time.time() > float(u.get("until") or 0):
            raise ValueError("已經超過復原時間；刪掉的可到回收桶還原，改過的可用記憶歷史改回")
        errs = _review_revert(u.get("steps") or [])
        if errs:
            st.pop("undo", None)
            st.update({"status": "failed", "note": "復原沒有全部成功：" + "；".join(errs)})
            state[iid] = st
        elif u.get("prev"):
            state[iid] = u["prev"]
        else:
            state.pop(iid, None)
        _review_write(rid, ".state.json", state)
        _review_log(rid, {"item": iid, "undo": True, "errors": errs})
    if errs:
        raise ValueError("復原沒有全部成功：" + "；".join(errs))
    return {"item": iid, "status": (state.get(iid) or {}).get("status") or ""}


def _review_upload(agent: str, payload: dict) -> dict:
    """記憶整理人／每週排程上傳清單（MCP 權杖，只收 claude-*）。同一個 id 再傳會換掉清單，已經按過的狀態保留；
    results 可以把記憶整理人做完的項目標成完成（交給它做的那些）。"""
    review = payload.get("review") if isinstance(payload, dict) else None
    if not isinstance(review, dict) or not isinstance(review.get("tabs"), list):
        raise ValueError("review 要是清單的 JSON（data/<id>.json 的內容）")
    rid = str(review.get("id") or "")
    _review_path(rid, ".json")
    ids = [str(it.get("id") or "") for t in review["tabs"] for g in t.get("groups") or [] for it in g.get("items") or []]
    bad = [i for i in ids if not ITEM_ID_RE.match(i)]
    if bad or len(ids) != len(set(ids)):
        raise ValueError("項目 id 要用英數字、同一份不能重複：" + "、".join((bad or [i for i in ids if ids.count(i) > 1])[:5]))
    review.update({"uploaded_by": agent, "uploaded_at": _now_tw(), "dry_run": bool(payload.get("dry_run"))})
    os.makedirs(REVIEW_DIR, exist_ok=True)
    with _review_lock:
        _review_write(rid, ".json", review)
        if payload.get("refs") is not None:
            _review_write(rid, ".refs.json", payload["refs"])
        state = _review_read(rid, ".state.json", {}) or {}
        marked = 0
        for k, v in (payload.get("results") or {}).items():
            if k in ids and isinstance(v, dict) and v.get("status") in ("done", "skipped", "waiting", "failed") \
                    and (state.get(k) or {}).get("status") not in REVIEW_FINAL:
                state[k] = {"status": v["status"], "note": str(v.get("note") or "")[:500], "at": _now_tw(), "by": agent}
                marked += 1
        _review_write(rid, ".state.json", state)
        _review_log(rid, {"upload": agent, "items": len(ids), "results": marked, "dry_run": review["dry_run"]})
    return {"id": rid, "items": len(ids), "results": marked, "dry_run": review["dry_run"]}

COOKIE = "rt_board"
COOKIE_DAYS = 30


def _sign(exp: str) -> str:
    return _board_auth.sign(exp)


def _cookie_ok(request) -> bool:
    v = request.cookies.get(COOKIE, "")
    exp, _, sig = v.partition(".")
    try:
        return bool(sig) and hmac.compare_digest(sig, _sign(exp)) and int(exp) > dt.datetime.now().timestamp()
    except Exception:
        return False


async def _basic_ok(request):
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        u, _, p = base64.b64decode(auth[6:]).decode().partition(":")
        result = await _board_login_ok(u, p)
        return False if result is None else result
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


def _preview_login_page(msg: str = "", signed_in: bool = False) -> str:
    page = _login_page(msg).replace('action="/board/login"', 'action="/board/preview/login"')
    page = page.replace('<b>圓桌公告欄</b>', '<b>圓桌帳號｜預覽</b><div>登入後返回公告欄預覽。</div>')
    if signed_in:
        page = page.replace('<input name="u"', '<p style="color:inherit">目前已登入。</p><input name="u"')
    return page.replace('</body>', '<div style="position:fixed;bottom:18px;text-align:center"><a href="/board/preview">返回預覽</a> · <a href="/board/preview/password">更改密碼</a></div></body>')


def _preview_password_page() -> str:
    if _board_auth.available():
        return '''<!doctype html><html lang="zh-Hant"><meta name="viewport" content="width=device-width,initial-scale=1"><title>更改密碼</title><style>:root{color-scheme:light dark}body{font:16px system-ui;background:light-dark(#f2f3f5,#1e2128);color:light-dark(#161b26,#e8ebf1)}form{border:1px solid #8992a0;border-radius:12px;padding:20px}label{display:grid;gap:6px}input,button{box-sizing:border-box;font:inherit;max-width:100%;padding:10px;border-radius:8px;border:1px solid #8992a0}input{width:100%}button{cursor:pointer}a{color:light-dark(#2456d6,#7aa2ff)}</style><body style="font:16px system-ui;max-width:480px;margin:40px auto;padding:16px"><h1>更改密碼</h1><form method="post" action="/board/preview/password"><p><label>目前密碼 <input type="password" name="current" autocomplete="current-password" required></label></p><p><label>新密碼 <input type="password" name="new" autocomplete="new-password" required></label></p><p><label>確認新密碼 <input type="password" name="confirm" autocomplete="new-password" required></label></p><p>12至128個UTF-8位元組。儲存後其他已登入的頁面須重新登入。</p><button>儲存新密碼</button></form><p><a href="/board/preview">返回預覽</a></p></body></html>'''
    return """<!doctype html><html lang="zh-Hant"><meta name="viewport" content="width=device-width,initial-scale=1"><title>更改密碼｜預覽</title><body style="font:16px system-ui;max-width:480px;margin:40px auto;padding:16px"><h1>更改密碼</h1><p>此功能尚待密碼儲存位置的批准，目前不能變更密碼。</p><fieldset disabled><label>目前密碼 <input type="password" autocomplete="current-password"></label><p><label>新密碼 <input type="password" autocomplete="new-password"></label></p><label>確認新密碼 <input type="password" autocomplete="new-password"></label><p><button>儲存新密碼（尚未啟用）</button></p></fieldset><p><a href="/board/preview">返回預覽</a></p></body></html>"""


_auth_slots = asyncio.Semaphore(2)


async def _auth_call(fn, *args):
    # Wait outside the shared worker pool; at most two board-auth jobs run.
    # 只給 scrypt（verify/change）用；cookie 與簽章很便宜，走 _cheap_call，不跟慢的登入搶這兩格。
    async with _auth_slots:
        return await run_in_threadpool(fn, *args)


async def _cheap_call(fn, *args):
    return await run_in_threadpool(fn, *args)


LOGIN_FAIL_WINDOW = 60  # 秒
LOGIN_FAIL_MAX = 10     # 視窗內失敗超過這麼多次，暫停登入到視窗結束（只在內網，代價是使用者最多等一分鐘）
_login_fails: list = []


def _login_throttled() -> bool:
    now = dt.datetime.now().timestamp()
    _login_fails[:] = [t for t in _login_fails if now - t < LOGIN_FAIL_WINDOW]
    return len(_login_fails) >= LOGIN_FAIL_MAX


async def _board_login_ok(username, password):
    """回傳 True／False；驗證檔異常回 None（503）；失敗太多次回 "throttled"（429）。"""
    if _login_throttled():
        return "throttled"
    async with _auth_slots:
        # Queued requests must see failures recorded by earlier verification jobs.
        if _login_throttled():
            return "throttled"
        if not hmac.compare_digest(username.encode(), BOARD_USER.encode()):
            ok = False
        else:
            try:
                ok = await run_in_threadpool(_board_auth.verify, password)
            except ValueError:
                return None
        if not ok:
            _login_fails.append(dt.datetime.now().timestamp())
        return ok


async def board_app(scope, receive, send):
    request = Request(scope, receive)
    if not BOARD_USER or not BOARD_PASSWORD:
        return await Response("board disabled", status_code=404)(scope, receive, send)
    path = scope["path"].rstrip("/")
    secure = request.headers.get("x-forwarded-proto", "") == "https"
    if path.startswith("/board/preview/") and request.method == "POST":
        origin = request.headers.get("origin", "")
        expected_origin = ("https" if secure else request.url.scheme) + "://" + request.headers.get("host", "")
        if origin != expected_origin:
            return await Response("bad origin", status_code=403)(scope, receive, send)
    if request.method == "GET" and path == "/board/preview/login":
        page = _preview_login_page(signed_in=await _cheap_call(_cookie_ok, request))
        return await HTMLResponse(page, headers={"Cache-Control": "no-store"})(scope, receive, send)
    if request.method == "POST" and path == "/board/preview/login":
        body = await request.body()
        if len(body) > 8192:
            return await Response("too large", status_code=413)(scope, receive, send)
        form = {k: v[0] for k, v in urllib.parse.parse_qs(body.decode("utf-8")).items()}
        login_ok = await _board_login_ok(form.get("u", ""), form.get("p", ""))
        if login_ok is None:
            return await Response("帳號驗證資料異常，請聯絡維護者", status_code=503, headers={"Cache-Control":"no-store"})(scope, receive, send)
        if login_ok == "throttled":
            return await Response("登入失敗次數太多，請一分鐘後再試", status_code=429, headers={"Cache-Control":"no-store", "Retry-After": str(LOGIN_FAIL_WINDOW)})(scope, receive, send)
        if not login_ok:
            return await HTMLResponse(_preview_login_page("帳號或密碼不對"), status_code=401, headers={"Cache-Control": "no-store"})(scope, receive, send)
        exp = str(int(dt.datetime.now().timestamp()) + COOKIE_DAYS * 86400)
        resp = RedirectResponse("/board/preview", status_code=303, headers={"Cache-Control": "no-store"})
        resp.set_cookie(COOKIE, f"{exp}.{await _cheap_call(_sign, exp)}", max_age=COOKIE_DAYS * 86400, path="/board", httponly=True, secure=secure, samesite="strict")
        return await resp(scope, receive, send)
    if request.method == "POST" and path == "/board/preview/logout":
        resp = HTMLResponse(_preview_login_page("已登出。若曾用瀏覽器的 Basic 認證登入，請一併關閉該瀏覽器工作階段。"), headers={"Cache-Control": "no-store"})
        resp.delete_cookie(COOKIE, path="/board", httponly=True, secure=secure, samesite="strict")
        return await resp(scope, receive, send)
    if request.method == "POST" and path == "/board/login":
        form = {k: v[0] for k, v in urllib.parse.parse_qs((await request.body()).decode("utf-8")).items()}
        login_ok = await _board_login_ok(form.get("u", ""), form.get("p", ""))
        if login_ok is None:
            return await Response("帳號驗證資料異常，請聯絡維護者", status_code=503, headers={"Cache-Control":"no-store"})(scope, receive, send)
        if login_ok == "throttled":
            return await Response("登入失敗次數太多，請一分鐘後再試", status_code=429, headers={"Cache-Control":"no-store", "Retry-After": str(LOGIN_FAIL_WINDOW)})(scope, receive, send)
        if login_ok:
            exp = str(int(dt.datetime.now().timestamp()) + COOKIE_DAYS * 86400)
            resp = RedirectResponse("/board", status_code=303)
            resp.set_cookie(COOKIE, f"{exp}.{await _cheap_call(_sign, exp)}", max_age=COOKIE_DAYS * 86400, path="/board",
                            httponly=True, secure=secure, samesite="strict")
            return await resp(scope, receive, send)
        return await HTMLResponse(_login_page("帳號或密碼不對"), status_code=401)(scope, receive, send)
    auth_ok = True if await _cheap_call(_cookie_ok, request) else await _basic_ok(request)
    if auth_ok == "throttled":
        return await Response("登入失敗次數太多，請一分鐘後再試", status_code=429, headers={"Cache-Control":"no-store", "Retry-After": str(LOGIN_FAIL_WINDOW)})(scope, receive, send)
    if auth_ok is not True:
        if request.method == "GET" and path in ("/board/preview", "/board/preview/memory", "/board/preview/versions", "/board/preview/password"):
            return await HTMLResponse(_preview_login_page(), headers={"Cache-Control": "no-store"})(scope, receive, send)
        if request.method == "GET" and path in ("/board", "/board/legacy"):
            return await HTMLResponse(_login_page(), status_code=200, headers={"Cache-Control": "no-store"})(scope, receive, send)
        return await Response("需要登入", status_code=401)(scope, receive, send)
    if request.method == "POST" and path == "/board/preview/password":
        if not _board_auth.available():
            return await Response("credential storage not configured", status_code=409)(scope, receive, send)
        body = await request.body()
        if len(body) > 8192:
            return await Response("too large", status_code=413)(scope, receive, send)
        form = {k:v[0] for k,v in urllib.parse.parse_qs(body.decode("utf-8")).items()}
        try:
            await _auth_call(_board_auth.change, form.get("current", ""), form.get("new", ""), form.get("confirm", ""))
        except ValueError as exc:
            return await Response(str(exc), status_code=400, headers={"Cache-Control":"no-store"})(scope, receive, send)
        exp = str(int(dt.datetime.now().timestamp()) + COOKIE_DAYS * 86400)
        resp = RedirectResponse("/board/preview", status_code=303, headers={"Cache-Control":"no-store"})
        resp.set_cookie(COOKIE, f"{exp}.{await _cheap_call(_sign, exp)}", max_age=COOKIE_DAYS*86400, path="/board", httponly=True, secure=secure, samesite="strict")
        return await resp(scope, receive, send)
    if request.method == "GET" and path == "/board/preview/password":
        return await HTMLResponse(_preview_password_page(), headers={"Cache-Control": "no-store"})(scope, receive, send)
    wants_json = "application/json" in request.headers.get("accept", "")
    try:
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
                return await Response("bad origin", status_code=403)(scope, receive, send)
        if request.method == "POST" and BOARD_REV_CHECK and (path in _REV_WRITE_PATHS or path.startswith(_REV_WRITE_PREFIXES)):
            token = request.headers.get("x-board-rev", "")
            form_post = "application/x-www-form-urlencoded" in request.headers.get("content-type", "")
            if not token and form_post:  # 舊式頁面用一般表單送出，版號放在隱藏欄位（request.body() 會留著給後面再讀）
                token = (urllib.parse.parse_qs((await request.body()).decode("utf-8")).get("_board_rev") or [""])[0]
            why = await run_in_threadpool(_rev_stale, token)
            if why:
                print(f"[board] 擋下舊版頁面的寫入 {path}（版號 {token or '沒帶'}）", flush=True)
                headers = {"X-Board-Stale": "1", "Cache-Control": "no-store"}
                if wants_json or not form_post or request.headers.get("x-board-rev"):
                    return await JSONResponse({"error": why, "stale": True}, status_code=409, headers=headers)(scope, receive, send)
                return await HTMLResponse(f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                                          f'<body style="font:16px/1.6 system-ui;max-width:560px;margin:40px auto;padding:16px"><p>{html.escape(why)}</p>'
                                          f'<p><a href="/board">回公告欄</a></p></body>', status_code=409, headers=headers)(scope, receive, send)
        if request.method == "POST" and path == "/board/send":
            form = {k: v[0] for k, v in urllib.parse.parse_qs((await request.body()).decode("utf-8")).items()}
            await run_in_threadpool(_send, "user", form.get("to", "all"), form.get("subject", ""),
                                    form.get("body", ""), form.get("needs_action") == "yes", form.get("project", ""), "",
                                     form.get("attachments", ""), form.get("reply_to", ""),
                                     ask_opinion=form.get("ask_opinion") == "yes")
            if wants_json:
                return await JSONResponse({"sent": True})(scope, receive, send)
            return await RedirectResponse("/board", status_code=303)(scope, receive, send)
        if request.method == "POST" and path == "/board/preview/vote":
            form = {k: v[0] for k, v in urllib.parse.parse_qs((await request.body()).decode("utf-8")).items()}
            result = await run_in_threadpool(_vote, "user", form.get("message_id", ""), int(form.get("vote", "9")))
            return await JSONResponse(result)(scope, receive, send)
        if request.method == "POST" and path == "/board/preview/close":  # 使用者在公告欄結案／按「看過了」／取消結案
            form = {k: v[0] for k, v in urllib.parse.parse_qs((await request.body()).decode("utf-8")).items()}
            result = await run_in_threadpool(_close, "user", form.get("message_id", ""), form.get("reason", ""),
                                             form.get("undo") == "yes")
            return await JSONResponse(result)(scope, receive, send)
        if request.method == "POST" and path == "/board/preview/recall":  # 使用者在公告欄撤回任何一則（作廢）／取消撤回
            form = {k: v[0] for k, v in urllib.parse.parse_qs((await request.body()).decode("utf-8")).items()}
            result = await run_in_threadpool(_recall, "user", form.get("message_id", ""), form.get("reason", ""),
                                             form.get("undo") == "yes")
            return await JSONResponse(result)(scope, receive, send)
        if request.method == "GET" and path.startswith("/board/api/message/"):
            items = await run_in_threadpool(_list_all)
            m = _find_message(path[len("/board/api/message/"):], items)
            return await JSONResponse(_message_feed(m, items), headers={"Cache-Control": "no-store"})(scope, receive, send)
        if request.method == "GET" and path == "/board/api/review/list":
            return await JSONResponse(await run_in_threadpool(_review_list), headers={"Cache-Control": "no-store"})(scope, receive, send)
        if request.method == "GET" and path.startswith("/board/api/review/"):
            data = await run_in_threadpool(_review_get, path[len("/board/api/review/"):])
            return await JSONResponse(data, headers={"Cache-Control": "no-store"})(scope, receive, send)
        if request.method == "POST" and path.startswith("/board/api/review/") and path.endswith(("/act", "/undo")):
            if not request.headers.get("origin"):  # 會改記憶的按鈕：一定要是公告欄頁面自己送的
                return await Response("bad origin", status_code=403)(scope, receive, send)
            body = await request.body()
            if len(body) > 65536:
                return await Response("too large", status_code=413)(scope, receive, send)
            form = json.loads(body.decode("utf-8") or "{}")
            rid, _, verb = path[len("/board/api/review/"):].rpartition("/")
            if verb == "act":
                result = await run_in_threadpool(_as_user, _review_act, rid, str(form.get("item") or ""),
                                                 str(form.get("choice") or ""), str(form.get("text") or ""))
            else:
                result = await run_in_threadpool(_as_user, _review_undo, rid, str(form.get("item") or ""), str(form.get("token") or ""))
            return await JSONResponse(result)(scope, receive, send)
        if request.method == "POST" and path.startswith("/board/api/memory/") and path.endswith(("/footnote", "/footnote/undo")):
            if not request.headers.get("origin"):  # 加註腳會改記憶：一定要是公告欄頁面自己送的
                return await Response("bad origin", status_code=403)(scope, receive, send)
            body = await request.body()
            if len(body) > 16384:
                return await Response("too large", status_code=413)(scope, receive, send)
            form = json.loads(body.decode("utf-8") or "{}")
            if path.endswith("/undo"):
                result = await run_in_threadpool(_as_user, _memory_footnote_undo, str(form.get("token") or ""))
            else:
                result = await run_in_threadpool(_as_user, _memory_footnote, path[len("/board/api/memory/"):-len("/footnote")],
                                                 str(form.get("text") or ""))
            return await JSONResponse(result)(scope, receive, send)
        if request.method == "POST" and path == "/board/api/project/pingpong":
            if not request.headers.get("origin"):  # 會改專案進度卡：一定要是公告欄頁面自己送的
                return await Response("bad origin", status_code=403)(scope, receive, send)
            body = await request.body()
            if len(body) > 4096:
                return await Response("too large", status_code=413)(scope, receive, send)
            form = json.loads(body.decode("utf-8") or "{}")
            try:
                result = await run_in_threadpool(_as_user, _project_pingpong, str(form.get("project") or ""), form.get("on"))
            except LookupError as e:
                return await JSONResponse({"error": str(e)}, status_code=404)(scope, receive, send)
            return await JSONResponse(result)(scope, receive, send)
        if request.method == "POST" and path in ("/board/api/project/priority", "/board/api/avatar"):
            if not request.headers.get("origin"):  # 會改專案進度卡或頭像：一定要是公告欄頁面自己送的
                return await Response("bad origin", status_code=403)(scope, receive, send)
            body = await request.body()
            if len(body) > 4096:
                return await Response("too large", status_code=413)(scope, receive, send)
            form = json.loads(body.decode("utf-8") or "{}")
            if path.endswith("/avatar"):
                result = await run_in_threadpool(_as_user, _set_avatar, form.get("who"), form.get("file"))
                return await JSONResponse(result)(scope, receive, send)
            try:
                result = await run_in_threadpool(_as_user, _project_priority, str(form.get("project") or ""), form.get("priority"))
            except LookupError as e:
                return await JSONResponse({"error": str(e)}, status_code=404)(scope, receive, send)
            return await JSONResponse(result)(scope, receive, send)
        if request.method == "GET" and path == "/board/api/experience":
            return await JSONResponse(await run_in_threadpool(_as_user, _experience_browse),
                                      headers={"Cache-Control": "no-store"})(scope, receive, send)
        if request.method == "POST" and path.startswith("/board/api/experience/") and path.endswith(("/footnote", "/footnote/undo")):
            if not request.headers.get("origin"):  # 補充會改經歷區：一定要是公告欄頁面自己送的
                return await Response("bad origin", status_code=403)(scope, receive, send)
            body = await request.body()
            if len(body) > 16384:
                return await Response("too large", status_code=413)(scope, receive, send)
            form = json.loads(body.decode("utf-8") or "{}")
            if path == "/board/api/experience/footnote/undo":
                result = await run_in_threadpool(_experience_footnote_undo, str(form.get("token") or ""))
            else:
                result = await run_in_threadpool(_experience_footnote, urllib.parse.unquote(
                    path[len("/board/api/experience/"):-len("/footnote")]), str(form.get("text") or ""))
            return await JSONResponse(result)(scope, receive, send)
        if request.method == "POST" and path == "/board/upload":
            fid = await run_in_threadpool(_save_upload, await request.body(), request.headers.get("content-type", ""))
            return await JSONResponse({"id": fid})(scope, receive, send)
        if request.method == "POST" and path in ("/board/promote", "/board/rollback"):
            if path.endswith("promote"):
                version = await run_in_threadpool(_ui_promote)
            else:  # 可帶 {"rev": 內部流水號} 回滾到指定的那一版；不帶就退一版（舊頁面照舊能用）
                body = await request.body()
                if len(body) > 1024:
                    return await Response("too large", status_code=413)(scope, receive, send)
                rev = (json.loads(body.decode("utf-8") or "{}") or {}).get("rev") if body.strip() else None
                if rev is not None and (isinstance(rev, bool) or not str(rev).isdigit()):
                    raise ValueError("要回滾的版本編號看不懂")
                version = await run_in_threadpool(_ui_rollback, None if rev is None else int(rev))
            return await JSONResponse({"ok": True, "version": version, "changed": version != "already-current",
                                       **await run_in_threadpool(_ui_state)})(scope, receive, send)
        if request.method == "GET" and path == "/board/api/feed":
            try:
                n = int(request.query_params.get("limit", "50"))
            except ValueError:
                n = 50
            data = await run_in_threadpool(_feed, n, request.query_params.get("multi_to") == "1")
            data.update(await run_in_threadpool(_ui_state))
            return await JSONResponse(data, headers={"Cache-Control": "no-store"})(scope, receive, send)
        if request.method == "GET" and path.startswith("/board/files/"):
            p = _file_path(path[len("/board/files/"):])
            return await FileResponse(p, headers={"Cache-Control": "private, max-age=86400"})(scope, receive, send)
        if request.method == "GET" and path in ("/board/versions", "/board/preview/versions"):
            which = "preview" if path.startswith("/board/preview/") else "stable"
            page = await run_in_threadpool(_ui_html, which, "versions.html")
            if page is not None:
                return await HTMLResponse(_rev_guard(page, which), headers={"Cache-Control": "no-store"})(scope, receive, send)
            return await Response("尚無版本更新紀錄", status_code=404)(scope, receive, send)
        if request.method == "GET" and path in ("/board", "/board/preview"):
            which = "preview" if path.endswith("preview") else "stable"
            page = await run_in_threadpool(_ui_html, which, "board.html")
            if page is not None:
                return await HTMLResponse(_rev_guard(page, which), headers={"Cache-Control": "no-store"})(scope, receive, send)
            if path.endswith("preview"):
                return await Response("還沒有預覽版", status_code=404)(scope, receive, send)
        if request.method == "GET" and path in ("/board", "/board/legacy"):
            page = await run_in_threadpool(_board_html)
            return await HTMLResponse(_rev_guard(page, "legacy"), headers={"Cache-Control": "no-store"})(scope, receive, send)
        if request.method == "GET" and path in ("/board/memory", "/board/preview/memory"):
            which = "preview" if path == "/board/preview/memory" else "stable"
            page = await run_in_threadpool(_memory_html, which == "preview")
            return await HTMLResponse(_rev_guard(page, which), headers={"Cache-Control": "no-store"})(scope, receive, send)
    except Exception as e:  # 顯示給使用者看的錯誤，不含金鑰
        if wants_json or request.method == "POST":
            return await JSONResponse({"error": str(e)}, status_code=400)(scope, receive, send)
        return await Response(f"出錯了：{e}", status_code=400)(scope, receive, send)
    return await Response("not found", status_code=404)(scope, receive, send)


async def skill_bundle_app(scope, receive, send):
    """GET /skills/<name>.tar.gz：整包下載一個共用技能（已通過權杖檢查才會到這裡）。"""
    name = scope["path"][len("/skills/"):-len(".tar.gz")]
    try:
        d = _skill_dir(name)
    except ValueError as e:
        return await JSONResponse({"error": str(e)}, status_code=404)(scope, receive, send)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(d, arcname=name)
    return await Response(buf.getvalue(), media_type="application/gzip")(scope, receive, send)


class AuthMiddleware:
    """Bearer 權杖 → agent 身分。沒有或錯誤就 401。/board 另走帳密登入。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":  # 例如 websocket：一律拒絕，不讓它繞過權杖檢查
            return await send({"type": "websocket.close", "code": 1008})
        if scope["path"] == "/board/api/review/upload":  # 記憶整理人上傳審核清單：用 MCP 權杖，不是公告欄登入
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            agent = _agent_for(auth)
            if not agent or not agent.startswith("claude") or scope.get("method") != "POST":
                return await JSONResponse({"error": "只有 Claude 的權杖可以上傳審核清單"}, status_code=401)(scope, receive, send)
            body = await Request(scope, receive).body()
            if len(body) > 8 * 1024 * 1024:
                return await JSONResponse({"error": "清單太大（上限 8MB）"}, status_code=413)(scope, receive, send)
            try:
                result = await run_in_threadpool(_review_upload, agent, json.loads(body.decode("utf-8")))
            except Exception as e:
                return await JSONResponse({"error": str(e)}, status_code=400)(scope, receive, send)
            return await JSONResponse(result)(scope, receive, send)
        if scope["path"] == "/board" or scope["path"].startswith("/board/"):
            return await board_app(scope, receive, send)
        if scope["path"] == "/health":
            return await JSONResponse({"ok": True, "agents": sorted(set(TOKENS.values()))})(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        agent = _agent_for(auth)
        if not agent:
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        if scope["path"] in ("/skills/manifest.json", "/skills/sync.py"):
            f = "manifest.json" if scope["path"].endswith(".json") else "tools/roundtable_skills_sync.py"
            mt = "application/json" if f.endswith(".json") else "text/x-python"
            return await Response(open(os.path.join(SKILLS_ROOT, f), "rb").read(), media_type=mt)(scope, receive, send)
        if scope["path"].startswith("/files/"):
            try:
                p = _file_path(scope["path"][len("/files/"):])
            except ValueError as e:
                return await JSONResponse({"error": str(e)}, status_code=404)(scope, receive, send)
            return await FileResponse(p)(scope, receive, send)
        if scope["path"].startswith("/skills/") and scope["path"].endswith(".tar.gz"):
            return await skill_bundle_app(scope, receive, send)
        token = current_agent.set(agent)
        try:
            await self.app(scope, receive, send)
        finally:
            current_agent.reset(token)


from experience_store import register as register_experience_tools, validate as validate_experience
EXPERIENCE = register_experience_tools(mcp, _http, current_agent.get, _write_lock)  # 記憶頁的經歷區分頁也用這一份


app = AuthMiddleware(mcp.streamable_http_app())

if __name__ == "__main__":
    import uvicorn

    if MSG_ARCHIVE_ON:
        threading.Thread(target=_archive_loop, name="msg-archive", daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8890)
