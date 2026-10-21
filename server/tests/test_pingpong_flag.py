"""離線測試：白板專案卡的「乒乓」開關（POST /board/api/project/pingpong、feed 的 pingpong、project_status 的 pingpong）。
用假的 Mem0（記憶體裡的清單＋歷史），真的 starlette 跑公告欄端點，不連任何真實服務。
用法：uv run --no-project --with starlette --with httpx python test_pingpong_flag.py <server.py 目錄>"""
import copy
import importlib.util
import os
import sys
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
_stubmod("experience_store", register=lambda *a, **k: None)
for k in [k for k in os.environ if k.startswith("MCP_TOKEN_")]:
    del os.environ[k]
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE_LAPTOP": "t1",
                   "BOARD_USER": "u", "BOARD_PASSWORD": "p" * 16, "MSG_ARCHIVE": "0"})

DB, HIST, TICK = [], {}, [0]
FAIL = {"put_ignored": False}


def _ts():
    TICK[0] += 1
    return f"2026-10-19T02:{TICK[0] // 60:02d}:{TICK[0] % 60:02d}+00:00"


class R:
    def __init__(self, d=None): self.d = d if d is not None else {}
    def raise_for_status(self): pass
    def json(self): return self.d


class FakeClient:
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get(self, path, params=None, **k):
        if path.endswith("/history"):
            return R(HIST.get(path.split("/")[2], []))
        return R({"results": [copy.deepcopy(m) for m in DB if m["user_id"] == (params or {}).get("user_id")]})
    def post(self, path, json=None, **k):
        mid, t = str(uuid.uuid4()), _ts()
        DB.append({"id": mid, "memory": json["messages"][0]["content"], "metadata": copy.deepcopy(json.get("metadata") or {}),
                   "agent_id": json.get("agent_id"), "user_id": json["user_id"], "created_at": t, "updated_at": t})
        HIST[mid] = [{"event": "ADD"}]
        return R({"results": [{"id": mid, "event": "ADD"}]})
    def put(self, path, json=None, **k):
        mid = path.rsplit("/", 1)[1]
        if FAIL["put_ignored"]:
            return R()
        for m in DB:
            if m["id"] == mid:
                HIST.setdefault(mid, []).append({"event": "UPDATE", "old_memory": m["memory"], "new_memory": json["text"]})
                m["memory"], m["metadata"], m["updated_at"] = json["text"], copy.deepcopy(json["metadata"]), _ts()
        return R()
    def delete(self, path, **k):
        DB[:] = [m for m in DB if m["id"] != path.rsplit("/", 1)[1]]
        return R()


spec = importlib.util.spec_from_file_location("srv", os.path.join(SRC, "server.py"))
S = importlib.util.module_from_spec(spec)
sys.path.insert(0, SRC)
spec.loader.exec_module(S)
S.STORE.http = lambda: FakeClient()
S._cookie_ok = lambda request: True
S.BOARD_REV_CHECK = False
from starlette.testclient import TestClient  # noqa: E402

web = TestClient(S.board_app)
results = []


def check(name, ok):
    results.append((name, bool(ok)))


def raises(exc, fn, *a):
    try:
        fn(*a)
        return False
    except exc:
        return True


def status_md(p):
    return next(m for m in DB if (m["metadata"] or {}).get("type") == "status" and m["metadata"].get("project") == p)


def card(p):
    return next(s for s in S._whiteboard(S._list_all())[0] if s["project"] == p)


def flip(p, on, origin="http://testserver"):
    h = {"origin": origin} if origin else {}
    return web.post("/board/api/project/pingpong", json={"project": p, "on": on}, headers=h)


S.current_agent.set("claude-laptop")
check("沒有進度卡時 project_status 的 pingpong 是 false", S.project_status("mem0")["pingpong"] is False)
S.project_set_status("mem0", "名稱：圓桌記憶\n現在：測試中\n- [ ] 第一步", "implementing")
v0 = S.project_status("mem0")
t0 = status_md("mem0")["updated_at"]
check("預設是關的（project_status）", v0["pingpong"] is False)
check("預設是關的（白板卡片）", card("mem0")["pingpong"] is False)

# 打開
r = flip("mem0", True)
check("打開：回 200 和 {project, pingpong:true}", r.status_code == 200 and r.json() == {"project": "mem0", "pingpong": True})
md = status_md("mem0")["metadata"]
check("metadata 記 pingpong=yes、誰改的是 user、有時間", md.get("pingpong") == "yes" and md.get("pingpong_by") == "user"
      and md.get("pingpong_at", "").startswith("20"))
check("內文沒動", status_md("mem0")["memory"] == "[狀態] 名稱：圓桌記憶\n現在：測試中\n- [ ] 第一步")
check("version 沒變（AI 的 base_version 照樣能用）", S.project_status("mem0")["version"] == v0["version"])
check("project_status 回 pingpong=true", S.project_status("mem0")["pingpong"] is True)
c = card("mem0")
check("白板卡片 pingpong=true", c["pingpong"] is True)
check("白板卡片的時間還是切開關之前的", S.project_status("mem0")["status"]["updated"] == t0 and c["tw"] == S._tw(t0))
check("白板「更新幾次」不把切開關算進去", c["updates"] == 0)
check("更新者還是原本的 AI", c["who"] == "claude-laptop")

# AI 更新進度：開關要留著
S.current_agent.set("claude-laptop")
S.project_set_status("mem0", "名稱：圓桌記憶\n現在：第二步\n- [x] 第一步", "", S.project_status("mem0")["version"])
md = status_md("mem0")["metadata"]
check("AI 更新進度後開關還在", md.get("pingpong") == "yes" and md.get("pingpong_by") == "user" and md.get("pingpong_flips") == "1")
check("AI 更新進度後時間改用新的更新時間", "status_at" not in md and card("mem0")["tw"] == S._tw(status_md("mem0")["updated_at"]))
check("AI 更新進度算一次更新（切開關不算）", card("mem0")["updates"] == 1)
check("階段沿用", md.get("stage") == "implementing")

# 關掉
r = flip("mem0", False)
check("關掉：回 pingpong:false", r.status_code == 200 and r.json()["pingpong"] is False)
check("關掉後 project_status 和白板都是 false", S.project_status("mem0")["pingpong"] is False and card("mem0")["pingpong"] is False)
check("切兩次後更新次數還是 1", card("mem0")["updates"] == 1 and status_md("mem0")["metadata"]["pingpong_flips"] == "2")

# 錯誤情況
check("沒有進度卡的專案回 404", flip("nosuch", True).status_code == 404)
check("專案名稱不合法回 400", flip("Bad Name!", True).status_code == 400)
r = web.post("/board/api/project/pingpong", json={"project": "mem0", "on": "yes"}, headers={"origin": "http://testserver"})
check("on 不是 true/false 回 400", r.status_code == 400)
check("不是公告欄頁面送的（沒有 origin）回 403", flip("mem0", True, origin="").status_code == 403)
check("錯誤的請求都沒改到資料", status_md("mem0")["metadata"]["pingpong"] == "no")
r = web.post("/board/api/project/pingpong", content=b"x" * 5000, headers={"origin": "http://testserver"})
check("太大回 413", r.status_code == 413)
FAIL["put_ignored"] = True
r = flip("mem0", True)
FAIL["put_ignored"] = False
check("寫完讀回對不上就回錯（400）", r.status_code == 400 and "讀回" in r.json().get("error", ""))

# 舊版頁面擋寫入
check("新端點在「擋舊版頁面」的名單裡", "/board/api/project/pingpong".startswith(S._REV_WRITE_PREFIXES))
S.BOARD_REV_CHECK = True
check("舊版頁面（沒帶版號）送出回 409", flip("mem0", True).status_code == 409)
S.BOARD_REV_CHECK = False

# 沒有乒乓欄位的舊卡片
S.current_agent.set("claude-laptop")
S.project_set_status("pve", "名稱：PVE", "implementing")
check("舊卡片沒有 pingpong 欄位就是 false", card("pve")["pingpong"] is False and "pingpong" not in status_md("pve")["metadata"])
check("project_set_status 不會無中生有開關欄位", not any(k in status_md("pve")["metadata"] for k in S.PINGPONG_KEYS))

fail = [n for n, ok in results if not ok]
for n, ok in results:
    print(("通過 " if ok else "失敗 ") + n)
print(f"共 {len(results)} 項，失敗 {len(fail)} 項")
sys.exit(1 if fail else 0)
