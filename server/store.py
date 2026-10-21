"""儲存層：圓桌記憶的所有讀寫都經過這裡，Mem0 從「必備」變成「可選的後端之一」。

介面（Store）8 個方法：list、count、add、get、update、delete、search、history。
每一列統一長這樣：{id, memory, metadata, agent_id, user_id, created_at, updated_at[, score]}。
- RT_STORE=pg（2.0.0 預設）：PgStore，psycopg 3 直連 Postgres＋pgvector，向量自己算，資料放 rt_memories／rt_history。
- RT_STORE=mem0：Mem0Store，一比一包 1.0.0 原本的 httpx 呼叫（含 _items、/entities 核對、cap 分批撈），行為零改變。
"""
import datetime as dt
import json
import os
import sys
import time
import uuid

import httpx

LIST_CAP = 10000  # Mem0 列表一次最多回這麼多筆（mem0-api 的 ALL_MEMORIES_LIMIT，兩邊要一致）；
                  # 不帶 top_k 只會拿到預設筆數，資料多了會默默漏看；滿了 list() 會改依作者分批撈


class Store:
    """後端共同介面。子類別只要實作這 8 個方法，server.py 不用知道資料放哪。"""
    name = "base"

    def start(self) -> None:
        """服務啟動時呼叫一次：連線、建表（Mem0 後端不用做事）。"""

    def ping(self) -> bool:
        """/health 用：後端現在連得上嗎。"""
        return True

    def list(self, uid: str, agent_id=None) -> list:
        """某一區（user_id）的全部記憶，一筆不漏；撈不完整要直接報錯，不要默默少給。agent_id 有給就只列那個作者的。"""
        raise NotImplementedError

    def count(self, uid: str):
        """某一區的總筆數；後端算不出來就回 None。"""
        raise NotImplementedError

    def add(self, uid: str, agent_id, text: str, metadata: dict) -> dict:
        """新增一筆，回統一格式的那一列（至少有 id、memory、metadata、agent_id、user_id）。"""
        raise NotImplementedError

    def get(self, memory_id: str):
        """讀一筆；不存在回 None。"""
        raise NotImplementedError

    def update(self, memory_id: str, text: str, metadata: dict) -> None:
        """整筆覆寫內文與 metadata（server.py 每次都送完整 metadata，所以「覆寫」和 Mem0 的「合併」結果一樣）。
        不存在要報錯（跟 Mem0 回 404 一樣）。"""
        raise NotImplementedError

    def delete(self, memory_id: str) -> None:
        """刪一筆；刪不存在的不算錯。"""
        raise NotImplementedError

    def search(self, uid: str, query: str, filters: dict, top_k: int, threshold=None) -> list:
        """向量搜尋：filters 裡的 agent_id 比對作者欄位，其他鍵比對 metadata（相等）；每列多一個 score（越大越像）。
        先取最像的 top_k 筆，再把 score 低於 threshold 的去掉（跟 Mem0 一樣，所以可能不到 top_k 筆）。"""
        raise NotImplementedError

    def history(self, memory_id: str) -> list:
        """一筆的異動紀錄（舊的在前），每列至少有 event（ADD／UPDATE／DELETE）、old_memory、new_memory、at、actor。"""
        raise NotImplementedError


def _items(data) -> list:
    if isinstance(data, dict):
        return data.get("results", [])
    return data or []


class Mem0Store(Store):
    """包 Mem0 REST 的 8 個端點。`http` 是 client 工廠，測試可以換成假的：STORE.http = lambda: FakeClient()。"""
    name = "mem0"

    def __init__(self, url=None, key=None, http=None, cap: int = LIST_CAP):
        self.url = (url or os.environ.get("MEM0_URL", "http://mem0:8000")).rstrip("/")
        self.key = key if key is not None else os.environ.get("MEM0_API_KEY", "")
        self.cap = cap  # 測試會改小（原本改 server.LIST_CAP，現在改 STORE.cap）
        self.http = http or self._client

    def _client(self) -> httpx.Client:
        if not self.key:
            raise RuntimeError("RT_STORE=mem0 需要 MEM0_API_KEY")
        return httpx.Client(base_url=self.url, headers={"X-API-Key": self.key},
                            timeout=httpx.Timeout(20.0, connect=5.0))

    def ping(self) -> bool:
        try:
            with self.http() as c:
                c.get("/entities").raise_for_status()
            return True
        except Exception:
            return False

    @staticmethod
    def _total(ents, uid: str):
        return next((e.get("total_memories") for e in ents if e.get("type") == "user" and e.get("id") == uid), None)

    def list(self, uid: str, agent_id=None) -> list:
        """Mem0 一次最多回 cap 筆、沒有分頁：沒滿就是全部；滿了改依作者（agent_id）分批撈，
        再用 /entities 的該區總數核對一筆不漏，對不上就報錯（寧可停也不默默漏看）。"""
        with self.http() as c:
            params = {"user_id": uid, "top_k": self.cap}
            if agent_id is not None:
                params["agent_id"] = agent_id
            r = c.get("/memories", params=params)
            r.raise_for_status()
            items = _items(r.json())
            if len(items) < self.cap:
                return items
            if agent_id is not None:
                raise RuntimeError(f"{uid} 區裡 {agent_id} 一個作者就有 {self.cap} 筆以上，列表會不完整；請使用者先整理舊記憶")
            r = c.get("/entities")
            r.raise_for_status()
            ents = r.json()
            total = self._total(ents, uid)
            seen = {}
            for agent in sorted({e.get("id") for e in ents if e.get("type") == "agent" and e.get("id")}):
                r = c.get("/memories", params={"user_id": uid, "agent_id": agent, "top_k": self.cap})
                r.raise_for_status()
                part = _items(r.json())
                if len(part) >= self.cap:
                    raise RuntimeError(f"{uid} 區裡 {agent} 一個作者就有 {self.cap} 筆以上，列表會不完整，"
                                       "claim 與 status 檢查不可靠；請使用者先整理舊記憶")
                seen.update((m.get("id"), m) for m in part)
        if total is None or len(seen) != total:
            raise RuntimeError(f"{uid} 區超過 {self.cap} 筆，分批撈到 {len(seen)} 筆但總數是 {total}，列表可能不完整，"
                               "claim 與 status 檢查不可靠；請使用者先整理舊記憶")
        return list(seen.values())

    def count(self, uid: str):
        with self.http() as c:
            r = c.get("/entities")
            r.raise_for_status()
            return self._total(r.json(), uid)

    def add(self, uid: str, agent_id, text: str, metadata: dict) -> dict:
        body = {
            "messages": [{"role": "user", "content": text}],
            "user_id": uid,
            "infer": False,
            "metadata": metadata,
        }
        if agent_id is not None:  # 1.0.0 的 _add 連空字串都會送；留言封存只在有作者時才帶，呼叫端傳 None 就不送
            body["agent_id"] = agent_id
        with self.http() as c:
            r = c.post("/memories", json=body)
            r.raise_for_status()
            res = r.json()
        rows = _items(res)
        first = rows[0] if rows else {}
        return {
            "id": first.get("id"),
            "memory": first.get("memory", text),
            "metadata": metadata,
            "agent_id": agent_id,
            "user_id": uid,
            "raw": res,  # Mem0 原始回應（{"results":[{"id","memory","event":"ADD",...}]}），message_send 回給呼叫端用
        }

    def get(self, memory_id: str):
        with self.http() as c:
            r = c.get(f"/memories/{memory_id}")
            r.raise_for_status()
            return r.json() or None  # Mem0 找不到時回 200＋null（跟 1.0.0 的讀回檢查一樣，不另看 404）

    def update(self, memory_id: str, text: str, metadata: dict) -> None:
        with self.http() as c:
            c.put(f"/memories/{memory_id}", json={"text": text, "metadata": metadata}).raise_for_status()

    def delete(self, memory_id: str) -> None:
        with self.http() as c:
            c.delete(f"/memories/{memory_id}").raise_for_status()

    def search(self, uid: str, query: str, filters: dict, top_k: int, threshold=None) -> list:
        body = {"query": query, "filters": {"user_id": uid, **(filters or {})}, "top_k": top_k}
        if threshold is not None:
            body["threshold"] = threshold
        with self.http() as c:
            r = c.post("/search", json=body)
            r.raise_for_status()
            return _items(r.json())

    def history(self, memory_id: str) -> list:
        with self.http() as c:
            r = c.get(f"/memories/{memory_id}/history")
            r.raise_for_status()
            rows = _items(r.json())
        out = []
        for h in rows:
            h = dict(h)
            h.setdefault("at", h.get("created_at"))   # Mem0 的欄位名照留，另外補統一名稱
            h.setdefault("actor", h.get("actor_id"))
            out.append(h)
        return out


# ---- Postgres＋pgvector ----

_COLS = "id, user_id, agent_id, memory, metadata, created_at, updated_at"
_SCHEMA = (
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE TABLE IF NOT EXISTS rt_memories ("
    " id uuid PRIMARY KEY,"
    " user_id text NOT NULL,"
    " agent_id text,"
    " memory text NOT NULL,"
    " metadata jsonb NOT NULL DEFAULT '{}'::jsonb,"
    " embedding vector({dims}),"
    " created_at timestamptz NOT NULL DEFAULT now(),"
    " updated_at timestamptz NOT NULL DEFAULT now())",
    "CREATE INDEX IF NOT EXISTS rt_memories_user_id ON rt_memories (user_id)",
    "CREATE INDEX IF NOT EXISTS rt_memories_metadata ON rt_memories USING gin (metadata jsonb_path_ops)",
    # 向量索引先不建：資料少時全掃最準也夠快；筆數破十萬再建 HNSW（見 docs/operations.md）
    "CREATE TABLE IF NOT EXISTS rt_history ("
    " id bigserial PRIMARY KEY,"
    " memory_id uuid NOT NULL,"
    " event text NOT NULL,"
    " old_memory text,"
    " new_memory text,"
    " old_metadata jsonb,"
    " new_metadata jsonb,"
    " actor text,"
    " at timestamptz NOT NULL DEFAULT now())",
    "CREATE INDEX IF NOT EXISTS rt_history_memory_id ON rt_history (memory_id)",
)


def _iso(d):
    """timestamptz → ISO 字串（統一轉 UTC，帶 +00:00；server.py 顯示時再換成當地時區）。"""
    return d.astimezone(dt.timezone.utc).isoformat() if d else None


def _vlit(vec) -> str:
    """向量 → pgvector 的文字寫法 '[0.1,0.2,...]'（不用另裝 pgvector 套件）。"""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _jd(md) -> str:
    return json.dumps(md or {}, ensure_ascii=False)


def _is_uuid(s) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _row(r) -> dict:
    return {"id": str(r[0]), "user_id": r[1], "agent_id": r[2], "memory": r[3], "metadata": r[4] or {},
            "created_at": _iso(r[5]), "updated_at": _iso(r[6])}


class PgStore(Store):
    """直連 Postgres＋pgvector（2.0.0 預設）。向量用 embed(texts)->vectors 自己算；每次呼叫開一條連線、做完就關
    （圓桌是單一程序、量不大，不用連線池；資料庫重啟也不會留壞掉的連線）。psycopg 用到才 import，RT_STORE=mem0 的人不用裝。"""
    name = "pg"

    def __init__(self, dsn=None, embed=None, actor=None, dims=None):
        self.dsn = dsn or os.environ.get("DATABASE_URL", "")
        if not self.dsn:
            raise RuntimeError("RT_STORE=pg 需要 DATABASE_URL（例如 postgresql://roundtable:密碼@postgres:5432/roundtable）")
        if embed is None:
            raise RuntimeError("PgStore 需要 embed(texts) 函式來算向量")
        self.embed = embed
        self.actor = actor or (lambda: None)
        self.dims = int(dims or os.environ.get("EMB_DIMS", "1024"))

    def _connect(self):
        import psycopg  # 用到才載入
        return psycopg.connect(self.dsn)  # with 區塊結束自動 commit（有例外就 rollback）再關閉

    def _who(self):
        """異動紀錄裡的操作者；server.py 的 current_agent 沒設時是空字串，存成 NULL。"""
        return self.actor() or None

    def _vec(self, text: str) -> str:
        v = self.embed([text])[0]
        if len(v) != self.dims:
            raise RuntimeError(f"embedding 回 {len(v)} 維，EMB_DIMS 是 {self.dims}；EMB_MODEL 跟 EMB_DIMS 要成對改")
        return _vlit(v)

    def start(self, wait: float = 60) -> None:
        """連得上就建擴充、建表；連不上每 5 秒重試、wait 秒（預設 60）後放棄。
        表已存在但向量維度跟 EMB_DIMS 不同就停（換模型要重建表）。"""
        deadline = time.monotonic() + wait
        while True:
            try:
                with self._connect() as c:
                    for stmt in _SCHEMA:
                        c.execute(stmt.replace("{dims}", str(self.dims)))  # 不用 str.format：SQL 裡的 '{}' 會被當成佔位
                    have = c.execute("SELECT atttypmod FROM pg_attribute WHERE attrelid = 'rt_memories'::regclass AND attname = 'embedding'").fetchone()
                break
            except Exception as e:
                import psycopg
                if not isinstance(e, psycopg.OperationalError):  # 不是連線問題（例如沒裝 pgvector、SQL 錯）就不用等，直接報
                    raise RuntimeError(f"Postgres 建表失敗：{e}") from e
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"連不上 Postgres（DATABASE_URL）：{e}") from e
                print(f"[store] 等 Postgres（{e}）", file=sys.stderr)
                time.sleep(min(5, max(0.1, deadline - time.monotonic())))
        if have and have[0] > 0 and have[0] != self.dims:
            raise RuntimeError(f"rt_memories 的向量是 {have[0]} 維，EMB_DIMS 設 {self.dims}；換 embedding 模型要重建表（見 docs/operations.md）")

    def ping(self) -> bool:
        try:
            with self._connect() as c:
                c.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    def list(self, uid: str, agent_id=None) -> list:
        sql, args = f"SELECT {_COLS} FROM rt_memories WHERE user_id = %s", [uid]
        if agent_id is not None:
            sql += " AND agent_id = %s"
            args.append(agent_id)
        with self._connect() as c:
            return [_row(r) for r in c.execute(sql + " ORDER BY created_at, id", args).fetchall()]

    def count(self, uid: str):
        with self._connect() as c:
            return c.execute("SELECT count(*) FROM rt_memories WHERE user_id = %s", (uid,)).fetchone()[0]

    def add(self, uid: str, agent_id, text: str, metadata: dict) -> dict:
        mid, md, vec = str(uuid.uuid4()), dict(metadata or {}), self._vec(text)
        with self._connect() as c:
            r = c.execute(f"INSERT INTO rt_memories (id, user_id, agent_id, memory, metadata, embedding)"
                          f" VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector) RETURNING {_COLS}",
                          (mid, uid, agent_id, text, _jd(md), vec)).fetchone()
            c.execute("INSERT INTO rt_history (memory_id, event, new_memory, new_metadata, actor) VALUES (%s, 'ADD', %s, %s::jsonb, %s)",
                      (mid, text, _jd(md), self._who()))
        return _row(r)

    def get(self, memory_id: str):
        if not _is_uuid(memory_id):
            return None
        with self._connect() as c:
            r = c.execute(f"SELECT {_COLS} FROM rt_memories WHERE id = %s", (memory_id,)).fetchone()
        return _row(r) if r else None

    def update(self, memory_id: str, text: str, metadata: dict) -> None:
        if not _is_uuid(memory_id):
            raise KeyError(f"記憶編號格式不對：{memory_id}")
        md = dict(metadata or {})
        with self._connect() as c:
            old = c.execute("SELECT memory, metadata FROM rt_memories WHERE id = %s FOR UPDATE", (memory_id,)).fetchone()
            if not old:
                raise KeyError(f"記憶 {memory_id} 不存在，不能更新")
            if text != old[0]:  # 內文有改才重算向量；多數更新只改 metadata（已讀、狀態），不用打 embedding
                c.execute("UPDATE rt_memories SET memory = %s, metadata = %s::jsonb, embedding = %s::vector, updated_at = now() WHERE id = %s",
                          (text, _jd(md), self._vec(text), memory_id))
            else:
                c.execute("UPDATE rt_memories SET metadata = %s::jsonb, updated_at = now() WHERE id = %s", (_jd(md), memory_id))
            c.execute("INSERT INTO rt_history (memory_id, event, old_memory, new_memory, old_metadata, new_metadata, actor)"
                      " VALUES (%s, 'UPDATE', %s, %s, %s::jsonb, %s::jsonb, %s)",
                      (memory_id, old[0], text, _jd(old[1]), _jd(md), self._who()))

    def delete(self, memory_id: str) -> None:
        if not _is_uuid(memory_id):
            return
        with self._connect() as c:
            old = c.execute("SELECT memory, metadata FROM rt_memories WHERE id = %s FOR UPDATE", (memory_id,)).fetchone()
            if not old:
                return  # 刪不存在的不算錯
            c.execute("INSERT INTO rt_history (memory_id, event, old_memory, old_metadata, actor) VALUES (%s, 'DELETE', %s, %s::jsonb, %s)",
                      (memory_id, old[0], _jd(old[1]), self._who()))
            c.execute("DELETE FROM rt_memories WHERE id = %s", (memory_id,))

    def search(self, uid: str, query: str, filters: dict, top_k: int, threshold=None) -> list:
        vec, f = self._vec(query), dict(filters or {})
        agent = f.pop("agent_id", None)
        sql = f"SELECT {_COLS}, 1 - (embedding <=> %s::vector) AS score FROM rt_memories WHERE user_id = %s AND embedding IS NOT NULL"
        args = [vec, uid]
        if agent is not None:
            sql += " AND agent_id = %s"
            args.append(agent)
        if f:
            sql += " AND metadata @> %s::jsonb"
            args.append(_jd(f))
        sql += " ORDER BY embedding <=> %s::vector, id LIMIT %s"
        args += [vec, int(top_k)]
        with self._connect() as c:
            rows = c.execute(sql, args).fetchall()
        out = []
        for r in rows:
            score = max(0.0, float(r[7]))  # cosine 距離 0～2 → 分數 1～-1，負的視為 0（跟 Mem0 的 pgvector 後端一樣）
            if threshold is not None and score < threshold:
                continue
            out.append({**_row(r), "score": score})
        return out

    def history(self, memory_id: str) -> list:
        if not _is_uuid(memory_id):
            return []
        with self._connect() as c:
            rows = c.execute("SELECT id, memory_id, event, old_memory, new_memory, old_metadata, new_metadata, actor, at"
                             " FROM rt_history WHERE memory_id = %s ORDER BY id", (memory_id,)).fetchall()
        return [{"id": r[0], "memory_id": str(r[1]), "event": r[2], "old_memory": r[3], "new_memory": r[4],
                 "old_metadata": r[5], "new_metadata": r[6], "actor": r[7], "actor_id": r[7],
                 "at": _iso(r[8]), "created_at": _iso(r[8])} for r in rows]


def make_store(embed=None, actor=None) -> Store:
    """照 RT_STORE 挑後端。embed(texts)->vectors、actor()->agent 名稱 只有 PgStore 會用到。"""
    kind = os.environ.get("RT_STORE", "pg").strip().lower()
    if kind == "mem0":
        return Mem0Store(key=os.environ["MEM0_API_KEY"])  # 跟 1.0.0 一樣：沒給金鑰啟動就失敗
    if kind == "pg":
        return PgStore(embed=embed, actor=actor)
    raise RuntimeError(f"RT_STORE 只能是 pg 或 mem0，不是 {kind!r}")
