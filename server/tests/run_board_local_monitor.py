"""本機看監控頁用的小伺服器：送 ui/preview/monitor.html，加上照 monitor-page/SPEC.md 格式產生的假 series JSON。
不碰 Mem0、不碰 VM819；資料全是假的，關掉就沒了。
用法：python run_board_local_monitor.py [UI 資料夾（預設 ../ui）] [埠（預設 8907）]
  → http://127.0.0.1:8907/board/monitor （也送 /board/preview/monitor）
假資料：24h／7d／30d 三份，含 null 斷點（收集器停了一段）、主節點接住比例掉到 90% 的一段、考題 7 天的每日點。"""
import json
import math
import os
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "..", "ui")
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8907
STEP = {"24h": 300, "7d": 3600, "30d": 86400}
TOOLS = ["memory_search", "inbox", "message_send", "memory_add", "project_status", "memory_get", "claim_check", "message_ack", "其他"]
AGENTS = ["claude-laptop", "codex-laptop", "codex-max", "hermes", "claude-relay", "codex-mini", "其他"]
NODES = [("mini", "Mini 主節點", 31), ("vm821", "VM821 備援", 58), ("emb-gw", "EMB 閘道", 34), ("mem0-api", "mem0-api", 12),
         ("pg", "VM820 資料庫", 3), ("mcp", "圓桌 MCP", 5)]


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def fake(rng_key):
    """照 SPEC 的 API 約定產一份；每種 range 固定種子，重抓時數字不會跳。"""
    step = STEP[rng_key]
    rnd = random.Random({"24h": 1, "7d": 2, "30d": 3}[rng_key])
    now = int(time.time()) // step * step
    n = {"24h": 288, "7d": 168, "30d": 30}[rng_key]
    t = [now - (n - 1 - i) * step for i in range(n)]
    scale = step / 300  # 一點涵蓋幾個 5 分鐘
    gap = set(range(n * 2 // 3, n * 2 // 3 + max(2, n // 24)))  # 收集器停了一段：null 斷點
    bad = set(range(n // 3, n // 3 + max(3, n // 12)))  # 主節點掉到 90% 的一段

    def daily_wave(i):
        h = time.localtime(t[i]).tm_hour
        return 0.55 + 0.45 * math.sin((h - 6) / 24 * 2 * math.pi)

    def series_of(fn):
        return [None if i in gap else fn(i) for i in range(n)]

    burst = set(rnd.sample(range(n), max(1, n // 20)))  # 偶爾一串狂寫（像 10/21 分層搬家那種）
    add = series_of(lambda i: max(0, int((rnd.gauss(1.6, 1.2) + (9 if i in burst else 0)) * scale * daily_wave(i))))
    upd = series_of(lambda i: max(0, int(rnd.gauss(1.0, 0.8) * scale * daily_wave(i))))
    dele = series_of(lambda i: int(rnd.random() * 3) if rnd.random() < 0.04 * scale else 0)
    human = series_of(lambda i: max(0, int(rnd.gauss(90, 25) * scale * daily_wave(i))))
    poll = series_of(lambda i: max(0, int(rnd.gauss(77, 8) * scale)))
    total_calls = [None if h is None else h + p for h, p in zip(human, poll)]
    by_tool, by_agent = {}, {}
    weights_t = [0.3, 0.25, 0.1, 0.08, 0.07, 0.06, 0.05, 0.04, 0.05]
    weights_a = [0.32, 0.2, 0.08, 0.06, 0.18, 0.1, 0.06]
    for name, w in zip(TOOLS, weights_t):
        by_tool[name] = [None if c is None else int(c * w * rnd.uniform(0.8, 1.2)) for c in total_calls]
    for name, w in zip(AGENTS, weights_a):
        by_agent[name] = [None if c is None else int(c * w * rnd.uniform(0.8, 1.2)) for c in total_calls]
    tool_p50 = series_of(lambda i: round(rnd.gauss(180, 25), 1))
    tool_p95 = [None if v is None else round(v * rnd.uniform(2.2, 3.4), 1) for v in tool_p50]
    search_p50 = series_of(lambda i: round(rnd.gauss(95, 12), 1))
    search_p95 = [None if v is None else round(v * rnd.uniform(1.6, 2.4), 1) for v in search_p50]
    emb_calls = series_of(lambda i: max(0, int(rnd.gauss(5, 2) * scale * daily_wave(i))))
    emb_p50 = series_of(lambda i: round(rnd.gauss(32, 4) * (2.2 if i in bad else 1), 1))
    emb_p95 = [None if v is None else round(v * rnd.uniform(1.3, 1.9), 1) for v in emb_p50]
    share = series_of(lambda i: round(rnd.uniform(0.88, 0.93) if i in bad else 1.0, 3))
    err_mcp = series_of(lambda i: max(0, int(rnd.gauss(0.14, 0.5) * scale)) if rnd.random() < 0.25 else 0)
    err_tool = series_of(lambda i: 1 if rnd.random() < 0.03 * scale else 0)
    err_emb = series_of(lambda i: int(rnd.random() * 4) if i in bad else 0)
    base = {"roundtable": 2951 - int(1.2 * n * scale), "roundtable-ops": 1005 - int(0.4 * n * scale), "archive": 426}
    totals = {k: series_of(lambda i, k=k, v=v: v + int(i * scale * (1.2 if k == "roundtable" else 0.4 if k == "roundtable-ops" else 0)))
              for k, v in base.items()}
    node_ms = {k: series_of(lambda i, ms=ms: round(rnd.gauss(ms, ms * 0.15), 1)) for k, _, ms in NODES}

    day = 86400
    today0 = (now + 8 * 3600) // day * day - 8 * 3600  # 台北今天 00:00
    ev_t = [today0 - (6 - i) * day for i in range(7)]
    ev = {"t": ev_t, "hit1": [round(0.40 + 0.01 * i + rnd.uniform(-0.02, 0.02), 3) for i in range(7)],
          "hit5": [round(0.64 + 0.008 * i + rnd.uniform(-0.02, 0.02), 3) for i in range(7)],
          "p50_ms": [round(rnd.gauss(95, 6), 1) for _ in range(7)], "scored": [1000 + i for i in range(7)]}
    if rng_key == "30d":  # 30 天那份多給幾天，看每日點在長範圍怎麼畫
        ev = {"t": [today0 - (20 - i) * day for i in range(21)], "hit1": [round(0.38 + 0.002 * i + rnd.uniform(-0.02, 0.02), 3) for i in range(21)],
              "hit5": [round(0.62 + 0.002 * i + rnd.uniform(-0.02, 0.02), 3) for i in range(21)],
              "p50_ms": [round(rnd.gauss(95, 6), 1) for _ in range(21)], "scored": [990 + i for i in range(21)]}

    def s24(arr, fn=sum):
        k = max(1, int(86400 / step))
        return fn(v for v in arr[-k:] if v is not None)

    summary = {
        "writes_24h": {"add": s24(add), "update": s24(upd), "delete": s24(dele)},
        "calls_24h": {"total": s24(human) + s24(poll), "human": s24(human), "poll": s24(poll)},
        "tool_ms_24h": {"p50": 181.4, "p95": 540.2}, "search_ms_24h": {"p50": 95.5, "p95": 198.0},
        "emb_24h": {"calls": s24(emb_calls), "p50_ms": 32.4, "p95_ms": 48.9, "primary": "192.0.2.4:1235",
                    "primary_share": 0.986 if rng_key == "24h" else 0.998, "fallback": 20 if rng_key == "24h" else 3},
        "errors_24h": {"mcp": s24(err_mcp), "tool": s24(err_tool), "emb": s24(err_emb)},
        "totals": {k: v[-1] or v[-2] for k, v in totals.items()},
        "inbox": {"unread": 7, "needs_action": 3, "claims": 2, "stale_claims": 1},
        "eval": {"date": time.strftime("%Y-%m-%d", time.localtime(ev["t"][-1])), "hit1": ev["hit1"][-1], "hit5": ev["hit5"][-1],
                 "p50_ms": ev["p50_ms"][-1], "scored": ev["scored"][-1]},
    }
    nodes = [{"key": k, "label": lb, "up": k != "vm821", "ms": None if k == "vm821" else ms, "since": _iso(now - 86400 * 3)} for k, lb, ms in NODES]
    return {"generated": _iso(now), "range": rng_key, "step_sec": step, "summary": summary, "nodes": nodes,
            "series": {"t": t, "writes": {"add": add, "update": upd, "delete": dele}, "calls": {"human": human, "poll": poll},
                       "calls_by_tool": by_tool, "calls_by_agent": by_agent, "tool_ms": {"p50": tool_p50, "p95": tool_p95},
                       "search_ms": {"p50": search_p50, "p95": search_p95},
                       "emb": {"calls": emb_calls, "p50_ms": emb_p50, "p95_ms": emb_p95, "primary_share": share},
                       "errors": {"mcp": err_mcp, "tool": err_tool, "emb": err_emb}, "totals": totals, "node_ms": node_ms},
            "eval_daily": ev}


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        print("[假站]", self.path, flush=True)

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/board/monitor", "/board/preview/monitor"):
            with open(os.path.join(UI, "preview", "monitor.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/board/api/monitor/series":
            r = (parse_qs(u.query).get("range") or ["24h"])[0]
            if r not in STEP:
                return self._send(400, json.dumps({"error": "range 只能是 24h、7d、30d"}).encode(), "application/json")
            if r == "30d" and parse_qs(u.query).get("fail"):
                return self._send(500, json.dumps({"error": "假的伺服器錯誤"}).encode(), "application/json")
            return self._send(200, json.dumps(fake(r), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
        if u.path in ("/board", "/board/preview", "/board/memory", "/board/preview/memory", "/board/versions", "/board/preview/versions"):
            return self._send(200, "<!doctype html><meta charset=utf-8><p>假站：這裡只有監控頁，<a href=/board/monitor>回監控頁</a>".encode("utf-8"), "text/html; charset=utf-8")
        return self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    if not os.path.isfile(os.path.join(UI, "preview", "monitor.html")):
        sys.exit("找不到 " + os.path.join(UI, "preview", "monitor.html"))
    print(f"[假站] 監控頁：http://127.0.0.1:{PORT}/board/monitor （假資料；?range=24h|7d|30d）", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
