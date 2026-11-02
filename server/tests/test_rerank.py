"""離線測試：memory_search 的排序（rerank）。假的 Mem0 client＋假的 /v1/rerank，不連任何真實服務。
用法：python test_rerank.py <server.py 目錄>
涵蓋：正常重排、逾時退回、回錯退回、開關關掉時完全不呼叫。千題考試的命中率見 bench/rerank-stress-20261021。"""
import contextlib
import importlib.util
import io
import os
import sys
import types

NEW_DIR = sys.argv[1]


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


for n in ["httpx", "starlette", "starlette.concurrency", "starlette.requests", "starlette.responses",
          "mcp", "mcp.server", "mcp.server.transport_security"]:
    _stubmod(n)
_stubmod("mcp.server.fastmcp", FastMCP=FastMCP, Image=_Any)
_stubmod("experience_store", register=lambda *a, **k: None)
for k in [k for k in os.environ if k.startswith("MCP_TOKEN_") or k.startswith("MEM0_RERANK_")]:
    del os.environ[k]
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE_LAPTOP": "t1"})


def mem(i, text, type="note", project="mem0", agent="claude-laptop"):
    return {"id": i, "memory": text, "agent_id": agent, "metadata": {"type": type, "project": project},
            "created_at": "2026-10-18T00:00:00+00:00", "updated_at": "2026-10-18T00:00:00+00:00"}


STORE = {}       # id → 記憶（整區）
VEC = {}         # 問句 → [(id, 相似度)]，照 Mem0 的順序


class Resp:
    def __init__(self, d, status=200): self.d, self.status = d, status
    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")
    def json(self): return self.d


class Client:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, path, params=None):
        return Resp({"results": list(STORE.values())})
    def post(self, path, json=None):
        return Resp({"results": [{**STORE[i], "score": s} for i, s in VEC.get(json["query"], [])][: json.get("top_k") or 20]})


class FakeRerank:
    """假的 llama-server /v1/rerank：mode 決定這次怎麼回。"""
    def __init__(self):
        self.calls, self.mode, self.scores = [], "ok", {}

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.mode == "timeout":
            raise TimeoutError("read timed out")
        if self.mode == "conn":
            raise ConnectionError("connection refused")
        if self.mode == "http500":
            return Resp({"error": "boom"}, 500)
        if self.mode == "noresults":
            return Resp({"data": []})
        if self.mode == "badindex":
            return Resp({"results": [{"index": 99, "relevance_score": 1.0}]})
        if self.mode == "badscore":
            return Resp({"results": [{"index": 0, "relevance_score": "高"}]})
        docs = json["documents"]  # ok：照 self.scores（文件內文 → 分數）給分，跟 llama-server 一樣按分數高到低回
        res = [{"index": k, "relevance_score": self.scores.get(d, 0.0)} for k, d in enumerate(docs)]
        res.sort(key=lambda x: -x["relevance_score"])
        return Resp({"model": json["model"], "object": "list", "results": res[: json["top_n"]]})


def load(d):
    sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location("srv_rerank", os.path.join(d, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STORE.http = lambda: Client()
    return mod


NEW = load(NEW_DIR)
FAKE = FakeRerank()
NEW.httpx = FAKE
fails = []


def check(name, cond):
    print(("通過 " if cond else "失敗 ") + name)
    if not cond:
        fails.append(name)


def reset(enabled=True, mode="ok"):
    STORE.clear(); VEC.clear(); NEW._kw_snap.clear()
    FAKE.calls.clear(); FAKE.mode = mode; FAKE.scores = {}
    NEW.RERANK_ENABLED = enabled
    NEW.RERANK_TOPN = 20


def ids(res):
    return [r["id"] for r in res]


def search(*a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        r = NEW.memory_search(*a, **k)
    return r, buf.getvalue()


def setup_30():
    """30 則筆記，向量順序 n00 > n01 > … > n29；排序服務認為 n05 最相關、n12 第二。"""
    for n in range(30):
        STORE["n%02d" % n] = mem("n%02d" % n, "筆記 %d" % n)
    VEC["筆記"] = [("n%02d" % n, 0.9 - n * 0.01) for n in range(30)]
    FAKE.scores = {"筆記 5": 0.9, "筆記 12": 0.8}


BASE = ["n%02d" % n for n in range(10)]  # 沒重排時的前 10 名

# 0. 預設關：沒設 MEM0_RERANK_ENABLED 就是關的，網址、筆數、逾時是規格的預設值
check("預設關", NEW.RERANK_ENABLED is False)
check("預設網址", NEW.RERANK_URL == "http://rerank:8300")
check("預設前 20 則", NEW.RERANK_TOPN == 20)
check("預設逾時 4 秒", NEW.RERANK_TIMEOUT == 4.0)

# 1. 正常重排：前 20 名照分數重排，沒進前 20 的接在後面照原順序；回傳欄位不變
reset()
setup_30()
r, log = search("筆記", limit=20)
check("正常：只呼叫一次排序服務", len(FAKE.calls) == 1)
c = FAKE.calls[0]
check("正常：打 {RERANK_URL}/v1/rerank", c["url"] == "http://rerank:8300/v1/rerank")
check("正常：body 有 model／query／documents／top_n", set(c["json"]) == {"model", "query", "documents", "top_n"})
check("正常：只送前 20 則、top_n=20", len(c["json"]["documents"]) == 20 and c["json"]["top_n"] == 20)
check("正常：送的是混合排序前 20 名的全文", c["json"]["documents"] == ["筆記 %d" % n for n in range(20)])
check("正常：帶逾時 4 秒", c["timeout"] == 4.0)
want = ["n05", "n12"] + ["n%02d" % n for n in range(20) if n not in (5, 12)]
check("正常：前 20 名照排序分數重排（同分維持原順序）", ids(r) == want)
check("正常：預設 10 則照樣截斷", ids(search("筆記")[0]) == want[:10])
check("正常：欄位跟沒排序時一樣", set(r[0]) == {"id", "type", "project", "agent", "date", "text", "chars", "score"})
check("正常：不印退回的 log", "退回" not in log)
NEW.RERANK_TOPN = 5
r, _ = search("筆記", limit=20)
check("前 N 之外的維持原順序接在後面（N=5）", ids(r) == ["n00", "n01", "n02", "n03", "n04"] + ["n%02d" % n for n in range(5, 20)])
check("N=5 時只送 5 則", len(FAKE.calls[-1]["json"]["documents"]) == 5)
NEW.RERANK_TOPN = 20
FAKE.scores = {"筆記 5": 0.9, "筆記 25": 5.0}
check("第 21 名以後就算分數高也不會被拉上來（根本沒送）", "n25" not in ids(search("筆記")[0]))

# 每則截在 RERANK_DOC_CHARS 字以內
reset()
STORE["long"] = mem("long", "長" * 5000)
STORE["short"] = mem("short", "短")
VEC["長短"] = [("long", 0.6), ("short", 0.5)]
search("長短")
check("每則最多送 1500 字", [len(d) for d in FAKE.calls[0]["json"]["documents"]] == [1500, 1])

# 只有一個候選就不呼叫
reset()
STORE["a"] = mem("a", "只有一則")
VEC["只有"] = [("a", 0.6)]
r, _ = search("只有")
check("只有一個候選時不呼叫排序服務", FAKE.calls == [] and ids(r) == ["a"])

# 2. 逾時、連不上：退回原排序、記一行 log、搜尋不報錯
for mode, label in (("timeout", "逾時"), ("conn", "連不上")):
    reset(mode=mode)
    setup_30()
    try:
        r, log = search("筆記")
        check(f"{label}：退回原本的排序", ids(r) == BASE)
        check(f"{label}：記一行 log", log.count("[搜尋排序] 退回混合排序") == 1 and len(log.strip().splitlines()) == 1)
    except Exception as e:
        check(f"{label}：搜尋不能失敗（丟出 {e!r}）", False)

# 3. 回錯：HTTP 500、少了 results、index 超出範圍、分數不是數字，都退回原排序
for mode in ("http500", "noresults", "badindex", "badscore"):
    reset(mode=mode)
    setup_30()
    try:
        r, log = search("筆記")
        check(f"回錯（{mode}）：退回原本的排序並記 log", ids(r) == BASE and "[搜尋排序] 退回混合排序" in log)
    except Exception as e:
        check(f"回錯（{mode}）：搜尋不能失敗（丟出 {e!r}）", False)

# 4. 開關關掉：完全不呼叫，結果跟沒這功能時一樣
reset(enabled=False)
setup_30()
r, log = search("筆記")
check("開關關掉：完全不呼叫排序服務", FAKE.calls == [])
check("開關關掉：維持混合排序", ids(r) == BASE)
check("開關關掉：分數由高到低", [x["score"] for x in r] == sorted((x["score"] for x in r), reverse=True))
check("開關關掉：沒有 log", log == "")

print("\n全部通過" if not fails else "\n有 %d 項失敗：%s" % (len(fails), "、".join(fails)))
sys.exit(1 if fails else 0)
