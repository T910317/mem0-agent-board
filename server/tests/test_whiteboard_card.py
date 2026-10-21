"""離線測試：白板專案卡（名稱／主管／做事／現在／小目標、第幾天、更新幾次）。
沒照新格式寫的 status，舊欄位要跟改版前一模一樣；照新格式寫的，新欄位要解析對、舊頁面用的 summary 不再是「名稱：…」。
用法：python test_whiteboard_card.py <改版前 server.py 目錄> <改版後 server.py 目錄>（不連任何真實服務）"""
import copy
import datetime as dt
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
    return m


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
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE_LAPTOP": "t1",
                   "MCP_TOKEN_CODEX_LAPTOP": "t2"})

NOW = dt.datetime.now(dt.timezone.utc)
TPE = dt.timezone(dt.timedelta(hours=8))
HIST = {}      # 記憶 id → 歷史事件
CALLS = []     # 查過哪些歷史


def _ts(days_ago=0.0):
    return (NOW - dt.timedelta(days=days_ago)).isoformat()


class FakeResp:
    def __init__(self, data=None, code=200): self._d, self.code = data, code
    def raise_for_status(self):
        if self.code != 200:
            raise RuntimeError("http %d" % self.code)
    def json(self): return self._d


class FakeClient:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get(self, path, **k):
        mid = path.split("/")[2]
        CALLS.append(mid)
        if mid not in HIST:
            return FakeResp(None, 500)
        return FakeResp(HIST[mid])


def load(d, name):
    sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(name, os.path.join(d, "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.path.pop(0)
    if hasattr(mod, "STORE"):  # 2.0.0 起走儲存層；舊版（1.0.0）還是 _http
        mod.STORE.http = lambda: FakeClient()
    else:
        mod._http = lambda: FakeClient()
    return mod


old = load(OLD_DIR, "server_old")
new = load(NEW_DIR, "server_new")


def status(mid, project, text, created_days, updated_days, stage="implementing", updates=0):
    HIST[mid] = [{"event": "ADD"}] + [{"event": "UPDATE"}] * updates
    return {"id": mid, "memory": "[狀態] " + text, "agent_id": "codex-laptop",
            "metadata": {"type": "status", "project": project, "stage": stage, "updated_by": "codex-laptop"},
            "created_at": _ts(created_days), "updated_at": _ts(updated_days)}


PLAIN = "2026-10-17｜首小考已結案：Mini/Max34配置/33key\n第二行技術細節"
CARD = ("名稱：弱點掃描\n主管：使用者\n做事：Codex laptop、Claude 安全串\n現在：Codex 發起，等 Claude 回覆\n"
        "- [x] 已確認客戶端設備\n- [x] 掃描範圍定好\n- [X] 拿到授權書\n- [ ] 客戶端回覆\n✗ 出報告\n☑ 備份\n\n"
        "技術細節：nmap -sV 跑在 Kali VM110\n下一步：等回覆")
items = [status("a" * 8, "evalscope", PLAIN, 4.2, 0.1, updates=42),
         status("b" * 8, "secaudit", CARD, 1.5, 0.05, updates=2),
         status("c" * 8, "aria2", "已完成的舊專案", 5, 3, stage="done", updates=0),
         status("d" * 8, "nohist", "查不到歷史的", 0.0, 0.0)]
del HIST["d" * 8]

fails = []


def check(name, cond):
    print(("通過 " if cond else "失敗 ") + name)
    if not cond:
        fails.append(name)


os_, _ = old._whiteboard(copy.deepcopy(items))
ns, _ = new._whiteboard(copy.deepcopy(items))
by = {s["project"]: s for s in ns}
obp = {s["project"]: s for s in os_}

# 1. 沒照新格式寫的：舊欄位一模一樣，新欄位空的
keep = ("project", "stage", "stage_label", "who", "whoc", "when", "tw", "epoch", "summary", "text")
for p in ("evalscope", "aria2", "nohist"):
    check(p + " 舊欄位跟改版前一樣", all(by[p][k] == obp[p][k] for k in keep))
    check(p + " 沒有名稱／主管／小目標", not by[p]["name"] and not by[p]["lead"] and by[p]["steps"] == [])
check("排序跟改版前一樣", [s["project"] for s in ns] == [s["project"] for s in os_])
check("secaudit 的 text（舊頁面展開看的全文）跟改版前一樣", by["secaudit"]["text"] == obp["secaudit"]["text"])

# 2. 新格式解析
s = by["secaudit"]
check("名稱", s["name"] == "弱點掃描")
check("主管", s["lead"] == "使用者")
check("做事", s["doers"] == "Codex laptop、Claude 安全串")
check("現在", "Codex 發起，等 Claude 回覆" in s["now"])
check("小目標 6 個、照順序", [x["t"] for x in s["steps"]] == ["已確認客戶端設備", "掃描範圍定好", "拿到授權書", "客戶端回覆", "出報告", "備份"])
check("完成 4 個（[x]、[X]、☑ 算完成；[ ]、✗ 不算）", [x["done"] for x in s["steps"]] == [True, True, True, False, False, True])
check("summary 改成技術細節第一行，不是「名稱：…」", s["summary"].startswith("技術細節") and "名稱" not in s["summary"])
check("detail 不含已拆出來的行", "名稱" not in s["detail"] and "客戶端回覆" not in s["detail"] and "nmap" in s["detail"])

# 3. 第幾天、更新幾次
def days(created, updated=0.0, done=False):
    a = (NOW - dt.timedelta(days=created)).astimezone(TPE).date()
    b = (NOW - dt.timedelta(days=updated)).astimezone(TPE).date() if done else dt.datetime.now(TPE).date()
    return (b - a).days + 1
check("evalscope 第幾天", by["evalscope"]["day"] == days(4.2))
check("secaudit 第幾天", s["day"] == days(1.5))
check("完成的專案算到最後更新那天", by["aria2"]["day"] == days(5, 3, True))
check("今天建立的是第 1 天", by["nohist"]["day"] == 1)
check("更新次數 = UPDATE 筆數", by["evalscope"]["updates"] == 42 and s["updates"] == 2 and by["aria2"]["updates"] == 0)
check("查不到歷史就是 None（頁面不顯示）", by["nohist"]["updates"] is None)
check("since 是建立日期", by["evalscope"]["since"] == (NOW - dt.timedelta(days=4.2)).astimezone(TPE).strftime("%m-%d"))

# 4. 快取：內容沒變不重查；改過才重查
CALLS.clear()
new._whiteboard(copy.deepcopy(items))
check("第二次輪詢只重查之前失敗的那筆", CALLS == ["d" * 8])
items[0]["updated_at"] = _ts(0)
HIST["a" * 8].append({"event": "UPDATE"})
CALLS.clear()
ns2, _ = new._whiteboard(copy.deepcopy(items))
check("改過的那筆重查、次數加一", "a" * 8 in CALLS and {x["project"]: x for x in ns2}["evalscope"]["updates"] == 43)

# 5. 擋 HTML
items.append(status("e" * 8, "xss", "名稱：<b>壞</b>\n主管：<img src=x>\n- [ ] <script>1</script>", 1, 0))
x = {q["project"]: q for q in new._whiteboard(copy.deepcopy(items))[0]}["xss"]
check("名稱、主管、小目標都有跳脫", "<b>" not in x["name"] and "<img" not in x["lead"] and "<script>" not in x["steps"][0]["t"])

print("全部通過" if not fails else "失敗 %d 項" % len(fails))
sys.exit(1 if fails else 0)
