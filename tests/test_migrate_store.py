"""搬資料工具（tools/migrate_store.py）的離線測試：造一張假的 Mem0 pgvector 表＋假的 history.db，
sql 路和 rest 路（假的 Mem0 REST）各搬一次，核對筆數、metadata 拆法、向量照抄、異動紀錄、重跑不重複、核對會抓到少筆和改過的資料。
要一個拋棄式 Postgres＋pgvector（見 server/tests/_pgtest.py）：
    uv run --no-project --python 3.12 --with httpx --with "psycopg[binary]" --with pgserver python tests/test_migrate_store.py
沒有 Postgres 時整個略過（印「注意：」、結束碼 0）。"""
import datetime as dt
import hashlib
import json
import math
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "tests"))
sys.path.insert(0, str(ROOT / "tools"))
os.environ["RT_SERVER_DIR"] = str(ROOT / "server")
from _pgtest import start_pg  # noqa: E402
import migrate_store as M  # noqa: E402

dsn, cleanup = start_pg()
if not dsn:
    print(f"注意：{cleanup}；test_migrate_store 整個略過")
    sys.exit(0)

import httpx  # noqa: E402
import psycopg  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("通過  " if ok else "失敗  ") + name + (f"  → {detail}" if detail and not ok else ""))


VOCAB = ["蘋果", "香蕉", "貓", "狗", "資料庫", "向量", "留言"]
DIMS = len(VOCAB) + 1


def toy_embed(texts):
    out = []
    for t in texts:
        v = [float(t.count(w)) for w in VOCAB] + [0.1]
        n = math.sqrt(sum(x * x for x in v))
        out.append([x / n for x in v])
    return out


def vlit(v):
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def cos(a, b):
    return sum(x * y for x, y in zip(a, b))


T0 = dt.datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=dt.timezone.utc)


def ts(minutes):
    return (T0 + dt.timedelta(minutes=minutes)).isoformat()


# ---- 假資料：payload 照 Mem0 2.2.1 的樣子平鋪 ----
SRC = []   # 每筆 {id, zone, agent, text, md, created, updated, payload, vector}


def mk(zone, agent, text, md, minute, updated=True, extra=None):
    mid = str(uuid.uuid4())
    payload = {"data": text, "hash": hashlib.md5(text.encode()).hexdigest(), "created_at": ts(minute),
               "user_id": zone, "role": "user", **md}
    if updated:
        payload["updated_at"] = ts(minute)
    else:
        payload["updated_at"] = None
    if agent is not None:
        payload["agent_id"] = agent
        payload["actor_id"] = agent
    payload.update(extra or {})
    SRC.append({"id": mid, "zone": zone, "agent": agent, "text": text, "md": md, "created": ts(minute),
                "payload": payload, "vector": toy_embed([text])[0]})
    return mid


r0 = mk("roundtable", "claude", "蘋果 香蕉 的決定", {"type": "decision", "project": "mcp", "tags": ["a", "b"]}, 0)
mk("roundtable", "claude", "貓 狗 筆記", {"type": "note", "project": "mcp"}, 1, extra={"text_lemmatized": "貓 狗", "attributed_to": "codex"})
mk("roundtable", "codex", "資料庫 向量 進度", {"type": "status", "project": "mem0", "goals": [{"text": "x", "done": True}], "status_at": ""}, 2)
mk("roundtable", "codex", "向量 向量 向量", {"type": "note", "project": "mem0", "n": 3, "f": 1.5, "nested": {"k": None}}, 3, updated=False)
mk("roundtable", "claude", "留言 蘋果", {"type": "decision", "project": "board", "中文鍵": "中文值"}, 4)
mk("roundtable-ops", "claude", "留言 給 codex", {"type": "message", "to": ["codex"], "read_by": [], "needs_action": True}, 5)
mk("roundtable-ops", "codex", "留言 給 user 貓", {"type": "message", "to": ["user"], "read_by": ["user"]}, 6)
mk("roundtable-ops", None, "沒有作者的封存留言 狗", {"type": "message-archive", "from": "hermes"}, 7)
mk("personas", "claude", "人格 蘋果 貓", {"type": "persona", "name": "alpha"}, 8)
mk("profile", "claude", "經歷 資料庫", {"type": "experience", "source": "chat"}, 9)
mk("profile", "codex", "經歷 香蕉 狗", {"type": "experience", "source": "chat"}, 10)
mk("sandbox", "claude", "別區 的 資料 向量", {"type": "note"}, 11)
ZONES_ALL = sorted({r["zone"] for r in SRC})
DELETED_ID = str(uuid.uuid4())  # 早就刪掉的記憶：只在 history 裡有


def expect_md(r):
    return {k: v for k, v in r["payload"].items() if k not in M.CORE_KEYS and k not in M.PROMOTED_KEYS}


# ---- 來源：pgvector 表 memories 在主資料庫；目的地各用一個資料庫 ----
def admin():
    return psycopg.connect(dsn, autocommit=True)


with admin() as c:
    c.execute("CREATE EXTENSION IF NOT EXISTS vector")
    c.execute(f"CREATE TABLE memories (id uuid PRIMARY KEY, vector vector({DIMS}), payload jsonb)")
    for r in SRC:
        c.execute("INSERT INTO memories (id, vector, payload) VALUES (%s, %s::vector, %s::jsonb)",
                  (r["id"], vlit(r["vector"]), json.dumps(r["payload"], ensure_ascii=False)))
    for db in ("rt_sql", "rt_zone", "rt_rest", "rt_dry", "rt_dims"):
        c.execute(f"CREATE DATABASE {db}")


def db_url(name):
    base, _, _ = dsn.rpartition("/")
    return f"{base}/{name}"


# history.db：每筆 ADD，r0 一筆 UPDATE，一筆早就刪掉的 DELETE，一筆編號壞掉的
tmp = Path(tempfile.mkdtemp(prefix="rt-migrate-"))
hist = tmp / "history.db"
con = sqlite3.connect(hist)
con.execute("CREATE TABLE history (id TEXT PRIMARY KEY, memory_id TEXT, old_memory TEXT, new_memory TEXT, event TEXT,"
            " created_at DATETIME, updated_at DATETIME, is_deleted INTEGER, actor_id TEXT, role TEXT)")
HROWS = []
for r in SRC:
    HROWS.append((str(uuid.uuid4()), r["id"], None, r["text"], "ADD", r["created"], None, 0, r["agent"], "user"))
HROWS.append((str(uuid.uuid4()), r0, "蘋果 的決定（舊）", "蘋果 香蕉 的決定", "UPDATE", SRC[0]["created"], ts(30), 0, "claude", "user"))
HROWS.append((str(uuid.uuid4()), DELETED_ID, "被刪掉的", None, "DELETE", ts(-5), ts(20), 1, "codex", "user"))
HROWS.append((str(uuid.uuid4()), "not-a-uuid", None, "壞掉的", "ADD", ts(0), None, 0, None, None))
con.executemany("INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?,?)", HROWS)
con.commit()
con.close()


def run(argv, db, **env):
    """跑一次工具，回結束碼。"""
    old = dict(os.environ)
    http = env.pop("_http", None)
    # 來源表 memories 放在主資料庫、目的地各用一個資料庫（同一個叢集的另一個 DB，MEM0_PG_DSN 指過去）
    os.environ.update({"DATABASE_URL": db_url(db), "MEM0_PG_DSN": dsn, "EMB_DIMS": str(DIMS), "MEM0_HISTORY_DB": str(hist),
                       "MEM0_COLLECTION": "memories"}, **env)
    for k in ("MEM0_URL", "MEM0_API_KEY"):
        if k not in env:
            os.environ.pop(k, None)
    try:
        return M.main(argv, embed=toy_embed, mem0_http=http)
    except SystemExit as e:
        if isinstance(e.code, int):
            return e.code
        print(f"  （工具以訊息結束：{e.code}）")
        return 1
    finally:
        os.environ.clear()
        os.environ.update(old)


def q(db, sql, args=()):
    with psycopg.connect(db_url(db)) as c:
        return c.execute(sql, args).fetchall()


try:
    # 1. sql 路：搬整張表
    print("--- sql 路 ---")
    rc = run(["--mode", "sql", "--sample", "50", "--seed", "1"], "rt_sql")
    check("sql：結束碼 0", rc == 0, rc)
    n = q("rt_sql", "SELECT count(*) FROM rt_memories")[0][0]
    check("sql：筆數跟來源一樣", n == len(SRC), f"{n} != {len(SRC)}")
    for z in ZONES_ALL:
        want = sum(1 for r in SRC if r["zone"] == z)
        got = q("rt_sql", "SELECT count(*) FROM rt_memories WHERE user_id = %s", (z,))[0][0]
        check(f"sql：{z} 區 {want} 筆", got == want, got)
    rows = {str(r[0]): r for r in q("rt_sql", "SELECT id, user_id, agent_id, memory, metadata, embedding::text, created_at, updated_at FROM rt_memories")}
    with psycopg.connect(dsn) as c:
        src_vec = {str(r[0]): r[1] for r in c.execute("SELECT id, vector::text FROM memories").fetchall()}
    ok_md = ok_vec = ok_ts = ok_agent = True
    for r in SRC:
        p = rows[r["id"]]
        ok_md &= (p[4] == expect_md(r)) and p[3] == r["text"]
        ok_vec &= (p[5] == src_vec[r["id"]])
        ok_ts &= abs((p[6] - dt.datetime.fromisoformat(r["created"])).total_seconds()) < 0.001
        ok_agent &= ((p[2] or None) == r["agent"]) and p[1] == r["zone"]
    check("sql：metadata 只剩圓桌的鍵（Mem0 的 data/hash/role/actor_id/text_lemmatized/attributed_to 都拆掉）", ok_md)
    check("sql：向量照抄（embedding::text 跟來源一樣）", ok_vec)
    check("sql：created_at 照來源", ok_ts)
    check("sql：agent_id、user_id 照來源", ok_agent)
    p3 = rows[SRC[3]["id"]]
    check("sql：updated_at 是 null 的列用 created_at 補", p3[7] == p3[6])
    md0 = rows[r0][4]
    check("sql：metadata 的清單與中文鍵都完整", md0.get("tags") == ["a", "b"] and rows[SRC[4]["id"]][4].get("中文鍵") == "中文值")
    hcount = q("rt_sql", "SELECT count(*) FROM rt_history")[0][0]
    check("sql：異動紀錄 = 每筆 ADD + 1 UPDATE + 1 DELETE（壞編號那筆略過）", hcount == len(SRC) + 2, hcount)
    h0 = q("rt_sql", "SELECT event, old_memory, new_memory, actor, at FROM rt_history WHERE memory_id = %s ORDER BY id", (r0,))
    check("sql：r0 的紀錄是 ADD 再 UPDATE，操作者與時間對", [h[0] for h in h0] == ["ADD", "UPDATE"] and h0[1][1] == "蘋果 的決定（舊）"
          and h0[1][3] == "claude" and abs((h0[1][4] - dt.datetime.fromisoformat(ts(30))).total_seconds()) < 0.001
          and abs((h0[0][4] - dt.datetime.fromisoformat(SRC[0]["created"])).total_seconds()) < 0.001)
    hd = q("rt_sql", "SELECT event, actor FROM rt_history WHERE memory_id = %s", (DELETED_ID,))
    check("sql：早就刪掉的記憶也留 DELETE 紀錄", hd == [("DELETE", "codex")], hd)

    # 2. 重跑：不重複
    rc = run(["--mode", "sql", "--sample", "50"], "rt_sql")
    n2, h2 = q("rt_sql", "SELECT count(*) FROM rt_memories")[0][0], q("rt_sql", "SELECT count(*) FROM rt_history")[0][0]
    check("sql：重跑結束碼 0、記憶和紀錄都不會多", rc == 0 and n2 == n and h2 == hcount, f"rc={rc} {n2}/{n} {h2}/{hcount}")

    # 3. 只搬指定的區（別區的紀錄、刪掉的紀錄不帶）
    rc = run(["--mode", "sql", "--zones", "roundtable,personas", "--sample", "50"], "rt_zone")
    zs = sorted({r[0] for r in q("rt_zone", "SELECT DISTINCT user_id FROM rt_memories")})
    hz = q("rt_zone", "SELECT count(*) FROM rt_history")[0][0]
    check("sql：--zones 只搬那兩區", rc == 0 and zs == ["personas", "roundtable"], f"rc={rc} {zs}")
    check("sql：--zones 時只帶那兩區的紀錄（5+1 ADD、1 UPDATE，沒有 DELETE）", hz == 7, hz)

    # 4. 核對會抓到少筆、改過的欄位
    with psycopg.connect(db_url("rt_sql")) as c:
        c.execute("DELETE FROM rt_memories WHERE id = %s", (SRC[5]["id"],))
    rc = run(["--mode", "sql", "--verify-only", "--sample", "50"], "rt_sql")
    check("sql：少一筆時 --verify-only 結束碼 2", rc == 2, rc)
    rc = run(["--mode", "sql", "--sample", "50"], "rt_sql")
    check("sql：再跑一次會把少的那筆補回來、結束碼 0", rc == 0 and q("rt_sql", "SELECT count(*) FROM rt_memories")[0][0] == n, rc)
    with psycopg.connect(db_url("rt_sql")) as c:
        c.execute("UPDATE rt_memories SET metadata = metadata || '{\"tampered\": 1}'::jsonb WHERE id = %s", (SRC[6]["id"],))
    rc = run(["--mode", "sql", "--verify-only", "--sample", "50"], "rt_sql")
    check("sql：metadata 被改過時 --verify-only 結束碼 2", rc == 2, rc)
    with psycopg.connect(db_url("rt_sql")) as c:
        c.execute("UPDATE rt_memories SET metadata = metadata - 'tampered' WHERE id = %s", (SRC[6]["id"],))
    rc = run(["--mode", "sql", "--verify-only", "--sample", "50"], "rt_sql")
    check("sql：改回來就通過", rc == 0, rc)

    # 5. dry-run 不寫
    rc = run(["--dry-run"], "rt_dry")
    check("auto：--dry-run 挑到 sql 路、結束碼 0、不寫資料", rc == 0 and q("rt_dry", "SELECT count(*) FROM rt_memories")[0][0] == 0, rc)

    # 6. 維度不合要停（EMB_DIMS 說 4 維、目的地表就建成 4 維，來源向量是 8 維 → 不能照抄）
    rc = run(["--mode", "sql"], "rt_dims", EMB_DIMS="4")
    n6 = q("rt_dims", "SELECT count(*) FROM rt_memories")[0][0]
    check("sql：EMB_DIMS 跟來源向量維度不同時以非 0 結束、不寫資料", rc not in (0, None) and n6 == 0, f"rc={rc} rows={n6}")

    # 7. rest 路：假的 Mem0 REST（在記憶體裡，用同一份假資料）
    print("--- rest 路 ---")
    by_id = {r["id"]: r for r in SRC}

    def rest_row(r):
        return {"id": r["id"], "memory": r["text"], "metadata": expect_md(r), "user_id": r["zone"], "agent_id": r["agent"],
                "created_at": r["created"], "updated_at": r["payload"].get("updated_at"), "hash": r["payload"]["hash"]}

    def handler(req):
        if req.headers.get("X-API-Key") != "k":
            return httpx.Response(401, json={"detail": "bad key"})
        path, params = req.url.path, dict(req.url.params)
        if req.method == "GET" and path == "/entities":
            users, agents = {}, {}
            for r in SRC:
                users[r["zone"]] = users.get(r["zone"], 0) + 1
                if r["agent"]:
                    agents[r["agent"]] = agents.get(r["agent"], 0) + 1
            return httpx.Response(200, json=[{"type": "user", "id": u, "total_memories": n} for u, n in users.items()]
                                  + [{"type": "agent", "id": a, "total_memories": n} for a, n in agents.items()])
        if req.method == "GET" and path == "/memories":
            out = [rest_row(r) for r in SRC if r["zone"] == params.get("user_id")
                   and ("agent_id" not in params or r["agent"] == params["agent_id"])]
            return httpx.Response(200, json={"results": out[: int(params.get("top_k", 100))]})
        if req.method == "GET" and path.endswith("/history"):
            mid = path.split("/")[2]
            return httpx.Response(200, json=[{"id": h[0], "memory_id": h[1], "old_memory": h[2], "new_memory": h[3], "event": h[4],
                                              "created_at": h[5], "updated_at": h[6], "is_deleted": h[7], "actor_id": h[8], "role": h[9]}
                                             for h in HROWS if h[1] == mid])
        if req.method == "GET" and path.startswith("/memories/"):
            r = by_id.get(path.split("/")[2])
            return httpx.Response(200, json=rest_row(r) if r else None)
        if req.method == "POST" and path == "/search":
            body = json.loads(req.content)
            f = dict(body.get("filters") or {})
            uid = f.pop("user_id", None)
            qv = toy_embed([body["query"]])[0]
            hits = [{**rest_row(r), "score": max(0.0, min(1.0, cos(qv, r["vector"])))} for r in SRC if r["zone"] == uid]
            hits.sort(key=lambda h: (-h["score"], h["id"]))
            return httpx.Response(200, json={"results": hits[: int(body.get("top_k", 10))]})
        return httpx.Response(404, json={"detail": "not found"})

    def http():  # 金鑰照 MEM0_API_KEY 帶，所以金鑰不對的情況也測得到
        return httpx.Client(base_url="http://fake-mem0", headers={"X-API-Key": os.environ.get("MEM0_API_KEY", "")},
                            transport=httpx.MockTransport(handler))

    rc = run(["--mode", "rest", "--sample", "50", "--seed", "2"], "rt_rest", MEM0_URL="http://fake-mem0", MEM0_API_KEY="k", _http=http)
    check("rest：結束碼 0", rc == 0, rc)
    n = q("rt_rest", "SELECT count(*) FROM rt_memories")[0][0]
    check("rest：筆數跟來源一樣", n == len(SRC), f"{n} != {len(SRC)}")
    rows = {str(r[0]): r for r in q("rt_rest", "SELECT id, user_id, agent_id, memory, metadata, embedding::text, created_at FROM rt_memories")}
    ok = all(rows[r["id"]][4] == expect_md(r) and rows[r["id"]][3] == r["text"] and (rows[r["id"]][2] or None) == r["agent"]
             and rows[r["id"]][5] == src_vec[r["id"]]
             and abs((rows[r["id"]][6] - dt.datetime.fromisoformat(r["created"])).total_seconds()) < 0.001 for r in SRC)
    check("rest：內文、metadata、agent_id、created_at 照來源，向量重算後跟來源一樣", ok)
    hr = q("rt_rest", "SELECT count(*) FROM rt_history")[0][0]
    check("rest：異動紀錄逐筆撈（每筆 ADD + r0 的 UPDATE；刪掉的那筆 REST 看不到）", hr == len(SRC) + 1, hr)
    rc = run(["--mode", "rest", "--sample", "50"], "rt_rest", MEM0_URL="http://fake-mem0", MEM0_API_KEY="k", _http=http)
    check("rest：重跑不重複", rc == 0 and q("rt_rest", "SELECT count(*) FROM rt_history")[0][0] == hr
          and q("rt_rest", "SELECT count(*) FROM rt_memories")[0][0] == n, rc)
    rc = run(["--mode", "rest"], "rt_rest", MEM0_URL="http://fake-mem0", MEM0_API_KEY="wrong", _http=http)
    check("rest：金鑰不對時以非 0 結束", rc not in (0, None), rc)
finally:
    cleanup()

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} 項通過" + (f"；失敗：{failed}" if failed else ""))
sys.exit(1 if failed else 0)
