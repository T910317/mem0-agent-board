"""離線測試：運作監控（monitor.py）與 server.py 的監控掛鉤。
用假的 history.db、假的 EMB access log、假的工具呼叫與假時鐘，不連任何真實服務、不打 embedding；
檢查 series() 的輸出格式和 monitor-page/SPEC.md 一模一樣，再用真的 starlette 打 /board/preview/monitor 與 /board/api/monitor/series。
用法：uv run --with starlette --with httpx python test_monitor.py <server.py 所在目錄>"""
import asyncio
import datetime as dt
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import types

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = tempfile.mkdtemp(prefix="monitor-")
os.environ["MONITOR_DIR"] = os.path.join(ROOT, "metrics")
os.environ["MONITOR_EVAL_WINDOW"] = "0-24"   # 測試不管幾點都讓考題可以跑
os.environ["TZ_OFFSET_HOURS"] = "8"
sys.path.insert(0, SRC)
import monitor  # noqa: E402

FAILS = []


def check(name, ok):
    print(("通過 " if ok else "失敗 ") + name)
    if not ok:
        FAILS.append(name)


TZ = dt.timezone(dt.timedelta(hours=8))
T = 1761200530.0  # 固定的「現在」；桶界線不是整點，比較會抓到對齊錯誤
B = lambda ts: int(ts // 300) * 300


def iso(ts, tz=TZ):
    return dt.datetime.fromtimestamp(ts, tz).isoformat()


# ── 假資料 ─────────────────────────────────────────────────────────
hist = os.path.join(ROOT, "history.db")
h = sqlite3.connect(hist)
h.execute("CREATE TABLE history (id TEXT, memory_id TEXT, old_memory TEXT, new_memory TEXT, event TEXT, created_at TEXT, updated_at TEXT)")
rows = [("ADD", iso(T - 1000)), ("ADD", iso(T - 1000, dt.timezone(dt.timedelta(hours=-7)))), ("UPDATE", iso(T - 900)),
        ("DELETE", iso(T - 1000, dt.timezone.utc).replace("+00:00", "Z")), ("ADD", iso(T - 2000)), ("ADD", "看不懂的時間")]
for i, (ev, ts) in enumerate(rows):
    h.execute("INSERT INTO history VALUES (?, ?, '', '', ?, ?, ?)", (f"h{i}", f"m{i}", ev, ts, ts))
h.commit()
h.close()

logdir = os.path.join(ROOT, "emb-logs")
os.makedirs(logdir)
PRIMARY, BACKUP = "192.0.2.4:1235", "192.0.2.95:8080"


def logline(ts, status="200", rt="0.032", ua=PRIMARY, probe=""):
    return json.dumps({"t": iso(ts), "status": status, "rt": rt, "ua": ua, "us": status, "urt": rt, "bytes": "100", "uri": "/v1/embeddings", "probe": probe}) + "\n"


day = dt.datetime.fromtimestamp(T - 1000, TZ).strftime("%Y-%m-%d")
with open(os.path.join(logdir, f"access-{day}.log"), "w", encoding="utf-8") as f:
    f.write(logline(T - 1000) + logline(T - 990, rt="0.050") + logline(T - 980, rt="0.040")
            + logline(T - 970, ua=f"{PRIMARY}, {BACKUP}", rt="2.1")           # 主節點失敗、備援接住
            + logline(T - 960, status="502", ua=PRIMARY, rt="2.0")             # 兩邊都掛
            + logline(T - 950, probe="1")                                      # 監控自己的探測：不算
            + "這不是 JSON\n")

conf = os.path.join(ROOT, "emb.conf")
with open(conf, "w", encoding="utf-8") as f:
    f.write("upstream emb {\n    zone emb 64k;\n    server %s max_fails=1 fail_timeout=15s;\n    server %s backup;\n}\n"
            "upstream embfb { server %s; }\nserver { listen 80; }\n" % (PRIMARY, BACKUP, BACKUP))

# ── 解析小工具 ────────────────────────────────────────────────────
servers = monitor.parse_emb_conf(open(conf, encoding="utf-8").read())
check("emb.conf 讀出兩個上游、第一個不是 backup", [s["addr"] for s in servers] == [PRIMARY, BACKUP] and [s["backup"] for s in servers] == [False, True])
check("主節點＝第一個非 backup 的 server", monitor.primary_from_conf(servers) == PRIMARY)
check("upstream_addr 取最後一個（重試、切備援）", monitor.final_upstream(f"{PRIMARY}, {BACKUP}") == BACKUP
      and monitor.final_upstream(f"{PRIMARY} : {BACKUP}") == BACKUP and monitor.final_upstream("-") is None)
check("帶 probe 的 log 行被排除", monitor.parse_emb_line(logline(T, probe="1")) is None and monitor.parse_emb_line("x") is None)
check("時間字串：有時區、Z、沒時區都能讀", abs(monitor._parse_time(iso(T - 1000)) - (T - 1000)) < 1
      and abs(monitor._parse_time(iso(T - 1000, dt.timezone.utc).replace("+00:00", "Z")) - (T - 1000)) < 1
      and monitor._parse_time("2026-01-01T00:00:00") == dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp()
      and monitor._parse_time("看不懂") is None)

# ── 收集器（假時鐘） ──────────────────────────────────────────────
col = monitor.Collector(db_path=os.path.join(ROOT, "metrics", "metrics.db"), history_db=hist, emb_log_dir=logdir, emb_conf=conf, now=lambda: T)
col.started_at = T - 3000
col.db()
check("metrics.db 用 WAL", col.db().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal")

# 工具呼叫：人用、輪詢、出錯、搜尋
monitor._queue.clear()
for i in range(3):
    monitor._queue.append(("call", T - 1000 + i, "claude-a", "memory_search", 100.0 + i * 10, 1, "", 0))
monitor._queue.append(("call", T - 990, "claude-a", "memory_get", 20.0, 1, "", 0))
monitor._queue.append(("call", T - 985, "relay", "inbox", 5.0, 1, "", 1))
monitor._queue.append(("call", T - 980, "codex-b", "memory_add", 300.0, 0, "ValueError", 0))
for i in range(9):  # 湊滿 9 種工具，檢查前 8 名＋其他
    monitor._queue.append(("call", T - 970, "codex-b", f"tool{i}", 1.0 + i, 1, "", 0))
monitor._queue.append(("mcp_err", T - 975, 500))
col.flush_queue()
check("工具呼叫寫進 calls_raw", col.db().execute("SELECT count(*) FROM calls_raw").fetchone()[0] == 15)

# history.db
n = col.ingest_history()
w = dict((ts, (a, u, d)) for ts, a, u, d in col.db().execute("SELECT ts, add_n, upd_n, del_n FROM writes_5m").fetchall())
check("history.db 讀進 writes_5m（不同時區寫法都算對桶）", n == 6 and w.get(B(T - 1000)) == (2, 1, 1) and w.get(B(T - 2000), (0, 0, 0))[0] == 1)
check("history.db 第二次不重讀", col.ingest_history() == 0)

# EMB log：接著讀、不重複、半行等下次
n = col.ingest_emb_log()
emb = col.db().execute("SELECT ms, status, upstream, is_primary, ok FROM emb_raw ORDER BY ts").fetchall()
check("EMB log 讀進 5 筆（探測與壞行排除）", n == 5 and len(emb) == 5)
check("主節點／備援／錯誤判斷正確", [r[3] for r in emb] == [1, 1, 1, 0, 1] and [r[4] for r in emb] == [1, 1, 1, 1, 0] and emb[3][2] == BACKUP)
with open(os.path.join(logdir, f"access-{day}.log"), "a", encoding="utf-8") as f:
    f.write(logline(T - 940)[:-10])  # 半行
check("EMB log 第二次只讀新的、半行先不算", col.ingest_emb_log() == 0)
with open(os.path.join(logdir, f"access-{day}.log"), "a", encoding="utf-8") as f:
    f.write(logline(T - 940)[-10:])
check("補完那一行就讀到", col.ingest_emb_log() == 1)
nxt = dt.datetime.fromtimestamp(T - 1000 + 86400, TZ).strftime("%Y-%m-%d")
with open(os.path.join(logdir, f"access-{nxt}.log"), "w", encoding="utf-8") as f:
    f.write(logline(T - 930) + logline(T - 925))
check("換日新檔接著讀", col.ingest_emb_log() == 2 and col._state("emb_log")["file"] == f"access-{nxt}.log")
with open(os.path.join(logdir, f"access-{nxt}.log"), "w", encoding="utf-8") as f:
    f.write(logline(T - 920))  # 檔被截短重寫（輪替）
check("檔案變短就從頭讀", col.ingest_emb_log() == 1)
# 到這裡 EMB 共 9 筆：成功 8（主節點 7、備援 1）、錯 1

# 節點探測（假的探測函式）
col.probes = [("emb-gw", "EMB 閘道", lambda: (True, 12.0, PRIMARY)), ("emb-primary", "EMB 主節點", lambda: (True, 8.0, "HTTP 200")),
              ("postgres", "資料庫", lambda: (_ for _ in ()).throw(OSError("連不上")))]
col.now = lambda: T - 1000
col.probe_nodes()
col.now = lambda: T
col.probe_nodes()
st = {r[0]: r for r in col.db().execute("SELECT node, up, since, ms FROM node_state").fetchall()}
check("節點探測寫 node_state，since 不會每分鐘重設", st["emb-gw"][1] == 1 and st["emb-gw"][2] == T - 1000 and st["postgres"][1] == 0)

# 節點狀態與最後成功時間（獨立假 DB，不影響後面的統計）
nc = monitor.Collector(db_path=os.path.join(ROOT, "nodes", "metrics.db"), now=lambda: T)
nc.probes = [("pending", "尚未開始", lambda: (_ for _ in ()).throw(AssertionError("不應啟動探測")))]
check("DB 尚未建立也回報已配置節點不明", nc.series()["nodes"][0]["state"] == "unknown")
nc.probes = []
nc._cache.clear()
nc.db()
check("沒有探測紀錄就沒有假造節點", nc.series()["nodes"] == [])
nc.probes = [("n", "測試節點", lambda: (True, 4.0, "成功"))]
nc._cache.clear()
check("已配置但尚無結果的節點是不明", nc.series()["nodes"][0] == {
    "key": "n", "label": "測試節點", "up": False, "ms": None, "since": None, "state": "unknown", "last_seen": None,
    "slow_ms": None, "rps": None, "req_1m": None})
nc.probe_nodes()
nc._cache.clear()
node = nc.series()["nodes"][0]
check("成功探測在線且最後成功是 ISO 時間", node["state"] == "up" and node["up"] is True and node["last_seen"] == monitor._utc(T))
nc.now = lambda: T + 179.9
nc._cache.clear()
check("三輪期限前仍在線", nc.series()["nodes"][0]["state"] == "up")
nc.now = lambda: T + 180
check("恰好三輪仍在線", nc.series()["nodes"][0]["state"] == "up")
nc.now = lambda: T + 180.01
node = nc.series()["nodes"][0]
check("快取不能越過三輪期限且舊 up 不變", node["state"] == "unknown" and node["up"] is True and node["last_seen"] == monitor._utc(T))
nc.now = lambda: T + 240
nc.probes = [("n", "測試節點", lambda: (False, None, "失敗")),
             ("never", "從未成功", lambda: (_ for _ in ()).throw(OSError("連不上")))]
nc.probe_nodes()
nc._cache.clear()
ns = {n["key"]: n for n in nc.series()["nodes"]}
check("成功後失敗是離線但最後成功不變", ns["n"]["state"] == "down" and ns["n"]["up"] is False and ns["n"]["last_seen"] == monitor._utc(T))
check("探測例外是離線且從未成功時間為空", ns["never"]["state"] == "down" and ns["never"]["last_seen"] is None)
nc.now = lambda: T + 420
nc._cache.clear()
check("失敗結果恰好三輪仍離線", nc.series()["nodes"][0]["state"] == "down")
nc.now = lambda: T + 420.01
check("失敗結果過期也變不明", nc.series()["nodes"][0]["state"] == "unknown")
nc.db().execute("UPDATE node_state SET ts = NULL WHERE node = 'never'")
nc.db().commit()
nc._cache.clear()
check("結果缺探測時間是不明", {n["key"]: n for n in nc.series()["nodes"]}["never"]["state"] == "unknown")
nc._pool.shutdown(wait=True)

# 舊 schema 沒有新增欄位也能直接唯讀輸出，不必先跑 open()。
legacy_path = os.path.join(ROOT, "legacy.db")
lc = sqlite3.connect(legacy_path)
for stmt in monitor._SCHEMA:
    lc.execute(stmt)
lc.executemany("INSERT INTO node_state VALUES (?, ?, ?, ?, ?, ?, ?)", [
    ("old-up", "舊在線", 1, T - 600, 1.0, T, ""),
    ("old-down", "舊離線", 0, T - 60, None, T, ""),
    ("old-never", "舊無成功", 0, T - 60, None, T, "")])
lc.execute("INSERT INTO node_1m VALUES (?, 'old-down', 1, 2.0)", (T - 120,))
lc.commit()
lc.close()
old = monitor.Collector(db_path=legacy_path, now=lambda: T)
ns = {n["key"]: n for n in old.series()["nodes"]}
check("舊 DB 在線以成功的 ts 作最後成功時間", ns["old-up"]["state"] == "up" and ns["old-up"]["last_seen"] == monitor._utc(T))
check("舊 DB 離線只從歷史成功取時間", ns["old-down"]["state"] == "down" and ns["old-down"]["last_seen"] == monitor._utc(T - 120)
      and ns["old-never"]["last_seen"] is None)
old.probes = [("old-down", "舊離線", lambda: (False, None, "仍失敗"))]
old.probe_nodes()
old.db().execute("DELETE FROM node_1m WHERE up = 1")
old.db().commit()
old._cache.clear()
check("舊成功證據保存後不受歷史清理影響", {n["key"]: n for n in old.series()["nodes"]}["old-down"]["last_seen"] == monitor._utc(T - 120))
old._pool.shutdown(wait=True)
reopened = monitor.Collector(db_path=nc.db_path, now=lambda: T + 240)
check("重開收集器仍保留成功後失敗的最後成功時間", {n["key"]: n for n in reopened.series()["nodes"]}["n"]["last_seen"] == monitor._utc(T))

# ── LAYA：正常／慢／不通／不明（獨立假 DB） ─────────────────────────
lay = monitor.Collector(db_path=os.path.join(ROOT, "laya", "metrics.db"), emb_log_dir=os.path.join(ROOT, "沒有這個資料夾"), now=lambda: T)
lay.slow_ms = {"laya": 500.0}
lay.probes = [("laya", "LAYA 客服分類 192.0.2.77:8765", lambda: (True, 30.0, "HTTP 200"))]
lay.probe_nodes()
ln = lay.series()["nodes"][0]
check("LAYA 30 毫秒是正常、帶慢的門檻 500", ln["state"] == "up" and ln["ms"] == 30.0 and ln["slow_ms"] == 500.0 and ln["last_seen"] == monitor._utc(T))
lay.now = lambda: T + 60
lay.probes = [("laya", "LAYA 客服分類 192.0.2.77:8765", lambda: (True, 800.0, "HTTP 200"))]
lay.probe_nodes()
lay._cache.clear()
ln = lay.series()["nodes"][0]
check("LAYA 800 毫秒是慢、up 仍是 true、最後成功跟著更新", ln["state"] == "slow" and ln["up"] is True and ln["last_seen"] == monitor._utc(T + 60))
lay.now = lambda: T + 120
lay.probes = [("laya", "LAYA 客服分類 192.0.2.77:8765", lambda: (_ for _ in ()).throw(TimeoutError("逾時")))]
lay.probe_nodes()
lay._cache.clear()
ln = lay.series()["nodes"][0]
check("LAYA 逾時是不通、最後成功停在上一次", ln["state"] == "down" and ln["last_seen"] == monitor._utc(T + 60))
lay.now = lambda: T + 120 + 180.01
lay._cache.clear()
check("LAYA 超過三輪沒結果是不明", lay.series()["nodes"][0]["state"] == "unknown")
lay.slow_ms = {}
lay.now = lambda: T + 180
lay.probes = [("laya", "LAYA", lambda: (True, 800.0, ""))]
lay.probe_nodes()
lay._cache.clear()
check("沒設慢門檻的節點不會變慢", lay.series()["nodes"][0]["state"] == "up")
lay._pool.shutdown(wait=True)

# default_probes：LAYA 打 /health、逾時 3 秒、預設慢門檻 500；設 off 就不探
_seen = {}


def _fake_probe_http(url, timeout=3.0, ok_status=None, headers=None):
    _seen[url] = (timeout, ok_status)
    return lambda: (True, 1.0, "")


_orig_probe_http = monitor.probe_http
monitor.probe_http = _fake_probe_http
os.environ["LAYA_URL"] = "http://192.0.2.77:8765"
try:
    dc = monitor.Collector(db_path=os.path.join(ROOT, "dp", "metrics.db"), emb_conf=os.path.join(ROOT, "沒有.conf"))
    keys = [k for k, _, _ in monitor.default_probes(dc)]
    check("預設探測有 LAYA，打 /health、逾時 3 秒、要 200", "laya" in keys and _seen.get("http://192.0.2.77:8765/health") == (3.0, 200))
    check("LAYA 預設慢門檻 500 毫秒", dc.slow_ms.get("laya") == 500.0)
    os.environ["MONITOR_LAYA_URL"] = "off"
    dc2 = monitor.Collector(db_path=os.path.join(ROOT, "dp2", "metrics.db"), emb_conf=os.path.join(ROOT, "沒有.conf"))
    check("MONITOR_LAYA_URL=off 就不探、也不設門檻", "laya" not in [k for k, _, _ in monitor.default_probes(dc2)] and "laya" not in dc2.slow_ms)
finally:
    os.environ.pop("MONITOR_LAYA_URL", None)
    os.environ.pop("LAYA_URL", None)
    monitor.probe_http = _orig_probe_http
check("emb.conf 上游對到節點 key", monitor.emb_probe_keys(servers) == [("emb-primary", f"EMB 主節點 {PRIMARY}", PRIMARY),
                                                                   ("emb-backup", f"EMB 備援 {BACKUP}", BACKUP)])

# ── 每秒請求數（近 1 分鐘）：用假 log 檔與假工具呼叫 ─────────────────
rdir = os.path.join(ROOT, "rps-logs")
os.makedirs(rdir)
rday = dt.datetime.fromtimestamp(T, TZ).strftime("%Y-%m-%d")
with open(os.path.join(rdir, f"access-{rday}.log"), "w", encoding="utf-8") as f:
    for k in range(30):
        f.write(logline(T - 59 + k))                                  # 近 1 分鐘 30 筆主節點
    for k in range(6):
        f.write(logline(T - 50 + k, ua=f"{PRIMARY}, {BACKUP}"))       # 6 筆切到備援
    f.write(logline(T - 40, probe="1") + logline(T - 30, probe="1"))  # 監控探測：不算
    f.write(logline(T - 120) + logline(T - 61))                       # 超過 1 分鐘：不算
rc = monitor.Collector(db_path=os.path.join(ROOT, "rps", "metrics.db"), emb_log_dir=rdir, emb_conf=conf, now=lambda: T)
rc.probes = [("emb-gw", "EMB 閘道", lambda: (True, 10.0, "")), ("emb-primary", f"EMB 主節點 {PRIMARY}", lambda: (True, 5.0, "")),
             ("emb-backup", f"EMB 備援 {BACKUP}", lambda: (True, 5.0, "")), ("mem0-mcp", "圓桌 MCP", lambda: (True, 2.0, "")),
             ("mem0-api", "mem0-api", lambda: (True, 3.0, "")), ("laya", "LAYA", lambda: (True, 6.0, ""))]
monitor._queue.clear()
for k in range(12):
    monitor._queue.append(("call", T - 55 + k, "claude-a", "memory_search", 50.0, 1, "", 0))
for k in range(3):
    monitor._queue.append(("call", T - 50 + k, "relay", "inbox", 5.0, 1, "", 1))  # 自動查詢也是請求
monitor._queue.append(("call", T - 90, "claude-a", "memory_get", 5.0, 1, "", 0))  # 超過 1 分鐘：不算
rc.tick()
cnt = rc._state("node_rps")
check("tick 會數每秒請求數並存起來", cnt and cnt["n"] == {"mem0-mcp": 15, "emb-gw": 36, "emb-primary": 30, "emb-backup": 6} and cnt["win"] == 60)
rn = {n["key"]: n for n in rc.series()["nodes"]}
check("EMB 閘道每秒 0.6（近 1 分鐘 36 筆，探測與舊的不算）", rn["emb-gw"]["rps"] == 0.6 and rn["emb-gw"]["req_1m"] == 36)
check("GPU 主節點 0.5、備援 0.1（看最後回應的上游）", rn["emb-primary"]["rps"] == 0.5 and rn["emb-backup"]["rps"] == 0.1)
check("圓桌 MCP 每秒 0.25（工具呼叫含自動查詢）", rn["mem0-mcp"]["rps"] == 0.25 and rn["mem0-mcp"]["req_1m"] == 15)
check("沒有資料來源的節點是 null，不是 0", rn["mem0-api"]["rps"] is None and rn["mem0-api"]["req_1m"] is None and rn["laya"]["rps"] is None)
rc.now = lambda: T + 180.01
rc._cache.clear()
rn = {n["key"]: n for n in rc.series()["nodes"]}
check("超過三輪沒重數，每秒請求數變 null", rn["emb-gw"]["rps"] is None and rn["mem0-mcp"]["rps"] is None)
rc.now = lambda: T + 3600
rc.emb_log_dir = os.path.join(ROOT, "沒有這個資料夾")
n_ = rc.count_rps()
check("讀不到 access log 時 EMB 節點不放、MCP 照數（一小時後是 0）", n_ == {"mem0-mcp": 0})
rc._cache.clear()
rn = {n["key"]: n for n in rc.series()["nodes"]}
check("讀不到 log：EMB 是 null、MCP 是真的 0", rn["emb-gw"]["rps"] is None and rn["mem0-mcp"]["rps"] == 0.0)
if rc._pool:
    rc._pool.shutdown(wait=True)
monitor._queue.clear()

# 快照
col.snapshot_fn = lambda: {"totals": {"roundtable": 3813, "roundtable-ops": 120}, "inbox": {"unread": 2, "needs_action": 1, "claims": 3, "stale_claims": 1}}
col.take_snapshot(T - 1000)

# 彙總（桶要收完 60 秒才算；只算啟動以後的桶）
col.aggregate(T)
aggs = [r[0] for r in col.db().execute("SELECT ts FROM agg_5m ORDER BY ts").fetchall()]
check("彙總從啟動那一桶開始、到收完的最後一桶", aggs and aggs[0] == B(T - 3000) and aggs[-1] == max(b for b in range(aggs[0], int(T), 300) if b + 360 <= T))
blob = json.loads(col.db().execute("SELECT data FROM agg_5m WHERE ts = ?", (B(T - 1000),)).fetchone()[0])
check("桶裡人用 14、輪詢 1、工具錯 1、/mcp 錯 1", blob["calls"] == {"human": 14, "poll": 1} and blob["errors"]["tool"] == 1 and blob["errors"]["mcp"] == 1)
check("桶裡搜尋耗時 p50=110", blob["search_ms"]["p50"] == 110.0 and blob["tool_ms"]["n"] == 15)
check("桶裡 EMB：成功 8、主節點 7、錯 1", blob["emb"]["calls"] == 8 and blob["emb"]["primary"] == 7 and blob["errors"]["emb"] == 1)

# ── series：格式要跟 SPEC 一模一樣 ────────────────────────────────
SPEC = {
    "generated": "", "range": "", "step_sec": 0,
    "summary": {"writes_24h": {"add": 0, "update": 0, "delete": 0}, "calls_24h": {"total": 0, "human": 0, "poll": 0},
                "tool_ms_24h": {"p50": 0, "p95": 0}, "search_ms_24h": {"p50": 0, "p95": 0},
                "emb_24h": {"calls": 0, "p50_ms": 0, "p95_ms": 0, "primary": "", "primary_share": 0, "fallback": 0},
                "errors_24h": {"mcp": 0, "tool": 0, "emb": 0}, "totals": {"roundtable": 0, "roundtable-ops": 0},
                "inbox": {"unread": 0, "needs_action": 0, "claims": 0, "stale_claims": 0},
                "eval": {"date": "", "hit1": 0, "hit5": 0, "p50_ms": 0, "scored": 0},
                "eval_spot": {k: {"at": "", "hit1": 0, "hit5": 0, "scored": 0, "today_planned": 0, "today_done": 0, "today_skipped": 0}
                              for k in ("random", "idle")}},
    "nodes": [{"key": "", "label": "", "up": True, "ms": 0, "since": "", "state": "", "last_seen": "", "slow_ms": 0, "rps": 0, "req_1m": 0}],
    "series": {"t": [], "writes": {"add": [], "update": [], "delete": []}, "calls": {"human": [], "poll": []},
               "calls_by_tool": {}, "calls_by_agent": {}, "tool_ms": {"p50": [], "p95": []}, "search_ms": {"p50": [], "p95": []},
               "emb": {"calls": [], "p50_ms": [], "p95_ms": [], "primary_share": []}, "errors": {"mcp": [], "tool": [], "emb": []},
               "totals": {}, "node_ms": {}},
    "eval_daily": {"t": [], "hit1": [], "hit5": [], "p50_ms": [], "scored": []},
    "eval_spot": {"t": [], "kind": [], "hit1": [], "hit5": [], "p50_ms": [], "scored": [], "load_calls": [], "load_emb": []},
}
DYNAMIC = {("series", "calls_by_tool"), ("series", "calls_by_agent"), ("series", "totals"), ("series", "node_ms"), ("summary", "totals"), ("summary", "inbox")}


def shape_ok(spec, got, path=()):
    """got 的結構要和 spec 一樣：dict 的 key 相同（動態 key 的只檢查值是 list）、list 的每一項照 spec 第一項檢查。"""
    if isinstance(spec, dict):
        if not isinstance(got, dict):
            return f"{'/'.join(path)} 不是物件"
        if path in DYNAMIC:
            bad = [k for k, v in got.items() if not isinstance(v, (list, int, float)) and v is not None]
            return f"{'/'.join(path)} 的值型別不對：{bad}" if bad else ""
        if set(spec) - set(got):
            return f"{'/'.join(path)} 缺 {sorted(set(spec) - set(got))}"
        if set(got) - set(spec):
            return f"{'/'.join(path)} 多了 {sorted(set(got) - set(spec))}"
        for k in spec:
            why = shape_ok(spec[k], got[k], path + (k,))
            if why:
                return why
        return ""
    if isinstance(spec, list):
        if not isinstance(got, list):
            return f"{'/'.join(path)} 不是陣列"
        for item in got:
            if spec:
                why = shape_ok(spec[0], item, path + ("[]",))
                if why:
                    return why
        return ""
    return ""


s = col.series("24h")
why = shape_ok(SPEC, s)
check("24h 的 JSON 結構和 SPEC 一樣" + (f"（{why}）" if why else ""), why == "")
t = s["series"]["t"]
check("24h：288 個點、step 300、最後一桶是收完的那桶", len(t) == 288 and s["step_sec"] == 300 and t[-1] == B(T - 60) and t[1] - t[0] == 300)
i = t.index(B(T - 1000))
S_ = s["series"]
check("每個數列長度都 288", all(len(v) == 288 for k in ("writes", "calls", "tool_ms", "search_ms", "emb", "errors") for v in S_[k].values()))
check("沒資料的桶是 null、不是 0", S_["calls"]["human"][0] is None and S_["writes"]["add"][0] is None and S_["emb"]["p50_ms"][0] is None)
check("啟動後沒呼叫的桶是 0", S_["calls"]["human"][t.index(B(T - 2400))] == 0)
check("調用：人用 14、輪詢 1", S_["calls"]["human"][i] == 14 and S_["calls"]["poll"][i] == 1)
check("寫入：ADD 2、UPDATE 1、DELETE 1", S_["writes"]["add"][i] == 2 and S_["writes"]["update"][i] == 1 and S_["writes"]["delete"][i] == 1)
check("搜尋耗時 p50 110、p95 120", S_["search_ms"]["p50"][i] == 110.0 and S_["search_ms"]["p95"][i] == 120.0)
check("EMB：8 次成功、主節點接住比例 7/8", S_["emb"]["calls"][i] == 8 and S_["emb"]["primary_share"][i] == round(7 / 8, 3))
check("錯誤：mcp 1、tool 1、emb 1", S_["errors"]["mcp"][i] == 1 and S_["errors"]["tool"][i] == 1 and S_["errors"]["emb"][i] == 1)
check("按工具：前 8 名＋其他", len(S_["calls_by_tool"]) == 9 and "其他" in S_["calls_by_tool"] and S_["calls_by_tool"]["memory_search"][i] == 3
      and sum(v[i] for v in S_["calls_by_tool"].values()) == 15)
check("按 agent：三個", set(S_["calls_by_agent"]) == {"claude-a", "relay", "codex-b"} and S_["calls_by_agent"]["codex-b"][i] == 10)
check("記憶總數快照進 series", S_["totals"]["roundtable"][i] == 3813 and S_["totals"]["roundtable"][0] is None)
check("節點耗時進 node_ms", S_["node_ms"]["emb-gw"][i] == 12.0)
sm = s["summary"]
check("摘要：24 小時調用 15（人用 14、輪詢 1）", sm["calls_24h"] == {"total": 15, "human": 14, "poll": 1})
check("摘要：寫入 add 3、update 1、delete 1", sm["writes_24h"] == {"add": 3, "update": 1, "delete": 1})
check("摘要：EMB 主節點從 emb.conf 來、備援接住 1 次", sm["emb_24h"]["primary"] == PRIMARY and sm["emb_24h"]["fallback"] == 1 and sm["emb_24h"]["calls"] == 8)
check("摘要：工具 p50／搜尋 p50 從原始列精算", sm["tool_ms_24h"]["p50"] == 7.0 and sm["search_ms_24h"]["p50"] == 110.0)
check("摘要：記憶總數與收件匣", sm["totals"] == {"roundtable": 3813, "roundtable-ops": 120} and sm["inbox"]["stale_claims"] == 1)
check("摘要：還沒考題時 eval 各欄是 null", sm["eval"] == {"date": None, "hit1": None, "hit5": None, "p50_ms": None, "scored": None})
nodes = {n["key"]: n for n in s["nodes"]}
check("節點燈號：3 個、資料庫是紅的、since 是 UTC 字串", len(nodes) == 3 and nodes["postgres"]["up"] is False and nodes["emb-gw"]["since"].endswith("Z") and nodes["emb-gw"]["ms"] == 12.0)
check("輸出可以轉成 JSON", json.dumps(s, ensure_ascii=False))

col._cache.clear()
s7 = col.series("7d")
check("7d：168 個點、step 3600、人用 14 併進同一小時", len(s7["series"]["t"]) == 168 and s7["step_sec"] == 3600
      and s7["series"]["calls"]["human"][s7["series"]["t"].index(int((T - 1000) // 3600) * 3600)] == 14 and shape_ok(SPEC, s7) == "")
s30 = col.series("30d")
check("30d：30 個點、step 86400、今天那一格有資料", len(s30["series"]["t"]) == 30 and s30["step_sec"] == 86400
      and s30["series"]["calls"]["human"][-1] == 14 and shape_ok(SPEC, s30) == "")
check("看不懂的 range 當 24h", col.series("x")["range"] == "24h")

# 每日彙總與清理
col.rollup_daily(T + 86400)
check("每日彙總寫進 agg_1d", col.db().execute("SELECT count(*) FROM agg_1d").fetchone()[0] == 1)
col.db().execute("INSERT INTO calls_raw VALUES (?, 'x', 'y', 1, 1, '', 0)", (T - 9 * 86400,))
col.cleanup(T)
check("8 天前的原始列清掉、新的留著", col.db().execute("SELECT count(*) FROM calls_raw WHERE ts < ?", (T - 8 * 86400,)).fetchone()[0] == 0
      and col.db().execute("SELECT count(*) FROM calls_raw").fetchone()[0] == 15)

# 每晚考題（假的 monitor_eval）
qpath = os.path.join(ROOT, "questions.json")
with open(qpath, "w", encoding="utf-8") as f:
    f.write('{"user_id": "roundtable", "questions": []}')
calls = {}


def fake_run_nightly(search_fn, questions_path, sleep_sec=0.2, limit=None, *, exists_fn=None, existing_ids=None, max_sec=0, log=None):
    calls.update(q=questions_path, sleep=sleep_sec, ids=existing_ids, search=search_fn("q", "roundtable", 5))
    return {"hit1": 0.42, "hit5": 0.66, "p50_ms": 95.0, "p95_ms": 200.0, "scored": 1000, "total": 1020, "skipped_missing": 20,
            "errors": 0, "elapsed_sec": 400.0, "started": "s", "finished": "f"}


sys.modules["monitor_eval"] = types.SimpleNamespace(run_nightly=fake_run_nightly)
col.search_fn = lambda q, uid, limit: [{"id": "m1"}]
col.eval_setup_fn = lambda: {"existing_ids": {"m1"}}
col.questions = qpath
col.eval_sleep = 0.5
col.maybe_eval(T)
for th in threading.enumerate():
    if th.name == "monitor-eval":
        th.join(10)
row = col.db().execute("SELECT hit1, hit5, p50_ms, scored, skipped_missing, extra, finished FROM eval_daily").fetchone()
check("考題跑完寫進 eval_daily（含 skipped_missing、其他欄存 JSON）", row and row[0] == 0.42 and row[4] == 20 and json.loads(row[5])["elapsed_sec"] == 400.0 and row[6])
check("考題拿到題庫路徑、節流秒數、現存 id 集合、正式搜尋", calls["q"] == qpath and calls["sleep"] == 0.5 and calls["ids"] == {"m1"} and calls["search"] == [{"id": "m1"}])
col.maybe_eval(T + 60)
check("同一天不會再跑第二次", col.db().execute("SELECT count(*) FROM eval_daily").fetchone()[0] == 1 and not col._eval_running)
col._cache.clear()
s = col.series("24h")
check("摘要與 eval_daily 數列有考題結果", s["summary"]["eval"]["hit5"] == 0.66 and s["summary"]["eval"]["scored"] == 1000
      and s["eval_daily"]["hit1"] == [0.42] and s["eval_daily"]["t"] == [monitor._day_start(T)] and shape_ok(SPEC, s) == "")
sys.modules.pop("monitor_eval")
col2 = monitor.Collector(db_path=os.path.join(ROOT, "m2.db"), now=lambda: T)
col2.search_fn = col.search_fn
col2.questions = os.path.join(ROOT, "沒有這個檔.json")
col2.maybe_eval(T)
check("題庫不存在：今天略過、不出錯", col2.db().execute("SELECT count(*) FROM eval_daily").fetchone()[0] == 0 and col2._eval_skip_day)

# 工具呼叫掛鉤：計時、出錯也記、例外照樣往外丟
monitor._queue.clear()


class FakeTM:
    async def call_tool(self, name, arguments, context=None, convert_result=False):
        if name == "bad":
            raise KeyError("沒有這個工具")
        return ["ok", name, arguments, convert_result]


tm = FakeTM()
monitor.install(tm, lambda: "claude-a")


async def drive():
    r = await tm.call_tool("memory_search", {"query": "x"}, convert_result=True)
    try:
        await tm.call_tool("bad", {})
    except KeyError:
        return r, "raised"
    return r, "swallowed"


r, raised = asyncio.run(drive())
q = list(monitor._queue)
check("掛鉤後回傳值與參數原樣通過、例外照樣往外丟", r == ["ok", "memory_search", {"query": "x"}, True] and raised == "raised")
check("兩次呼叫都記了：成功一筆、失敗一筆帶錯誤類型", len(q) == 2 and q[0][3] == "memory_search" and q[0][5] == 1 and q[1][3] == "bad" and q[1][5] == 0 and q[1][6] == "KeyError"
      and q[0][2] == "claude-a" and q[0][4] >= 0)
check("inbox 算輪詢、monitor-eval 算輪詢、其他算人用", (monitor.record_call("x", "inbox", 1, True) or monitor._queue[-1][7]) == 1
      and (monitor.record_call("monitor-eval", "memory_search", 1, True) or monitor._queue[-1][7]) == 1
      and (monitor.record_call("claude-a", "memory_search", 1, True) or monitor._queue[-1][7]) == 0)

# ASGI 包裝：記來源 IP、數 /mcp 的錯誤回應
monitor._queue.clear()
seen = {}


async def inner(scope, receive, send):
    seen["ip"] = monitor._client_ip.get()
    await send({"type": "http.response.start", "status": 500, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def drive_asgi():
    out = []
    w = monitor.wrap_asgi(inner)
    await w({"type": "http", "path": "/mcp", "client": ("192.0.2.9", 1234)}, None, lambda m: out.append(m) or asyncio.sleep(0))
    await w({"type": "http", "path": "/board", "client": ("192.0.2.9", 1234)}, None, lambda m: out.append(m) or asyncio.sleep(0))
    return out


out = asyncio.run(drive_asgi())
check("ASGI 包裝：來源 IP 進 contextvar、/mcp 回 500 記一筆、/board 不記、回應原樣送出",
      seen["ip"] == "192.0.2.9" and [x[0] for x in monitor._queue] == ["mcp_err"] and monitor._queue[0][2] == 500 and len(out) == 4)

# ── server.py 的掛鉤：頁面與 API 路由、四頁一起採用／回滾、舊備份照樣能讀 ────────────────


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
UI = os.path.join(ROOT, "ui")
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE_LAPTOP": "t1", "UI_DIR": UI,
                   "BOARD_USER": "u", "BOARD_PASSWORD": "p" * 16})
spec = importlib.util.spec_from_file_location("srv", os.path.join(SRC, "server.py"))
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)
from starlette.testclient import TestClient  # noqa: E402

check("server.py 掛鉤載入成功（_monitor 不是 None）", S._monitor is monitor)
check("_UI_FILES 含 monitor.html，_REV_META 認得 monitor", "monitor.html" in S._UI_FILES and S._REV_META.search('<meta name="monitor-revision" content="7"'))
monitor.COLLECTOR = col  # API 讀我們上面那個假資料庫


def put(which, rev, version, names=None):
    d = os.path.join(UI, which)
    os.makedirs(d, exist_ok=True)
    for name, kind in zip(S._UI_FILES, ("board", "memory", "release", "monitor")):
        if names is not None and name not in names:
            continue
        page = ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="%s-version" content="%s">'
                '<meta name="%s-revision" content="%d"></head><body>%s <a href="/board/preview/memory">記憶</a></body></html>' % (kind, version, kind, rev, name))
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(page)


S._cookie_ok = lambda request: True
S._list_all = lambda: []
c = TestClient(S.board_app, base_url="http://board.test")
put("stable", 31, "1.8.0", names=("board.html", "memory.html", "versions.html"))  # 監控頁上線前的正式版：只有三頁
put("preview", 32, "1.9.0")
r = c.get("/board/monitor")
check("還沒採用前 /board/monitor 回 404、訊息是中文", r.status_code == 404 and "監控頁" in r.text)
r = c.get("/board/preview/monitor")
check("/board/preview/monitor 回 200、塞了版號 preview:32", r.status_code == 200 and '"preview:32"' in r.text and "monitor.html" in r.text)
r = c.get("/board/api/monitor/series?range=7d")
check("/board/api/monitor/series 回 200、range=7d、168 個點", r.status_code == 200 and r.json()["range"] == "7d" and len(r.json()["series"]["t"]) == 168
      and r.headers.get("cache-control") == "no-store")
check("沒帶 range 就是 24h", c.get("/board/api/monitor/series").json()["range"] == "24h")
check("三頁的正式版（沒有監控頁）版號照樣讀得出", S._ui_stable_rev(S._ui_snapshot("stable")) == 31)
stamp = S._ui_promote()
check("採用：四頁一起換到正式版", stamp not in ("already-current",) and S._ui_stable_rev(S._ui_snapshot("stable")) == 32
      and os.path.isfile(os.path.join(UI, "stable", "monitor.html")))
r = c.get("/board/monitor")
check("採用後 /board/monitor 回 200、預覽連結換成正式版", r.status_code == 200 and '"stable:32"' in r.text and 'href="/board/memory"' in r.text)
baks = [n for n in S._ui_backup_entries() if n.startswith("ui.bak-")]
check("採用前那份三頁備份讀得出版號 31", baks and S._ui_bak_rev(baks[-1]) == (31, "1.8.0"))
old3 = {"schema": 1, "files": {n: None for n in ("board.html", "memory.html", "versions.html")}}
with open(os.path.join(ROOT, "old.json"), "w", encoding="utf-8") as f:
    json.dump(old3, f)
check("舊格式（三頁）備份可以載入，監控頁當作沒有", S._ui_load_snapshot(os.path.join(ROOT, "old.json"))["monitor.html"] is None)
S._ui_rollback(31)
check("回滾到沒有監控頁的那一版：正式版監控頁跟著拿掉", S._ui_stable_rev(S._ui_snapshot("stable")) == 31
      and not os.path.isfile(os.path.join(UI, "stable", "monitor.html")) and c.get("/board/monitor").status_code == 404)
S._ui_rollback(32)
check("再換回 32：監控頁回來", c.get("/board/monitor").status_code == 200)


async def _no(request):
    return False


S._cookie_ok = lambda request: False
S._basic_ok = _no
r = c.get("/board/preview/monitor")
check("沒登入看預覽監控頁 → 登入頁（200）", r.status_code == 200 and "<form" in r.text)
check("沒登入打 API → 401", c.get("/board/api/monitor/series").status_code == 401)

# _mon_snapshot 只走 STORE
calls_count = []


class FakeStore:
    def count(self, uid):
        calls_count.append(uid)
        return {"roundtable": 3813, "roundtable-ops": 120}[uid]


S.STORE = FakeStore()
S._list_uid = lambda uid: [
    {"id": "1", "metadata": {"type": "message", "to": "claude", "needs_action": "yes", "read_by": ""}},
    {"id": "2", "metadata": {"type": "message", "to": "claude", "needs_action": "", "read_by": "claude"}},
    {"id": "3", "metadata": {"type": "message", "to": "claude", "read_by": "", "closed_by": "x"}},
    {"id": "4", "metadata": {"type": "claim"}, "updated_at": "2020-01-01T00:00:00Z"},
    {"id": "5", "metadata": {"type": "claim"}, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()},
]
snap = S._mon_snapshot()
check("快照：總數走 STORE.count，收件匣 1 未讀 1 待辦、認領 2 其中 1 過期",
      snap["totals"] == {"roundtable": 3813, "roundtable-ops": 120} and snap["inbox"] == {"unread": 1, "needs_action": 1, "claims": 2, "stale_claims": 1}
      and sorted(calls_count) == ["roundtable", "roundtable-ops"])
S.memory_search = lambda query, limit=10: [{"id": "m1", "q": query, "limit": limit}]
check("考題搜尋走 memory_search", S._mon_eval_search("q", "roundtable", 5) == [{"id": "m1", "q": "q", "limit": 5}])
try:
    S._mon_eval_search("q", "other", 5)
    check("考題搜尋：區不對就拒絕", False)
except ValueError:
    check("考題搜尋：區不對就拒絕", True)
kw = S._mon_eval_setup()
check("考題開跑前拿到現存 id 集合、身分改成 monitor-eval", kw["existing_ids"] == {"1", "2", "3", "4", "5"} and S.current_agent.get() == "monitor-eval")

print("全部通過" if not FAILS else "有 %d 項失敗" % len(FAILS))
sys.exit(1 if FAILS else 0)
