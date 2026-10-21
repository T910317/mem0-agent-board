#!/usr/bin/env python3
"""把 Mem0 裡的圓桌記憶搬進圓桌自己的表（rt_memories／rt_history），搬完逐區核對。2.0.0 的 ./install.sh migrate 就是跑這支。

兩條路，--mode auto 自己挑：
1. sql：Mem0 跟圓桌用同一個 Postgres（一鍵全包、1.0.0 升上來都是這樣）。直接讀 Mem0 的 pgvector 表
   memories(id, vector, payload)：payload 拆回 user_id／agent_id／內文（data）／metadata（其餘的鍵），向量照抄不重算；
   異動紀錄從 Mem0 的 SQLite（history.db）匯進 rt_history。
2. rest：Mem0 在別台（例如另一組 Postgres）。走 Mem0 REST：每區 GET /memories 撈完、用 EMB_URL 重算向量存進 pg；
   異動紀錄逐筆 GET /memories/{id}/history。

核對（兩條路都做）：每區筆數（來源的每一筆都要在）、每區隨機抽 --sample 筆逐欄比（內文、metadata、agent_id、created_at）、
每區拿 --queries 筆內文當問句比搜尋前 10 筆。有一項不過就以結束碼 2 結束，install.sh 看到就不切 RT_STORE。
可以重跑：已經搬過的列（同 id）跳過，同一筆異動紀錄不會進兩次。不會動 Mem0 的資料。

環境變數（在 roundtable 容器裡跑時 compose 已經給了大半）：
  DATABASE_URL                   目的地，圓桌的 Postgres（必要）
  EMB_URL、EMB_MODEL、EMB_DIMS   embedding 服務（rest 路重算向量、核對搜尋都要用）；預設 http://embed:8080、qwen3-embedding-0.6b、1024
  MEM0_PG_DSN                    sql 路的來源；沒設就用 DATABASE_URL（同一個資料庫）
  MEM0_COLLECTION                Mem0 的表名，預設 memories
  MEM0_HISTORY_DB                Mem0 的 history.db（install.sh 掛在 /history/history.db）；找不到就略過異動紀錄並印「沒驗證」
  MEM0_URL、MEM0_API_KEY         rest 路的來源
  RT_SERVER_DIR                  store.py 所在資料夾（容器裡是 /app，本機是 ../server，通常不用設）
用法：python tools/migrate_store.py [--mode auto|sql|rest] [--zones a,b] [--sample 20] [--queries 3] [--dry-run] [--verify-only] [--skip-history]
結束碼：0 搬好且核對通過；1 設定或連線問題；2 核對沒過。
"""
import argparse
import datetime as dt
import json
import os
import random
import re
import sqlite3
import sys
import uuid

# Mem0 把 metadata 平鋪在 payload 同一層；這些鍵是 Mem0 自己的，不是 metadata（照 Mem0 2.2.1 main.py 的 core_and_promoted_keys）
CORE_KEYS = {"data", "hash", "created_at", "updated_at", "id", "text_lemmatized"}
PROMOTED_KEYS = {"user_id", "agent_id", "run_id", "actor_id", "role", "attributed_to", "expiration_date"}
BATCH = 200


def say(*a):
    print(*a, flush=True)


def redact(dsn: str) -> str:
    """連線字串裡的密碼不印出來。"""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn or "")


def load_store():
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.environ.get("RT_SERVER_DIR"), "/app", os.path.join(os.path.dirname(here), "server")):
        if d and os.path.isfile(os.path.join(d, "store.py")):
            if d not in sys.path:
                sys.path.insert(0, d)
            import store
            return store
    sys.exit("找不到 store.py（圓桌伺服器的儲存層）：請在 roundtable 容器裡跑（./install.sh migrate），或設 RT_SERVER_DIR 指到 server/ 資料夾")


def parse_ts(s):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def same_ts(a, b) -> bool:
    a, b = parse_ts(a), parse_ts(b)
    if a is None or b is None:
        return a is None and b is None
    return abs((a - b).total_seconds()) < 0.001


def is_uuid(s) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def from_payload(mid, vec, p) -> dict:
    """Mem0 的一列（id、向量、payload）→ 統一格式；metadata 是 payload 扣掉 Mem0 自己的鍵，跟 Mem0 REST 回給圓桌的一樣。"""
    p = p or {}
    return {"id": str(mid), "user_id": p.get("user_id"), "agent_id": p.get("agent_id"), "memory": p.get("data") or "",
            "metadata": {k: v for k, v in p.items() if k not in CORE_KEYS and k not in PROMOTED_KEYS},
            "created_at": p.get("created_at"), "updated_at": p.get("updated_at"), "vector": vec}


def make_embed(url: str, model: str):
    import httpx

    def embed(texts):
        out = []
        for i in range(0, len(texts), 32):
            r = httpx.post(url + "/v1/embeddings", json={"model": model, "input": texts[i:i + 32]}, timeout=120)
            r.raise_for_status()
            out.extend(d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d.get("index", 0)))
        return out
    return embed


# ---- 來源 ----

class SqlSource:
    """同一個 Postgres 裡 Mem0 的 pgvector 表。"""
    name = "sql"

    def __init__(self, dsn: str, table: str, embed, vlit):
        self.dsn, self.table, self.embed, self.vlit, self.error = dsn, table, embed, vlit, ""

    def describe(self) -> str:
        return f"Mem0 的 {self.table} 表（{redact(self.dsn)}）"

    def connect(self):
        import psycopg
        return psycopg.connect(self.dsn)

    def available(self) -> bool:
        try:
            with self.connect() as c:
                return c.execute("SELECT to_regclass(%s)", (self.table,)).fetchone()[0] is not None
        except Exception as e:
            self.error = str(e).strip()
            return False

    def counts(self) -> dict:
        with self.connect() as c:
            return {r[0]: r[1] for r in c.execute(f"SELECT payload->>'user_id', count(*) FROM {self.table} GROUP BY 1").fetchall()
                    if r[0] is not None}

    def zones(self) -> list:
        return sorted(self.counts())

    def all_ids(self) -> set:
        with self.connect() as c:
            return {str(r[0]) for r in c.execute(f"SELECT id FROM {self.table}").fetchall()}

    def rows(self, zone: str):
        with self.connect() as c:
            cur = c.execute(f"SELECT id::text, vector::text, payload FROM {self.table} WHERE payload->>'user_id' = %s"
                            " ORDER BY payload->>'created_at', id", (zone,))
            while True:
                chunk = cur.fetchmany(BATCH)
                if not chunk:
                    break
                for mid, vec, p in chunk:
                    yield from_payload(mid, vec, p)

    def search(self, zone: str, query: str, top_k: int) -> list:
        """跟 PgStore.search 一樣的算法（cosine 距離、同分依 id），所以向量照抄的話前 10 筆要一模一樣。"""
        q = self.vlit(self.embed([query])[0])
        with self.connect() as c:
            rows = c.execute(f"SELECT id::text, 1 - (vector <=> %s::vector) FROM {self.table} WHERE payload->>'user_id' = %s"
                             " AND vector IS NOT NULL ORDER BY vector <=> %s::vector, id LIMIT %s", (q, zone, q, top_k)).fetchall()
        return [(r[0], max(0.0, float(r[1]))) for r in rows]


class RestSource:
    """別台的 Mem0，走 REST（用 server/store.py 的 Mem0Store，跟 1.0.0 圓桌讀 Mem0 的方式一樣）。"""
    name = "rest"

    def __init__(self, mem0):
        self.mem0 = mem0

    def describe(self) -> str:
        return f"Mem0 REST（{self.mem0.url}）"

    def available(self) -> bool:
        return self.mem0.ping()

    def _entities(self) -> list:
        with self.mem0.http() as c:
            r = c.get("/entities")
            r.raise_for_status()
            return r.json() or []

    def counts(self) -> dict:
        return {e["id"]: e.get("total_memories") for e in self._entities() if e.get("type") == "user" and e.get("id")}

    def zones(self) -> list:
        return sorted(self.counts())

    def all_ids(self):
        return None  # REST 看不到整張表；history 只搬有搬到的那些

    def rows(self, zone: str):
        for m in self.mem0.list(zone):
            yield {"id": m.get("id"), "user_id": m.get("user_id") or zone, "agent_id": m.get("agent_id"), "memory": m.get("memory") or "",
                   "metadata": dict(m.get("metadata") or {}), "created_at": m.get("created_at"), "updated_at": m.get("updated_at"),
                   "vector": None}

    def search(self, zone: str, query: str, top_k: int) -> list:
        return [(h.get("id"), float(h.get("score") or 0.0)) for h in self.mem0.search(zone, query, {}, top_k)]

    def history(self, mid: str) -> list:
        return self.mem0.history(mid)


# ---- 目的地 ----

class Target:
    def __init__(self, store_mod, dsn: str, embed, dims: int):
        self.pg = store_mod.PgStore(dsn=dsn, embed=embed, dims=dims)
        self.vlit, self.dims = store_mod._vlit, dims

    def prepare(self):
        self.pg.start(wait=60)  # 建擴充、建表；維度跟 EMB_DIMS 不同會停

    def existing_ids(self, zone: str) -> set:
        with self.pg._connect() as c:
            return {str(r[0]) for r in c.execute("SELECT id FROM rt_memories WHERE user_id = %s", (zone,)).fetchall()}

    def insert(self, rows: list) -> int:
        now = dt.datetime.now(dt.timezone.utc)
        data = []
        for r in rows:
            ca = parse_ts(r["created_at"]) or now
            ua = parse_ts(r["updated_at"]) or ca
            data.append((r["id"], r["user_id"], r["agent_id"], r["memory"], json.dumps(r["metadata"] or {}, ensure_ascii=False),
                         r["vector"], ca, ua))
        with self.pg._connect() as c:
            cur = c.cursor()
            cur.executemany("INSERT INTO rt_memories (id, user_id, agent_id, memory, metadata, embedding, created_at, updated_at)"
                            " VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector, %s, %s) ON CONFLICT (id) DO NOTHING", data)
        return len(data)

    def history_keys(self) -> set:
        with self.pg._connect() as c:
            return {(str(r[0]), r[1], round(r[2].timestamp(), 3)) for r in c.execute("SELECT memory_id, event, at FROM rt_history").fetchall()}

    def insert_history(self, recs: list) -> int:
        """recs：(memory_id, event, old_memory, new_memory, actor, at)。已經有的（同 memory_id、事件、時間）跳過。"""
        have, data = self.history_keys(), []
        for mid, event, old, new, actor, at in recs:
            key = (mid, event, round(at.timestamp(), 3))
            if key in have:
                continue
            have.add(key)
            data.append((mid, event, old, new, actor, at))
        if data:
            with self.pg._connect() as c:
                c.cursor().executemany("INSERT INTO rt_history (memory_id, event, old_memory, new_memory, actor, at) VALUES (%s, %s, %s, %s, %s, %s)", data)
        return len(data)


# ---- 搬 ----

def copy_zone(src, tgt: Target, zone: str, embed) -> tuple:
    have, buf, done, skipped = tgt.existing_ids(zone), [], 0, 0

    def flush():
        nonlocal done
        if not buf:
            return
        todo = [r for r in buf if r["vector"] is None]
        if todo:  # rest 路（或 Mem0 那列沒向量）：重算
            for r, v in zip(todo, embed([r["memory"] for r in todo])):
                if len(v) != tgt.dims:
                    sys.exit(f"embedding 回 {len(v)} 維，EMB_DIMS 是 {tgt.dims}；EMB_MODEL 跟 EMB_DIMS 要成對")
                r["vector"] = tgt.vlit(v)
        for r in buf:
            n = r["vector"].count(",") + 1
            if n != tgt.dims:
                sys.exit(f"Mem0 的向量是 {n} 維，EMB_DIMS 是 {tgt.dims}：兩邊的 embedding 模型不同，不能照抄向量（改 EMB_DIMS，或用 --mode rest 重算）")
        done += tgt.insert(buf)
        buf.clear()

    for r in src.rows(zone):
        if not is_uuid(r["id"]):
            say(f"  警告：{zone} 有一筆編號不是 uuid（{r['id']!r}），跳過")
            continue
        if r["id"] in have:
            skipped += 1
            continue
        buf.append(r)
        if len(buf) >= BATCH:
            flush()
    flush()
    say(f"{zone}：搬了 {done} 筆" + (f"（已經在的 {skipped} 筆跳過）" if skipped else ""))
    return done, skipped


def history_from_sqlite(path: str, keep) -> list:
    """Mem0 的 history.db → (memory_id, event, old, new, actor, at)。keep(mid) 決定要不要這一筆。"""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT memory_id, old_memory, new_memory, event, created_at, updated_at, actor_id FROM history"
                           " ORDER BY created_at, updated_at").fetchall()
    finally:
        con.close()
    out, bad = [], 0
    for mid, old, new, event, created, updated, actor in rows:
        if not is_uuid(mid) or not keep(str(mid)):
            bad += 1
            continue
        at = parse_ts(updated) or parse_ts(created) or dt.datetime.now(dt.timezone.utc)  # 更新、刪除記在 updated_at；新增只有 created_at
        out.append((str(mid), (event or "").upper(), old, new, actor, at))
    return out, bad


def copy_history(src, tgt: Target, zones: list, migrated_ids: set, all_zones: bool):
    """搬異動紀錄。搬不了（沒有 history.db、讀不到）不算失敗：記憶本身已經在，只是 memory_history 看不到搬之前的變動；
    回一句警告讓主程式印在最後。"""
    if src.name == "sql":
        path = os.environ.get("MEM0_HISTORY_DB", "")
        if not path or not os.path.isfile(path):
            return f"找不到 Mem0 的 history.db（MEM0_HISTORY_DB={path or '沒設'}），異動紀錄沒搬（沒驗證）；memory_history 只會看到搬完之後的變動"
        src_all = src.all_ids()
        # 搬過的那些列的紀錄要；已經刪掉的記憶（表裡沒有）的紀錄在搬整個區時也帶著；別區的不要
        keep = (lambda mid: mid in migrated_ids or (all_zones and mid not in src_all))
        try:
            recs, dropped = history_from_sqlite(path, keep)
        except sqlite3.Error as e:
            return f"讀不了 Mem0 的 history.db（{path}）：{e}；異動紀錄沒搬（沒驗證），可以之後再跑一次 migrate 補"
        n = tgt.insert_history(recs)
        say(f"異動紀錄：history.db 有 {len(recs) + dropped} 筆，匯進 rt_history {n} 筆" + (f"（略過 {dropped} 筆：別區的或編號不對）" if dropped else "")
            + ("" if n == len(recs) else f"（{len(recs) - n} 筆已經在，跳過）"))
        return None
    recs = []
    for mid in sorted(migrated_ids):
        for h in src.history(mid):
            at = parse_ts(h.get("updated_at")) or parse_ts(h.get("created_at") or h.get("at")) or dt.datetime.now(dt.timezone.utc)
            recs.append((mid, (h.get("event") or "").upper(), h.get("old_memory"), h.get("new_memory"), h.get("actor_id") or h.get("actor"), at))
    n = tgt.insert_history(recs)
    say(f"異動紀錄：從 Mem0 REST 逐筆撈到 {len(recs)} 筆，匯進 rt_history {n} 筆" + ("" if n == len(recs) else f"（{len(recs) - n} 筆已經在，跳過）"))
    return None


# ---- 核對 ----

def compare_search(s_hits: list, p_hits: list, mode: str) -> tuple:
    s_ids, p_ids = [h[0] for h in s_hits], [h[0] for h in p_hits]
    if s_ids == p_ids:
        return True, f"前 {len(s_ids)} 筆順序一致"
    overlap = len(set(s_ids) & set(p_ids))
    if set(s_ids) == set(p_ids) and [round(h[1], 6) for h in s_hits] == [round(h[1], 6) for h in p_hits]:
        return True, f"前 {len(s_ids)} 筆相同，只有同分的順序不同"
    if mode == "rest" and overlap >= max(1, len(s_ids) - 1):
        return True, f"前 {len(s_ids)} 筆有 {overlap} 筆相同（REST 路向量是重算的，邊界差一筆算正常）"
    return False, f"前 {len(s_ids)} 筆只有 {overlap} 筆相同：來源 {s_ids[:5]}… 圓桌 {p_ids[:5]}…"


def verify(src, tgt: Target, zones: list, sample: int, queries: int, rnd: random.Random) -> list:
    fails = []
    for z in zones:
        srows = {r["id"]: r for r in src.rows(z) if is_uuid(r["id"])}
        prows = {r["id"]: r for r in tgt.pg.list(z)}
        missing, extra = set(srows) - set(prows), set(prows) - set(srows)
        line = f"{z}：來源 {len(srows)} 筆、rt_memories {len(prows)} 筆"
        if missing:
            fails.append(f"{z} 少了 {len(missing)} 筆")
            line += f"，少了 {len(missing)} 筆（例如 {sorted(missing)[:3]}）"
        else:
            line += "，來源的每一筆都在" + (f"（rt_memories 另有 {len(extra)} 筆是之後新寫的）" if extra else "")
        say(line)
        if not srows:
            continue
        ids = rnd.sample(sorted(srows), min(sample, len(srows)))
        bad = 0
        for mid in ids:
            s, p = srows[mid], prows.get(mid)
            if p is None:
                bad += 1
                continue
            diffs = []
            if (s["memory"] or "") != (p["memory"] or ""):
                diffs.append("內文")
            if (s["metadata"] or {}) != (p["metadata"] or {}):
                diffs.append("metadata")
            if (s["agent_id"] or None) != (p["agent_id"] or None):
                diffs.append("agent_id")
            if not same_ts(s["created_at"], p["created_at"]):
                diffs.append("created_at")
            if diffs:
                bad += 1
                say(f"  {mid}：{'、'.join(diffs)} 不一致")
        say(f"  抽 {len(ids)} 筆逐欄比：" + ("全部一致" if not bad else f"{bad} 筆不一致"))
        if bad:
            fails.append(f"{z} 抽樣 {bad} 筆不一致")
        for mid in ids[:max(0, queries)]:
            q = srows[mid]["memory"]
            if not q.strip():
                continue
            ok, why = compare_search(src.search(z, q, 10), [(r["id"], r["score"]) for r in tgt.pg.search(z, q, {}, 10)], src.name)
            say(f"  搜尋「{q[:20]}…」：{why}")
            if not ok:
                fails.append(f"{z} 搜尋前 10 筆不一致")
    return fails


# ---- 主程式 ----

def pick_source(mode: str, store_mod, dsn: str, embed, mem0_http):
    table = os.environ.get("MEM0_COLLECTION", "memories")
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table):
        sys.exit(f"MEM0_COLLECTION 只能是英數和底線：{table!r}")
    src_dsn = os.environ.get("MEM0_PG_DSN") or dsn
    url, key = os.environ.get("MEM0_URL", ""), os.environ.get("MEM0_API_KEY", "")
    if mode in ("auto", "sql"):
        s = SqlSource(src_dsn, table, embed, store_mod._vlit)
        if s.available():
            return s
        if mode == "sql":
            sys.exit(f"sql 模式找不到 Mem0 的表 {table}（{redact(src_dsn)}）：{s.error or '表不存在'}")
    if mode in ("auto", "rest"):
        if url and (key or mem0_http):
            r = RestSource(store_mod.Mem0Store(url=url, key=key, http=mem0_http))
            if r.available():
                return r
            sys.exit(f"連不上 Mem0 REST（{url}）：Mem0 沒在跑，或 MEM0_API_KEY 不對")
        if mode == "rest":
            sys.exit("rest 模式要設 MEM0_URL 和 MEM0_API_KEY")
    sys.exit(f"找不到來源：{redact(src_dsn)} 裡沒有 Mem0 的表 {table}（MEM0_PG_DSN／MEM0_COLLECTION），也沒設 MEM0_URL＋MEM0_API_KEY 可以走 REST")


def main(argv=None, embed=None, mem0_http=None) -> int:
    ap = argparse.ArgumentParser(description="把 Mem0 裡的記憶搬進圓桌自己的表並核對")
    ap.add_argument("--mode", choices=["auto", "sql", "rest"], default="auto", help="來源：同一個 Postgres 直接讀（sql）或走 Mem0 REST（rest）；auto 自己挑")
    ap.add_argument("--zones", default="", help="只搬這些區（逗號隔開）；預設搬來源裡的每一區")
    ap.add_argument("--sample", type=int, default=20, help="每區隨機抽幾筆逐欄比對（預設 20）")
    ap.add_argument("--queries", type=int, default=3, help="每區拿幾筆內文當問句比搜尋前 10 筆（預設 3）")
    ap.add_argument("--dry-run", action="store_true", help="只列出來源各區筆數，不搬、不核對")
    ap.add_argument("--verify-only", action="store_true", help="不搬，只核對")
    ap.add_argument("--skip-history", action="store_true", help="不搬異動紀錄")
    ap.add_argument("--seed", type=int, default=None, help="抽樣的亂數種子（要重現時用）")
    a = ap.parse_args(argv)

    store_mod = load_store()
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        sys.exit("要設 DATABASE_URL（目的地，圓桌的 Postgres）")
    dims = int(os.environ.get("EMB_DIMS", "1024"))
    embed = embed or make_embed(os.environ.get("EMB_URL", "http://embed:8080").rstrip("/"), os.environ.get("EMB_MODEL", "qwen3-embedding-0.6b"))
    src = pick_source(a.mode, store_mod, dsn, embed, mem0_http)
    say(f"來源：{src.describe()}")
    say(f"目的地：rt_memories／rt_history（{redact(dsn)}，向量 {dims} 維）")
    tgt = Target(store_mod, dsn, embed, dims)
    try:
        tgt.prepare()
    except RuntimeError as e:  # 連不上、沒裝 pgvector、表的維度跟 EMB_DIMS 不同
        sys.exit(str(e))

    counts = src.counts()
    zones = [z for z in a.zones.split(",") if z] or sorted(counts)
    all_zones = not a.zones
    say("來源各區：" + ("、".join(f"{z} {counts.get(z, 0)} 筆" for z in zones) if zones else "（空的，沒有東西可搬）"))
    if a.dry_run or not zones:
        return 0

    warn = None
    if not a.verify_only:
        migrated = set()
        for z in zones:
            copy_zone(src, tgt, z, embed)
            migrated |= tgt.existing_ids(z)
        if not a.skip_history:
            warn = copy_history(src, tgt, zones, migrated, all_zones)
            if warn:
                say("注意：" + warn)
    say("核對…")
    fails = verify(src, tgt, zones, a.sample, a.queries, random.Random(a.seed))
    if fails:
        say("核對沒過：" + "；".join(fails))
        say("RT_STORE 不要切成 pg。處理好再跑一次（已搬的會跳過），或加 --verify-only 只核對。")
        return 2
    say("核對全部通過：筆數對、抽樣逐欄一致、搜尋前 10 筆一致。" + (f"（但：{warn}）" if warn else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
