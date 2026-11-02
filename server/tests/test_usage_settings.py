"""離線測試：額度頁「用量門檻」設定（Codex 兩個＋Antigravity 兩池保底）（usage.settings_set／settings_get、view()／put() 的 settings、POST /board/api/usage/settings）。
測：預設值、存讀、範圍與格式錯回 400（繁中原因）、沒登入 401、沒有或別站 Origin 403、太大 413、每分鐘 20 次以上 429、
重開（換新連線）資料還在、舊版頁面 409、設定不跑進提供商卡片、usage_report 照收。
用假的 Mem0，真的 starlette 跑公告欄端點，資料庫在暫存資料夾，不連任何真實服務。
用法：uv run --no-project --with starlette --with httpx python test_usage_settings.py <server.py 目錄>"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import types

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="usage-settings-")


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
                   "BOARD_USER": "u", "BOARD_PASSWORD": "p" * 16, "MSG_ARCHIVE": "0", "MONITOR_DIR": TMP, "MONITOR_ON": "0"})

spec = importlib.util.spec_from_file_location("srv", os.path.join(SRC, "server.py"))
S = importlib.util.module_from_spec(spec)
sys.path.insert(0, SRC)
spec.loader.exec_module(S)
U = S._usage
results = []
DEF = {"radar_avg_threshold": 60, "normal_floor": 30, "radar_floor": 15, "agy_gemini_floor": 0, "agy_claude_gpt_floor": 0,
       "updated_at": None, "updated_by": None}


def check(name, ok):
    results.append((name, bool(ok)))


check("server 載入了額度模組", U is not None)
DB = os.path.join(TMP, "unit.db")
U._conn(DB)
T = 1_793_000_000.0

# ---- 單元：預設值
check("沒設定過：settings_get 回預設 60／30／15、Antigravity 兩池 0、沒有更新時間和人", U.settings_get() == DEF)
v = U.view(now=T)
check("沒設定過：view() 有 settings 而且是預設", v["settings"] == DEF)
check("設定不會變成一家提供商卡片", all(not x["id"].startswith("_") and x["id"] != "settings" for x in v["providers"]))

# ---- 存讀
r = U.settings_set({"radar_avg_threshold": 70}, "user", now=T)
check("只改雷達門檻：ok、70、保底沿用 30", r["ok"] and r["settings"]["radar_avg_threshold"] == 70 and r["settings"]["normal_floor"] == 30)
check("回傳更新時間和登入者", r["settings"]["updated_at"] == U._iso(T) and r["settings"]["updated_by"] == "user")
check("只改雷達門檻：放寬保底 radar_floor 沿用預設 15", r["settings"]["radar_floor"] == 15)
r = U.settings_set({"normal_floor": 20}, "user", now=T + 1)
check("只改保底：20、雷達門檻留著 70", r["ok"] and r["settings"] == dict(DEF, radar_avg_threshold=70, normal_floor=20,
                                                                updated_at=U._iso(T + 1), updated_by="user"))
r = U.settings_set({"radar_avg_threshold": 100, "normal_floor": 0}, "user", now=T + 2)
check("兩個一起改、邊界值 100 和 0 收", r["ok"] and r["settings"]["radar_avg_threshold"] == 100 and r["settings"]["normal_floor"] == 0)
for i, n in enumerate((33, 0, 100, 1, 99)):  # 一格 1%：不是 5 的倍數也收
    r = U.settings_set({"agy_gemini_floor": n}, "user", now=T + 2.1 + i * 0.01)
    check(f"一格 1%：Gemini 池保底 {n} 收（ok、存成 {n}）", r["ok"] and r["settings"]["agy_gemini_floor"] == n)
r = U.settings_set({"normal_floor": 33.0}, "user", now=T + 2.2)
check("33.0（沒小數的浮點數）當 33 收", r["ok"] and r["settings"]["normal_floor"] == 33 and isinstance(r["settings"]["normal_floor"], int))
r = U.settings_set({"radar_avg_threshold": 30.0, "normal_floor": 60}, "user", now=T + 3)
check("邊界值 30（JSON 寫成 30.0 也收）和 60 收", r["ok"] and r["settings"]["radar_avg_threshold"] == 30 and r["settings"]["normal_floor"] == 60)
check("view() 讀得到剛存的", U.view(now=T + 4)["settings"]["radar_avg_threshold"] == 30)
for i, n in enumerate((0, 100, 37)):  # 10/28 起放寬保底也開放調
    r = U.settings_set({"radar_floor": n}, "user", now=T + 3.1 + i * 0.01)
    check(f"放寬保底 radar_floor {n} 收（ok、存成 {n}）", r["ok"] and r["settings"]["radar_floor"] == n)
check("只送 radar_floor：其他鍵沿用舊值（30／60、Gemini 池 99）", r["settings"] == dict(DEF, radar_avg_threshold=30, normal_floor=60,
      radar_floor=37, agy_gemini_floor=99, updated_at=U._iso(T + 3.12), updated_by="user"))
check("view() 讀得到剛存的 radar_floor 37", U.view(now=T + 4)["settings"]["radar_floor"] == 37)
r = U.settings_set({"radar_floor": 15}, "user", now=T + 3.2)
check("radar_floor 改回 15", r["ok"] and r["settings"]["radar_floor"] == 15)
r = U.settings_set({"agy_gemini_floor": 20}, "user", now=T + 4)
check("只改 Antigravity Gemini 池保底：20、其他鍵都沿用", r["ok"] and r["settings"] == dict(DEF, radar_avg_threshold=30, normal_floor=60,
      agy_gemini_floor=20, updated_at=U._iso(T + 4), updated_by="user"))
r = U.settings_set({"agy_claude_gpt_floor": 60}, "user", now=T + 4.5)
check("只改 Antigravity Claude 和 GPT 池保底：60（邊界）、Gemini 池留著 20", r["ok"] and r["settings"]["agy_claude_gpt_floor"] == 60
      and r["settings"]["agy_gemini_floor"] == 20 and r["settings"]["normal_floor"] == 60)
r = U.settings_set({"radar_avg_threshold": 30, "normal_floor": 60, "agy_gemini_floor": 0, "agy_claude_gpt_floor": 35}, "user", now=T + 4.6)
check("四鍵一起改、邊界值 0 收", r["ok"] and r["settings"]["agy_gemini_floor"] == 0 and r["settings"]["agy_claude_gpt_floor"] == 35)
check("view() 的 settings 有 Antigravity 兩鍵", U.view(now=T + 4.7)["settings"]["agy_claude_gpt_floor"] == 35)
got = U.put({"source": "pc", "providers": [{"id": "codex", "name": "Codex", "windows": [
    {"key": "weekly", "label": "每週", "used": 40, "resets_at": T + 86400, "minutes": 10080}]}]}, "pc", now=T + 5)
check("usage_report（put）照收，回傳多帶 settings 給收集程式", got["providers"] == 1 and got["windows"] == 1
      and got["settings"]["normal_floor"] == 60)

# ---- 範圍、格式錯
before = U.settings_get()
bad = [({}, "至少要帶"), ({"radar_floor": 101}, "提早重置時放寬的保底"), ({"normal_floor": 30, "evil": 1}, "不認得的欄位"),
       ([1, 2], "JSON 物件"), (None, "JSON 物件"), ({"agy_floor": 10}, "不認得的欄位"), ({"agy_floor": 10}, "agy_claude_gpt_floor"),
       ({"agy_gemini_floor": 101}, "Antigravity Gemini 池保底"), ({"agy_claude_gpt_floor": 101}, "Antigravity Claude 和 GPT 池保底"),
       ({"normal_floor": 101}, "Codex 平常保底"), ({"radar_avg_threshold": -1}, "提早重置機率門檻")]
for k in ("radar_avg_threshold", "normal_floor", "radar_floor", "agy_gemini_floor", "agy_claude_gpt_floor"):  # 五鍵同一張錯誤表
    bad += [({k: -1}, "要在 0% 到 100% 之間，收到 -1"), ({k: 101}, "要在 0% 到 100% 之間，收到 101"),
            ({k: 33.5}, "要是整數"), ({k: "33"}, "要是整數"), ({k: True}, "要是整數"), ({k: False}, "要是整數"),
            ({k: None}, "要是整數")]
assert len(bad) * 10 < 1000  # 每筆隔 10 秒，下面「一個錯一個對」用 T + 1000 不會撞到
for i, (data, word) in enumerate(bad):
    r = U.settings_set(data, "user", now=T + 100 + i * 10)  # 隔 10 秒，不碰到每分鐘上限
    check(f"錯的輸入 {data!r}：status 400、原因有「{word}」", r["ok"] is False and r["status"] == 400 and word in r["error"])
check("錯的輸入都沒改到門檻", U.settings_get() == before)
check("一個錯一個對也不存（整筆不收）", U.settings_set({"radar_avg_threshold": 50, "normal_floor": 101}, "user", now=T + 1000)["ok"] is False
      and U.settings_get()["radar_avg_threshold"] == before["radar_avg_threshold"])

# ---- 每分鐘 20 次
U._conn(os.path.join(TMP, "rate.db"))
oks = [U.settings_set({"normal_floor": 5 * (i % 12)}, "user", now=T + i)["ok"] for i in range(20)]
check("一分鐘內前 20 次都過", all(oks))
r = U.settings_set({"normal_floor": 30}, "user", now=T + 20)
check("第 21 次：status 429、說明寫 20 次、附 retry_after", r["ok"] is False and r["status"] == 429 and "20 次" in r["error"]
      and r["retry_after"] == 40)
check("被擋那次沒存", U.settings_get()["normal_floor"] == 5 * (19 % 12))
check("最舊那次滿一分鐘又可以改", U.settings_set({"normal_floor": 30}, "user", now=T + 60)["ok"] is True)

# ---- 重開（新連線、同一個檔）資料還在
U._conn(os.path.join(TMP, "persist.db"))
U.settings_set({"radar_avg_threshold": 75, "normal_floor": 25}, "user", now=T)
U._db.close()
U._conn(os.path.join(TMP, "persist.db"))
s = U.settings_get()
check("重開後門檻還在（75／25、更新者 user）", s["radar_avg_threshold"] == 75 and s["normal_floor"] == 25 and s["updated_by"] == "user")
conn = sqlite3.connect(os.path.join(TMP, "persist.db"))
check("設定存在 flags 表 settings 那一列、不在 meta", conn.execute("SELECT COUNT(*) FROM flags WHERE name = 'settings'").fetchone()[0] == 1
      and conn.execute("SELECT COUNT(*) FROM meta WHERE provider LIKE '%setting%'").fetchone()[0] == 0)
conn.execute("UPDATE flags SET info = '壞掉的' WHERE name = 'settings'")
conn.commit()
conn.close()
check("存壞了就回預設、不會出錯", U.settings_get() == DEF)

# ---- 舊資料庫：settings 那一列只有加滑桿前的三個鍵（沒有 Antigravity 兩鍵）
U._db.close()
OLD = os.path.join(TMP, "old.db")
U._conn(OLD)
U._db.execute("INSERT OR REPLACE INTO flags VALUES ('settings', ?, ?)",
              ('{"radar_avg_threshold": 75, "normal_floor": 25, "radar_floor": 15, "updated_ts": %r, "updated_by": "user"}' % T, T))
U._db.commit()
s = U.settings_get()
check("舊資料庫沒 Antigravity 兩鍵：當成 0，原本的 75／25 和更新者照讀", s == dict(DEF, radar_avg_threshold=75, normal_floor=25,
      updated_at=U._iso(T), updated_by="user"))
check("舊資料庫：view() 也一樣", U.view(now=T)["settings"] == s)
r = U.settings_set({"agy_claude_gpt_floor": 10}, "user", now=T + 10)
check("舊資料庫第一次改 Antigravity 一池：另一池存成 0、Codex 兩個沿用", r["ok"] and r["settings"] == dict(
      DEF, radar_avg_threshold=75, normal_floor=25, agy_claude_gpt_floor=10, updated_at=U._iso(T + 10), updated_by="user"))

# ---- 端點：權限、格式、限次
from starlette.testclient import TestClient  # noqa: E402

U._conn(os.path.join(TMP, "web.db"))
S.BOARD_REV_CHECK = False
web = TestClient(S.board_app)
ORIG = {"origin": "http://testserver"}
URL = "/board/api/usage/settings"
S._cookie_ok = lambda request: False
r = web.post(URL, headers=ORIG, json={"normal_floor": 20})
check("沒登入：401", r.status_code == 401)
check("沒登入改的不算數", U.settings_get()["normal_floor"] == 30)
S._cookie_ok = lambda request: True
check("沒有 Origin：403", web.post(URL, json={"normal_floor": 20}).status_code == 403)
check("別的網站的 Origin：403", web.post(URL, headers={"origin": "https://evil.example"}, json={"normal_floor": 20}).status_code == 403)
check("太大：413", web.post(URL, headers=ORIG, content=b"x" * 2000).status_code == 413)
check("被擋的都沒改到", U.settings_get() == DEF)
check("GET 不能改", web.get(URL).status_code != 200 and U.settings_get() == DEF)
r = web.post(URL, headers=ORIG, json={"radar_avg_threshold": 65, "normal_floor": 20})
j = r.json()
check("登入＋Origin：200、ok、回新的 settings", r.status_code == 200 and j["ok"] is True
      and j["settings"]["radar_avg_threshold"] == 65 and j["settings"]["normal_floor"] == 20 and j["settings"]["updated_by"] == "user")
check("成功回應不帶內部的 status 欄位", "status" not in j)
r = web.post(URL, headers=ORIG, json={"normal_floor": 101})
check("範圍錯：400、ok=false、繁中原因", r.status_code == 400 and r.json()["ok"] is False and "0% 到 100%" in r.json()["error"]
      and "status" not in r.json())
r = web.post(URL, headers=dict(ORIG, **{"content-type": "application/json"}), content=b"{not json")
check("不是 JSON：400", r.status_code == 400 and "JSON 物件" in r.json()["error"])
g = web.get("/board/api/usage").json()
check("GET /board/api/usage 有 settings 而且是剛存的", g["settings"]["radar_avg_threshold"] == 65 and g["settings"]["normal_floor"] == 20
      and g["settings"]["radar_floor"] == 15 and g["settings"]["updated_at"])
codes = [web.post(URL, headers=ORIG, json={"normal_floor": 25}).status_code for _ in range(17)]  # 前面已經算了 3 次
check("同一分鐘到第 20 次都 200", all(c == 200 for c in codes))
r = web.post(URL, headers=ORIG, json={"normal_floor": 30})
check("第 21 次：429、有 Retry-After、ok=false", r.status_code == 429 and r.json()["ok"] is False
      and int(r.headers.get("retry-after", "0")) > 0 and "status" not in r.json())
check("429 那次沒存", U.settings_get()["normal_floor"] == 25)

# MCP：usage_report 照收、多回 settings；沒有新增 MCP 工具
S.current_agent.set("claude-laptop")
got = S.usage_report({"source": "pc", "providers": [{"id": "codex", "windows": []}]})
check("MCP usage_report 照收、回傳帶 settings", got["providers"] == 1 and got["settings"]["normal_floor"] == 25)
check("沒有多出設定用的 MCP 工具", not any(hasattr(S, n) for n in ("usage_settings", "usage_settings_set", "usage_set_settings")))

# 端點：四鍵一起送（1 KB 上限放得下）、只送 Antigravity 一鍵、不認得的鍵 400
U._conn(os.path.join(TMP, "web4.db"))
four = {"radar_avg_threshold": 65, "normal_floor": 20, "agy_gemini_floor": 15, "agy_claude_gpt_floor": 45}
r = web.post(URL, headers=ORIG, json=four)
check("四鍵一起送：200、四個都存", r.status_code == 200 and all(r.json()["settings"][k] == v for k, v in four.items()))
r = web.post(URL, headers=ORIG, json={"agy_gemini_floor": 30})
check("端點只送 Gemini 池：200、Claude 和 GPT 池留著 45", r.status_code == 200 and r.json()["settings"]["agy_gemini_floor"] == 30
      and r.json()["settings"]["agy_claude_gpt_floor"] == 45)
r = web.post(URL, headers=ORIG, json={"agy_codex_floor": 30})
check("端點不認得的鍵：400、繁中原因", r.status_code == 400 and "不認得的欄位" in r.json()["error"])
r = web.post(URL, headers=ORIG, json={"agy_claude_gpt_floor": 101})
check("端點 Antigravity 範圍錯：400、原因寫池名", r.status_code == 400 and "Antigravity Claude 和 GPT 池保底" in r.json()["error"])
r = web.post(URL, headers=ORIG, json={"radar_floor": 40})
check("端點只送 radar_floor 40：200、其他鍵沿用", r.status_code == 200 and r.json()["settings"]["radar_floor"] == 40
      and r.json()["settings"]["agy_gemini_floor"] == 30 and r.json()["settings"]["normal_floor"] == 20)
r = web.post(URL, headers=ORIG, json={"radar_floor": "20"})
check("端點 radar_floor 送字串：400、要是整數", r.status_code == 400 and "要是整數" in r.json()["error"])
r = web.post(URL, headers=ORIG, json={"agy_claude_gpt_floor": 33.5})
check("端點小數 33.5：400、要是整數", r.status_code == 400 and "要是整數" in r.json()["error"])
g = web.get("/board/api/usage").json()
check("GET /board/api/usage 的 settings 五個門檻鍵都在", all(k in g["settings"] for k in
      ("radar_avg_threshold", "normal_floor", "radar_floor", "agy_gemini_floor", "agy_claude_gpt_floor")))
check("GET /board/api/usage 的 settings 有 Antigravity 兩鍵", g["settings"]["agy_gemini_floor"] == 30 and g["settings"]["agy_claude_gpt_floor"] == 45)
got = S.usage_report({"source": "pc", "providers": [{"id": "agy", "windows": []}]})
check("MCP usage_report 回傳的 settings 帶 Antigravity 兩鍵", got["settings"]["agy_gemini_floor"] == 30 and got["settings"]["agy_claude_gpt_floor"] == 45)

# 舊版頁面擋寫入
check("新端點在「擋舊版頁面」的名單裡", URL in S._REV_WRITE_PATHS)
S.BOARD_REV_CHECK = True
r = web.post(URL, headers=ORIG, json={"normal_floor": 25})
check("舊版頁面（沒帶版號）改門檻：409", r.status_code == 409)
S.BOARD_REV_CHECK = False

fail = [n for n, ok in results if not ok]
for n, ok in results:
    print(("通過 " if ok else "失敗 ") + n)
print(f"共 {len(results)} 項，失敗 {len(fail)} 項")
sys.exit(1 if fail else 0)
