"""運作監控：工具呼叫計時、每分鐘彙總、EMB 閘道流量、節點探測、每晚考題，全部在背景執行緒做，
結果寫 sqlite（MONITOR_DIR/metrics.db，WAL），`series(range)` 產出監控頁要的 JSON（格式見 monitor-page/SPEC.md）。

原則：
- 工具呼叫路徑上只做一件事：把一列 (ts, agent, tool, ms, ok, err, poll) append 進記憶體佇列，其他都在背景執行緒。
- 任何收集失敗只印一行 log，不能影響工具呼叫、也不能讓服務起不來（server.py 只在 __main__ 才 start()）。
- 保留：原始列 8 天、5 分鐘彙總 90 天、每日彙總與考題結果永久；磁碟總量目標 <200 MB。

環境變數（都有預設，不寫死內網位址）：
  MONITOR_DIR            metrics.db 放哪（預設 /metrics，由 docker-compose 把主機資料夾掛進來）
  MONITOR_HISTORY_DB     Mem0 history.db 路徑（唯讀；預設 /mem0-history/history.db）
  MONITOR_EMB_LOG_DIR    emb-gw 的 access log 資料夾（唯讀；預設 /emb-logs，檔名 access-YYYY-MM-DD.log）
  MONITOR_EMB_CONF       emb.conf 路徑（唯讀；預設 /emb-gw/emb.conf；讀不到就從 log 推斷主節點）
  MONITOR_EMB_PRIMARY    手動指定主節點 host:port（emb.conf 讀不到時才用）
  MONITOR_POLL_TOOLS     算「機器輪詢」的工具名（逗號分隔，預設 inbox）
  MONITOR_POLL_IPS       算「機器輪詢」的來源 IP（逗號分隔，例如等信器那台；預設空）
  MONITOR_POLL_AGENTS    算「機器輪詢」的 agent 名（預設 monitor-eval，每晚考題自己打的搜尋不算人用）
  MONITOR_SEARCH_TOOLS   算「搜尋延遲」的工具名（預設 memory_search）
  MONITOR_QUESTIONS      每晚考題題庫（預設 /app/eval/questions.json）
  MONITOR_EVAL_SLEEP     考題題與題之間停幾秒（預設 2）
  MONITOR_EVAL_WINDOW    考題時段，當地時間小時 "0-6"（預設 0 到 6 點之間跑一次）
  ── 白天抽考（eval_spot）：除了每晚那次（nightly），白天另外抽小份考題，分兩種 ──
     random＝事先抽好的隨機時間點，到點就考、不管忙不忙（順便記下當時多忙）；
     idle  ＝事先抽好的最早時間，到點後等到「近 10 分鐘夠閒」才考，等太久就記一筆略過。
  MONITOR_SPOT_ON        設 1 才開（預設 0：沒經批准部署也不會自己開始考）
  MONITOR_SPOT_WINDOW    白天抽考可以開考的時段（當地 "HH:MM-HH:MM"，預設 06:00-23:30；離峰 00:00–05:59 一律不碰）
  MONITOR_SPOT_RANDOM    每天 random 幾次（"最少-最多"，預設 2-4，每天隨機）
  MONITOR_SPOT_IDLE      每天 idle 幾次（預設 2-4）
  MONITOR_SPOT_N         每次考幾題（預設 20，從題庫隨機抽，不重複）
  MONITOR_SPOT_SLEEP     題與題之間停幾秒（預設 1）
  MONITOR_SPOT_GAP       同一天任兩次預定時間至少隔幾分鐘（預設 60）
  MONITOR_SPOT_IDLE_CALLS idle 的門檻：近 10 分鐘人用的工具呼叫（不含輪詢、不含考題自己）最多幾次（預設 10）
  MONITOR_SPOT_IDLE_EMB  idle 的門檻：近 10 分鐘 EMB 閘道請求（不含探測）最多幾次（預設 30）
  MONITOR_SPOT_IDLE_WAIT idle 到點後最多等幾分鐘（預設 180，等不到就記 skipped）
  MONITOR_SELF_URL       探測自己的網址（預設 http://127.0.0.1:8890）
  MONITOR_PG_ADDR        資料庫探測的 host:port（沒有 DATABASE_URL 時才看；不設就不探）
  MONITOR_LAYA_URL       LAYA 客服分類的網址（沒設就不探；設 off 也不探）；探 /health，逾時 3 秒
  MONITOR_LAYA_SLOW_MS   LAYA /health 回應超過幾毫秒算「慢」（預設 500；設 0 不分慢）
  MONITOR_ON             設 0 整個不啟動（server.py 看的）
  TZ_OFFSET_HOURS        當地時區（預設 8）
"""
import collections
import contextvars
import datetime as dt
import json
import os
import random
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

try:
    import httpx
except Exception:  # 測試環境可能沒裝；探測會略過
    httpx = None

DIR = os.environ.get("MONITOR_DIR", "/metrics")
HISTORY_DB = os.environ.get("MONITOR_HISTORY_DB", "/mem0-history/history.db")
EMB_LOG_DIR = os.environ.get("MONITOR_EMB_LOG_DIR", "/emb-logs")
EMB_CONF = os.environ.get("MONITOR_EMB_CONF", "/emb-gw/emb.conf")
EMB_PRIMARY_ENV = os.environ.get("MONITOR_EMB_PRIMARY", "").strip()
POLL_TOOLS = {x.strip() for x in os.environ.get("MONITOR_POLL_TOOLS", "inbox").split(",") if x.strip()}
POLL_IPS = {x.strip() for x in os.environ.get("MONITOR_POLL_IPS", "").split(",") if x.strip()}
POLL_AGENTS = {x.strip() for x in os.environ.get("MONITOR_POLL_AGENTS", "monitor-eval").split(",") if x.strip()}
SEARCH_TOOLS = {x.strip() for x in os.environ.get("MONITOR_SEARCH_TOOLS", "memory_search").split(",") if x.strip()}
QUESTIONS = os.environ.get("MONITOR_QUESTIONS", "/app/eval/questions.json")
EVAL_SLEEP = float(os.environ.get("MONITOR_EVAL_SLEEP", "2"))
SELF_URL = os.environ.get("MONITOR_SELF_URL", "http://127.0.0.1:8890").rstrip("/")
TZ = dt.timezone(dt.timedelta(hours=float(os.environ.get("TZ_OFFSET_HOURS", "8"))))
RAW_DAYS = 8      # 原始列保留天數（7 天 + 1 天餘裕，7d 的最舊一小時才不會缺）
AGG_DAYS = 90     # 5 分鐘彙總保留天數
STEP = {"24h": 300, "7d": 3600, "30d": 86400}
SPAN = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
TOP_N = 8         # calls_by_tool／calls_by_agent 只回前幾名，其餘併成「其他」
OTHER = "其他"
QUEUE_MAX = 200000
PROBE_INTERVAL = 60  # 每分鐘探測；超過三輪沒有結果就不明
RPS_WIN = 60         # 節點「每秒請求數」看近幾秒（近 1 分鐘）

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS calls_raw (ts REAL, agent TEXT, tool TEXT, ms REAL, ok INTEGER, err TEXT, poll INTEGER)",
    "CREATE INDEX IF NOT EXISTS calls_raw_ts ON calls_raw (ts)",
    "CREATE TABLE IF NOT EXISTS mcp_err_raw (ts REAL, status INTEGER)",
    "CREATE INDEX IF NOT EXISTS mcp_err_raw_ts ON mcp_err_raw (ts)",
    "CREATE TABLE IF NOT EXISTS emb_raw (ts REAL, ms REAL, status INTEGER, upstream TEXT, is_primary INTEGER, ok INTEGER)",
    "CREATE INDEX IF NOT EXISTS emb_raw_ts ON emb_raw (ts)",
    "CREATE TABLE IF NOT EXISTS writes_5m (ts INTEGER PRIMARY KEY, add_n INTEGER, upd_n INTEGER, del_n INTEGER)",
    "CREATE TABLE IF NOT EXISTS agg_5m (ts INTEGER PRIMARY KEY, data TEXT)",
    "CREATE TABLE IF NOT EXISTS agg_1d (day TEXT PRIMARY KEY, ts INTEGER, data TEXT)",
    "CREATE TABLE IF NOT EXISTS snapshot_5m (ts INTEGER PRIMARY KEY, totals TEXT, inbox TEXT)",
    "CREATE TABLE IF NOT EXISTS node_1m (ts INTEGER, node TEXT, up INTEGER, ms REAL)",
    "CREATE INDEX IF NOT EXISTS node_1m_ts ON node_1m (ts)",
    "CREATE TABLE IF NOT EXISTS node_state (node TEXT PRIMARY KEY, label TEXT, up INTEGER, since REAL, ms REAL, ts REAL, note TEXT)",
    "CREATE TABLE IF NOT EXISTS eval_daily (day TEXT PRIMARY KEY, ts INTEGER, hit1 REAL, hit5 REAL, p50_ms REAL, p95_ms REAL,"
    " scored INTEGER, total INTEGER, skipped_missing INTEGER, extra TEXT, started REAL, finished REAL, error TEXT)",
    "CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT)",
    # 白天抽考：一次一列；kind＝random／idle；status＝running／done／error／skipped；load_*＝開考當下近 10 分鐘的忙碌程度
    "CREATE TABLE IF NOT EXISTS eval_spot (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, day TEXT, planned REAL, started REAL,"
    " finished REAL, status TEXT, n INTEGER, seed INTEGER, load_calls INTEGER, load_emb INTEGER, hit1 REAL, hit5 REAL,"
    " p50_ms REAL, p95_ms REAL, scored INTEGER, total INTEGER, skipped_missing INTEGER, extra TEXT, error TEXT)",
    "CREATE INDEX IF NOT EXISTS eval_spot_started ON eval_spot (started)",
)
SPOT_KINDS = ("random", "idle")
SPOT_LOAD_SEC = 600   # 「多忙」看近 10 分鐘


def _log(msg: str) -> None:
    print(f"[監控] {msg}", flush=True)


def _pct(values, p: float):
    """百分位（values 已排序）；沒資料回 None。"""
    if not values:
        return None
    i = int(round(p * (len(values) - 1)))
    return round(float(values[max(0, min(i, len(values) - 1))]), 1)


def _bucket(ts: float, step: int = 300) -> int:
    return int(ts // step) * step


def _parse_time(v):
    """ISO 字串（有無時區都行，沒有就當 UTC）或 epoch 數字 → epoch 秒；看不懂回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        if re.fullmatch(r"\d+(\.\d+)?", s):
            return float(s)
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except ValueError:
        return None


def _utc(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


def _day_start(ts: float) -> int:
    d = dt.datetime.fromtimestamp(ts, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp())


# ───────────────────────── 工具呼叫路徑：只做 append ─────────────────────────

_queue: collections.deque = collections.deque(maxlen=QUEUE_MAX)  # ("call", ts, agent, tool, ms, ok, err, poll) 或 ("mcp_err", ts, status)
_client_ip: contextvars.ContextVar = contextvars.ContextVar("monitor_client_ip", default="")


def record_call(agent: str, tool: str, ms: float, ok: bool, err: str = "", poll=None) -> None:
    """記一次工具呼叫；只 append 進佇列，背景每分鐘才寫 sqlite。永遠不丟例外。"""
    try:
        if poll is None:
            poll = tool in POLL_TOOLS or agent in POLL_AGENTS or (_client_ip.get() in POLL_IPS if POLL_IPS else False)
        _queue.append(("call", time.time(), agent or "", tool or "", float(ms), 1 if ok else 0, err or "", 1 if poll else 0))
    except Exception:
        pass


def record_mcp_error(status: int) -> None:
    try:
        _queue.append(("mcp_err", time.time(), int(status)))
    except Exception:
        pass


def install(tool_manager, agent_fn) -> None:
    """包住 FastMCP 的 ToolManager.call_tool 來計時（FastMCP.call_tool 每次都從 _tool_manager 取方法，換掉這個就夠）。"""
    orig = tool_manager.call_tool

    async def call_tool(name, arguments, *a, **k):
        t0 = time.perf_counter()
        ok, err = True, ""
        try:
            return await orig(name, arguments, *a, **k)
        except BaseException as e:
            ok, err = False, type(e).__name__
            raise
        finally:
            try:
                record_call(agent_fn(), name, (time.perf_counter() - t0) * 1000.0, ok, err)
            except Exception:
                pass

    tool_manager.call_tool = call_tool


def wrap_asgi(app, mcp_path: str = "/mcp"):
    """最外層 ASGI 包裝：記下來源 IP（分辨等信器輪詢用），並數 /mcp 回非 2xx 的次數。失敗一律放行。"""

    async def wrapped(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)
        try:
            client = scope.get("client")
            token = _client_ip.set(client[0] if client else "")
        except Exception:
            token = None
        is_mcp = (scope.get("path") or "").rstrip("/") == mcp_path

        async def send_counted(message):
            try:
                if message.get("type") == "http.response.start" and int(message.get("status", 0)) >= 400:
                    record_mcp_error(message["status"])
            except Exception:
                pass
            await send(message)

        try:
            await app(scope, receive, send_counted if is_mcp else send)
        finally:
            if token is not None:
                _client_ip.reset(token)

    return wrapped


# ───────────────────────── emb.conf 與 access log ─────────────────────────

def parse_emb_conf(text: str) -> list:
    """第一個 upstream 區塊裡的 server 清單：[{"addr": "host:port", "backup": bool}]，順序照檔案。"""
    m = re.search(r"upstream\s+\S+\s*\{(.*?)\}", text, re.S)
    if not m:
        return []
    out = []
    for line in m.group(1).splitlines():
        line = line.split("#", 1)[0].strip().rstrip(";")
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "server":
            out.append({"addr": parts[1], "backup": "backup" in parts[2:]})
    return out


def primary_from_conf(servers: list):
    for s in servers:
        if not s["backup"]:
            return s["addr"]
    return None


def final_upstream(ua: str):
    """nginx 的 $upstream_addr 可能是 "A, B" 或 "A : B"（重試、切備援）；最後一個才是真正回應的。"""
    s = (ua or "").strip()
    if not s or s == "-":
        return None
    parts = [p.strip() for p in re.split(r"\s*[,:]\s+", s) if p.strip()]
    return parts[-1] if parts else None


def parse_emb_line(line: str):
    """一行 JSON access log → (ts, ms, status, upstream, ok)；探測、看不懂的回 None。"""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict) or d.get("probe"):
        return None
    ts = _parse_time(d.get("t"))
    if ts is None:
        return None
    try:
        status = int(d.get("status") or 0)
    except ValueError:
        status = 0
    try:
        ms = float(d.get("rt") or 0) * 1000.0
    except ValueError:
        ms = 0.0
    return ts, round(ms, 1), status, final_upstream(d.get("ua")), 1 if 200 <= status < 400 else 0


class Collector:
    def __init__(self, db_path=None, history_db=None, emb_log_dir=None, emb_conf=None, now=None):
        self.db_path = db_path or os.path.join(DIR, "metrics.db")
        self.history_db = history_db or HISTORY_DB
        self.emb_log_dir = emb_log_dir or EMB_LOG_DIR
        self.emb_conf = emb_conf or EMB_CONF
        self.now = now or time.time          # 測試可以換成假時鐘
        self.probes: list = []               # [(key, label, fn)]，fn() -> (up, ms, note)
        self.slow_ms: dict = {}              # 節點 key -> 在線但回應超過幾毫秒算「慢」（沒列的節點不分慢）
        self.snapshot_fn = None              # () -> {"totals": {...}, "inbox": {...}}
        self.search_fn = None                # 每晚考題用的搜尋函式：search_fn(query, user_id, limit) -> list[dict]
        self.eval_setup_fn = None            # 考題開跑前在考題執行緒裡呼叫一次，回 run_nightly 的額外關鍵字參數（例如 existing_ids）
        self.questions = QUESTIONS
        self.eval_sleep = EVAL_SLEEP
        self.started_at = None
        self._db = None
        self._last_agg = None                # 已彙總到哪個 5 分鐘桶（桶起點）
        self._last_cleanup = 0.0
        self._last_snapshot = 0
        self._eval_running = False
        self._eval_skip_day = ""             # 這一天已經確定不用再試（題庫缺、模組缺）
        self.rand = random.Random()          # 白天抽考抽時間、抽題用；測試可以換成固定種子
        self._conf_cache = (None, None)      # (mtime, servers)
        self._cache: dict = {}
        self._cache_lock = threading.Lock()
        self._pool = None
        self._tz_hours = None

    # ---- sqlite ----
    def open(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        c = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        for stmt in _SCHEMA:
            c.execute(stmt)
        c.commit()
        return c

    def db(self):
        if self._db is None:
            self._db = self.open()
        return self._db

    def _state(self, k, default=None):
        r = self.db().execute("SELECT v FROM state WHERE k = ?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def _set_state(self, k, v):
        self.db().execute("INSERT OR REPLACE INTO state (k, v) VALUES (?, ?)", (k, json.dumps(v)))

    # ---- 每分鐘的工作 ----
    def start(self):
        self.started_at = self.now()
        try:
            self.db()
        except Exception as e:
            _log(f"metrics.db 開不起來，監控停用：{e}")
            return None
        t = threading.Thread(target=self._loop, name="monitor", daemon=True)
        t.start()
        _log(f"背景執行緒已啟動，資料庫 {self.db_path}")
        return t

    def _loop(self):
        while True:
            now = time.time()
            nxt = (int(now // PROBE_INTERVAL) + 1) * PROBE_INTERVAL + 2  # 每分鐘第 2 秒做事，讓整分鐘的呼叫都進佇列
            time.sleep(max(1.0, nxt - now))
            t0 = time.perf_counter()
            try:
                self.tick()
            except Exception as e:
                _log(f"這一輪失敗：{type(e).__name__}: {e}")
            spent = time.perf_counter() - t0
            if spent > 1.0:
                _log(f"這一輪花了 {spent:.1f} 秒（目標 1 秒內）")

    def tick(self):
        """一分鐘一輪；每一步各自 try，一步壞了不影響其他步。"""
        now = self.now()
        for name, fn in (("寫入佇列", self.flush_queue), ("讀 history.db", self.ingest_history),
                         ("讀 EMB log", self.ingest_emb_log), ("節點探測", self.probe_nodes),
                         ("每秒請求數", self.count_rps)):
            try:
                fn()
            except Exception as e:
                _log(f"{name}失敗：{type(e).__name__}: {e}")
        try:
            self.aggregate(now)
        except Exception as e:
            _log(f"彙總失敗：{type(e).__name__}: {e}")
        try:
            if self.snapshot_fn and _bucket(now) != self._last_snapshot and now - _bucket(now) < 120:
                self._last_snapshot = _bucket(now)
                self.take_snapshot(now)
        except Exception as e:
            _log(f"快照失敗：{type(e).__name__}: {e}")
        try:
            if now - self._last_cleanup > 3600:
                self._last_cleanup = now
                self.cleanup(now)
                self.rollup_daily(now)
        except Exception as e:
            _log(f"清理失敗：{type(e).__name__}: {e}")
        try:
            self.maybe_eval(now)
        except Exception as e:
            _log(f"考題排程失敗：{type(e).__name__}: {e}")
        try:
            self.maybe_spot(now)
        except Exception as e:
            _log(f"白天抽考排程失敗：{type(e).__name__}: {e}")
        self.db().commit()

    def flush_queue(self):
        calls, errs = [], []
        while True:
            try:
                item = _queue.popleft()
            except IndexError:
                break
            if item[0] == "call":
                calls.append(item[1:])
            elif item[0] == "mcp_err":
                errs.append(item[1:])
        c = self.db()
        if calls:
            c.executemany("INSERT INTO calls_raw (ts, agent, tool, ms, ok, err, poll) VALUES (?, ?, ?, ?, ?, ?, ?)", calls)
        if errs:
            c.executemany("INSERT INTO mcp_err_raw (ts, status) VALUES (?, ?)", errs)
        c.commit()

    # ---- Mem0 history.db：每小時／每 5 分鐘的 ADD／UPDATE／DELETE ----
    def ingest_history(self, limit: int = 20000):
        if not os.path.isfile(self.history_db):
            return 0
        last = int(self._state("history_rowid", 0) or 0)
        src = sqlite3.connect(f"file:{urllib.parse.quote(self.history_db)}?mode=ro", uri=True, timeout=5)
        try:
            rows = src.execute("SELECT rowid, event, created_at FROM history WHERE rowid > ? ORDER BY rowid LIMIT ?",
                               (last, limit)).fetchall()
        finally:
            src.close()
        if not rows:
            return 0
        counts: dict = {}
        for rowid, event, created in rows:
            last = max(last, rowid)
            ts = _parse_time(created)
            if ts is None:
                continue
            key = (_bucket(ts), (event or "").upper())
            counts[key] = counts.get(key, 0) + 1
        c = self.db()
        for (b, event), n in counts.items():
            col = {"ADD": "add_n", "UPDATE": "upd_n", "DELETE": "del_n"}.get(event)
            if not col:
                continue
            c.execute("INSERT INTO writes_5m (ts, add_n, upd_n, del_n) VALUES (?, 0, 0, 0) ON CONFLICT(ts) DO NOTHING", (b,))
            c.execute(f"UPDATE writes_5m SET {col} = {col} + ? WHERE ts = ?", (n, b))
        self._set_state("history_rowid", last)
        c.commit()
        return len(rows)

    # ---- emb-gw access log ----
    def emb_servers(self) -> list:
        try:
            mtime = os.path.getmtime(self.emb_conf)
        except OSError:
            return []
        if self._conf_cache[0] != mtime:
            try:
                with open(self.emb_conf, encoding="utf-8") as f:
                    self._conf_cache = (mtime, parse_emb_conf(f.read()))
            except OSError:
                return []
        return self._conf_cache[1]

    def primary(self, c=None):
        """主節點：emb.conf 第一個非 backup 的 server；讀不到就用環境變數；再不行就拿最近 24 小時最常回應的。"""
        p = primary_from_conf(self.emb_servers())
        if p:
            return p
        if EMB_PRIMARY_ENV:
            return EMB_PRIMARY_ENV
        try:
            r = (c or self.db()).execute("SELECT upstream, count(*) n FROM emb_raw WHERE ts >= ? AND upstream IS NOT NULL"
                                         " GROUP BY upstream ORDER BY n DESC LIMIT 1", (self.now() - 86400,)).fetchone()
            return r[0] if r else None
        except Exception:
            return None

    def ingest_emb_log(self, max_bytes: int = 4 * 1024 * 1024):
        if not os.path.isdir(self.emb_log_dir):
            return 0
        names = sorted(n for n in os.listdir(self.emb_log_dir) if n.startswith("access") and n.endswith(".log"))
        if not names:
            return 0
        st = self._state("emb_log", {"file": "", "offset": 0}) or {}
        cur_file, cur_off = st.get("file", ""), int(st.get("offset", 0))
        if cur_file and cur_file not in names and all(n < cur_file for n in names):
            cur_file, cur_off = "", 0  # 記的檔不見了而且沒有更新的檔：從頭來
        primary = self.primary()
        rows, budget = [], max_bytes
        for name in names:
            if name < cur_file:
                continue
            path = os.path.join(self.emb_log_dir, name)
            offset = cur_off if name == cur_file else 0
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if offset > size:
                offset = 0  # 檔被截短（輪替）：從頭讀
            if offset == size:
                cur_file, cur_off = name, offset
                continue
            with open(path, "rb") as f:
                f.seek(offset)
                chunk = f.read(min(budget, size - offset))
            budget -= len(chunk)
            end = chunk.rfind(b"\n")
            if end < 0:
                cur_file, cur_off = name, offset
                break  # 半行：下次再讀
            for line in chunk[:end].decode("utf-8", "replace").splitlines():
                p = parse_emb_line(line)
                if p:
                    ts, ms, status, up, ok = p
                    rows.append((ts, ms, status, up, 1 if (up and up == primary) else 0, ok))
            cur_file, cur_off = name, offset + end + 1
            if budget <= 0:
                break
        if rows:
            self.db().executemany("INSERT INTO emb_raw (ts, ms, status, upstream, is_primary, ok) VALUES (?, ?, ?, ?, ?, ?)", rows)
        self._set_state("emb_log", {"file": cur_file, "offset": cur_off})
        self.db().commit()
        return len(rows)

    # ---- 節點探測 ----
    def probe_nodes(self):
        if not self.probes:
            return
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="monitor-probe")
        now = self.now()
        futures = [(key, label, self._pool.submit(fn)) for key, label, fn in self.probes]
        c = self.db()
        for key, label, fut in futures:
            try:
                up, ms, note = fut.result(timeout=8)
            except Exception as e:
                up, ms, note = False, None, type(e).__name__
            c.execute("INSERT INTO node_1m (ts, node, up, ms) VALUES (?, ?, ?, ?)", (int(now // 60) * 60, key, 1 if up else 0, ms))
            prev = c.execute("SELECT up, since, ts FROM node_state WHERE node = ?", (key,)).fetchone()
            # 沿用既有 state 表，保留最後成功時間；失敗不能把它改成這次的 ts。
            seen_key = "node_last_seen:" + key
            if up:
                self._set_state(seen_key, now)
            elif self._state(seen_key) is None and prev:
                last_seen = self._node_last_seen(c, key, prev[0], prev[2])
                if last_seen is not None:
                    self._set_state(seen_key, last_seen)
            since = prev[1] if prev and prev[0] == (1 if up else 0) and prev[1] else now
            c.execute("INSERT OR REPLACE INTO node_state (node, label, up, since, ms, ts, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (key, label, 1 if up else 0, since, ms, now, note or ""))
        c.commit()

    # ---- 節點每秒請求數（近 1 分鐘） ----
    def count_rps(self):
        """每輪數一次近 RPS_WIN 秒的請求數，存進 state 的 node_rps。只數真的讀得到的來源，讀不到的節點不放（頁面顯示「—」）：
        圓桌 MCP＝calls_raw（工具呼叫，含自動查詢）；EMB 閘道＝emb_raw 全部（access log，已排除監控探測）；
        EMB 主節點／備援＝emb_raw 裡最後回應的上游是那一台的筆數。access log 資料夾不存在時 EMB 三個都不放。"""
        now = self.now()
        t0 = now - RPS_WIN
        c = self.db()
        n = {"mem0-mcp": c.execute("SELECT count(*) FROM calls_raw WHERE ts > ? AND ts <= ?", (t0, now)).fetchone()[0]}
        if os.path.isdir(self.emb_log_dir):
            by_up = dict(c.execute("SELECT coalesce(upstream, ''), count(*) FROM emb_raw WHERE ts > ? AND ts <= ? GROUP BY 1",
                                   (t0, now)).fetchall())
            n["emb-gw"] = sum(by_up.values())
            for key, _label, addr in emb_probe_keys(self.emb_servers()):
                n[key] = by_up.get(addr, 0)
        self._set_state("node_rps", {"ts": now, "win": RPS_WIN, "n": n})
        c.commit()
        return n

    # ---- 5 分鐘彙總 ----
    def _agg_bucket(self, b: int) -> dict:
        c = self.db()
        t0, t1 = b, b + 300
        rows = c.execute("SELECT agent, tool, ms, ok, poll FROM calls_raw WHERE ts >= ? AND ts < ?", (t0, t1)).fetchall()
        human = poll = tool_err = 0
        by_tool: dict = {}
        by_agent: dict = {}
        ms_all, ms_search = [], []
        for agent, tool, ms, ok, is_poll in rows:
            if is_poll:
                poll += 1
            else:
                human += 1
            if not ok:
                tool_err += 1
            by_tool[tool] = by_tool.get(tool, 0) + 1
            a = agent or "?"
            by_agent[a] = by_agent.get(a, 0) + 1
            ms_all.append(ms)
            if tool in SEARCH_TOOLS:
                ms_search.append(ms)
        ms_all.sort()
        ms_search.sort()
        mcp_err = c.execute("SELECT count(*) FROM mcp_err_raw WHERE ts >= ? AND ts < ?", (t0, t1)).fetchone()[0]
        emb = c.execute("SELECT ms, ok, is_primary FROM emb_raw WHERE ts >= ? AND ts < ?", (t0, t1)).fetchall()
        emb_ms = sorted(ms for ms, ok, _ in emb if ok)
        emb_ok = len(emb_ms)
        emb_primary = sum(1 for ms, ok, p in emb if ok and p)
        emb_err = sum(1 for _, ok, _ in emb if not ok)
        node: dict = {}
        for key, up, ms in c.execute("SELECT node, up, ms FROM node_1m WHERE ts >= ? AND ts < ?", (t0, t1)).fetchall():
            d = node.setdefault(key, {"n": 0, "up": 0, "ms_n": 0, "ms_sum": 0.0})
            d["n"] += 1
            d["up"] += 1 if up else 0
            if up and ms is not None:
                d["ms_n"] += 1
                d["ms_sum"] += float(ms)
        return {
            "calls": {"human": human, "poll": poll},
            "by_tool": by_tool, "by_agent": by_agent,
            "tool_ms": {"p50": _pct(ms_all, 0.5), "p95": _pct(ms_all, 0.95), "n": len(ms_all)},
            "search_ms": {"p50": _pct(ms_search, 0.5), "p95": _pct(ms_search, 0.95), "n": len(ms_search)},
            "errors": {"mcp": mcp_err, "tool": tool_err, "emb": emb_err},
            "emb": {"calls": emb_ok, "p50_ms": _pct(emb_ms, 0.5), "p95_ms": _pct(emb_ms, 0.95), "primary": emb_primary},
            "node": node,
        }

    def aggregate(self, now: float):
        """把已經收完的 5 分鐘桶（桶結束 + 60 秒 ≤ 現在）各算一筆寫進 agg_5m；只算這次啟動以後的桶，沒資料的時段留空（網頁畫斷線）。"""
        c = self.db()
        if self._last_agg is None:
            r = c.execute("SELECT max(ts) FROM agg_5m").fetchone()
            start = _bucket(self.started_at or now)
            self._last_agg = max(r[0], start - 300) if r and r[0] is not None else start - 300
        b = self._last_agg + 300
        n = 0
        while b + 300 + 60 <= now and n < 12:
            c.execute("INSERT OR REPLACE INTO agg_5m (ts, data) VALUES (?, ?)", (b, json.dumps(self._agg_bucket(b), ensure_ascii=False)))
            self._last_agg = b
            b += 300
            n += 1
        if n:
            c.commit()
            with self._cache_lock:
                self._cache.clear()

    def take_snapshot(self, now: float):
        snap = self.snapshot_fn() or {}
        self.db().execute("INSERT OR REPLACE INTO snapshot_5m (ts, totals, inbox) VALUES (?, ?, ?)",
                          (_bucket(now), json.dumps(snap.get("totals") or {}, ensure_ascii=False),
                           json.dumps(snap.get("inbox") or {}, ensure_ascii=False)))
        self.db().commit()

    def cleanup(self, now: float):
        c = self.db()
        raw_cut, agg_cut = now - RAW_DAYS * 86400, now - AGG_DAYS * 86400
        for table in ("calls_raw", "mcp_err_raw", "emb_raw", "node_1m"):
            c.execute(f"DELETE FROM {table} WHERE ts < ?", (raw_cut,))
        for table in ("agg_5m", "writes_5m", "snapshot_5m"):
            c.execute(f"DELETE FROM {table} WHERE ts < ?", (agg_cut,))
        c.commit()
        try:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

    def rollup_daily(self, now: float):
        """每日彙總永久保留：把昨天（當地時間）以前還沒做的日子各併成一筆（一次最多 7 天）。"""
        c = self.db()
        today = _day_start(now)
        done = {r[0] for r in c.execute("SELECT day FROM agg_1d").fetchall()}
        day = today - 86400
        n = 0
        while n < 7 and day > now - AGG_DAYS * 86400:
            key = _local_day(day + 1)
            if key not in done:
                blobs = [json.loads(r[0]) for r in c.execute("SELECT data FROM agg_5m WHERE ts >= ? AND ts < ?", (day, day + 86400)).fetchall()]
                if blobs:
                    w = c.execute("SELECT coalesce(sum(add_n),0), coalesce(sum(upd_n),0), coalesce(sum(del_n),0) FROM writes_5m WHERE ts >= ? AND ts < ?",
                                  (day, day + 86400)).fetchone()
                    data = _merge(blobs)
                    data["writes"] = {"add": w[0], "update": w[1], "delete": w[2]}
                    c.execute("INSERT OR REPLACE INTO agg_1d (day, ts, data) VALUES (?, ?, ?)", (key, day, json.dumps(data, ensure_ascii=False)))
                    n += 1
            day -= 86400
        c.commit()

    # ---- 每晚考題 ----
    def eval_window(self) -> tuple:
        raw = os.environ.get("MONITOR_EVAL_WINDOW", "0-6")
        try:
            a, b = raw.split("-")
            return int(a), int(b)
        except ValueError:
            return 0, 6

    def maybe_eval(self, now: float):
        if not self.search_fn or self._eval_running:
            return
        local = dt.datetime.fromtimestamp(now, TZ)
        a, b = self.eval_window()
        if not (a <= local.hour < b):
            return
        day = local.strftime("%Y-%m-%d")
        if day == self._eval_skip_day:
            return
        r = self.db().execute("SELECT started, finished FROM eval_daily WHERE day = ?", (day,)).fetchone()
        if r and (r[1] is not None or (r[0] and now - r[0] < 3 * 3600)):
            return  # 今天跑完了，或 3 小時內剛開始跑（例如容器重啟前那一次）
        if not os.path.isfile(self.questions):
            self._eval_skip_day = day
            _log(f"考題題庫不存在，今天略過：{self.questions}")
            return
        try:
            import monitor_eval  # 考題模組由另一個檔提供；缺了就略過
        except Exception as e:
            self._eval_skip_day = day
            _log(f"monitor_eval 載入失敗，今天略過：{type(e).__name__}: {e}")
            return
        self._eval_running = True
        self.db().execute("INSERT OR REPLACE INTO eval_daily (day, ts, started) VALUES (?, ?, ?)", (day, _day_start(now), now))
        self.db().commit()
        threading.Thread(target=self._run_eval, args=(monitor_eval, day, _day_start(now)), name="monitor-eval", daemon=True).start()

    def _run_eval(self, mod, day: str, day_ts: int):
        _log(f"每晚考題開始（{day}）")
        c = None
        try:
            kw = dict(self.eval_setup_fn() or {}) if self.eval_setup_fn else {}
            res = mod.run_nightly(self.search_fn, self.questions, self.eval_sleep, **kw) or {}
            main = ("hit1", "hit5", "p50_ms", "p95_ms", "scored", "total", "skipped_missing")
            extra = {k: v for k, v in res.items() if k not in main}
            c = self.open()
            c.execute("UPDATE eval_daily SET ts = ?, hit1 = ?, hit5 = ?, p50_ms = ?, p95_ms = ?, scored = ?, total = ?, skipped_missing = ?,"
                      " extra = ?, finished = ?, error = NULL WHERE day = ?",
                      (day_ts, res.get("hit1"), res.get("hit5"), res.get("p50_ms"), res.get("p95_ms"), res.get("scored"), res.get("total"),
                       res.get("skipped_missing"), json.dumps(extra, ensure_ascii=False, default=str), time.time(), day))
            c.commit()
            _log(f"每晚考題完成（{day}）：hit@1 {res.get('hit1')}、hit@5 {res.get('hit5')}、p50 {res.get('p50_ms')} ms、"
                 f"可計分 {res.get('scored')}/{res.get('total')}、答案已刪 {res.get('skipped_missing')}、出錯 {res.get('errors')}")
        except Exception as e:
            _log(f"每晚考題失敗（{day}）：{type(e).__name__}: {e}")
            try:
                c = c or self.open()
                c.execute("UPDATE eval_daily SET finished = ?, error = ? WHERE day = ?", (time.time(), f"{type(e).__name__}: {e}"[:300], day))
                c.commit()
            except Exception:
                pass
        finally:
            if c is not None:
                c.close()
            self._eval_running = False
            with self._cache_lock:
                self._cache.clear()

    # ---- 白天抽考（random／idle）----
    @staticmethod
    def spot_cfg() -> dict:
        env = os.environ.get

        def rng(k, d):
            try:
                a, b = (int(x) for x in env(k, d).split("-"))
                if a < 0 or b < 0:
                    raise ValueError
                return min(a, b), max(a, b)
            except ValueError:
                return tuple(int(x) for x in d.split("-"))

        def hm(x):
            h, m = x.strip().split(":")
            return int(h) * 60 + int(m)

        try:
            lo, hi = (hm(x) for x in env("MONITOR_SPOT_WINDOW", "06:00-23:30").split("-"))
        except ValueError:
            lo, hi = 360, 1410
        lo = max(lo, 360)            # 離峰 00:00–05:59 一律不碰，設錯也一樣
        hi = min(hi, 23 * 60 + 45)   # 最晚 23:45 開考，一次約 1 分鐘，不會跨進 00:00

        def num(k, d, f=int):
            try:
                return f(env(k, str(d)))
            except ValueError:
                return d

        return {"on": env("MONITOR_SPOT_ON", "0") == "1", "lo": lo, "hi": hi,
                "random": rng("MONITOR_SPOT_RANDOM", "2-4"), "idle": rng("MONITOR_SPOT_IDLE", "2-4"),
                "n": num("MONITOR_SPOT_N", 20), "sleep": num("MONITOR_SPOT_SLEEP", 1.0, float), "gap": num("MONITOR_SPOT_GAP", 60),
                "idle_calls": num("MONITOR_SPOT_IDLE_CALLS", 10), "idle_emb": num("MONITOR_SPOT_IDLE_EMB", 30),
                "wait": num("MONITOR_SPOT_IDLE_WAIT", 180)}

    def spot_plan(self, now: float, cfg: dict) -> dict:
        """當天的預定表（存在 state，重啟不會重抽）：random 和 idle 各抽 2～4 個時間，任兩個至少隔 gap 分鐘。"""
        day = _local_day(now)
        plan = self._state("eval_spot_plan")
        if plan and plan.get("day") == day:
            return plan
        kinds = [k for k in SPOT_KINDS for _ in range(self.rand.randint(*cfg[k]))]
        self.rand.shuffle(kinds)
        lo, hi, gap = cfg["lo"], cfg["hi"], max(0, cfg["gap"])
        mins = None
        for _ in range(500):
            m = sorted(self.rand.randint(lo, hi) for _ in kinds)
            if all(b - a >= gap for a, b in zip(m, m[1:])):
                mins = m
                break
        if mins is None:  # 抽不到夠分散的就平均分
            step = (hi - lo) / max(1, len(kinds))
            mins = [int(lo + step * (i + 0.5)) for i in range(len(kinds))]
        d0 = _day_start(now)
        plan = {"day": day, "slots": [{"kind": k, "at": d0 + m * 60 + self.rand.randint(0, 59), "state": "wait"}
                                      for k, m in zip(kinds, mins)]}
        self._set_state("eval_spot_plan", plan)
        return plan

    def load_now(self, now: float) -> tuple:
        """近 10 分鐘有多忙：(人用工具呼叫數, EMB 閘道請求數)。輪詢和考題自己（monitor-eval）不算人用；EMB 探測本來就不進 emb_raw。"""
        c = self.db()
        calls = c.execute("SELECT count(*) FROM calls_raw WHERE ts >= ? AND poll = 0", (now - SPOT_LOAD_SEC,)).fetchone()[0]
        emb = c.execute("SELECT count(*) FROM emb_raw WHERE ts >= ?", (now - SPOT_LOAD_SEC,)).fetchone()[0]
        return int(calls), int(emb)

    def maybe_spot(self, now: float):
        cfg = self.spot_cfg()
        if not cfg["on"] or not self.search_fn or self._eval_running:
            return
        local = dt.datetime.fromtimestamp(now, TZ)
        if not (cfg["lo"] <= local.hour * 60 + local.minute <= cfg["hi"]):
            return
        plan = self.spot_plan(now, cfg)
        if not os.path.isfile(self.questions):
            return
        changed = False
        start = None
        for sl in plan["slots"]:
            if sl.get("state") != "wait" or now < sl["at"]:
                continue
            calls, emb = self.load_now(now)
            if sl["kind"] == "idle":
                if now - sl["at"] > cfg["wait"] * 60:
                    cur = self.db().execute(
                        "INSERT INTO eval_spot (kind, day, planned, finished, status, load_calls, load_emb, error)"
                        " VALUES ('idle', ?, ?, ?, 'skipped', ?, ?, ?)",
                        (plan["day"], sl["at"], now, calls, emb, f"等了 {cfg['wait']} 分鐘都不夠閒"))
                    sl.update(state="skipped", id=cur.lastrowid)
                    changed = True
                    continue
                if calls > cfg["idle_calls"] or emb > cfg["idle_emb"]:
                    continue  # 還不夠閒，下一分鐘再看
            start = (sl, calls, emb)
            break
        if start is None:
            if changed:
                self._set_state("eval_spot_plan", plan)
            return
        try:
            import monitor_eval
        except Exception as e:
            _log(f"monitor_eval 載入失敗，白天抽考略過：{type(e).__name__}: {e}")
            return
        sl, calls, emb = start
        seed = self.rand.randrange(2 ** 31)
        cur = self.db().execute(
            "INSERT INTO eval_spot (kind, day, planned, started, status, n, seed, load_calls, load_emb)"
            " VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)", (sl["kind"], plan["day"], sl["at"], now, cfg["n"], seed, calls, emb))
        sl.update(state="started", id=cur.lastrowid)
        self._set_state("eval_spot_plan", plan)
        self.db().commit()
        self._eval_running = True
        threading.Thread(target=self._run_spot, args=(monitor_eval, cur.lastrowid, sl["kind"], cfg, seed),
                         name="monitor-eval-spot", daemon=True).start()

    def _run_spot(self, mod, rowid: int, kind: str, cfg: dict, seed: int):
        c = None
        try:
            kw = dict(self.eval_setup_fn() or {}) if self.eval_setup_fn else {}
            kw.pop("log", None)  # 小份考題不印進度
            _, qs = mod.load_questions(self.questions)
            pick = sorted(random.Random(seed).sample(range(len(qs)), min(max(1, cfg["n"]), len(qs))))
            res = mod.run_nightly(self.search_fn, self.questions, cfg["sleep"], pick=pick, max_sec=900, **kw) or {}
            main = ("hit1", "hit5", "p50_ms", "p95_ms", "scored", "total", "skipped_missing")
            extra = {k: v for k, v in res.items() if k not in main}
            c = self.open()
            c.execute("UPDATE eval_spot SET status = 'done', hit1 = ?, hit5 = ?, p50_ms = ?, p95_ms = ?, scored = ?, total = ?,"
                      " skipped_missing = ?, extra = ?, finished = ? WHERE id = ?",
                      (res.get("hit1"), res.get("hit5"), res.get("p50_ms"), res.get("p95_ms"), res.get("scored"), res.get("total"),
                       res.get("skipped_missing"), json.dumps(extra, ensure_ascii=False, default=str), time.time(), rowid))
            c.commit()
            _log(f"白天抽考完成（{kind}）：hit@1 {res.get('hit1')}、hit@5 {res.get('hit5')}、可計分 {res.get('scored')}/{res.get('total')}、"
                 f"花 {res.get('elapsed_sec')} 秒")
        except Exception as e:
            _log(f"白天抽考失敗（{kind}）：{type(e).__name__}: {e}")
            try:
                c = c or self.open()
                c.execute("UPDATE eval_spot SET status = 'error', finished = ?, error = ? WHERE id = ?",
                          (time.time(), f"{type(e).__name__}: {e}"[:300], rowid))
                c.commit()
            except Exception:
                pass
        finally:
            if c is not None:
                c.close()
            self._eval_running = False
            with self._cache_lock:
                self._cache.clear()

    @staticmethod
    def _node_last_seen(c, key, up, ts):
        saved = c.execute("SELECT v FROM state WHERE k = ?", ("node_last_seen:" + key,)).fetchone()
        if saved:
            return json.loads(saved[0])
        # 舊 DB 尚未保存成功時間：在線時 ts 是成功證據，否則只用保留的成功探測。
        if up and ts is not None:
            return ts
        return c.execute("SELECT max(ts) FROM node_1m WHERE node = ? AND up = 1", (key,)).fetchone()[0]

    # ---- 輸出 ----
    def series(self, rng: str = "24h") -> dict:
        rng = rng if rng in STEP else "24h"
        with self._cache_lock:
            hit = self._cache.get(rng)
            if hit and self.now() - hit[0] < 30 and (hit[2] is None or self.now() <= hit[2]):
                return hit[1]
        out, stale_at = self._series(rng)
        with self._cache_lock:
            self._cache[rng] = (self.now(), out, stale_at)
        return out

    def _series(self, rng: str) -> tuple[dict, float | None]:
        now = self.now()
        step, span = STEP[rng], SPAN[rng]
        c = sqlite3.connect(f"file:{urllib.parse.quote(self.db_path)}?mode=ro", uri=True, timeout=10) if os.path.isfile(self.db_path) else None
        try:
            out = self._build(c, now, rng, step, span)
            # 快取不能跨過任何節點的三輪期限；恰好三輪仍沿用探測結果。
            row = c.execute("SELECT min(ts) FROM node_state WHERE ts >= ? AND up IS NOT NULL",
                            (now - 3 * PROBE_INTERVAL,)).fetchone() if c is not None else None
            stale_at = row[0] + 3 * PROBE_INTERVAL if row and row[0] is not None else None
            return out, stale_at
        finally:
            if c is not None:
                c.close()

    def _build(self, c, now, rng, step, span) -> dict:
        if step == 86400:
            end = _day_start(now) + 86400  # 今天（當地）算進去
        else:
            end = _bucket(now - 60, 300) + 300 if step == 300 else _bucket(now, step) + step
        t = list(range(end - span, end, step))
        n = len(t)
        nulls = lambda: [None] * n
        out = {
            "generated": _utc(now), "range": rng, "step_sec": step,
            "summary": {
                "writes_24h": {"add": 0, "update": 0, "delete": 0},
                "calls_24h": {"total": 0, "human": 0, "poll": 0},
                "tool_ms_24h": {"p50": None, "p95": None},
                "search_ms_24h": {"p50": None, "p95": None},
                "emb_24h": {"calls": 0, "p50_ms": None, "p95_ms": None, "primary": None, "primary_share": None, "fallback": 0},
                "errors_24h": {"mcp": 0, "tool": 0, "emb": 0},
                "totals": {}, "inbox": {},
                "eval": {"date": None, "hit1": None, "hit5": None, "p50_ms": None, "scored": None},  # 還沒考過就全是 null
            },
            "nodes": [],
            "series": {
                "t": t,
                "writes": {"add": nulls(), "update": nulls(), "delete": nulls()},
                "calls": {"human": nulls(), "poll": nulls()},
                "calls_by_tool": {}, "calls_by_agent": {},
                "tool_ms": {"p50": nulls(), "p95": nulls()},
                "search_ms": {"p50": nulls(), "p95": nulls()},
                "emb": {"calls": nulls(), "p50_ms": nulls(), "p95_ms": nulls(), "primary_share": nulls()},
                "errors": {"mcp": nulls(), "tool": nulls(), "emb": nulls()},
                "totals": {}, "node_ms": {},
            },
            "eval_daily": {"t": [], "hit1": [], "hit5": [], "p50_ms": [], "scored": []},
            # 白天抽考：每次一個點，t 是開考時間（epoch 秒），kind 是 random／idle；近 90 天、只列考完的
            "eval_spot": {"t": [], "kind": [], "hit1": [], "hit5": [], "p50_ms": [], "scored": [], "load_calls": [], "load_emb": []},
        }
        out["summary"]["eval_spot"] = {k: {"at": None, "hit1": None, "hit5": None, "scored": None, "today_planned": 0,
                                           "today_done": 0, "today_skipped": 0} for k in SPOT_KINDS}
        if c is None:
            out["nodes"] = [{"key": key, "label": label, "up": False, "ms": None, "since": None,
                             "state": "unknown", "last_seen": None, "slow_ms": self.slow_ms.get(key) or None,
                             "rps": None, "req_1m": None} for key, label, _ in self.probes]
            out["note"] = "監控資料庫還沒建立"
            return out
        idx = lambda ts: int((ts - t[0]) // step) if t[0] <= ts < end else None
        S = out["series"]
        # 5 分鐘彙總 → 桶
        groups: list = [[] for _ in range(n)]
        for ts, data in c.execute("SELECT ts, data FROM agg_5m WHERE ts >= ? AND ts < ? ORDER BY ts", (t[0], end)).fetchall():
            i = idx(ts)
            if i is not None:
                groups[i].append(json.loads(data))
        tool_tot: dict = {}
        agent_tot: dict = {}
        merged = []
        for i, g in enumerate(groups):
            if not g:
                merged.append(None)
                continue
            m = _merge(g)
            merged.append(m)
            S["calls"]["human"][i], S["calls"]["poll"][i] = m["calls"]["human"], m["calls"]["poll"]
            S["tool_ms"]["p50"][i], S["tool_ms"]["p95"][i] = m["tool_ms"]["p50"], m["tool_ms"]["p95"]
            S["search_ms"]["p50"][i], S["search_ms"]["p95"][i] = m["search_ms"]["p50"], m["search_ms"]["p95"]
            S["emb"]["calls"][i], S["emb"]["p50_ms"][i], S["emb"]["p95_ms"][i] = m["emb"]["calls"], m["emb"]["p50_ms"], m["emb"]["p95_ms"]
            S["emb"]["primary_share"][i] = round(m["emb"]["primary"] / m["emb"]["calls"], 3) if m["emb"]["calls"] else None
            for k in ("mcp", "tool", "emb"):
                S["errors"][k][i] = m["errors"][k]
            for k, v in m["by_tool"].items():
                tool_tot[k] = tool_tot.get(k, 0) + v
            for k, v in m["by_agent"].items():
                agent_tot[k] = agent_tot.get(k, 0) + v
            for key, d in m["node"].items():
                S["node_ms"].setdefault(key, nulls())[i] = round(d["ms_sum"] / d["ms_n"], 1) if d["ms_n"] else None
        for field, totals in (("calls_by_tool", tool_tot), ("calls_by_agent", agent_tot)):
            top = [k for k, _ in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N]]
            rest = [k for k in totals if k not in top]
            for k in top:
                S[field][k] = nulls()
            if rest:
                S[field][OTHER] = nulls()
            for i, m in enumerate(merged):
                if m is None:
                    continue
                src = m["by_tool"] if field == "calls_by_tool" else m["by_agent"]
                for k in top:
                    S[field][k][i] = src.get(k, 0)
                if rest:
                    S[field][OTHER][i] = sum(src.get(k, 0) for k in rest)
        # 寫入
        for ts, a, u, d in c.execute("SELECT ts, add_n, upd_n, del_n FROM writes_5m WHERE ts >= ? AND ts < ?", (t[0], end)).fetchall():
            i = idx(ts)
            if i is None:
                continue
            for k, v in (("add", a), ("update", u), ("delete", d)):
                S["writes"][k][i] = (S["writes"][k][i] or 0) + v
        # 記憶總數（桶裡最後一次快照）
        for ts, totals in c.execute("SELECT ts, totals FROM snapshot_5m WHERE ts >= ? AND ts < ? ORDER BY ts", (t[0], end)).fetchall():
            i = idx(ts)
            if i is None:
                continue
            for k, v in (json.loads(totals) or {}).items():
                S["totals"].setdefault(k, nulls())[i] = v
        # 24 小時摘要（從原始列算，百分位是精確的）
        day0 = now - 86400
        sm = out["summary"]
        w = c.execute("SELECT coalesce(sum(add_n),0), coalesce(sum(upd_n),0), coalesce(sum(del_n),0) FROM writes_5m WHERE ts >= ?", (_bucket(day0),)).fetchone()
        sm["writes_24h"] = {"add": w[0], "update": w[1], "delete": w[2]}
        human = poll = 0
        for is_poll, cnt in c.execute("SELECT poll, count(*) FROM calls_raw WHERE ts >= ? GROUP BY poll", (day0,)).fetchall():
            if is_poll:
                poll += cnt
            else:
                human += cnt
        sm["calls_24h"] = {"total": human + poll, "human": human, "poll": poll}
        ms_all = sorted(r[0] for r in c.execute("SELECT ms FROM calls_raw WHERE ts >= ?", (day0,)).fetchall())
        sm["tool_ms_24h"] = {"p50": _pct(ms_all, 0.5), "p95": _pct(ms_all, 0.95)}
        q = ",".join("?" * len(SEARCH_TOOLS)) or "''"
        ms_s = sorted(r[0] for r in c.execute(f"SELECT ms FROM calls_raw WHERE ts >= ? AND tool IN ({q})", (day0, *SEARCH_TOOLS)).fetchall())
        sm["search_ms_24h"] = {"p50": _pct(ms_s, 0.5), "p95": _pct(ms_s, 0.95)}
        emb = c.execute("SELECT ms, ok, is_primary FROM emb_raw WHERE ts >= ?", (day0,)).fetchall()
        emb_ms = sorted(ms for ms, ok, _ in emb if ok)
        emb_ok = len(emb_ms)
        emb_pri = sum(1 for _, ok, p in emb if ok and p)
        sm["emb_24h"] = {"calls": emb_ok, "p50_ms": _pct(emb_ms, 0.5), "p95_ms": _pct(emb_ms, 0.95), "primary": self.primary(c),
                         "primary_share": round(emb_pri / emb_ok, 3) if emb_ok else None, "fallback": emb_ok - emb_pri}
        sm["errors_24h"] = {
            "mcp": c.execute("SELECT count(*) FROM mcp_err_raw WHERE ts >= ?", (day0,)).fetchone()[0],
            "tool": c.execute("SELECT count(*) FROM calls_raw WHERE ts >= ? AND ok = 0", (day0,)).fetchone()[0],
            "emb": sum(1 for _, ok, _ in emb if not ok),
        }
        snap = c.execute("SELECT totals, inbox FROM snapshot_5m ORDER BY ts DESC LIMIT 1").fetchone()
        if snap:
            sm["totals"], sm["inbox"] = json.loads(snap[0]) or {}, json.loads(snap[1]) or {}
        # 節點現況：state 四種＝up 正常／slow 慢（在線但超過 slow_ms）／down 不通／unknown 不明（超過三輪沒結果）
        rr = c.execute("SELECT v FROM state WHERE k = 'node_rps'").fetchone()
        rr = json.loads(rr[0]) if rr else {}
        if not rr or rr.get("ts") is None or now - rr["ts"] > 3 * PROBE_INTERVAL:
            rr = {}  # 太久沒數（或沒數過）：不拿舊數字充數
        win = rr.get("win") or RPS_WIN

        def rps(key):
            req = (rr.get("n") or {}).get(key)
            return (round(req / win, 2), req) if req is not None else (None, None)

        for key, label, up, since, ms, ts, note in c.execute("SELECT node, label, up, since, ms, ts, note FROM node_state ORDER BY rowid").fetchall():
            last_seen = self._node_last_seen(c, key, up, ts)
            state = "unknown" if ts is None or now - ts > 3 * PROBE_INTERVAL or up is None else ("up" if up else "down")
            slow = self.slow_ms.get(key) or None
            if state == "up" and slow and ms is not None and ms > slow:
                state = "slow"
            r, req = rps(key)
            out["nodes"].append({"key": key, "label": label, "up": bool(up), "ms": ms, "since": _utc(since) if since else None,
                                 "state": state, "last_seen": _utc(last_seen) if last_seen is not None else None,
                                 "slow_ms": slow, "rps": r, "req_1m": req})
        known = {node["key"] for node in out["nodes"]}
        for key, label, _ in self.probes:
            if key not in known:
                r, req = rps(key)
                out["nodes"].append({"key": key, "label": label, "up": False, "ms": None, "since": None,
                                     "state": "unknown", "last_seen": None, "slow_ms": self.slow_ms.get(key) or None,
                                     "rps": r, "req_1m": req})
        # 每晚考題
        rows = c.execute("SELECT day, ts, hit1, hit5, p50_ms, p95_ms, scored, total, error FROM eval_daily WHERE finished IS NOT NULL"
                         " ORDER BY day DESC LIMIT 90").fetchall()
        for day, ts, h1, h5, p50, p95, scored, total, err in reversed(rows):
            if err or h1 is None:
                continue
            ed = out["eval_daily"]
            ed["t"].append(ts)
            ed["hit1"].append(h1)
            ed["hit5"].append(h5)
            ed["p50_ms"].append(p50)
            ed["scored"].append(scored)
        for day, ts, h1, h5, p50, p95, scored, total, err in rows:
            if err or h1 is None:
                continue
            sm["eval"] = {"date": day, "hit1": h1, "hit5": h5, "p50_ms": p50, "scored": scored}
            break
        # 白天抽考
        es = out["eval_spot"]
        for ts, kind, h1, h5, p50, scored, lc, le in c.execute(
                "SELECT started, kind, hit1, hit5, p50_ms, scored, load_calls, load_emb FROM eval_spot"
                " WHERE status = 'done' AND hit1 IS NOT NULL AND started >= ? ORDER BY started", (now - 90 * 86400,)).fetchall():
            for k, v in (("t", int(ts)), ("kind", kind), ("hit1", h1), ("hit5", h5), ("p50_ms", p50), ("scored", scored),
                         ("load_calls", lc), ("load_emb", le)):
                es[k].append(v)
            if kind in sm["eval_spot"]:
                sm["eval_spot"][kind].update(at=_utc(ts), hit1=h1, hit5=h5, scored=scored)
        today = _local_day(now)
        for kind, status, n in c.execute("SELECT kind, status, count(*) FROM eval_spot WHERE day = ? GROUP BY kind, status", (today,)).fetchall():
            key = {"done": "today_done", "skipped": "today_skipped"}.get(status)
            if kind in sm["eval_spot"] and key:
                sm["eval_spot"][kind][key] += n
        plan = c.execute("SELECT v FROM state WHERE k = 'eval_spot_plan'").fetchone()
        p = json.loads(plan[0]) if plan else {}
        if (p or {}).get("day") == today:
            for sl in p.get("slots") or []:
                if sl.get("kind") in sm["eval_spot"]:
                    sm["eval_spot"][sl["kind"]]["today_planned"] += 1
        return out


def _wavg(pairs):
    """[(值, 權重)] 加權平均；沒有有效值回 None。"""
    tot = sum(w for v, w in pairs if v is not None and w)
    if not tot:
        vals = [v for v, _ in pairs if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None
    return round(sum(v * w for v, w in pairs if v is not None and w) / tot, 1)


def _merge(blobs: list) -> dict:
    """好幾個 5 分鐘桶併成一個（7 天用 1 小時、30 天用 1 天）：次數相加；百分位用呼叫數加權平均（近似值）。"""
    if len(blobs) == 1:
        b = blobs[0]
        return {**b, "node": {k: dict(v) for k, v in b.get("node", {}).items()}}
    out = {"calls": {"human": 0, "poll": 0}, "by_tool": {}, "by_agent": {}, "errors": {"mcp": 0, "tool": 0, "emb": 0},
           "emb": {"calls": 0, "primary": 0}, "node": {}}
    tp50, tp95, sp50, sp95, ep50, ep95 = [], [], [], [], [], []
    tn = sn = 0
    for b in blobs:
        out["calls"]["human"] += b["calls"]["human"]
        out["calls"]["poll"] += b["calls"]["poll"]
        for k, v in b.get("by_tool", {}).items():
            out["by_tool"][k] = out["by_tool"].get(k, 0) + v
        for k, v in b.get("by_agent", {}).items():
            out["by_agent"][k] = out["by_agent"].get(k, 0) + v
        for k in ("mcp", "tool", "emb"):
            out["errors"][k] += b["errors"].get(k, 0)
        n = b["tool_ms"].get("n", b["calls"]["human"] + b["calls"]["poll"])
        tp50.append((b["tool_ms"]["p50"], n))
        tp95.append((b["tool_ms"]["p95"], n))
        tn += n
        m = b["search_ms"].get("n", 0)
        sp50.append((b["search_ms"]["p50"], m))
        sp95.append((b["search_ms"]["p95"], m))
        sn += m
        ec = b["emb"]["calls"]
        out["emb"]["calls"] += ec
        out["emb"]["primary"] += b["emb"]["primary"]
        ep50.append((b["emb"]["p50_ms"], ec))
        ep95.append((b["emb"]["p95_ms"], ec))
        for key, d in b.get("node", {}).items():
            o = out["node"].setdefault(key, {"n": 0, "up": 0, "ms_n": 0, "ms_sum": 0.0})
            for f in ("n", "up", "ms_n", "ms_sum"):
                o[f] += d.get(f, 0)
    out["tool_ms"] = {"p50": _wavg(tp50), "p95": _wavg(tp95), "n": tn}
    out["search_ms"] = {"p50": _wavg(sp50), "p95": _wavg(sp95), "n": sn}
    out["emb"].update(p50_ms=_wavg(ep50), p95_ms=_wavg(ep95))
    return out


# ───────────────────────── 節點探測函式 ─────────────────────────

def probe_http(url: str, timeout: float = 3.0, ok_status=None, headers=None):
    """GET 一個網址；回 (up, ms, note)。ok_status 不給就只要有回應（<500）都算活著。"""
    def fn():
        if httpx is None:
            return False, None, "沒有 httpx"
        t0 = time.perf_counter()
        r = httpx.get(url, timeout=timeout, headers=headers or {})
        ms = round((time.perf_counter() - t0) * 1000, 1)
        up = (r.status_code == ok_status) if ok_status else r.status_code < 500
        return up, ms, f"HTTP {r.status_code}"
    return fn


def probe_embedding(url: str, model: str, timeout: float = 8.0):
    """真的打一句 embedding 到閘道，記耗時與 X-Emb-Backend（帶 X-Monitor-Probe 標頭，access log 會把它排除在真實流量外）。"""
    def fn():
        if httpx is None:
            return False, None, "沒有 httpx"
        t0 = time.perf_counter()
        r = httpx.post(url.rstrip("/") + "/v1/embeddings", json={"model": model, "input": ["監控探測"]},
                       headers={"X-Monitor-Probe": "1"}, timeout=timeout)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        backend = r.headers.get("x-emb-backend", "")
        return r.status_code == 200, ms, backend or f"HTTP {r.status_code}"
    return fn


def probe_tcp(host: str, port: int, timeout: float = 3.0):
    def fn():
        t0 = time.perf_counter()
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return True, round((time.perf_counter() - t0) * 1000, 1), ""
    return fn


def pg_host_port(dsn: str):
    """DATABASE_URL → (host, port)；看不懂回 None。"""
    try:
        u = urllib.parse.urlsplit(dsn)
        if u.hostname:
            return u.hostname, u.port or 5432
    except ValueError:
        pass
    return None


def emb_probe_keys(servers: list) -> list:
    """emb.conf 的 server 清單 → [(節點 key, 標籤, host:port)]；探測和每秒請求數用同一套 key。"""
    out, nb = [], 0
    for s in servers:
        if s["backup"]:
            nb += 1
            out.append((f"emb-backup{nb if nb > 1 else ''}", f"EMB 備援 {s['addr']}", s["addr"]))
        else:
            out.append(("emb-primary", f"EMB 主節點 {s['addr']}", s["addr"]))
    return out


def default_probes(collector: "Collector", emb_url: str = "", emb_model: str = "", mem0_url: str = "", pg_dsn: str = "") -> list:
    """照環境組出探測清單：EMB 閘道、emb.conf 裡每個上游、mem0-api、資料庫、自己。"""
    probes = []
    if emb_url:
        probes.append(("emb-gw", "EMB 閘道", probe_embedding(emb_url, emb_model)))
    for key, label, addr in emb_probe_keys(collector.emb_servers()):
        probes.append((key, label, probe_http(f"http://{addr}/v1/models")))
    if mem0_url:
        probes.append(("mem0-api", "mem0-api", probe_http(mem0_url.rstrip("/") + "/")))
    hp = pg_host_port(pg_dsn) if pg_dsn else None
    if not hp and os.environ.get("MONITOR_PG_ADDR", "").strip():  # 用 mem0 儲存層時沒有 DATABASE_URL，另外指定 host:port
        host, _, port = os.environ["MONITOR_PG_ADDR"].strip().rpartition(":")
        hp = (host, int(port)) if host and port.isdigit() else None
    if hp:
        probes.append(("postgres", "資料庫", probe_tcp(hp[0], hp[1])))
    # RNK 重排序（server.py 搜尋用的 llama-server）和 LAYA 客服分類（選配）：2.16.2 使用者要在節點列看到這兩台。
    # 網址照 server.py 同一個環境變數與預設值；設成 off 就不探測
    for key, label, env, default in (("rerank", "RNK 重排序", "MONITOR_RERANK_URL", (os.environ.get("MEM0_RERANK_URL", "") if os.environ.get("MEM0_RERANK_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on") else "")),
                                     ("laya", "LAYA 客服分類", "MONITOR_LAYA_URL", os.environ.get("LAYA_URL", ""))):
        url = (os.environ.get(env, "").strip() or default).rstrip("/")
        if url and url.lower() != "off":
            addr = urllib.parse.urlsplit(url).netloc or url
            probes.append((key, f"{label} {addr}", probe_http(url + "/health", timeout=3.0, ok_status=200)))
    # LAYA 分「慢」：/health 平常從伺服器本機量到約 30 毫秒，超過門檻算慢（只是提醒，不算不通）
    try:
        laya_slow = float(os.environ.get("MONITOR_LAYA_SLOW_MS", "500") or 0)
    except ValueError:
        laya_slow = 500.0
    if laya_slow > 0 and any(p[0] == "laya" for p in probes):
        collector.slow_ms["laya"] = laya_slow
    probes.append(("mem0-mcp", "圓桌 MCP", probe_http(SELF_URL + "/health", ok_status=200)))
    return probes


# ───────────────────────── 給 server.py 用的單例 ─────────────────────────

COLLECTOR = Collector()


def start(search_fn=None, snapshot_fn=None, emb_url: str = "", emb_model: str = "", mem0_url: str = "", pg_dsn: str = "",
          eval_setup_fn=None):
    """啟動背景執行緒（server.py 的 __main__ 呼叫）。任何錯都只記 log、回 None。"""
    try:
        COLLECTOR.search_fn = search_fn
        COLLECTOR.eval_setup_fn = eval_setup_fn
        COLLECTOR.snapshot_fn = snapshot_fn
        COLLECTOR.probes = default_probes(COLLECTOR, emb_url, emb_model, mem0_url, pg_dsn)
        return COLLECTOR.start()
    except Exception as e:
        _log(f"啟動失敗，監控停用：{type(e).__name__}: {e}")
        return None


def series(rng: str = "24h") -> dict:
    return COLLECTOR.series(rng)
