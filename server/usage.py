"""額度頁（/board/usage）的資料來源。

電腦上的收集程式（公開版不附；自己寫一支定時用 usage_report 工具送快照即可）和讀到 get_usage 的 Claude 串，用 MCP 工具 usage_report
送快照進來，存 sqlite（MONITOR_DIR/usage.db，跟監控的 metrics.db 分開，互不影響）。
view() 產出額度頁要的 JSON：每家最新額度、消費速度（近 6／24 小時、本期平均）、照速度會不會在重置前用完、近 14 天每日 token、從快照反推的重置紀錄。
2026-10-28 加：各家 hook 的連線健康（hook：最後一次 ping 的時間與名字）、近 15 分鐘用掉的 token（tok15，含近 24 小時歷史）。
只帶 hook／tok15、沒帶 windows 和 daily 的回報算「輕量回報」：不改 meta.ts（頁面「多久前更新」）和 source，舊額度照留。
2026-10-28 加：收集程式送的 model_totals（各模型每月、累計、每天 token），存在 meta 表 provider="__models__" 那一列的 info，
不改資料庫結構；view() 原樣（整理過）輸出成頂層 model_totals。
2026-10-28 加「請立刻收」旗標：公告欄額度頁按「更新」→ refresh_request() 設旗標（60 秒限一次、每小時最多 30 次）；
電腦上的收集程式每分鐘用 MCP 工具 usage_refresh_poll → refresh_poll() 讀旗標、讀到就清掉，再整包收一次，
送來的報告帶 "refresh": {"requested_at": ...}，put() 記完成時間。狀態存在另一張 flags 表（不放 meta，
回滾到舊版程式時舊的 view() 不會把它當成一家提供商列出來）；view() 輸出成頂層 refresh。
2026-10-28 加「Codex 用量門檻」設定：額度頁滑桿 → POST /board/api/usage/settings → settings_set()；
存在 flags 表 name="settings"（重開容器還在；不放 meta，理由同上）。view() 和 put() 的回傳都多一個 settings，
收集程式把 put() 回傳的 settings 寫進本機快取，給 usage_brief_hook.py 照著提醒。沒設定過就回預設值（60／30／15）。
2026-10-28 加「每小時 token」：收集程式在每家的報告上多帶 hourly（[[小時起點, token 數], ...] 或 [{"hour":..., "total":...}]），
存在新的 hourly 表（PRIMARY KEY (provider, hour)，同一小時重送就覆蓋）；表用 CREATE TABLE IF NOT EXISTS 在舊資料庫上自動補，不動既有的表。
put() 順手刪 60 天前的；view() 每家多一欄 hourly，只輸出最近 14 天、由舊到新。沒帶 hourly 的報告不動舊的每小時資料。
報告格式見 monitor-page/額度頁-設計草稿-20261025.md。
"""
import datetime as dt
import json
import os
import re
import sqlite3
import threading
import time

DIR = os.environ.get("MONITOR_DIR", "/metrics")
KEEP_DAYS = 60          # 額度快照留多久
DAILY_DAYS = 14         # 頁面畫幾天的每日 token
HOURLY_KEEP_DAYS = 60   # 每小時 token 留多久（put() 時順手刪更舊的）
HOURLY_VIEW_DAYS = 14   # view() 輸出最近幾天的每小時 token
HOURLY_MAX_PUT = 60 * 24  # 單次報告每家最多收幾筆每小時資料（多的截掉）
STALE_HOURS = 3         # 額度多久沒更新就標「舊資料」
RESETS_SHOWN = 40       # 頁面列幾筆重置紀錄（快照留 KEEP_DAYS 天，最多反推到那麼久以前）
TOK15_KEEP = 24 * 3600  # 近 15 分 token 的歷史留多久（存在 meta.info 的 tok15_hist，不另開表）
TOK15_STEP = 300        # 歷史最多每 5 分鐘一筆
ORDER = ("claude", "codex", "agy", "lmstudio")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS quota (ts REAL, provider TEXT, wkey TEXT, used REAL, resets_at REAL, minutes INTEGER)",
    "CREATE INDEX IF NOT EXISTS quota_pk ON quota (provider, wkey, ts)",
    "CREATE TABLE IF NOT EXISTS daily (day TEXT, provider TEXT, input INTEGER, output INTEGER, cache_read INTEGER,"
    " total INTEGER, requests INTEGER, PRIMARY KEY (day, provider))",
    "CREATE TABLE IF NOT EXISTS meta (provider TEXT PRIMARY KEY, info TEXT, ts REAL, source TEXT)",
    "CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, info TEXT, ts REAL)",
    "CREATE TABLE IF NOT EXISTS hourly (provider TEXT, hour INTEGER, total REAL, PRIMARY KEY (provider, hour))",
)
_ID = re.compile(r"^[a-z0-9_-]{1,24}$")
MT_ROW = "__models__"   # meta 表裡存 model_totals 的那一列（不是提供商，view 不列成卡片）
MT_MODELS = 80          # 最多幾個模型
MT_DAYS = 800           # 每天的資料最多留幾天（從新的往回）
MT_MONTHS = 120
MT_PROVS = ("claude", "codex", "agy", "local", "other")
RR_ROW = "_radar"       # meta 表裡存 reset_radar（各站預測的 24 小時重置機率）的那一列，view 不列成卡片
RR_PROVS = 8            # 最多幾家
RR_SOURCES = 6          # 每家最多幾個站
_RRKEY = re.compile(r"^[a-z0-9]{1,24}$")
RF_NAME = "refresh"     # flags 表裡「請立刻收」那一列
RF_GAP = 60             # 兩次請求至少隔幾秒
RF_HOUR_MAX = 30        # 每小時最多幾次
RF_TTL = 3600           # 請求放多久沒人來收就作廢（電腦關機時按的，不要隔天才收）
ST_NAME = "settings"    # flags 表裡「用量門檻」那一列（Codex 門檻＋Antigravity 兩池保底，整組一份）
ST_DEFAULT = {"radar_avg_threshold": 60, "normal_floor": 30, "radar_floor": 15,
              "agy_gemini_floor": 0, "agy_claude_gpt_floor": 0}  # 舊資料庫沒有 agy 兩鍵 → 用這裡的 0（等於加滑桿前的行為）
ST_RANGE = {"radar_avg_threshold": (0, 100, "提早重置機率門檻"), "normal_floor": (0, 100, "Codex 平常保底"),
            "radar_floor": (0, 100, "提早重置時放寬的保底"),
            "agy_gemini_floor": (0, 100, "Antigravity Gemini 池保底"),
            "agy_claude_gpt_floor": (0, 100, "Antigravity Claude 和 GPT 池保底")}  # 頁面可改的鍵（白名單）；radar_floor 10/28 起開放（使用者要自訂放寬的額度）
ST_STEP = 1             # 一格 1%
ST_MIN_MAX = 20         # 每分鐘最多改幾次
_MONTH = re.compile(r"^\d{4}-\d{2}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_lock = threading.Lock()
_db = None


def _conn(path=None):
    global _db
    if path:  # 測試用：換一個檔
        _db = None
    if _db is None:
        p = path or os.path.join(DIR, "usage.db")
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        c = sqlite3.connect(p, timeout=10, check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        for s in _SCHEMA:
            c.execute(s)
        c.commit()
        _db = c
    return _db


def _txt(v, n=60) -> str:
    return str(v if v is not None else "").strip()[:n]


def _num(v, lo=0.0, hi=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    x = max(lo, x)
    return min(hi, x) if hi is not None else x


def _ts(v):
    """resets_at 收 ISO 字串或 epoch 秒。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = _txt(v, 40)
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.timestamp()


def _iso(ts):
    return None if ts is None else dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(v, now):
    """回報裡的時間（epoch 秒或 ISO）；沒帶、讀不懂或比伺服器時間晚 5 分鐘以上，就用伺服器時間。"""
    t = _ts(v) if v is not None else None
    return now if t is None or t != t or t > now + 300 or t <= 0 else t


def _hook(info: dict, h, now: float) -> None:
    """hook ping：{"name": "Claude Code Stop hook", "ts": 秒}；也收只寫名字的字串。存最後一次的時間與名字。"""
    if h is None:
        return
    if isinstance(h, str):
        h = {"name": h}
    if not isinstance(h, dict):
        return
    t = _when(h.get("ts"), now)
    if t >= (info.get("hook_ts") or 0):  # 晚到的舊 ping 不蓋掉新的
        info["hook_ts"] = t
        info["hook_name"] = _txt(h.get("name"), 60) or info.get("hook_name") or "hook"


M5_KEEP = 2 * 86400      # 每 5 分鐘 token（m5）只收這麼久以內的格子（收集程式本來就只送 24 小時）
M5_MAX = 600             # 單次報告每家最多收幾格
CRED_CHANGES = 20        # credits.changes 最多留幾筆
MACHINES_MAX = 8         # 每家最多記幾台機器的 Codex 版本
HOOK_MS_KEEP = 30        # 每台掛勾最近幾次的耗時（毫秒）


def _m5_rows(items, now: float):
    """m5：[[UTC 時間, token], ...]（也收 {"t"/"hour": 時間, "total"/"tokens": n}）。時間 floor 到 5 分鐘、回 [[ISO, 整數 token]] 由舊到新；
    時間讀不懂、比現在晚 5 分鐘以上、早過 M5_KEEP，或數字不是 ≥0 的有限數字的格子跳過；同一格後面的蓋前面的。不是陣列回 None（當沒說，沿用上次）。"""
    if not isinstance(items, (list, tuple)):
        return None
    out = {}
    for it in items[:M5_MAX]:
        if isinstance(it, dict):
            t, n = it.get("t", it.get("hour")), it.get("total", it.get("tokens"))
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            t, n = it[0], it[1]
        else:
            continue
        if isinstance(n, bool) or n is None or isinstance(n, (list, dict)) or isinstance(t, bool) or t is None:
            continue
        n = _num(n, -1.0, 1e15)
        ts = _ts(t)
        if n is None or n < 0 or ts is None or ts != ts or ts <= 0 or ts > now + 300 or ts < now - M5_KEEP:
            continue
        out[int(ts // 300 * 300)] = int(n)
    return [[_iso(k), out[k]] for k in sorted(out)]


def _bool_or_none(v):
    return v if isinstance(v, bool) else None


def _credits(c):
    """credits：{balance, has_credits, unlimited, seen_at, changes:[{at, from, to}]}。數字夾範圍、字串截長度、changes 留最後 CRED_CHANGES 筆。
    不是物件回 None（當沒說，沿用上次）。"""
    if not isinstance(c, dict):
        return None
    ch = []
    for x in (c.get("changes") if isinstance(c.get("changes"), list) else [])[-CRED_CHANGES:]:
        if isinstance(x, dict):
            ch.append({"at": _txt(x.get("at"), 40) or None, "from": _num(x.get("from"), -1e12, 1e12), "to": _num(x.get("to"), -1e12, 1e12)})
    return {"balance": _num(c.get("balance"), -1e12, 1e12), "has_credits": _bool_or_none(c.get("has_credits")),
            "unlimited": _bool_or_none(c.get("unlimited")), "seen_at": _txt(c.get("seen_at"), 40) or None, "changes": ch}


def _machine_key(pid: str, agent: str):
    """Codex 是哪一台：誰送的（codex-laptop／codex-max／codex-mini，帶 :串名 的去掉串名）優先，其次提供商 id 本身以 codex 開頭；都不是回 None。"""
    a = _txt(agent, 40).split(":")[0].lower()
    for k in (a, pid):
        if k.startswith("codex") and re.match(r"^[a-z0-9_-]{1,24}$", k):
            return k
    return None


def _hook_extra(info: dict, p: dict, pid: str, agent: str, now: float) -> None:
    """hook 回報的新欄位：error → hook_error＋hook_error_ts；普通心跳不清除中斷紀錄。
    version → 這台 Codex 的 codex_version，掛勾最後回報時間 hook_last_seen 用伺服器時間（Codex 的 hook ping 都算，沒帶 version 就沿用舊版本）。"""
    h = p.get("hook")
    if not isinstance(h, dict):
        return
    err = _txt(h.get("error"), 60)
    event_ts = _when(h.get("ts"), now)
    if err and event_ts >= (info.get("hook_error_ts") or 0):
        info["hook_error"] = err
        info["hook_error_ts"] = event_ts
        sid = h.get("session_id")
        info["hook_error_session"] = sid if isinstance(sid, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", sid) else ""
    key = _machine_key(pid, agent)
    if key:
        ms = info.get("machines") if isinstance(info.get("machines"), dict) else {}
        if key in ms or len(ms) < MACHINES_MAX:
            old = ms.get(key) if isinstance(ms.get(key), dict) else {}
            runs = [x for x in (old.get("ms") or []) if isinstance(x, int) and not isinstance(x, bool)][-HOOK_MS_KEEP:]
            v = h.get("ms")  # 掛勾這次自己跑了幾毫秒（0.2.3 起帶）；不是 0～1 小時內的數字就當沒說
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and 0 <= v <= 3_600_000:
                runs = (runs + [int(round(v))])[-HOOK_MS_KEEP:]
            ms[key] = {"version": _txt(h.get("version"), 40) or old.get("version"), "seen_ts": now, "ms": runs}
        info["machines"] = ms


def _tok15(info: dict, t15, now: float) -> None:
    """近 15 分 token：{"tokens": 1490000, "ts": 秒, "minutes": 15}。存最新一筆，歷史留 24 小時、最多每 5 分鐘一筆。"""
    if not isinstance(t15, dict):
        return
    tok = _num(t15.get("tokens"), 0.0, 1e12)
    if tok is None:
        return
    t = _when(t15.get("ts"), now)
    mins = int(_num(t15.get("minutes"), 1.0, 1440.0) or 15)
    old = info.get("tok15") or {}
    if t >= (old.get("ts") or 0):
        info["tok15"] = {"tokens": int(tok), "ts": t, "minutes": mins}
    hist = [x for x in (info.get("tok15_hist") or []) if isinstance(x, list) and len(x) == 2 and x[0] >= now - TOK15_KEEP]
    if t >= now - TOK15_KEEP and all(abs(t - x[0]) >= TOK15_STEP for x in hist):
        hist.append([t, int(tok)])
        hist.sort(key=lambda x: x[0])
    info["tok15_hist"] = hist[-(TOK15_KEEP // TOK15_STEP + 12):]


def _hourly_rows(items, now: float) -> dict:
    """整理一家的每小時 token：每項 [時間, total] 或 {"hour": 時間, "total": n}；時間（ISO 字串或 epoch 秒）floor 到整點 UTC，
    回 {整點 epoch 秒: token 數}。時間讀不懂、比現在晚 1 小時以上、早過保留期限，或數字不是 ≥0 的有限數字，那一筆跳過；
    只看前 HOURLY_MAX_PUT 項；同一小時重複出現以後面的為準。"""
    out = {}
    if not isinstance(items, (list, tuple)):
        return out
    for it in items[:HOURLY_MAX_PUT]:
        if isinstance(it, dict):
            t, n = it.get("hour"), it.get("total")
        elif isinstance(it, (list, tuple)) and len(it) >= 2:
            t, n = it[0], it[1]
        else:
            continue
        if isinstance(n, bool) or isinstance(n, (list, dict)) or n is None:
            continue
        try:
            n = float(n)
        except (TypeError, ValueError):
            continue
        if n != n or n < 0 or n > 1e15:  # NaN、負數、不合理的大數
            continue
        ts = _ts(t) if t is not None and not isinstance(t, bool) else None
        if ts is None or ts != ts or ts <= 0 or ts > now + 3600 or ts < now - HOURLY_KEEP_DAYS * 86400:
            continue
        out[int(ts // 3600 * 3600)] = n
    return out


def _mname(v) -> str:
    """模型名稱照原文，只去掉控制字元、限 80 字。"""
    s = "".join(ch for ch in str(v if v is not None else "") if ch >= " " and ch != "\x7f").strip()[:80]
    return s


def _mtvals(v) -> dict:
    if not isinstance(v, dict):
        return None
    out = {k: int(_num(v.get(k), 0.0, 1e15) or 0) for k in ("in", "out", "cache", "requests")}
    out["total"] = out["in"] + out["out"] + out["cache"]
    return out


def _model_totals(mt) -> dict:
    """整理收集程式的 model_totals：名稱、日期格式不對的丟掉，數字限範圍；合計一律重算（輸入＋輸出＋快取讀取）。"""
    if not isinstance(mt, dict):
        raise ValueError("model_totals 要是物件")
    def models(d):
        out = {}
        if isinstance(d, dict):
            for k, v in list(d.items())[:MT_MODELS]:
                n, x = _mname(k), _mtvals(v)
                if n and x is not None:
                    out[n] = x
        return out
    months = {}
    for k, v in sorted((mt.get("months") or {}).items() if isinstance(mt.get("months"), dict) else [])[-MT_MONTHS:]:
        if _MONTH.match(_txt(k, 7)) and len(str(k)) == 7:
            months[k] = models(v)
    days = {}
    for k, v in sorted((mt.get("days") or {}).items() if isinstance(mt.get("days"), dict) else [])[-MT_DAYS:]:
        if _DAY.match(str(k)) and isinstance(v, dict):
            row = {}
            for m, n in list(v.items())[:MT_MODELS]:
                m, n = _mname(m), _num(n, 0.0, 1e15)
                if m and n is not None:
                    row[m] = int(n)
            days[k] = row
    provs = {}
    if isinstance(mt.get("providers"), dict):
        for k, v in list(mt["providers"].items())[:MT_MODELS * 2]:
            n, pv = _mname(k), _txt(v, 12)
            if n:
                provs[n] = pv if pv in MT_PROVS else "other"
    ls = mt.get("longest_session")
    longest = None
    if isinstance(ls, dict) and _num(ls.get("seconds"), 0.0, 1e10) is not None:
        longest = {"seconds": int(_num(ls.get("seconds"), 0.0, 1e10)), "provider": _txt(ls.get("provider"), 12),
                   "model": _mname(ls.get("model")), "start": _iso(_ts(ls.get("start"))), "end": _iso(_ts(ls.get("end")))}
    since = _txt(mt.get("since"), 10)
    return {"as_of": _iso(_ts(mt.get("as_of"))), "complete": mt.get("complete") is not False,
            "since": since if _DAY.match(since) else None, "providers": provs, "months": months,
            "all_time": models(mt.get("all_time")), "days": days, "longest_session": longest}


def _prob(v):
    x = _num(v, 0.0, 100.0)
    return None if x is None else round(x, 1)


def _url(v):
    s = _txt(v, 300)
    return s if re.match(r"^https?://[^\s\"'<>]+$", s, re.I) else None


def _reset_radar(rr) -> dict:
    """整理 reset_radar：{"codex": {"prob24": 0–100, "sources": [{name, url, prob24, detail, as_of}], "as_of": ...}}。
    鍵只收英數（轉小寫）、機率夾在 0–100、每家最多 6 站、字串截長度、網址只收 http/https；壞掉的那家丟掉。"""
    if not isinstance(rr, dict):
        raise ValueError("reset_radar 要是物件")
    out = {}
    for k, v in list(rr.items())[:RR_PROVS]:
        key = _txt(k, 24).lower()
        if not _RRKEY.match(key) or not isinstance(v, dict):
            continue
        srcs = []
        for s in (v.get("sources") if isinstance(v.get("sources"), list) else [])[:RR_SOURCES]:
            if not isinstance(s, dict):
                continue
            name = _txt(s.get("name"), 60)
            if not name:
                continue
            srcs.append({"name": name, "url": _url(s.get("url")), "prob24": _prob(s.get("prob24")),
                         "detail": _txt(s.get("detail"), 160), "as_of": _iso(_ts(s.get("as_of")))})
        prob = _prob(v.get("prob24"))
        if prob is None and not srcs:
            continue
        out[key] = {"prob24": prob, "sources": srcs, "as_of": _iso(_ts(v.get("as_of")))}
    return out


# ---------------------------------------------------------------- 撞限（備援監控 0.1.0，2026-10-30）
_LIMIT_KIND = ("five_hour", "seven_day", "model_weekly", "unknown", "none")


def _limit(x, now: float):
    """收集電腦 撞限偵測（limit_probe.py）送的 limit：只收固定幾個欄位，字串截長度、時間轉 epoch；
    ts 沒帶、讀不懂或比伺服器時間晚 5 分鐘以上就用伺服器時間。不是物件回 None。"""
    if not isinstance(x, dict):
        return None
    kind = _txt(x.get("kind"), 20)
    scope = re.sub(r"[^a-z0-9_]", "", _txt(x.get("scope"), 24).lower())
    return {"ts": _when(x.get("ts"), now), "kind": kind if kind in _LIMIT_KIND else "unknown", "scope": scope or None,
            "resets_at": _ts(x.get("resets_at")), "status": _txt(x.get("status"), 20),
            "overage_status": _txt(x.get("overage_status"), 20), "overage_in_use": x.get("overage_in_use") is True,
            "error": _txt(x.get("error"), 40), "source": _txt(x.get("source"), 20)}


def _limit_out(x):
    if not isinstance(x, dict):
        return None
    return dict(x, ts=_iso(x.get("ts")), resets_at=_iso(x.get("resets_at")))


def _probe_out(x):
    if not isinstance(x, dict):
        return None
    return dict(x, ts=_iso(x.get("ts")))


def _claude_status(res: dict, now: float) -> dict:
    """額度頁頂端要顯示的 Claude 狀態（正常／撞限／超額／收集電腦 離線…），判斷寫在 claude_status.py。
    PC_HOST 是 收集電腦 的名字或位址（預設 off＝不 ping）；名字向 PC_DNS 查（沒設就用系統的 DNS，
    DHCP 會登記名字），所以 收集電腦 換 IP 也找得到；PC_PING_FILE 是主機 cron 寫的 ping 結果檔（可不設）。
    判斷程式不在或出錯，只回「判斷不出來」，不影響額度頁其他資料。"""
    try:
        import claude_status as cs
        ping = cs.cached_ping(os.environ.get("PC_HOST", "off"),
                              ping_file=os.environ.get("PC_PING_FILE") or os.path.join(DIR, "pc_ping.json"), now=now,
                              dns_server=os.environ.get("PC_DNS", "") or None)
        st = cs.judge(cs.from_view(res, now, ping))
        if hasattr(cs, "last_ping_target") and isinstance(st.get("evidence"), dict):
            st["evidence"]["pc_target"] = cs.last_ping_target()
        return st
    except Exception as e:  # noqa: BLE001
        return {"state": "unknown", "label": "判斷不出來", "level": "off", "detail": "狀態判斷程式出錯：" + type(e).__name__,
                "secondary": [], "version": None}


def put(report: dict, agent: str = "", now: float = None) -> dict:
    """收一份報告。格式不對的欄位丟掉、其他照收；回傳收了幾家、幾個額度、幾天。"""
    if not isinstance(report, dict):
        raise ValueError("report 要是物件")
    provs = report.get("providers")
    has_mt = "model_totals" in report
    has_rr = "reset_radar" in report
    if (has_mt or has_rr) and provs is None:
        provs = []
    if not isinstance(provs, list) or (not provs and not has_mt and not has_rr):
        raise ValueError("report.providers 要是非空陣列")
    if len(provs) > 12:
        raise ValueError("一次最多 12 家")
    now = time.time() if now is None else now
    source = _txt(report.get("source"), 24) or "?"
    probe = source.startswith("vm119-probe")
    got = {"providers": 0, "windows": 0, "days": 0, "skipped": []}
    mt = _model_totals(report["model_totals"]) if has_mt else None
    rr = _reset_radar(report["reset_radar"]) if has_rr else None
    with _lock:
        c = _conn()
        if mt is not None:
            c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?, ?, ?)",
                      (MT_ROW, json.dumps(mt, ensure_ascii=False), now, f"{source}／{_txt(agent, 40)}"))
            got["models"] = len(mt["all_time"])
        if rr:  # 逐家合併：這次有帶的那家整筆換新，沒帶的那家沿用上次
            row = c.execute("SELECT info FROM meta WHERE provider = ?", (RR_ROW,)).fetchone()
            try:
                old = json.loads(row[0]) if row else {}
            except ValueError:
                old = {}
            old = old if isinstance(old, dict) else {}
            for k, v in rr.items():
                old[k] = dict(v, received=_iso(now))
            c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?, ?, ?)",
                      (RR_ROW, json.dumps(old, ensure_ascii=False), now, f"{source}／{_txt(agent, 40)}"))
        if has_rr:
            got["reset_radar"] = sorted(rr)
        for p in provs:
            pid = _txt((p or {}).get("id"), 24) if isinstance(p, dict) else ""
            if not _ID.match(pid) or pid in (MT_ROW, RR_ROW):
                got["skipped"].append(pid or "?")
                continue
            row = c.execute("SELECT info, ts, source FROM meta WHERE provider = ?", (pid,)).fetchone()
            info = json.loads(row[0]) if row else {}
            # 輕量回報（只有 hook ping／近 15 分 token）：不動「多久前更新」的時間和來源
            light = ("hook" in p or "tok15" in p) and not p.get("windows") and not p.get("daily")
            if probe:  # 伺服器端的額度探針（2026-10-30）：額度照收，但不算 收集電腦 的心跳，也不改「多久前更新」
                light = True
                pr = p.get("probe") if isinstance(p.get("probe"), dict) else {}
                info["probe"] = {"ts": now, "ok": pr.get("ok") is True or (pr.get("ok") is None and bool(p.get("windows"))),
                                 "error": _txt(pr.get("error"), 40), "version": _txt(pr.get("version"), 12)}
            elif p.get("windows"):
                info["quota_pc_ts"] = now  # 收集電腦 這邊（statusline、get_usage、收集排程）最後一次送額度百分比
            if pid in ("claude", "codex"):  # 只有這兩家有自己的 hook；其他家帶 hook 不收（10/29 Antigravity 被誤判久未回報）
                _hook(info, p.get("hook"), now)
            _tok15(info, p.get("tok15"), now)
            _hook_extra(info, p, pid, agent, now)
            if pid == "claude" and "limit" in p:  # 撞限偵測：最後一次撞限（哪個窗、幾點恢復、有沒有在用超額）
                lim = _limit(p.get("limit"), now)
                if lim and lim["ts"] >= ((info.get("limit") or {}).get("ts") or 0):
                    info["limit"] = lim
            if "m5" in p:  # 沒帶或格式不對就沿用上次
                m5 = _m5_rows(p.get("m5"), now)
                if m5 is not None:
                    info["m5"] = m5
                    got["m5"] = got.get("m5", 0) + len(m5)
            if "credits" in p:
                cr = _credits(p.get("credits"))
                if cr is not None:
                    info["credits"] = cr
                    got["credits"] = got.get("credits", 0) + 1
            for k, n in (("name", 40), ("plan", 24), ("billing", 60), ("note", 120)):
                if k in p and p[k] is not None:  # 沒帶的欄位沿用上次（Claude 串只送額度，不蓋掉收集程式寫的名稱）
                    info[k] = _txt(p[k], n)
            if "online" in p:
                info["online"] = p["online"] if isinstance(p["online"], bool) else None
                if "power_state" not in p:  # 沒帶開關機狀態的來源回報連線時，清掉舊的，不留過期的「關機」
                    info.pop("power_state", None)
            if "power_state" in p:  # 本地機器開關機（電源收集程式看本機與 BMC 判斷）：on／off／offline，其他值當沒說
                ps = p["power_state"]
                info["power_state"] = ps if ps in ("on", "off", "offline") else None
            if isinstance(p.get("power"), dict):  # 本地模型的電費（估算）：度數、元
                pw = {k: _num(p["power"].get(k), 0.0, 1e6) for k in ("kwh_today", "kwh_7d", "ntd_today", "ntd_7d")}
                pw["estimated"] = p["power"].get("estimated") is not False
                # 有實測電源（PSU）的機器多帶即時瓦數、效率、溫度、量測時間；沒帶就不存（Max/Mini 不受影響）
                src = p["power"]
                for k, lo, hi in (("watts_in", 0.0, 1e5), ("watts_out", 0.0, 1e5), ("temp_c", -50.0, 200.0)):
                    if k in src:
                        pw[k] = _num(src.get(k), lo, hi)
                if "efficiency" in src:
                    e = _num(src.get("efficiency"), 0.0, 100.0)
                    pw["efficiency"] = None if e is None else (round(e * 100, 1) if e <= 1 else e)
                if "at" in src:
                    pw["at"] = _iso(_ts(src.get("at")))
                info["power"] = pw
            labels = info.get("labels") or {}
            for w in (p.get("windows") or [])[:6]:
                if not isinstance(w, dict):
                    continue
                key, used = _txt(w.get("key"), 24), _num(w.get("used"), 0.0, 100.0)
                if not _ID.match(key) or used is None:
                    continue
                labels[key] = _txt(w.get("label"), 40) or labels.get(key) or key
                c.execute("INSERT INTO quota VALUES (?, ?, ?, ?, ?, ?)",
                          (now, pid, key, used, _ts(w.get("resets_at")), int(_num(w.get("minutes")) or 0)))
                got["windows"] += 1
            info["labels"] = labels
            for d in (p.get("daily") or [])[:31]:
                if not isinstance(d, dict) or not _DAY.match(_txt(d.get("day"), 10)):
                    continue
                vals = [int(_num(d.get(k)) or 0) for k in ("input", "output", "cache_read", "total", "requests")]
                c.execute("INSERT OR REPLACE INTO daily VALUES (?, ?, ?, ?, ?, ?, ?)", (d["day"][:10], pid, *vals))
                got["days"] += 1
            if "hourly" in p:  # 每小時 token：同一小時重送就覆蓋；沒帶 hourly 的報告完全不碰這張表
                hrs = _hourly_rows(p.get("hourly"), now)
                for h, n in hrs.items():
                    c.execute("INSERT OR REPLACE INTO hourly VALUES (?, ?, ?)", (pid, h, n))
                got["hourly"] = got.get("hourly", 0) + len(hrs)
            if light:  # 沿用上次的時間與來源；第一次就只收到 ping 時時間留空（頁面當「還沒有資料」）
                mts, msrc = (row[1], row[2]) if row else (None, f"{source}／{_txt(agent, 40)}")
            else:
                mts, msrc = now, f"{source}／{_txt(agent, 40)}"
            c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?, ?, ?)",
                      (pid, json.dumps(info, ensure_ascii=False), mts, msrc))
            got["providers"] += 1
            if "hook" in p:
                got["hooks"] = got.get("hooks", 0) + 1
            if "tok15" in p:
                got["tok15"] = got.get("tok15", 0) + 1
        if isinstance(report.get("refresh"), dict):  # 收集程式是被「請立刻收」叫起來的：記完成時間
            rf = _rf_get(c)
            rf["done_ts"] = now
            rf["done_req"] = _ts(report["refresh"].get("requested_at"))
            _rf_put(c, rf, now)
            got["refresh"] = True
        c.execute("DELETE FROM quota WHERE ts < ?", (now - KEEP_DAYS * 86400,))
        c.execute("DELETE FROM hourly WHERE hour < ?", (now - HOURLY_KEEP_DAYS * 86400,))
        c.commit()
        got["settings"] = _st_view(_st_get(c))  # 收集程式把它存進本機快取，給額度摘要提醒照著用
    return got


# ---------------------------------------------------------------- 「請立刻收」旗標
def _rf_get(c) -> dict:
    row = c.execute("SELECT info FROM flags WHERE name = ?", (RF_NAME,)).fetchone()
    try:
        info = json.loads(row[0]) if row else {}
    except ValueError:
        info = {}
    return info if isinstance(info, dict) else {}


def _rf_put(c, info: dict, now: float) -> None:
    c.execute("INSERT OR REPLACE INTO flags VALUES (?, ?, ?)", (RF_NAME, json.dumps(info, ensure_ascii=False), now))


def _rf_hist(info: dict, now: float) -> list:
    return sorted(t for t in (info.get("hist") or []) if isinstance(t, (int, float)) and now - 3600 < t <= now + 300)


def _rf_next(hist: list, now: float) -> float:
    """下次可以按的時間：上一次＋60 秒；一小時內已滿 30 次就等最舊那次滿一小時。"""
    nxt = now
    if hist:
        nxt = max(nxt, hist[-1] + RF_GAP)
    if len(hist) >= RF_HOUR_MAX:
        nxt = max(nxt, hist[-RF_HOUR_MAX] + 3600)
    return nxt


def _rf_pending(info: dict, now: float) -> bool:
    t = info.get("req_ts")
    return isinstance(t, (int, float)) and now - t <= RF_TTL


def refresh_request(who: str = "user", now: float = None) -> dict:
    """額度頁按「更新」：設旗標。被限次時 ok=false，附下次可以按的時間（next_allowed_at、retry_after 秒）。"""
    now = time.time() if now is None else now
    with _lock:
        c = _conn()
        info = _rf_get(c)
        hist = _rf_hist(info, now)
        nxt = _rf_next(hist, now)
        if nxt > now:
            wait = max(1, int(-(-(nxt - now) // 1)))
            why = (f"一小時內最多按 {RF_HOUR_MAX} 次" if len(hist) >= RF_HOUR_MAX and nxt > hist[-1] + RF_GAP
                   else f"兩次之間要隔 {RF_GAP} 秒")
            pend = _rf_pending(info, now)
            return {"ok": False, "error": f"{why}，{wait} 秒後可以再按", "retry_after": wait,
                    "next_allowed_at": _iso(nxt), "pending": pend,
                    "requested_at": _iso(info.get("req_ts")) if pend else None}
        hist.append(now)
        info["hist"] = hist[-RF_HOUR_MAX:]
        if not _rf_pending(info, now):  # 已經在等的就沿用原本的請求時間（合併成一次）
            info["req_ts"] = now
        info["req_by"] = _txt(who, 40) or "user"
        _rf_put(c, info, now)
        c.commit()
        return {"ok": True, "requested_at": _iso(info["req_ts"]), "next_allowed_at": _iso(_rf_next(info["hist"], now)),
                "pending": True}


def refresh_poll(agent: str = "", now: float = None) -> dict:
    """電腦端查旗標：有請求就回 pending=true 和請求時間，並清掉旗標（讀一次就沒了）；超過 RF_TTL 的請求當作廢。"""
    now = time.time() if now is None else now
    with _lock:
        c = _conn()
        info = _rf_get(c)
        req = info.get("req_ts")
        info["poll_ts"] = now
        info["poll_by"] = _txt(agent, 40)
        out = {"pending": False}
        if isinstance(req, (int, float)):
            info["req_ts"] = None
            if now - req <= RF_TTL:
                info["taken_ts"] = now
                info["taken_req"] = req
                out = {"pending": True, "requested_at": _iso(req)}
            else:
                out = {"pending": False, "expired": _iso(req)}
        _rf_put(c, info, now)
        c.commit()
        return out


def _rf_view(c, now: float) -> dict:
    """給頁面：有沒有在等、下次可以按的時間、電腦端最後一次來查、拿走、收完的時間。"""
    info = _rf_get(c)
    pend = _rf_pending(info, now)
    return {"pending": pend, "requested_at": _iso(info.get("req_ts")) if pend else None,
            "next_allowed_at": _iso(_rf_next(_rf_hist(info, now), now)),
            "last_poll_at": _iso(info.get("poll_ts")), "taken_at": _iso(info.get("taken_ts")),
            "done_at": _iso(info.get("done_ts")), "gap_seconds": RF_GAP, "hour_max": RF_HOUR_MAX}


# ---------------------------------------------------------------- 用量門檻（額度頁滑桿：Codex 兩個、Antigravity 兩池各一個）
def _st_get(c) -> dict:
    row = c.execute("SELECT info FROM flags WHERE name = ?", (ST_NAME,)).fetchone()
    try:
        info = json.loads(row[0]) if row else {}
    except ValueError:
        info = {}
    return info if isinstance(info, dict) else {}


def _st_put(c, info: dict, now: float) -> None:
    c.execute("INSERT OR REPLACE INTO flags VALUES (?, ?, ?)", (ST_NAME, json.dumps(info, ensure_ascii=False), now))
    c.commit()


def _st_view(info: dict) -> dict:
    """給頁面和 AI：所有門檻（沒設定過、舊資料庫缺鍵或存壞了就用預設）、最後誰在什麼時候改的（整組共用一份）。
    radar_floor（放寬時的保底）預設 15，10/28 起頁面可調。"""
    out = dict(ST_DEFAULT)
    for k, (lo, hi, _) in ST_RANGE.items():
        v = info.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi and v % ST_STEP == 0:
            out[k] = v
    ts = info.get("updated_ts")
    out["updated_at"] = _iso(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None
    out["updated_by"] = (_txt(info.get("updated_by"), 40) or None) if out["updated_at"] else None
    if info.get("codex_manual_reset"):
        out["codex_manual_reset"] = _iso(_ts(info["codex_manual_reset"]))
    return out


def _st_int(v):
    """只收整數（JSON 的 60 或 60.0）；布林、字串、帶小數的都不算。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == v and v.is_integer():
        return int(v)
    return None


def settings_get() -> dict:
    with _lock:
        return _st_view(_st_get(_conn()))


def settings_set(data, who: str = "user", now: float = None) -> dict:
    """額度頁拉滑桿或打數字：存 ST_RANGE 裡的鍵（radar_avg_threshold、normal_floor、radar_floor、agy_gemini_floor、
    agy_claude_gpt_floor 都是 0–100 的整數；33.0 這種沒小數的浮點數當 33 收），可以只帶一個或幾個，沒帶的沿用目前的值。
    回 {"ok": True, "settings": {...}}；格式或範圍不對回 {"ok": False, "status": 400, "error": 白話原因}；
    一分鐘內第 21 次起回 {"ok": False, "status": 429, "retry_after": 秒}（成功失敗都算一次，免得被亂打）。"""
    now = time.time() if now is None else now
    with _lock:
        c = _conn()
        info = _st_get(c)
        hist = sorted(t for t in (info.get("hist") or []) if isinstance(t, (int, float)) and now - 60 < t <= now + 300)
        if len(hist) >= ST_MIN_MAX:
            wait = max(1, int(-(-(hist[-ST_MIN_MAX] + 60 - now) // 1)))
            return {"ok": False, "status": 429, "error": f"一分鐘內最多改 {ST_MIN_MAX} 次，{wait} 秒後再試", "retry_after": wait}
        hist.append(now)
        info["hist"] = hist[-ST_MIN_MAX:]
        why = None
        new = {}
        if not isinstance(data, dict):
            why = '要送 JSON 物件，例如 {"normal_floor": 30}'
        elif not data:
            why = "至少要帶一個：" + "、".join(f"{v[2]}（{k}）" for k, v in ST_RANGE.items())
        else:
            extra = [str(k)[:30] for k in data if k not in ST_RANGE and k != "codex_manual_reset"]
            if extra:
                why = "不認得的欄位：" + "、".join(extra) + "（只能改 " + "、".join(ST_RANGE) + "）"
            for k in ([] if extra else [k for k in data if k in ST_RANGE]):
                lo, hi, name = ST_RANGE[k]
                v = _st_int(data[k])
                if v is None:
                    why = f"{name}（{k}）要是整數"
                elif not lo <= v <= hi:
                    why = f"{name}（{k}）要在 {lo}% 到 {hi}% 之間，收到 {v}"
                if why:
                    break
                new[k] = v
            if not why and "codex_manual_reset" in data:
                value = data["codex_manual_reset"]
                if value is None or value == "":
                    new["codex_manual_reset"] = None
                else:
                    try:
                        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
                        if parsed is None or parsed.tzinfo is None:
                            raise ValueError()
                        stamp = parsed.timestamp()
                        if stamp <= now or stamp > now + 366 * 86400:
                            raise ValueError()
                        new["codex_manual_reset"] = _iso(stamp)
                    except (ValueError, OverflowError):
                        why = "手填重置要是未來一年內、含時區的日期時間；清除請送 null"
        if why:
            _st_put(c, info, now)  # 只記這一次嘗試（限次用），門檻不動
            return {"ok": False, "status": 400, "error": why}
        cur = _st_view(info)
        for k in ST_RANGE:
            info[k] = new.get(k, cur[k])
        if "codex_manual_reset" in new:
            info["codex_manual_reset"] = new["codex_manual_reset"]
        info["updated_ts"] = now
        info["updated_by"] = _txt(who, 40) or "user"
        _st_put(c, info, now)
        return {"ok": True, "settings": _st_view(info)}


def _rate(rows, hours):
    """rows 是同一期（同一個重置時間）依時間排好的 (ts, used)。回傳每小時漲幾 %；資料不到半小時回 None。"""
    last = rows[-1]
    base = None
    for r in rows:
        if r[0] <= last[0] - hours * 3600:
            base = r
        else:
            break
    base = base or rows[0]
    span = (last[0] - base[0]) / 3600
    if span < 0.5:
        return None
    return {"per_hour": round((last[1] - base[1]) / span, 2), "span_hours": round(span, 1)}


def _window(c, pid, key, label, now):
    rows = c.execute("SELECT ts, used, resets_at, minutes FROM quota WHERE provider = ? AND wkey = ? ORDER BY ts",
                     (pid, key)).fetchall()
    if not rows:
        return None
    ts, used, resets, minutes = rows[-1]
    # 同一期：重置時間跟最新一筆差不到 10 分鐘（各家回傳的重置時間會有秒級晃動）
    same = [(r[0], r[1]) for r in rows if (r[2] is None) == (resets is None) and (resets is None or abs(r[2] - resets) < 600)]
    out = {"key": key, "label": label, "used": used, "remaining": round(100 - used, 1), "resets_at": _iso(resets),
           "minutes": minutes, "as_of": _iso(ts), "stale": now - ts > STALE_HOURS * 3600,
           "rate_6h": _rate(same, 6), "rate_24h": _rate(same, 24), "rate_period": None,
           "full_at": None, "runs_out_before_reset": None}
    if resets and minutes:
        elapsed = (ts - (resets - minutes * 60)) / 3600
        if elapsed >= 1:
            out["rate_period"] = {"per_hour": round(used / elapsed, 2), "span_hours": round(elapsed, 1)}
    speed = next((r["per_hour"] for r in (out["rate_24h"], out["rate_period"], out["rate_6h"]) if r), None)
    if speed and speed > 0 and used < 100:
        full = ts + (100 - used) / speed * 3600
        out["full_at"] = _iso(full)
        out["runs_out_before_reset"] = bool(resets) and full < resets
    step = max(1, len(same) // 200)
    out["points"] = [[_iso(t), u] for t, u in same[::step]]
    return out


def _resets(c, pid, key):
    """從快照反推這個窗重置過幾次：重置時間往後跳、而且舊的重置時間已經過了（或已用 % 明顯掉下來）才算。
    沒有重置時間的窗，只看已用 % 掉 20 點以上，時間是「大約」（看到掉下來的那一筆）。"""
    rows = c.execute("SELECT ts, used, resets_at FROM quota WHERE provider = ? AND wkey = ? ORDER BY ts", (pid, key)).fetchall()
    out = []
    for (pt, pu, pr), (ct, cu, cr) in zip(rows, rows[1:]):
        if pr is not None and cr is not None:
            if cr - pr <= 600 or not (pr <= ct + 600 or cu < pu - 5):
                continue  # 同一期（秒級晃動），或只是重置時間被往後改
            at, approx = min(pr, ct), pr > ct
        elif pr is None and cr is None and cu <= pu - 20:
            at, approx = ct, True
        else:
            continue
        out.append({"at": _iso(at), "approx": approx, "used_before": pu, "used_after": cu,
                    "last_seen": _iso(pt), "first_seen": _iso(ct)})
    return out


def _tok15_out(t):
    if not isinstance(t, dict) or t.get("tokens") is None:
        return None
    return {"tokens": t["tokens"], "minutes": t.get("minutes") or 15, "as_of": _iso(t.get("ts"))}


def _machines_out(ms):
    """{機器: {"codex_version": 版本或 None, "hook_last_seen": ISO 時間, "hook_ms": 最近最多 30 次耗時（毫秒，由舊到新）}}。"""
    out = {}
    for k, v in (ms.items() if isinstance(ms, dict) else []):
        if isinstance(v, dict):
            out[k] = {"codex_version": v.get("version") or None, "hook_last_seen": _iso(v.get("seen_ts")),
                      "hook_ms": [x for x in (v.get("ms") or []) if isinstance(x, int) and not isinstance(x, bool)][-HOOK_MS_KEEP:]}
    return out


def view(now: float = None) -> dict:
    now = time.time() if now is None else now
    with _lock:
        c = _conn()
        metas = c.execute("SELECT provider, info, ts, source FROM meta").fetchall()
        mrow = next((m for m in metas if m[0] == MT_ROW), None)
        rrow = next((m for m in metas if m[0] == RR_ROW), None)
        metas = [m for m in metas if m[0] not in (MT_ROW, RR_ROW)]
        refresh = _rf_view(c, now)
        settings = _st_view(_st_get(c))
        days = [(dt.datetime.fromtimestamp(now, dt.timezone(dt.timedelta(hours=float(os.environ.get("TZ_OFFSET_HOURS", "8"))))) - dt.timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(DAILY_DAYS - 1, -1, -1)]
        out, resets = [], []
        for pid, info, ts, source in sorted(metas, key=lambda m: (ORDER.index(m[0]) if m[0] in ORDER else 99, m[0])):
            info = json.loads(info)
            wins = [w for w in (_window(c, pid, k, lab, now) for k, lab in (info.get("labels") or {}).items()) if w]
            for k, lab in (info.get("labels") or {}).items():
                resets += [dict(r, provider=pid, name=info.get("name") or pid, key=k, label=lab) for r in _resets(c, pid, k)]
            daily = [dict(zip(("day", "input", "output", "cache_read", "total", "requests"), r)) for r in c.execute(
                "SELECT day, input, output, cache_read, total, requests FROM daily WHERE provider = ? AND day >= ? ORDER BY day",
                (pid, days[0]))]
            hourly = [[_iso(h), int(round(n))] for h, n in c.execute(
                "SELECT hour, total FROM hourly WHERE provider = ? AND hour >= ? ORDER BY hour",
                (pid, int(now // 3600 * 3600) - HOURLY_VIEW_DAYS * 86400))]
            out.append({"id": pid, "name": info.get("name") or pid, "plan": info.get("plan", ""),
                        "billing": info.get("billing", ""), "note": info.get("note", ""), "online": info.get("online"),
                        "power_state": info.get("power_state"),
                        "power": info.get("power"), "updated": _iso(ts), "source": source, "windows": wins, "daily": daily,
                        "hook_ts": _iso(info.get("hook_ts")), "hook_name": info.get("hook_name") or "",
                        "tok15": _tok15_out(info.get("tok15")),
                        "m5": info.get("m5") or [], "credits": info.get("credits"),
                        "hook_error": info.get("hook_error") or "", "hook_error_ts": _iso(info.get("hook_error_ts")),
                        "limit": _limit_out(info.get("limit")),
                        "probe": _probe_out(info.get("probe")), "quota_pc_ts": _iso(info.get("quota_pc_ts")),
                        "hook_error_session": info.get("hook_error_session") or "",
                        "codex_machines": _machines_out(info.get("machines")),
                        "tok15_series": [[_iso(t), n] for t, n in (info.get("tok15_hist") or []) if t >= now - TOK15_KEEP],
                        "hourly": hourly})
    resets.sort(key=lambda r: r["at"], reverse=True)
    res = {"generated": _iso(now), "days": days, "providers": out, "resets": resets[:RESETS_SHOWN], "model_totals": None,
           "reset_radar": None, "refresh": refresh, "settings": settings, "codex_machines": {}}
    for pr in out:  # 各家的 Codex 機器合成一份給頁面底部；同一台出現在兩家時留最近回報的
        for k, v in pr["codex_machines"].items():
            if k not in res["codex_machines"] or (v["hook_last_seen"] or "") >= (res["codex_machines"][k]["hook_last_seen"] or ""):
                res["codex_machines"][k] = v
    if mrow:
        try:
            res["model_totals"] = dict(json.loads(mrow[1]), received=_iso(mrow[2]))
        except ValueError:
            pass
    if rrow:
        try:
            rr = json.loads(rrow[1])
            res["reset_radar"] = rr if isinstance(rr, dict) and rr else None
        except ValueError:
            pass
    res["claude_status"] = _claude_status(res, now)
    return res
