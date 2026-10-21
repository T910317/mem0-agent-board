"""儲存層合約測試：同一套檢查對兩個後端跑，確認 PgStore 跟 Mem0Store 對 server.py 來說長得一樣。
- Mem0Store：接一個忠實模仿 Mem0 REST 的假 client（在記憶體裡，不連真 Mem0）。回應形狀照真的 Mem0：
  POST /memories 回 {"results":[{id, memory, event:"ADD"}]}、GET /memories/{id} 找不到回 200＋null、PUT 合併 metadata、
  /search 先取 top_k 再砍 threshold、history 有 ADD／UPDATE／DELETE。
- PgStore：接真的 Postgres＋pgvector（見 _pgtest.py：RT_TEST_PG_DSN 或 pgserver），向量用小維度的玩具 embedding。
用法：python test_store_contract.py <新版 server.py 目錄>
沒有 Postgres 可用時 pg 的部分略過（印「注意：」那行、結束碼仍是 0），Mem0 的部分照跑。"""
import datetime as dt
import math
import os
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
NEW_DIR = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, NEW_DIR)
import store  # noqa: E402
from _pgtest import start_pg  # noqa: E402

VOCAB = ["蘋果", "香蕉", "貓", "狗", "資料庫", "向量", "留言"]
DIMS = len(VOCAB) + 1  # 最後一維是常數，避免零向量（cosine 沒定義）


def toy_embed(texts):
    """玩具 embedding：每個詞出現幾次就是幾，最後一維固定 0.1，再正規化。同樣的詞→分數 1.0；完全不同→接近 0。"""
    out = []
    for t in texts:
        v = [float(t.count(w)) for w in VOCAB] + [0.1]
        n = math.sqrt(sum(x * x for x in v))
        out.append([x / n for x in v])
    return out


def cos(a, b):
    return sum(x * y for x, y in zip(a, b))  # 已正規化


FAILS = []


def check(name, ok, detail=""):
    print(("通過  " if ok else "失敗  ") + name + (f"  → {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def parse_ts(ts):
    return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))  # 跟 server.py 解析時間的方式一樣


def is_uuid(s):
    try:
        uuid.UUID(str(s))
        return True
    except ValueError:
        return False


# ---- 假 Mem0 ----

class R:
    def __init__(self, data, status=200):
        self.d, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.d


class FakeMem0:
    """記憶體裡的 Mem0：rows 是 id→一列，hist 是 id→異動紀錄。"""
    def __init__(self):
        self.rows, self.hist = {}, {}

    @staticmethod
    def now():
        return dt.datetime.now(dt.timezone.utc).isoformat()

    def log(self, mid, event, old, new, actor):
        self.hist.setdefault(mid, []).append({"id": str(uuid.uuid4()), "memory_id": mid, "old_memory": old, "new_memory": new,
                                              "event": event, "created_at": self.now(), "updated_at": None,
                                              "is_deleted": 1 if event == "DELETE" else 0, "actor_id": actor, "role": "user"})


class FakeClient:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, path, params=None, **k):
        params, rows = dict(params or {}), self.db.rows
        if path == "/entities":
            users, agents = {}, {}
            for r in rows.values():
                users[r["user_id"]] = users.get(r["user_id"], 0) + 1
                if r.get("agent_id"):
                    agents[r["agent_id"]] = agents.get(r["agent_id"], 0) + 1
            return R([{"type": "user", "id": u, "total_memories": n} for u, n in users.items()]
                     + [{"type": "agent", "id": a, "total_memories": n} for a, n in agents.items()])
        if path == "/memories":
            out = [r for r in rows.values() if r["user_id"] == params.get("user_id")
                   and ("agent_id" not in params or r.get("agent_id") == params["agent_id"])]
            return R({"results": [dict(r) for r in out[: int(params.get("top_k", 100))]]})
        if path.endswith("/history"):
            return R(list(self.db.hist.get(path.split("/")[2], [])))
        if path.startswith("/memories/"):
            r = rows.get(path.split("/")[2])
            return R(dict(r) if r else None)  # 真 Mem0 找不到回 200＋null
        return R({"detail": "not found"}, 404)

    def post(self, path, json=None, **k):
        body = dict(json or {})
        if path == "/memories":
            mid, text = str(uuid.uuid4()), body["messages"][0]["content"]
            self.db.rows[mid] = {"id": mid, "memory": text, "metadata": dict(body.get("metadata") or {}), "user_id": body["user_id"],
                                 "agent_id": body.get("agent_id"), "created_at": self.db.now(), "updated_at": None, "hash": "x"}
            self.db.log(mid, "ADD", None, text, body.get("agent_id"))
            return R({"results": [{"id": mid, "memory": text, "event": "ADD", "actor_id": body.get("agent_id"), "role": "user"}]})
        if path == "/search":
            f = dict(body.get("filters") or {})
            uid, agent = f.pop("user_id", None), f.pop("agent_id", None)
            q, hits = toy_embed([body["query"]])[0], []
            for r in self.db.rows.values():
                if r["user_id"] != uid or (agent is not None and r.get("agent_id") != agent):
                    continue
                if any((r.get("metadata") or {}).get(k) != v for k, v in f.items()):
                    continue
                hits.append({**r, "score": min(1.0, max(0.0, cos(q, toy_embed([r["memory"]])[0])))})  # pgvector 會把相似度夾在 ±1
            hits.sort(key=lambda h: -h["score"])
            hits = hits[: int(body.get("top_k", 10))]
            if body.get("threshold") is not None:
                hits = [h for h in hits if h["score"] >= body["threshold"]]
            return R({"results": hits})
        return R({"detail": "not found"}, 404)

    def put(self, path, json=None, **k):
        mid = path.split("/")[2]
        r = self.db.rows.get(mid)
        if not r:
            return R({"detail": "not found"}, 404)
        old, r["memory"] = r["memory"], json["text"]
        r["metadata"] = {**r["metadata"], **(json.get("metadata") or {})}  # Mem0 是合併
        r["updated_at"] = self.db.now()
        self.db.log(mid, "UPDATE", old, json["text"], None)
        return R({"message": "ok"})

    def delete(self, path, **k):
        mid = path.split("/")[2]
        r = self.db.rows.pop(mid, None)
        if not r:
            return R({"detail": "not found"}, 404)
        self.db.log(mid, "DELETE", r["memory"], None, None)
        return R({"message": "ok"})


# ---- 兩個後端共用的檢查 ----

ACTOR = ["tester"]


def contract(S, label, pg=False):
    """回 (這次用的 user_id 前綴, 造出來的 id 清單)。"""
    uid, ids = f"ct-{uuid.uuid4().hex[:8]}", []

    def P(name):
        return f"[{label}] {name}"

    check(P("ping"), S.ping() is True)
    check(P("list 空區回 []"), S.list(uid) == [])
    md1 = {"type": "note", "project": "p1"}
    r1 = S.add(uid, "alice", "蘋果 是 紅色 的", md1)
    id1 = r1.get("id")
    check(P("add 回統一格式"), is_uuid(id1) and r1["memory"] == "蘋果 是 紅色 的" and r1["metadata"] == md1
          and r1["agent_id"] == "alice" and r1["user_id"] == uid, repr(r1))
    id2 = S.add(uid, "bob", "香蕉 是 黃色 的", {"type": "note", "project": "p2"})["id"]
    md3 = {"type": "message", "project": "p1", "n": 1, "tags": ["a", "b"], "zh": "中文"}
    id3 = S.add(uid, "alice", "貓 在 睡覺", md3)["id"]
    other = S.add(f"{uid}-other", "alice", "狗 在 跑", {"type": "note"})["id"]  # 另一區，不該混進來
    ids += [id1, id2, id3, other]
    rows = S.list(uid)
    check(P("list 回全部 3 筆"), {r["id"] for r in rows} == {id1, id2, id3}, [r["id"] for r in rows])
    check(P("list 每筆欄位齊全、時間可解析"),
          all(r["memory"] and isinstance(r["metadata"], dict) and r["user_id"] == uid and r["agent_id"] and parse_ts(r["created_at"]) for r in rows),
          repr(rows[:1]))
    check(P("list 可依作者"), {r["id"] for r in S.list(uid, agent_id="alice")} == {id1, id3})
    check(P("count"), S.count(uid) == 3, S.count(uid))
    g = S.get(id3)
    check(P("get 讀回內文與 metadata（含整數、清單、中文）"),
          bool(g) and g["memory"] == "貓 在 睡覺" and g["metadata"] == md3 and g["agent_id"] == "alice" and g["user_id"] == uid, repr(g))
    check(P("get 不存在回 None"), S.get(str(uuid.uuid4())) is None)
    check(P("get 亂碼編號回 None"), S.get("not-a-uuid") is None)
    hits = S.search(uid, "蘋果", {}, 10)
    check(P("search 最像的排第一、每筆有 score"),
          bool(hits) and hits[0]["id"] == id1 and all(isinstance(h.get("score"), float) and 0 <= h["score"] <= 1 for h in hits),
          [(h["id"], h.get("score")) for h in hits])
    check(P("search 不跨區"), {h["id"] for h in hits} == {id1, id2, id3}, [h["id"] for h in hits])
    check(P("search 第一筆分數 1.0、其他接近 0"), bool(hits) and abs(hits[0]["score"] - 1.0) < 1e-6 and all(h["score"] < 0.1 for h in hits[1:]),
          [h["score"] for h in hits])
    check(P("search 用 metadata 篩"), [h["id"] for h in S.search(uid, "蘋果", {"project": "p2"}, 10)] == [id2])
    check(P("search 用作者篩"), [h["id"] for h in S.search(uid, "蘋果", {"agent_id": "bob"}, 10)] == [id2])
    check(P("search 用 metadata 的整數篩"), [h["id"] for h in S.search(uid, "貓", {"n": 1}, 10)] == [id3])
    check(P("search 兩個條件都要符合"), [h["id"] for h in S.search(uid, "蘋果", {"type": "note", "project": "p1"}, 10)] == [id1])
    check(P("search 條件對不到回 []"), S.search(uid, "蘋果", {"project": "nope"}, 10) == [])
    check(P("search 只取 top_k 筆"), len(S.search(uid, "蘋果", {}, 1)) == 1)
    check(P("search threshold 砍掉不像的"), [h["id"] for h in S.search(uid, "蘋果", {}, 10, threshold=0.5)] == [id1])
    check(P("search threshold=0 全留"), len(S.search(uid, "蘋果", {}, 10, threshold=0)) == 3)
    check(P("search 空區回 []"), S.search(f"{uid}-empty", "蘋果", {}, 10) == [])
    # 更新
    md1b = {"type": "note", "project": "p1", "edited": True}
    S.update(id1, "蘋果 是 綠色 的", md1b)
    g = S.get(id1)
    check(P("update 內文與 metadata 都換了、updated_at 可解析"),
          g["memory"] == "蘋果 是 綠色 的" and g["metadata"] == md1b and parse_ts(g["updated_at"]), repr(g))
    check(P("update 後搜尋用新向量"), S.search(uid, "蘋果", {}, 1)[0]["id"] == id1)
    S.update(id1, "蘋果 是 綠色 的", {**md1b, "read_by": "x"})  # 只改 metadata（已讀、狀態這類更新）
    g = S.get(id1)
    check(P("update 只改 metadata"), g["metadata"].get("read_by") == "x" and g["memory"] == "蘋果 是 綠色 的", repr(g))
    try:
        S.update(str(uuid.uuid4()), "x", {})
        check(P("update 不存在要報錯"), False)
    except Exception:
        check(P("update 不存在要報錯"), True)
    h = S.history(id1)
    check(P("history 舊的在前：ADD、UPDATE、UPDATE"), [str(e.get("event")).upper() for e in h] == ["ADD", "UPDATE", "UPDATE"],
          [e.get("event") for e in h])
    check(P("history 的 UPDATE 有新舊內文"), len(h) > 1 and h[1].get("old_memory") == "蘋果 是 紅色 的" and h[1].get("new_memory") == "蘋果 是 綠色 的",
          repr(h[1:2]))
    check(P("history 每筆有 at"), bool(h) and all(parse_ts(e.get("at")) for e in h))
    check(P("server.py 算修改次數的方式（event==UPDATE）"), sum(1 for e in h if str(e.get("event")).upper() == "UPDATE") == 2)
    # 刪除
    S.delete(id3)
    check(P("delete 後 get 回 None"), S.get(id3) is None)
    check(P("delete 後 list 少一筆"), {r["id"] for r in S.list(uid)} == {id1, id2})
    check(P("delete 後搜不到"), id3 not in {x["id"] for x in S.search(uid, "貓", {}, 10)})
    hd = S.history(id3)
    check(P("history 記了 DELETE"), bool(hd) and str(hd[-1].get("event")).upper() == "DELETE", [e.get("event") for e in hd])
    if pg:
        S.delete(str(uuid.uuid4()))
        check(P("delete 不存在的不報錯"), True)
        check(P("history 亂碼編號回 []"), S.history("not-a-uuid") == [])
        check(P("history 不存在回 []"), S.history(str(uuid.uuid4())) == [])
        check(P("history 的 actor 是呼叫時的身分"), all(e.get("actor") == "tester" for e in h), [e.get("actor") for e in h])
    e = S.add(uid, None, "沒有 metadata 也沒有作者", {})
    ids.append(e["id"])
    g = S.get(e["id"])
    check(P("add 空 metadata、無作者"), g["metadata"] == {} and g["agent_id"] is None, repr(g))
    return uid, ids


def pg_extra(dsn):
    S2 = store.PgStore(dsn=dsn, embed=toy_embed, actor=lambda: ACTOR[0], dims=DIMS)
    S2.start()
    S2.start()
    check("[pg] start 可重複呼叫（表已存在不報錯）", True)
    try:
        store.PgStore(dsn=dsn, embed=toy_embed, dims=DIMS + 1).start()
        check("[pg] 向量維度跟 EMB_DIMS 不同時 start 要停", False)
    except RuntimeError as e:
        check("[pg] 向量維度跟 EMB_DIMS 不同時 start 要停", "維" in str(e), str(e))
    try:
        store.PgStore(dsn=dsn, embed=lambda ts: [[1.0, 0.0, 0.0] for _ in ts], dims=DIMS).add("ct-x", "a", "x", {})
        check("[pg] embedding 回錯維度時 add 要停", False)
    except RuntimeError as e:
        check("[pg] embedding 回錯維度時 add 要停", "EMB_DIMS" in str(e), str(e))
    try:
        S2.update("not-a-uuid", "x", {})
        check("[pg] update 亂碼編號要報錯", False)
    except KeyError:
        check("[pg] update 亂碼編號要報錯", True)
    dead = store.PgStore(dsn="postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=3", embed=toy_embed, dims=DIMS)
    check("[pg] 連不上時 ping 回 False", dead.ping() is False)
    try:
        dead.start(wait=0)
        check("[pg] 連不上時 start 等到期限就停", False)
    except RuntimeError as e:
        check("[pg] 連不上時 start 等到期限就停", "連不上" in str(e), str(e))


def make_store_cases(dsn):
    env = dict(os.environ)
    try:
        os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake"})
        check("make_store：RT_STORE=mem0 → Mem0Store", isinstance(store.make_store(), store.Mem0Store))
        os.environ.pop("MEM0_API_KEY")
        try:
            store.make_store()
            check("make_store：mem0 沒金鑰要停", False)
        except KeyError:
            check("make_store：mem0 沒金鑰要停", True)
        os.environ.update({"RT_STORE": "pg", "EMB_DIMS": str(DIMS)})
        os.environ.pop("DATABASE_URL", None)
        try:
            store.make_store(embed=toy_embed)
            check("make_store：pg 沒 DATABASE_URL 要停", False)
        except RuntimeError:
            check("make_store：pg 沒 DATABASE_URL 要停", True)
        os.environ["DATABASE_URL"] = dsn or "postgresql://x:y@localhost/z"
        s = store.make_store(embed=toy_embed, actor=lambda: "me")
        check("make_store：RT_STORE=pg → PgStore，讀 DATABASE_URL 與 EMB_DIMS",
              isinstance(s, store.PgStore) and s.dims == DIMS and s.dsn == os.environ["DATABASE_URL"] and s.name == "pg")
        os.environ["RT_STORE"] = "foo"
        try:
            store.make_store()
            check("make_store：不認得的 RT_STORE 要停", False)
        except RuntimeError:
            check("make_store：不認得的 RT_STORE 要停", True)
        os.environ.pop("RT_STORE")
        check("make_store：預設是 pg", isinstance(store.make_store(embed=toy_embed), store.PgStore))
    finally:
        os.environ.clear()
        os.environ.update(env)


def main():
    db = FakeMem0()
    contract(store.Mem0Store(url="http://fake", key="fake", http=lambda: FakeClient(db)), "mem0")
    dsn, cleanup = start_pg()
    make_store_cases(dsn)
    if not dsn:
        print(f"注意：pg 的合約測試略過（{cleanup}）")
    else:
        try:
            P = store.PgStore(dsn=dsn, embed=toy_embed, actor=lambda: ACTOR[0], dims=DIMS)
            P.start()
            _, ids = contract(P, "pg", pg=True)
            pg_extra(dsn)
            import psycopg
            with psycopg.connect(dsn) as c:  # 用現成資料庫時不留垃圾：清掉這次造的列和它們的異動紀錄
                c.execute("DELETE FROM rt_history WHERE memory_id = ANY(%s::uuid[])", (ids,))
                c.execute("DELETE FROM rt_memories WHERE id = ANY(%s::uuid[])", (ids,))
        finally:
            cleanup()
    print("\n" + ("全部通過" if not FAILS else f"{len(FAILS)} 項失敗：" + "、".join(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
