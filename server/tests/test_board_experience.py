"""離線測試：記憶頁「經歷區」分頁（瀏覽＋追加補充＋復原）和匯入審核改寫進經歷區。
用假的 Mem0（依 user_id 分區）＋真的 experience_store.py＋真的 starlette，不連任何真實服務。
用法：uv run --no-project --with starlette --with httpx python test_board_experience.py <server.py 所在目錄>"""
import copy
import importlib.util
import json
import os
import sys
import tempfile
import types
import uuid

SRC = sys.argv[1]


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
for k in [k for k in os.environ if k.startswith("MCP_TOKEN_")]:
    del os.environ[k]
ROOT = tempfile.mkdtemp(prefix="expboard-")
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE_LAPTOP": "t1",
                   "UI_DIR": os.path.join(ROOT, "ui"), "REVIEW_DIR": os.path.join(ROOT, "reviews"),
                   "BOARD_USER": "u", "BOARD_PASSWORD": "p" * 16})
os.environ.pop("BOARD_REV_CHECK", None)
os.makedirs(os.environ["REVIEW_DIR"])

DB = []
TICK = [0]


def _ts():
    TICK[0] += 1
    return f"2026-10-18T02:{TICK[0] // 60:02d}:{TICK[0] % 60:02d}+00:00"


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
        return R({"results": [{"id": mid, "memory": text, "event": "ADD"}]})
    def put(self, path, json=None, **k):
        mid = path.rsplit("/", 1)[1]
        for m in DB:
            if m["id"] == mid:
                m["memory"], m["metadata"], m["updated_at"] = json["text"], copy.deepcopy(json["metadata"]), _ts()
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
S._send = lambda *a, **k: {"results": [{"id": "msg1"}]}

from starlette.testclient import TestClient  # noqa: E402

FAILS = []


def check(name, ok):
    print(("通過 " if ok else "失敗 ") + name)
    if not ok:
        FAILS.append(name)


def put_pages(which, rev, version):
    d = os.path.join(os.environ["UI_DIR"], which)
    os.makedirs(d, exist_ok=True)
    for name, kind in zip(S._UI_FILES, ("board", "memory", "release")):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write('<!doctype html><html><head><meta name="%s-version" content="%s"><meta name="%s-revision" content="%d">'
                    '</head><body></body></html>' % (kind, version, kind, rev))


def rec(rid, content, sens="一般", cat="工作", date="2025-04"):
    return {"record_id": rid, "content": content, "when": "2025 年 4 月", "sort_date": date, "category": cat,
            "sensitivity": sens, "confidence": "自述", "record_kind": "event",
            "source_refs": [{"conversation": "c1"}], "source_text": ["原句不該出現在頁面"]}


def exp(rid):
    return next(m for m in DB if m["user_id"] == "profile" and m["metadata"].get("record_id") == rid)


S.current_agent.set("codex-laptop")
S.EXPERIENCE.add(rec("e-work", "在 A 公司做網管"))
S.EXPERIENCE.add(rec("e-health", "看過某一科", sens="高度敏感", cat="健康", date="2024"))
S.current_agent.set("user")  # 安全修補後只有使用者能寫「使用者補充」行，種子資料改用 user 身分
S.EXPERIENCE.add(rec("e-old", "養過一隻貓\n2026-10-16 使用者補充：牠叫小黑", date=None))
S.current_agent.set("codex-laptop")
put_pages("stable", 31, "1.8.0")
put_pages("preview", 32, "1.9.0")
c = TestClient(S.board_app, base_url="http://board.test")
H = {"Origin": "http://board.test", "X-Board-Rev": "preview:32", "Accept": "application/json"}

# ---- 瀏覽 ----
r = c.get("/board/api/experience")
j = r.json()
check("列出經歷區 3 筆", r.status_code == 200 and j["total"] == 3 and len(j["items"]) == 3)
check("原句和出處不給頁面", "原句不該出現" not in r.text and "source_refs" not in r.text and "conversation" not in r.text)
ids = [x["id"] for x in j["items"]]
check("有日期的新到舊、沒日期的在最後", ids == ["e-work", "e-health", "e-old"])
old = next(x for x in j["items"] if x["id"] == "e-old")
check("舊的使用者補充分開顯示", old["text"] == "養過一隻貓" and old["feet"] == ["2026-10-16 使用者補充：牠叫小黑"])
check("高度敏感照樣列出、帶敏感度", next(x for x in j["items"] if x["id"] == "e-health")["sensitivity"] == "高度敏感")

# ---- 補充 ----
r = c.post("/board/api/experience/e-work/footnote", json={"text": "後來升組長"}, headers={"Origin": "http://board.test"})
check("沒帶版號（舊頁面）擋下", r.status_code == 409 and exp("e-work")["memory"] == "在 A 公司做網管")
r = c.post("/board/api/experience/e-work/footnote", json={"text": "後來升組長"}, headers={"X-Board-Rev": "preview:32"})
check("不是公告欄頁面送的（沒有 Origin）擋下", r.status_code == 403)
r = c.post("/board/api/experience/e-work/footnote", json={"text": "  後來升組長 \n 管三個人 "}, headers=H)
j = r.json()
cur = exp("e-work")
check("補充成功", r.status_code == 200 and j.get("undo_token"))
check("原文不動、最後面接使用者補充", cur["memory"].startswith("在 A 公司做網管\n") and cur["memory"].endswith(" 使用者補充：後來升組長 管三個人"))
check("回傳的補充行跟存的一樣", j["feet"] == [cur["memory"].split("\n")[1]])
check("敏感度、分類、出處都保留", cur["metadata"]["sensitivity"] == "一般" and cur["metadata"]["category"] == "工作"
      and cur["metadata"]["source_refs"] == [{"conversation": "c1"}])
check("以 user 身分改、版次 +1", cur["metadata"]["updated_by"] == "user" and cur["metadata"]["record_revision"] == 2)
check("舊版進經歷歷史", len(S.EXPERIENCE.history("e-work")) == 1)
check("紀錄檔不含補充內容", "後來升組長" not in open(os.path.join(os.environ["REVIEW_DIR"], "footnotes.log.jsonl"), encoding="utf-8").read())

r = c.post("/board/api/experience/e-work/footnote", json={"text": "密碼: abcdefgh1234"}, headers=H)
check("像密碼的字擋下、說白話", r.status_code == 400 and "經歷區不收" in r.json()["error"] and "abcdefgh" not in r.text)
check("擋下時沒寫入", exp("e-work")["memory"].count("使用者補充") == 1)
check("空白擋下", c.post("/board/api/experience/e-work/footnote", json={"text": "  "}, headers=H).status_code == 400)
check("超過 500 字擋下", c.post("/board/api/experience/e-work/footnote", json={"text": "字" * 501}, headers=H).status_code == 400)
check("找不到的代號擋下", c.post("/board/api/experience/nope/footnote", json={"text": "x"}, headers=H).status_code == 400)
check("代號格式不對擋下", c.post("/board/api/experience/Bad%20ID/footnote", json={"text": "x"}, headers=H).status_code == 400)

# ---- 復原 ----
r = c.post("/board/api/experience/footnote/undo", json={"token": j["undo_token"]}, headers=H)
check("五秒內復原成功", r.status_code == 200 and r.json()["feet"] == [] and exp("e-work")["memory"] == "在 A 公司做網管")
check("同一個復原碼不能用兩次", c.post("/board/api/experience/footnote/undo", json={"token": j["undo_token"]}, headers=H).status_code == 400)
j2 = c.post("/board/api/experience/e-old/footnote", json={"text": "後來送給朋友"}, headers=H).json()
check("已經有補充的再補一條，兩條都在", exp("e-old")["memory"].count("使用者補充") == 2)
S.current_agent.set("codex-laptop")
v = S.EXPERIENCE._format(exp("e-old"))["version"]
S.EXPERIENCE.update("e-old", {"category": "生活"}, v)
r = c.post("/board/api/experience/footnote/undo", json={"token": j2["undo_token"]}, headers=H)
check("之後又被別人改過就不復原", r.status_code == 400 and "又被改過" in r.json()["error"] and exp("e-old")["memory"].count("使用者補充") == 2)

# ---- 匯入審核：寫進經歷區，不寫 user-profile ----
review = {"id": "imp1", "title": "匯入審核", "created": "2026-10-18",
          "tabs": [{"key": "chatgpt", "label": "ChatGPT 草稿", "draft": "draft-profile.md", "groups": [
              {"key": "h", "label": "健康", "sensitive": True, "items": [
                  {"id": "G1", "text": "每天走路半小時"},
                  {"id": "G2", "text": "在 A 公司做網管"},
                  {"id": "G3", "text": "已經匯入過的", "exists": "e-xx"},
                  {"id": "G4", "text": "AI 整理的草稿", "category": "生活", "sensitivity": "一般", "sort_date": "2023-05", "kind": "plan"}]}]}]}
with open(os.path.join(os.environ["REVIEW_DIR"], "imp1.json"), "w", encoding="utf-8") as f:
    json.dump(review, f, ensure_ascii=False)
check("審核清單告訴頁面匯入寫到經歷區", c.get("/board/api/review/imp1").json().get("import_target") == "experience")
before_rt = [m for m in DB if m["user_id"] != "profile" and m["user_id"] != "profile-history"]
r = c.post("/board/api/review/imp1/act", json={"item": "G1", "choice": "do"}, headers=H)
g1 = next((m for m in DB if m["user_id"] == "profile" and m["metadata"].get("record_id") == "rv-imp1-g1"), None)
check("照做 → 寫進經歷區", r.status_code == 200 and r.json()["status"] == "done" and g1 is not None and "經歷區" in r.json()["note"])
check("分類用分組、敏感分組標敏感、草稿標摘要待核查", g1 and g1["memory"] == "每天走路半小時" and g1["metadata"]["category"] == "健康"
      and g1["metadata"]["sensitivity"] == "敏感" and g1["metadata"]["confidence"] == "摘要待核查"
      and g1["metadata"]["created_by"] == "user" and g1["metadata"]["import_batch_id"] == "board-review-imp1")
check("沒有寫進共用記憶（user-profile）", [m for m in DB if m["user_id"] not in ("profile", "profile-history")] == before_rt)
r = c.post("/board/api/review/imp1/act", json={"item": "G2", "choice": "do"}, headers=H)
check("經歷區一字不差已有 → 不再寫一份", r.status_code == 200 and r.json()["status"] == "skipped" and "e-work" in r.json()["note"])
r = c.post("/board/api/review/imp1/act", json={"item": "G3", "choice": "do"}, headers=H)
check("清單標了已經有的 → 擋下", r.status_code == 400 and "經歷區已經有了" in r.json()["error"])
r = c.post("/board/api/review/imp1/act", json={"item": "G4", "choice": "change", "text": "我自己說：明年想去日本"}, headers=H)
g4 = next((m for m in DB if m["metadata"].get("record_id") == "rv-imp1-g4"), None)
check("改成… → 用使用者的字、標自述、照清單的分類和時間", r.status_code == 200 and g4 and g4["memory"] == "我自己說：明年想去日本"
      and g4["metadata"]["confidence"] == "自述" and g4["metadata"]["category"] == "生活"
      and g4["metadata"]["sort_date"] == "2023-05" and g4["metadata"]["record_kind"] == "plan")
tok = c.post("/board/api/review/imp1/act", json={"item": "G1", "choice": "do"}, headers=H)
check("做過的不能再按", tok.status_code == 400)
st = json.load(open(os.path.join(os.environ["REVIEW_DIR"], "imp1.state.json"), encoding="utf-8"))
r = c.post("/board/api/review/imp1/undo", json={"item": "G1", "token": st["G1"]["undo"]["token"]}, headers=H)
check("寫進經歷區後按復原：說清楚退不回來", r.status_code == 400 and "經歷區沒有刪除" in r.json()["error"]
      and any(m["metadata"].get("record_id") == "rv-imp1-g1" for m in DB))

review["id"], review["dry_run"] = "imp2", True
with open(os.path.join(os.environ["REVIEW_DIR"], "imp2.json"), "w", encoding="utf-8") as f:
    json.dump(review, f, ensure_ascii=False)
n = len(DB)
r = c.post("/board/api/review/imp2/act", json={"item": "G1", "choice": "do"}, headers=H)
check("測試清單：只說會寫到哪，不真的寫", r.status_code == 200 and "經歷區「健康」" in r.json()["note"] and len(DB) == n)

print("全部通過" if not FAILS else "有 %d 項失敗" % len(FAILS))
sys.exit(1 if FAILS else 0)
