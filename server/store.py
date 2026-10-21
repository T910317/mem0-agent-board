"""儲存層：圓桌記憶的所有讀寫都經過這裡，Mem0 從「必備」變成「可選的後端之一」。

介面（Store）8 個方法：list、count、add、get、update、delete、search、history。
每一列統一長這樣：{id, memory, metadata, agent_id, user_id, created_at, updated_at[, score]}。
- RT_STORE=mem0：Mem0Store，一比一包 1.0.0 原本的 httpx 呼叫（含 _items、/entities 核對、cap 分批撈），行為零改變。
- RT_STORE=pg（2.0.0 預設）：PgStore，直連 Postgres＋pgvector。
"""
import os

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

    def list(self, uid: str) -> list:
        """某一區（user_id）的全部記憶，一筆不漏；撈不完整要直接報錯，不要默默少給。"""
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
        """整筆覆寫內文與 metadata（server.py 每次都送完整 metadata，所以「覆寫」和 Mem0 的「合併」結果一樣）。"""
        raise NotImplementedError

    def delete(self, memory_id: str) -> None:
        raise NotImplementedError

    def search(self, uid: str, query: str, filters: dict, top_k: int, threshold=None) -> list:
        """向量搜尋：filters 裡的 agent_id 比對作者欄位，其他鍵比對 metadata；每列多一個 score（越大越像）。"""
        raise NotImplementedError

    def history(self, memory_id: str) -> list:
        """一筆的異動紀錄，每列至少有 event（ADD／UPDATE／DELETE）、old_memory、new_memory、at、actor。"""
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

    def list(self, uid: str) -> list:
        """Mem0 一次最多回 cap 筆、沒有分頁：沒滿就是全部；滿了改依作者（agent_id）分批撈，
        再用 /entities 的該區總數核對一筆不漏，對不上就報錯（寧可停也不默默漏看）。"""
        with self.http() as c:
            r = c.get("/memories", params={"user_id": uid, "top_k": self.cap})
            r.raise_for_status()
            items = _items(r.json())
            if len(items) < self.cap:
                return items
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


def make_store(embed=None, actor=None) -> Store:
    """照 RT_STORE 挑後端。embed(texts)->vectors、actor()->agent 名稱 只有 PgStore 會用到。"""
    kind = os.environ.get("RT_STORE", "pg").strip().lower()
    if kind == "mem0":
        return Mem0Store(key=os.environ["MEM0_API_KEY"])  # 跟 1.0.0 一樣：沒給金鑰啟動就失敗
    if kind == "pg":
        raise RuntimeError("RT_STORE=pg 的 PgStore 還沒加進來（2.0.0 第二步）；先設 RT_STORE=mem0")
    raise RuntimeError(f"RT_STORE 只能是 pg 或 mem0，不是 {kind!r}")
