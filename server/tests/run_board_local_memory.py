"""本機跑真的公告欄後端（board_app）＋真的經歷區程式（experience_store.py），Mem0 換成記憶體裡的假資料。
給記憶頁的「共用記憶／經歷區／審核」三個分頁在瀏覽器實測用：列得出來、補充寫得進去讀得回來、匯入審核寫進經歷區。
資料全是假的，跟使用者真的經歷無關；伺服器關掉就沒了。
用法：uv run --no-project --with starlette --with httpx --with uvicorn python run_board_local_memory.py <server.py 目錄> <UI 資料夾> [埠，預設 8906]
UI 資料夾裡放 stable/ 和 preview/ 各三頁。"""
import copy
import importlib.util
import json
import os
import sys
import tempfile
import types
import uuid

SRC, UI = sys.argv[1], os.path.abspath(sys.argv[2])
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8906


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


for n in ["mcp", "mcp.server", "mcp.server.transport_security"]:
    _stubmod(n)
_stubmod("mcp.server.fastmcp", FastMCP=FastMCP, Image=_Any)
REVIEWS = tempfile.mkdtemp(prefix="board-reviews-")
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "UI_DIR": UI, "REVIEW_DIR": REVIEWS,
                   "BOARD_USER": "u", "BOARD_PASSWORD": "p" * 16})

DB = []
TICK = [0]


def _ts():
    TICK[0] += 1
    return f"2026-10-{1 + TICK[0] // 1440:02d}T{TICK[0] // 60 % 24:02d}:{TICK[0] % 60:02d}:00+00:00"


class R:
    def __init__(self, d=None): self.d = d if d is not None else {}
    def raise_for_status(self): pass
    def json(self): return self.d


class FakeClient:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get(self, path, params=None, **k):
        return R({"results": [copy.deepcopy(m) for m in DB if m["user_id"] == params["user_id"]][: params.get("top_k", 100)]})
    def post(self, path, json=None, **k):
        if path == "/search":
            return R({"results": []})
        text = json["messages"][0]["content"]
        mid, t = str(uuid.uuid4()), _ts()
        DB.append({"id": mid, "memory": text, "metadata": copy.deepcopy(json.get("metadata") or {}),
                   "agent_id": json.get("agent_id"), "user_id": json["user_id"], "created_at": t, "updated_at": t})
        print("[假站] 寫入", json["user_id"], (json.get("metadata") or {}).get("record_id") or mid[:8], flush=True)
        return R({"results": [{"id": mid, "memory": text, "event": "ADD"}]})
    def put(self, path, json=None, **k):
        mid = path.rsplit("/", 1)[1]
        for m in DB:
            if m["id"] == mid:
                m["memory"], m["metadata"], m["updated_at"] = json["text"], copy.deepcopy(json["metadata"]), _ts()
                print("[假站] 修改", m["user_id"], m["metadata"].get("record_id") or mid[:8], flush=True)
        return R()
    def delete(self, path, **k):
        DB[:] = [m for m in DB if m["id"] != path.rsplit("/", 1)[1]]
        return R()


sys.path.insert(0, SRC)
spec = importlib.util.spec_from_file_location("srv", os.path.join(SRC, "server.py"))
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)
S.STORE.http = lambda: FakeClient()
S._cookie_ok = lambda request: True
S._send = lambda *a, **k: (print("[假站] 收到寄信：", a[1:3], flush=True), {"results": [{"id": "msg1"}]})[1]
FEED = json.load(open(os.path.join(os.path.dirname(__file__), "../../whiteboard-v2/tools/feed.json"), encoding="utf-8"))
S._feed = lambda n: {"messages": [], "total": 0, "todo": 0, "todo_by": {}, "claims": [], "recipients": ["all", "claude", "codex"],
                     "aliases": {}, "sessions": {}, "statuses": FEED.get("statuses", []), "research": FEED.get("research", [])}

# ---- 假資料：共用記憶 ----
for i, (typ, proj, who, text) in enumerate([
        ("decision", "mem0", "claude-laptop", "（假資料）嵌入模型主備援：主用 A、備用 B，切換條件是連續三次逾時。"),
        ("note", "board-ui", "claude-laptop", "（假資料）公告欄預覽要使用者自己按採用才上正式版。\n2026-10-17 使用者補充：手機也要能按"),
        ("note", "homelab", "codex-laptop", "（假資料）NAS 每週日凌晨做一次快照。"),
        ("decision", "user-profile", "claude-laptop", "（假資料）回覆一律白話繁體中文。"),
        ("note", "user-profile", "claude-laptop", "（假資料）喜歡先看預覽再決定。\n2026-10-15 使用者補充：（假資料）手機上也要看得清楚"),
        ("status", "mem0", "claude-laptop", "（假資料）[進度] 經歷區分頁做到本機實測。")]):
    t = _ts()
    DB.append({"id": str(uuid.uuid4()), "memory": text, "metadata": {"type": typ, "project": proj},
               "agent_id": who, "user_id": "roundtable", "created_at": t, "updated_at": t})

# ---- 假資料：經歷區（用真的 experience_add 寫，驗證規則一樣）----
S.current_agent.set("codex-laptop")
CATS = [("工作", "一般"), ("學習", "一般"), ("興趣", "一般"), ("生活", "一般"), ("健康", "敏感"), ("家庭", "敏感"), ("感情", "高度敏感")]
for i in range(60):
    cat, sens = CATS[i % len(CATS)]
    year = 2015 + i % 11
    rec = {"record_id": f"demo-{i:03d}", "content": f"（假資料第 {i + 1} 條）{cat}方面的一件事，{year} 年左右。" + ("這一條比較長，用來看換行和手機寬度會不會破版。" * (i % 4)),
           "when": f"{year} 年" if i % 9 else "時間不明", "sort_date": f"{year}-{1 + i % 12:02d}" if i % 9 else None,
           "category": cat, "sensitivity": sens, "confidence": "摘要待核查" if i % 5 == 0 else "自述",
           "record_kind": ("event", "state", "plan", "preference")[i % 4], "source_refs": [{"demo": i}],
           "source_text": ["（假資料的原句，頁面不該出現）"]}
    S.EXPERIENCE.add(rec)
r = S._exp_find("demo-003")
S.EXPERIENCE.update("demo-003", {"content": r["content"] + "\n2026-10-16 使用者補充：（假資料）之前補過的一句"}, r["version"])

# ---- 假資料：一份大量匯入的審核清單 ----
review = {"id": "demo-import", "title": "ChatGPT 匯入審核（假資料）", "created": "2026-10-18",
          "tabs": [{"key": "chatgpt", "label": "ChatGPT 草稿", "draft": "draft-profile.md",
                    "intro": ["這些是從 ChatGPT 匯出整理的草稿（假資料）。選「寫進經歷區」才會寫進去。"], "groups": [
                        {"key": "work", "label": "工作", "items": [
                            {"id": "G1", "text": "（假資料）做過三年網管"},
                            {"id": "G2", "text": "（假資料）第 1 條已經在經歷區", "exists": "demo-000"},
                            {"id": "G3", "text": "（假資料）想學程式設計", "kind": "plan", "sort_date": "2026"}]},
                        {"key": "health", "label": "健康", "sensitive": True, "items": [
                            {"id": "G4", "text": "（假資料）每天走路半小時"},
                            {"id": "G5", "text": "（假資料）睡眠不太好"}]}]}]}
with open(os.path.join(REVIEWS, "demo-import.json"), "w", encoding="utf-8") as f:
    json.dump(review, f, ensure_ascii=False)
print(f"[假站] 共用記憶 {sum(m['user_id'] == 'roundtable' for m in DB)} 筆、經歷區 60 筆、審核清單 1 份；http://127.0.0.1:{PORT}/board/preview/memory", flush=True)

import uvicorn  # noqa: E402
uvicorn.run(S.board_app, host="127.0.0.1", port=PORT, log_level="warning")
