"""離線測試：memory_search 混合排序（關鍵字比對、類型前後、預設 10 則）。精簡版、封存另見 test_msg_archive.py。
用法：python test_memory_search.py <改版前 server.py 目錄> <改版後 server.py 目錄>（不連任何真實服務）
真實資料的 20 題評測另內部評測紀錄。"""
import importlib.util
import os
import sys
import types

OLD_DIR, NEW_DIR = sys.argv[1], sys.argv[2]


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
for k in [k for k in os.environ if k.startswith("MCP_TOKEN_")]:
    del os.environ[k]
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE_LAPTOP": "t1"})


def mem(i, text, type="note", project="mem0", agent="claude-laptop"):
    return {"id": i, "memory": text, "agent_id": agent, "metadata": {"type": type, "project": project},
            "created_at": "2026-10-18T00:00:00+00:00", "updated_at": "2026-10-18T00:00:00+00:00"}


STORE = {}       # id → 記憶（整區）
VEC = {}         # 問句 → [(id, 相似度)]，照 Mem0 的順序
CALLS = []


class Resp:
    def __init__(self, d): self.d = d
    def raise_for_status(self): pass
    def json(self): return self.d


class Client:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, path, params=None):
        CALLS.append(("list", path))
        if FAIL_LIST[0]:
            raise RuntimeError("roundtable 區超過上限")
        return Resp({"results": list(STORE.values())})
    def post(self, path, json=None):
        CALLS.append(("search", json))
        f = json["filters"]
        out = []
        for i, s in VEC.get(json["query"], []):
            m = STORE[i]
            md = m["metadata"]
            if f.get("project") and md.get("project") != f["project"]:
                continue
            if f.get("type") and md.get("type") != f["type"]:
                continue
            if f.get("agent_id") and m["agent_id"] != f["agent_id"]:
                continue
            out.append({**m, "score": s})
        return Resp({"results": out[: json.get("top_k") or 20]})


FAIL_LIST = [False]


def load(d, tag):
    sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location("srv_" + tag, os.path.join(d, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if hasattr(mod, "STORE"):  # 2.0.0 起走儲存層；舊版（1.0.0）還是 _http
        mod.STORE.http = lambda: Client()
    else:
        mod._http = lambda: Client()
    return mod


OLD, NEW = load(OLD_DIR, "old"), load(NEW_DIR, "new")
fails = []


def check(name, cond):
    print(("通過 " if cond else "失敗 ") + name)
    if not cond:
        fails.append(name)


def reset():
    STORE.clear(); VEC.clear(); CALLS.clear(); FAIL_LIST[0] = False
    NEW._kw_snap.clear()


def ids(res):
    return [r["id"] for r in res]


# 1. 預設筆數：改前 5 則，改後 10 則；上限 20、下限 1
reset()
for n in range(30):
    STORE["n%02d" % n] = mem("n%02d" % n, "筆記 %d" % n)
VEC["筆記"] = [("n%02d" % n, 0.9 - n * 0.01) for n in range(30)]
OLD_PRE = len(OLD.memory_search("筆記")) == 5  # 舊版還沒有這次的搜尋改動；舊版已是 1.0.0 的話「改前…」的檢查不適用


def check_old(name, cond):
    if OLD_PRE:
        check(name, cond())
    else:
        print("略過 " + name + "（舊版已含這次的改動）")


check_old("改前預設回 5 則", lambda: len(OLD.memory_search("筆記")) == 5)
check("改後預設回 10 則", len(NEW.memory_search("筆記")) == 10)
check("limit=50 最多回 20 則", len(NEW.memory_search("筆記", limit=50)) == 20)
check("limit=0 至少回 1 則", len(NEW.memory_search("筆記", limit=0)) == 1)
check("向 Mem0 要 60 筆向量結果", [c for c in CALLS if c[0] == "search"][-1][1].get("top_k") == 60)
check("純中文問句不列整區（沒有額外成本）", not any(c[0] == "list" for c in CALLS))
check_old("full=true 的輸出欄位跟改前一樣", lambda: set(NEW.memory_search("筆記", full=True)[0]) == set(OLD.memory_search("筆記")[0]))
check("預設回精簡版（沒有 version，有全文字數）", set(NEW.memory_search("筆記")[0]) == {"id", "type", "project", "agent", "date", "text", "chars", "score"})

# 2. 只打 IP：向量沒撈到、但內文有這個 IP 的記憶要補進來排第一；相近的 IP 不能算中
reset()
for n in range(60):
    STORE["x%02d" % n] = mem("x%02d" % n, "不相干的筆記 %d" % n)
STORE["apc"] = mem("apc", "APC UPS 網卡 10.0.0.226，管理介面 https", project="homelab-network")
STORE["near"] = mem("near", "另一台在 10.0.0.22 跟 10.0.0.2260")
VEC["10.0.0.226"] = [("x%02d" % n, 0.34 - n * 0.001) for n in range(60)]  # 向量前 60 名都不相干
r = NEW.memory_search("10.0.0.226")
check("只靠關鍵字補進來的 IP 記憶排第 1", ids(r)[:1] == ["apc"])
check("10.0.0.22／.2260 不算中 .226", "near" not in ids(r))
check_old("改前搜不到這則", lambda: "apc" not in ids(OLD.memory_search("10.0.0.226", limit=20)))
check("補進來的也守 project 篩選", "apc" not in ids(NEW.memory_search("10.0.0.226", project="mem0", limit=20)))
check("補進來的也守 type 篩選", "apc" not in ids(NEW.memory_search("10.0.0.226", type="decision", limit=20)))
check("補進來的也守 agent 篩選", "apc" not in ids(NEW.memory_search("10.0.0.226", agent="codex-laptop", limit=20)))
check("篩選對的時候照樣補進來", ids(NEW.memory_search("10.0.0.226", project="homelab-network"))[:1] == ["apc"])

# 3. 類型：一樣相關時，決策、狀態在前，留言在後；本來就不相關的決策不會被抬到含關鍵字的留言前面
reset()
STORE.update({"msg": mem("msg", "留言：備份規則", "message"), "note": mem("note", "筆記：備份規則", "note"),
              "dec": mem("dec", "決策：備份規則", "decision"), "st": mem("st", "狀態：備份規則", "status")})
for n in range(20):
    STORE["f%02d" % n] = mem("f%02d" % n, "無關 %d" % n)
VEC["備份規則是什麼"] = [("msg", 0.70), ("note", 0.70), ("dec", 0.70), ("st", 0.70)] + \
    [("f%02d" % n, 0.40 - n * 0.001) for n in range(20)]
r = ids(NEW.memory_search("備份規則是什麼"))
check("同分時：決策、狀態 > 筆記 > 留言", r[:4] in (["dec", "st", "note", "msg"], ["st", "dec", "note", "msg"]))
check_old("改前就照 Mem0 原順序", lambda: ids(OLD.memory_search("備份規則是什麼"))[:4] == ["msg", "note", "dec", "st"])
reset()
STORE["hit"] = mem("hit", "CT307 已加進每晚備份", "message", project="pve")
STORE["junkdec"] = mem("junkdec", "研究待辦清單", "decision")
for n in range(40):
    STORE["c%02d" % n] = mem("c%02d" % n, "其他筆記 %d" % n)
VEC["CT307"] = [("hit", 0.31), ("junkdec", 0.30)] + [("c%02d" % n, 0.29 - n * 0.001) for n in range(40)]
r = ids(NEW.memory_search("CT307"))
check("短詞題：含 CT307 的留言仍在不相干的決策前面", r.index("hit") < r.index("junkdec"))

# 4. 常見詞只加一點：大家都有的詞不能把不相關的抬過真正相關的
reset()
for n in range(50):
    STORE["p%02d" % n] = mem("p%02d" % n, "PVE 主機筆記 %d" % n, project="pve")
STORE["good"] = mem("good", "三台實體主機的位址整理", project="pve")
VEC["PVE 有哪幾台實體主機"] = [("good", 0.70)] + [("p%02d" % n, 0.60 - n * 0.001) for n in range(50)]
check("常見詞（PVE）不會把一般筆記抬過最相關那則", ids(NEW.memory_search("PVE 有哪幾台實體主機"))[:1] == ["good"])

# 5. 列整區失敗（例如超過 10000）時，搜尋照樣回向量結果、不報錯
reset()
STORE["a"] = mem("a", "VM119 的設定")
VEC["VM119 設定"] = [("a", 0.6)]
FAIL_LIST[0] = True
try:
    r = NEW.memory_search("VM119 設定")
    check("列整區失敗時只用向量結果", ids(r) == ["a"])
except Exception as e:
    check("列整區失敗時只用向量結果（丟出 %r）" % e, False)

# 6. 整區快照 30 秒內共用，第二次搜尋不再列整區
reset()
STORE["a"] = mem("a", "VM119 的設定")
VEC["VM119 設定"] = [("a", 0.6)]
NEW.memory_search("VM119 設定")
NEW.memory_search("VM119 設定")
check("30 秒內兩次搜尋只列一次整區", sum(1 for c in CALLS if c[0] == "list") == 1)

# 7. 分數是數字、由高到低
reset()
STORE.update({"a": mem("a", "Kuma DNS1 Discord 通知成功", "message"), "b": mem("b", "Kuma 規劃", "decision")})
VEC["Kuma DNS1 Discord"] = [("b", 0.5), ("a", 0.49)]
r = NEW.memory_search("Kuma DNS1 Discord")
check("分數是數字", all(isinstance(x["score"], float) for x in r))
check("分數由高到低", [x["score"] for x in r] == sorted((x["score"] for x in r), reverse=True))

print("\n全部通過" if not fails else "\n有 %d 項失敗：%s" % (len(fails), "、".join(fails)))
sys.exit(1 if fails else 0)
