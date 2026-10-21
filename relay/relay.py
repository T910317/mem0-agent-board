"""圓桌轉送站（大腦）。

替路由表上的每一串（以及另外給權杖的身分，例如 codex-laptop）定時看圓桌收件匣，
把該叫醒的信排進佇列；最後一哩（收信那台電腦的 wake/inbox_wait.py --relay）長輪詢 /wait 拿走一批，
去叫醒對應的 Claude 串或起一條 Codex。轉送站自己不 ack 圓桌的信、不回信、不做事。

只用標準函式庫；裝了 httpx（容器裡有）就用 httpx 連圓桌，沒裝就用 urllib。

環境變數
  RT_URL           圓桌 MCP 網址，例 http://roundtable:8890/mcp
  RT_TOKEN         路由表那個身分（例 claude-laptop）的權杖；用它帶 session=串名 逐串收信
  RT_TOKEN_<名字>  其他身分的權杖，名字寫法同伺服器的 MCP_TOKEN_<名字>（RT_TOKEN_CODEX_LAPTOP → codex-laptop）
  RT_EXTRA_TOKENS  同上的另一種寫法，給 docker compose 用：codex-laptop=權杖,hermes=權杖（逗號或分號隔開）
                   這些身分沒分串，整個收件匣一起看，標籤就是身分名；名字 codex 開頭的視為 Codex
  RELAY_TOKEN      最後一哩連 /wait、/ack 要帶的權杖（必填）
  POLL_SECONDS     幾秒看一次圓桌，預設 30
  ROUTES_FILE      路由表 session-routes.json（容器裡掛在 /data/ui/session-routes.json）
  RELAY_DATA       狀態資料夾：seen.json（排過的信）、queue.json（佇列）、wakes.json（每串每小時計數），預設 ./data/relay
  RELAY_BIND、RELAY_PORT  HTTP 綁的位址和埠，預設 127.0.0.1:8891（容器裡 0.0.0.0）
  MAX_PER_HOUR     每個標籤一小時最多排幾封 AI 寄的信，預設 3；多的留在圓桌下次再排。使用者寄的不受限
  LEASE_SECONDS    交給最後一哩後多久沒 ack 就再交一次，預設 300

挑信規則（同 wake/inbox_wait.py 的 action 模式）：使用者寄的、要動手（needs_action）、徵求意見
（you_owe_opinion／ask_opinion）、被撤回要停手（closed.kind=recall）。同一封對同一標籤只排一次
（seen.json 記「標籤:信編號」）。排進佇列後收件人自己已讀或信被結案，還沒交出去的就從佇列拿掉。

HTTP（只給內網，不要對外開；路徑前面可以多一層反向代理的前綴，只看最後一段）
  GET  /wait?max_wait=50    長輪詢：有信就回一批，最多等 max_wait 秒（上限 50）回空批
  POST /ack  {"keys":[...]} 最後一哩回報已送達，從佇列移除
  GET  /health              不用權杖：佇列長度、看哪些標籤、上次輪詢時間；不含信件內容
  權杖放 Authorization: Bearer … 或 ?token=…
"""
from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import httpx
except ImportError:  # 本機沒裝也能跑
    httpx = None

VERSION = "0.1.0"
MAX_WAIT = 50.0  # 長輪詢最多等幾秒
TEXT_LIMIT = 4000  # 交出去的本文最多幾字
SEEN_KEEP = 30 * 86400  # 排過的紀錄留幾秒


def now_str(ts: float | None = None) -> str:
    return dt.datetime.fromtimestamp(time.time() if ts is None else ts).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_str()}] {msg}", flush=True)


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.url = (env.get("RT_URL") or "").strip()
        self.token = (env.get("RT_TOKEN") or "").strip()
        self.relay_token = (env.get("RELAY_TOKEN") or "").strip()
        self.poll = float(env.get("POLL_SECONDS") or 30)
        self.routes = Path(env.get("ROUTES_FILE") or "./data/ui/session-routes.json")
        self.data = Path(env.get("RELAY_DATA") or "./data/relay")
        self.bind = env.get("RELAY_BIND") or "127.0.0.1"
        self.port = int(env.get("RELAY_PORT") or 8891)
        self.max_per_hour = int(env.get("MAX_PER_HOUR") or 3)
        self.lease = float(env.get("LEASE_SECONDS") or 300)
        self.extra: dict[str, str] = {}  # 身分名 -> 權杖（不印、不寫檔）
        for k, v in env.items():
            if k.startswith("RT_TOKEN_") and (v or "").strip():
                self.extra[k[len("RT_TOKEN_"):].lower().replace("_", "-")] = v.strip()
        for part in (env.get("RT_EXTRA_TOKENS") or "").replace(";", ",").split(","):
            name, _, tok = part.strip().partition("=")
            if name.strip() and tok.strip():
                self.extra[name.strip().lower()] = tok.strip()

    def problems(self) -> list[str]:
        out = []
        if not self.url:
            out.append("沒設 RT_URL（圓桌 MCP 網址）")
        if not self.token and not self.extra:
            out.append("沒設 RT_TOKEN（也沒有 RT_TOKEN_<名字>），不知道用誰的身分收信")
        if not self.relay_token:
            out.append("沒設 RELAY_TOKEN（最後一哩連進來要帶的權杖）")
        if self.poll < 5:
            out.append("POLL_SECONDS 太小（至少 5 秒），不要把圓桌打爆")
        return out


# ---------- 連圓桌 ----------
_client = None


def rpc(url: str, tok: str, name: str, args: dict, timeout: float = 20.0) -> list:
    """呼叫圓桌 MCP 的 tools/call（無狀態模式，不用 initialize）；回傳工具回的 list。"""
    global _client
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if httpx is not None:
        if _client is None:
            _client = httpx.Client(timeout=timeout)
        r = _client.post(url, json=body, headers=headers)
        r.raise_for_status()
        raw = r.text
    else:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
    return parse_result(raw)


def parse_result(raw: str) -> list:
    # 無狀態模式可能回純 JSON，也可能回 SSE（data: {...}）
    lines = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")] or [raw]
    out = []
    for ln in lines:
        try:
            res = json.loads(ln).get("result") or {}
        except (ValueError, AttributeError):
            continue
        if res.get("isError"):
            text = "；".join((c.get("text") or "")[:200] for c in res.get("content") or [] if isinstance(c, dict))
            raise RuntimeError(f"圓桌回錯誤：{text or '（沒說明）'}")
        for c in res.get("content") or []:
            try:
                v = json.loads(c.get("text") or "")
            except (ValueError, AttributeError):
                continue
            out.extend(v if isinstance(v, list) else [v])
    return [m for m in out if isinstance(m, dict) and m.get("id")]


def call_inbox(url: str, tok: str, session: str, projects: str) -> list:
    return rpc(url, tok, "inbox", {"session": session, "projects": projects})


# ---------- 路由表 ----------
def load_routes(path: Path) -> dict:
    """路由表裡真正的串：鍵是對話串編號，值有 label；底線開頭的鍵（說明、測試）跳過。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for k, v in data.items():
        if k.startswith("_") or not isinstance(v, dict) or not v.get("label"):
            continue
        out[k] = v
    return out


def projects_of(routes: dict, mine: dict) -> str:
    """同 wake/inbox_wait.py：本串訂閱的專案；領航員（*）再排除別串訂閱的。"""
    projs = list(mine.get("projects") or [])
    if "*" in projs:
        others = sorted({p for e in routes.values() if e is not mine for p in (e.get("projects") or []) if p != "*"})
        projs += ["-" + p for p in others]
    return ",".join(projs)


def targets(cfg: Config) -> list[dict]:
    """這一輪要看哪些收件匣：路由表每一串（帶 session=串名）＋另外給權杖的身分（整個收件匣）。"""
    out = []
    if cfg.token:
        routes = load_routes(cfg.routes)  # 每輪重讀，路由表改了下一輪就生效
        for sid, ent in routes.items():
            out.append({"label": ent["label"], "session": sid, "token": cfg.token,
                        "inbox_session": ent["label"], "projects": projects_of(routes, ent), "is_codex": False})
    for name, tok in sorted(cfg.extra.items()):
        out.append({"label": name, "session": "", "token": tok, "inbox_session": "", "projects": "",
                    "is_codex": name.startswith("codex")})
    return out


# ---------- 挑信 ----------
def reason_of(m: dict) -> str:
    """值得叫醒就回原因（白話），不值得回空字串。"""
    closed = m.get("closed") or {}
    rs = []
    if isinstance(closed, dict) and closed.get("kind") == "recall":
        rs.append("寄件人撤回，照它做的請停手")
    if m.get("you_owe_opinion") or m.get("ask_opinion"):
        rs.append("徵求意見，要回覆")
    if m.get("needs_action"):
        rs.append("要動手")
    if m.get("from") == "user" and not rs:
        rs.append("使用者寄的")
    return "、".join(rs)


def who(m: dict) -> str:
    if m.get("from") == "user":
        return "使用者"
    return f'{m.get("from")}:{m["from_session"]}' if m.get("from_session") else str(m.get("from"))


def make_item(t: dict, m: dict, reason: str, now: float) -> dict:
    closed = m.get("closed") or {}
    return {"key": f'{t["label"]}:{m["id"]}', "label": t["label"], "session": t["session"], "id": m["id"],
            "subject": (m.get("subject") or "")[:200], "from": who(m), "from_raw": m.get("from") or "",
            "from_session": m.get("from_session") or "", "is_codex": bool(t["is_codex"]), "reason": reason,
            "needs_action": bool(m.get("needs_action")),
            "ask_opinion": bool(m.get("you_owe_opinion") or m.get("ask_opinion")),
            "recall": isinstance(closed, dict) and closed.get("kind") == "recall",
            "project": m.get("project") or "", "ref": m.get("ref") or "", "reply_to": m.get("reply_to") or "",
            "attachments": [a.get("id") if isinstance(a, dict) else a for a in (m.get("attachments") or [])],
            "text": (m.get("text") or "")[:TEXT_LIMIT], "created": str(m.get("created") or ""),
            "queued_at": now, "attempts": 0, "lease_until": 0.0}


# ---------- 佇列 ----------
class Relay:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.stop = threading.Event()
        self.seen: dict[str, float] = {}
        self.queue: list[dict] = []
        self.wakes: dict[str, list[float]] = {}
        self.last_poll = 0.0
        self.last_error = ""
        self.polls = 0
        self.load()

    # 狀態檔
    def _read(self, name: str, default):
        try:
            return json.loads((self.cfg.data / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def load(self) -> None:
        seen = self._read("seen.json", {})
        self.seen = {k: float(v) for k, v in seen.items()} if isinstance(seen, dict) else {}
        q = self._read("queue.json", [])
        self.queue = [it for it in q if isinstance(it, dict) and it.get("key")] if isinstance(q, list) else []
        w = self._read("wakes.json", {})
        self.wakes = {k: [float(x) for x in v] for k, v in w.items()} if isinstance(w, dict) else {}

    def _write(self, name: str, obj) -> None:
        self.cfg.data.mkdir(parents=True, exist_ok=True)
        tmp = self.cfg.data / (name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=0), encoding="utf-8")
        os.replace(tmp, self.cfg.data / name)

    def _save_locked(self) -> None:
        self._write("seen.json", self.seen)
        self._write("queue.json", self.queue)
        self._write("wakes.json", self.wakes)

    def save(self) -> None:
        with self.lock:
            self._save_locked()

    # 看圓桌
    def poll_once(self, now: float | None = None) -> int:
        """看一輪所有收件匣，回新排進佇列的封數。單一收件匣出錯只記錄，不影響別的。"""
        now = time.time() if now is None else now
        try:
            ts = targets(self.cfg)
        except (OSError, ValueError) as e:
            with self.lock:
                self.last_error = f"讀不到路由表：{type(e).__name__}"
                self.last_poll, self.polls = now, self.polls + 1
            return 0
        queued, errors = 0, []
        for t in ts:
            try:
                msgs = call_inbox(self.cfg.url, t["token"], t["inbox_session"], t["projects"])
            except Exception as e:  # 連不上、權杖錯、回錯誤：這一串跳過
                errors.append(f'{t["label"]}：{type(e).__name__}')
                continue
            queued += self._absorb(t, msgs, now)
        with self.lock:
            self.last_poll, self.polls = now, self.polls + 1
            self.last_error = "；".join(errors)
            self._prune_locked(now)
            self._save_locked()
            if queued:
                self.cond.notify_all()
        if errors:
            log("這輪有收件匣看不到：" + self.last_error)
        return queued

    def _absorb(self, t: dict, msgs: list, now: float) -> int:
        label = t["label"]
        with self.lock:
            # 收件人自己已經讀掉或信被結案（不在收件匣了）：還沒交出去的不用叫醒
            current = {m["id"] for m in msgs}
            gone = [it for it in self.queue if it["label"] == label and it["id"] not in current and not it["attempts"]]
            for it in gone:
                self.queue.remove(it)
            budget = self.cfg.max_per_hour - sum(1 for x in self.wakes.get(label, []) if now - x < 3600)
            n, held = 0, 0
            for m in msgs:
                key = f'{label}:{m["id"]}'
                if key in self.seen:
                    continue
                reason = reason_of(m)
                if not reason:
                    continue  # 不記 seen：之後變成要動手（例如被撤回）還排得到
                if m.get("from") != "user":
                    if budget <= 0:
                        held += 1  # 留在圓桌，下一小時再排
                        continue
                    budget -= 1
                    self.wakes.setdefault(label, []).append(now)
                self.seen[key] = now
                self.queue.append(make_item(t, m, reason, now))
                n += 1
        for it in gone:
            log(f'{label}：信 {it["id"][:8]} 已被讀掉或結案，從佇列拿掉')
        if n:
            log(f"{label}：排進 {n} 封")
        if held:
            log(f"{label}：這一小時已排滿 {self.cfg.max_per_hour} 封 AI 的信，還有 {held} 封留到下次")
        return n

    def _prune_locked(self, now: float) -> None:
        self.seen = {k: v for k, v in self.seen.items() if now - v < SEEN_KEEP}
        self.wakes = {k: [x for x in v if now - x < 86400] for k, v in self.wakes.items()}
        self.wakes = {k: v for k, v in self.wakes.items() if v}

    # 交給最後一哩
    def _deliverable_locked(self, now: float) -> list[dict]:
        return [it for it in self.queue if it.get("lease_until", 0) <= now]

    @staticmethod
    def _public(it: dict) -> dict:
        out = {k: v for k, v in it.items() if k != "lease_until"}
        out["queued_at"] = now_str(it["queued_at"])
        return out

    def take(self, now: float | None = None) -> list[dict]:
        """拿走現在能交的，記成租出去（LEASE_SECONDS 內沒 ack 會再交一次）。"""
        now = time.time() if now is None else now
        with self.lock:
            items = self._deliverable_locked(now)
            for it in items:
                it["lease_until"] = now + self.cfg.lease
                it["attempts"] = it.get("attempts", 0) + 1
                it["delivered_at"] = now
            if items:
                self._save_locked()
            return [self._public(it) for it in items]

    def wait(self, max_wait: float) -> list[dict]:
        """長輪詢：有能交的就立刻回，不然最多等 max_wait 秒。"""
        deadline = time.time() + max(0.0, min(max_wait, MAX_WAIT))
        with self.cond:
            while not self.stop.is_set():
                now = time.time()
                if self._deliverable_locked(now) or now >= deadline:
                    break
                nxt = min((it.get("lease_until", 0) for it in self.queue), default=None)  # 租約到期也要醒
                timeout = deadline - now if nxt is None else max(0.05, min(deadline - now, nxt - now))
                self.cond.wait(timeout)
        return self.take()

    def ack(self, keys) -> int:
        keys = {str(k) for k in (keys or [])}
        with self.lock:
            before = len(self.queue)
            self.queue = [it for it in self.queue if it["key"] not in keys]
            removed = before - len(self.queue)
            if removed:
                self._save_locked()
        if removed:
            log(f"最後一哩回報送達 {removed} 封")
        return removed

    def health(self) -> dict:
        with self.lock:
            labels = sorted({it["label"] for it in self.queue})
            leased = sum(1 for it in self.queue if it.get("lease_until", 0) > time.time())
            return {"ok": True, "version": VERSION, "pid": os.getpid(), "queued": len(self.queue), "leased": leased,
                    "queued_labels": labels, "polls": self.polls, "poll_seconds": self.cfg.poll,
                    "last_poll": now_str(self.last_poll) if self.last_poll else "", "last_error": self.last_error}

    def shutdown(self) -> None:
        self.stop.set()
        with self.cond:
            self.cond.notify_all()


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "roundtable-relay/" + VERSION

    def log_message(self, fmt, *args):  # 不印一般存取紀錄，只印錯誤
        if args and str(args[0]).startswith(("4", "5")):
            log("HTTP " + fmt % args)

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        u = urllib.parse.urlsplit(self.path)
        name = u.path.rstrip("/").rsplit("/", 1)[-1]  # 反向代理加前綴也行，只看最後一段
        return name, urllib.parse.parse_qs(u.query)

    def _authed(self, q) -> bool:
        tok = (q.get("token") or [""])[0]
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            tok = auth[7:].strip()
        want = self.server.relay.cfg.relay_token
        return bool(want) and bool(tok) and hmac.compare_digest(tok, want)

    def do_GET(self):
        name, q = self._route()
        if name == "health":
            return self._send(200, self.server.relay.health())
        if name != "wait":
            return self._send(404, {"ok": False, "error": "沒有這個路徑"})
        if not self._authed(q):
            return self._send(401, {"ok": False, "error": "權杖不對"})
        try:
            max_wait = float((q.get("max_wait") or [MAX_WAIT])[0])
        except ValueError:
            max_wait = MAX_WAIT
        items = self.server.relay.wait(max_wait)
        return self._send(200, {"ok": True, "items": items, "server_time": now_str()})

    def do_POST(self):
        name, q = self._route()
        if name != "ack":
            return self._send(404, {"ok": False, "error": "沒有這個路徑"})
        if not self._authed(q):
            return self._send(401, {"ok": False, "error": "權杖不對"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
        except (ValueError, OSError):
            return self._send(400, {"ok": False, "error": "內容不是 JSON"})
        keys = body.get("keys") if isinstance(body, dict) else None
        if not isinstance(keys, list):
            return self._send(400, {"ok": False, "error": "要給 keys 清單"})
        removed = self.server.relay.ack(keys)
        return self._send(200, {"ok": True, "removed": removed, "queued": self.server.relay.health()["queued"]})


def serve(relay: Relay, bind: str, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    srv.relay = relay  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    return srv


def main() -> int:
    cfg = Config()
    problems = cfg.problems()
    if problems:
        for p in problems:
            log("設定不完整：" + p)
        return 2
    relay = Relay(cfg)
    try:
        srv = serve(relay, cfg.bind, cfg.port)
    except OSError as e:
        log(f"HTTP 開不起來（{cfg.bind}:{cfg.port}）：{e}")
        return 2
    try:
        names = [t["label"] for t in targets(cfg)]
    except (OSError, ValueError):
        names = []
    log(f"轉送站 {VERSION} 啟動：圓桌 {cfg.url}，每 {cfg.poll:g} 秒看一次，HTTP {cfg.bind}:{cfg.port}，"
        f"狀態放 {cfg.data}，佇列裡有 {len(relay.queue)} 封")
    log("看這些收件匣：" + ("、".join(names) if names else "（路由表讀不到，先等它出現）"))
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: relay.shutdown())
        except (ValueError, OSError):
            pass
    while not relay.stop.is_set():
        try:
            relay.poll_once()
        except Exception as e:  # 不讓任何意外把大腦弄死
            log(f"這輪出錯：{type(e).__name__}: {str(e)[:200]}")
        relay.stop.wait(cfg.poll)
    srv.shutdown()
    relay.save()
    log("轉送站結束，狀態已存")
    return 0


if __name__ == "__main__":
    sys.exit(main())
