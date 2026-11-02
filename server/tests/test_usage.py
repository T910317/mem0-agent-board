"""額度頁資料（usage.py）：收報告、合併欄位、算速度、預估用完、每日 token、格式不對的不收。"""
import os
import sys
import tempfile

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import usage  # noqa: E402

fails = []


def check(name, cond):
    print(("通過 " if cond else "失敗 ") + name)
    if not cond:
        fails.append(name)


d = tempfile.mkdtemp(prefix="usage-")
usage._conn(os.path.join(d, "usage.db"))
H = 3600
t0 = 1_793_000_000.0                      # 某一期開始
reset = t0 + 7 * 24 * H                   # 一週後重置
win = lambda used, rs=reset: {"key": "weekly", "label": "每週", "used": used, "resets_at": rs, "minutes": 10080}

# 收集程式送 Codex：名稱、方案、每日 token；10 小時內從 10% 漲到 30%（每小時 2%）
for i in range(11):
    usage.put({"source": "pc", "providers": [{"id": "codex", "name": "Codex", "plan": "Pro", "billing": "月費",
               "windows": [win(10 + 2 * i)],
               "daily": [{"day": "2026-10-24", "input": 100, "output": 50, "cache_read": 850, "total": 1000, "requests": 3}]}]},
              "codex-pc", now=t0 + 24 * H + i * H)
v = usage.view(now=t0 + 34 * H + 60)
p = v["providers"][0]
w = p["windows"][0]
check("一家、一個窗", len(v["providers"]) == 1 and len(p["windows"]) == 1)
check("剩 70%", w["used"] == 30 and w["remaining"] == 70)
check("近 6 小時每小時 2%", w["rate_6h"] and abs(w["rate_6h"]["per_hour"] - 2) < 0.01)
check("近 24 小時只有 10 小時資料也算", w["rate_24h"] and w["rate_24h"]["span_hours"] == 10.0)
check("本期平均＝30%／34 小時", w["rate_period"] and abs(w["rate_period"]["per_hour"] - 30 / 34) < 0.01)
check("每小時 2% 要 35 小時用完，比重置早", w["runs_out_before_reset"] is True)
check("每日 token 有一天", len(p["daily"]) == 0 or p["daily"][0]["total"] == 1000)
check("不是舊資料", w["stale"] is False)

# Claude 串只送額度：不蓋掉收集程式寫的名稱
usage.put({"source": "pc", "providers": [{"id": "claude", "name": "Claude", "plan": "Pro", "windows": []}]}, "pc", now=t0 + 30 * H)
usage.put({"source": "claude-session", "providers": [{"id": "claude", "windows": [win(50)]}]}, "claude-laptop", now=t0 + 30 * H)
v = usage.view(now=t0 + 40 * H)
c = v["providers"][0]
check("Claude 排第一", c["id"] == "claude")
check("名稱沿用收集程式的", c["name"] == "Claude" and c["plan"] == "Pro")
check("只有一筆：速度算不出來（但本期平均算得出來）", c["windows"][0]["rate_6h"] is None and c["windows"][0]["rate_period"])
check("10 小時沒更新標舊資料", c["windows"][0]["stale"] is True)

# 重置之後：舊一期的點不拿來算速度
usage.put({"source": "pc", "providers": [{"id": "codex", "windows": [win(1, reset + 7 * 24 * H)]}]}, "pc", now=reset + H)
usage.put({"source": "pc", "providers": [{"id": "codex", "windows": [win(1.5, reset + 7 * 24 * H)]}]}, "pc", now=reset + 2 * H)
w = [p for p in usage.view(now=reset + 2 * H)["providers"] if p["id"] == "codex"][0]["windows"][0]
check("重置後只用新一期的點", len(w["points"]) == 2 and abs(w["rate_6h"]["per_hour"] - 0.5) < 0.01)
check("撐得到重置", w["runs_out_before_reset"] is False)
rs = [r for r in usage.view(now=reset + 2 * H)["resets"] if r["provider"] == "codex"]
check("重置紀錄一筆：時間是舊的重置時間、重置前 30%", len(rs) == 1 and rs[0]["at"] == usage._iso(reset)
      and rs[0]["used_before"] == 30 and rs[0]["used_after"] == 1 and rs[0]["approx"] is False and rs[0]["name"] == "Codex")

# 重置時間只是被往後改（還沒到、已用也沒掉）不算重置
usage.put({"providers": [{"id": "agy", "windows": [{"key": "pool", "used": 40, "resets_at": t0 + 10 * H, "minutes": 300}]}]}, now=t0)
usage.put({"providers": [{"id": "agy", "windows": [{"key": "pool", "used": 41, "resets_at": t0 + 20 * H, "minutes": 300}]}]}, now=t0 + H)
check("重置時間往後改不算重置", not [r for r in usage.view(now=t0 + H)["resets"] if r["provider"] == "agy"])

# 本地模型的電費
usage.put({"providers": [{"id": "lmstudio_mini", "name": "Mini", "power": {"kwh_today": 0.4, "ntd_today": 2, "kwh_7d": "x"}}]}, now=t0)
m = [p for p in usage.view(now=t0)["providers"] if p["id"] == "lmstudio_mini"][0]
check("電費有收、壞數字變空、標估算", m["power"]["kwh_today"] == 0.4 and m["power"]["kwh_7d"] is None and m["power"]["estimated"] is True)
check("沒帶即時瓦數就不存那些欄位", "watts_in" not in m["power"] and "efficiency" not in m["power"])
usage.put({"providers": [{"id": "pc", "power": {"kwh_today": 1.2, "watts_in": 152.3, "watts_out": 128, "efficiency": 0.841,
                                                   "temp_c": 999, "at": "2026-10-28T03:00:00Z"}}]}, now=t0)
m = [p for p in usage.view(now=t0)["providers"] if p["id"] == "pc"][0]
check("電源即時瓦數、效率（小數轉百分比）、溫度截上限、時間正規化",
      m["power"]["watts_in"] == 152.3 and m["power"]["watts_out"] == 128 and m["power"]["efficiency"] == 84.1
      and m["power"]["temp_c"] == 200 and m["power"]["at"] == "2026-10-28T03:00:00Z")
check("沒送過開關機狀態就是空", m["power_state"] is None)
usage.put({"providers": [{"id": "desk", "online": False, "power_state": "off"}]}, now=t0)
E = lambda: [p for p in usage.view(now=t0)["providers"] if p["id"] == "desk"][0]
check("關機狀態有存", E()["power_state"] == "off" and E()["online"] is False)
usage.put({"providers": [{"id": "desk", "note": "只改說明"}]}, now=t0)
check("沒帶連線欄位就沿用開關機狀態", E()["power_state"] == "off")
usage.put({"providers": [{"id": "desk", "power_state": "boom"}]}, now=t0)
check("亂寫的開關機狀態當沒說", E()["power_state"] is None)
usage.put({"providers": [{"id": "desk", "online": True, "power_state": "on"}]}, now=t0)
usage.put({"providers": [{"id": "desk", "online": False}]}, now=t0)
check("別的來源只回報連線時清掉舊狀態", E()["power_state"] is None)

# 格式不對
try:
    usage.put({"providers": []})
    check("空的 providers 要拒絕", False)
except ValueError:
    check("空的 providers 要拒絕", True)
got = usage.put({"providers": [{"id": "BAD ID!"}, {"id": "x", "windows": [{"key": "w", "used": "abc"}, {"key": "w2", "used": 150}]}]}, now=t0)
check("壞 id 跳過、壞百分比不收、超過 100 截成 100", got["skipped"] == ["BAD ID!"] and got["windows"] == 1)

# reset_radar（各站預測的 24 小時重置機率）：存在 meta 的 _radar 那一列，不當成卡片
check("還沒收過 reset_radar 時是 None", usage.view(now=t0)["reset_radar"] is None)
rr = {"Codex": {"prob24": 146, "as_of": "2026-10-28T03:00:00Z", "sources": [
    {"name": "A codex-reset.com", "url": "https://codex-reset.com", "prob24": 46, "detail": "站方模型" * 80,
     "as_of": "2026-10-28T03:00:00Z"},
    {"name": "B 壞網址", "url": "javascript:alert(1)", "prob24": -5},
    {"name": "", "url": "https://x"}, "不是物件"] + [{"name": f"S{i}", "prob24": i} for i in range(10)]},
    "bad key!": {"prob24": 10}, "agy": "不是物件"}
got = usage.put({"source": "board-ui-test", "reset_radar": rr, "providers": []}, "t", now=t0 + H)
v = usage.view(now=t0 + H)
r = v["reset_radar"]
check("只帶 reset_radar、providers 空陣列也收", got["reset_radar"] == ["codex"] and got["providers"] == 0)
check("鍵轉小寫、壞鍵和不是物件的丟掉", r is not None and list(r) == ["codex"])
cx = r["codex"]
check("機率夾在 0–100", cx["prob24"] == 100 and cx["sources"][0]["prob24"] == 46 and cx["sources"][1]["prob24"] == 0)
check("最多 6 站（壞的先算進名額再丟）", len(cx["sources"]) <= 6 and [s["name"] for s in cx["sources"]][:2] == ["A codex-reset.com", "B 壞網址"])
check("網址只收 http/https", cx["sources"][0]["url"] == "https://codex-reset.com" and cx["sources"][1]["url"] is None)
check("說明截長度、時間正規化", len(cx["sources"][0]["detail"]) == 160 and cx["as_of"] == "2026-10-28T03:00:00Z"
      and cx["received"] == usage._iso(t0 + H))
check("providers 清單沒有 _radar", all(p["id"] != "_radar" for p in v["providers"]))
usage.put({"source": "pc", "providers": [{"id": "codex", "windows": [win(20)]}]}, "pc", now=t0 + 2 * H)
check("沒帶 reset_radar 的回報不清掉舊值", usage.view(now=t0 + 2 * H)["reset_radar"]["codex"]["prob24"] == 100)
usage.put({"reset_radar": {"claude": {"prob24": 5}}}, now=t0 + 3 * H)
r = usage.view(now=t0 + 3 * H)["reset_radar"]
check("逐家合併：新增 claude、codex 沿用", r["claude"]["prob24"] == 5 and r["codex"]["prob24"] == 100)
usage.put({"reset_radar": {"codex": {"prob24": "abc", "sources": []}}}, now=t0 + 4 * H)
check("整家壞掉不蓋掉舊值", usage.view(now=t0 + 4 * H)["reset_radar"]["codex"]["prob24"] == 100)
try:
    usage.put({"reset_radar": [1, 2]})
    check("reset_radar 不是物件要拒絕", False)
except ValueError:
    check("reset_radar 不是物件要拒絕", True)
got = usage.put({"providers": [{"id": "_radar", "name": "冒充"}]}, now=t0 + 5 * H)
check("provider id 不能用 _radar 冒充", got["skipped"] == ["_radar"]
      and usage.view(now=t0 + 5 * H)["reset_radar"]["codex"]["prob24"] == 100)

# 每小時 token（hourly）：收集程式在每家報告上多帶，存 hourly 表，view() 每家多一欄、只給最近 14 天、由舊到新
D = 86400
hn = int(t0 // H * H) + 200 * D                         # 這一段測試用的「現在」（整點，跟前面測試的時間拉開）
HH = lambda: [p for p in usage.view(now=hn + 100)["providers"] if p["id"] == "hh"][0]["hourly"]
check("還沒送過 hourly 時是空陣列", [p for p in usage.view(now=hn)["providers"] if p["id"] == "codex"][0]["hourly"] == [])
got = usage.put({"providers": [{"id": "hh", "name": "每小時測試", "hourly": [
    [usage._iso(hn - 2 * H), 300], {"hour": usage._iso(hn - 3 * H), "total": 100}, [usage._iso(hn - H), 200.0], [usage._iso(hn), 400]]}]},
    now=hn + 100)
check("兩種格式都收、回傳收了 4 筆", got["hourly"] == 4)
check("hourly 由舊到新、時間是 UTC 字串、數字對",
      HH() == [[usage._iso(hn - 3 * H), 100], [usage._iso(hn - 2 * H), 300], [usage._iso(hn - H), 200], [usage._iso(hn), 400]])
usage.put({"providers": [{"id": "hh", "hourly": [[usage._iso(hn - 2 * H), 999], [usage._iso(hn + H), 1]]}]}, now=hn + 100)
check("同一小時重送就覆蓋、其他小時不動", [n for _, n in HH()] == [100, 999, 200, 400, 1])
usage.put({"providers": [{"id": "hh", "hourly": [[usage._iso(hn + H), 0]]}]}, now=hn + 100)
check("重送成 0 也要覆蓋", HH()[-1] == [usage._iso(hn + H), 0])
usage.put({"providers": [{"id": "hh", "hourly": [[hn - 5 * H + 1234, 50], [usage._iso(hn - 6 * H + 3599), 60],
                                                  ["2026-10-01T05:59:59+00:00", 70]]}]}, now=hn + 100)
hs = dict(HH())
check("時間不是整點就 floor 到那一小時的起點（epoch 秒、ISO 字串都一樣）",
      hs[usage._iso(hn - 5 * H)] == 50 and hs[usage._iso(hn - 6 * H)] == 60 and usage._iso(hn - 5 * H + 1234) not in hs)
bad = usage.put({"providers": [{"id": "hh", "hourly": [
    [usage._iso(hn - 10 * H), -5], [usage._iso(hn - 11 * H), "abc"], [usage._iso(hn - 12 * H), None], [usage._iso(hn - 13 * H), True],
    [usage._iso(hn - 14 * H), float("nan")], [usage._iso(hn - 15 * H), float("inf")], ["不是時間", 5], [None, 5], "亂寫", 123, [1],
    {"hour": usage._iso(hn - 16 * H)}, [usage._iso(hn - 17 * H), "42"], [usage._iso(hn - 18 * H), 7]]}]}, now=hn + 100)
hs = dict(HH())
check("壞的那筆跳過、不整筆拒絕（負數、非數字、NaN、無限大、壞時間、缺欄位）", bad["hourly"] == 2 and hs[usage._iso(hn - 17 * H)] == 42
      and hs[usage._iso(hn - 18 * H)] == 7 and all(usage._iso(hn - k * H) not in hs for k in range(10, 17)))
n_before = len(HH())
usage.put({"providers": [{"id": "hh", "windows": [win(5)], "daily": [{"day": "2026-10-24", "total": 5}]}]}, now=hn + 200)
check("沒帶 hourly 的報告不清掉舊的每小時資料", len(HH()) == n_before)
usage.put({"providers": [{"id": "hh", "hourly": []}]}, now=hn + 200)
check("帶空的 hourly 也不清舊值", len(HH()) == n_before)
usage.put({"providers": [{"id": "hh", "hourly": "壞掉"}]}, now=hn + 200)
check("hourly 不是陣列就當沒帶", len(HH()) == n_before)

# 只輸出最近 14 天；資料庫裡留 60 天
old14 = hn - 14 * D - H                                # 14 天又 1 小時前：不輸出
edge14 = hn - 14 * D                                   # 剛好 14 天前的整點：輸出
usage.put({"providers": [{"id": "hh", "hourly": [[usage._iso(old14), 11], [usage._iso(edge14), 22], [usage._iso(hn - 30 * D), 33]]}]},
          now=hn + 300)
hs = dict(HH())
check("超過 14 天的不輸出、剛好 14 天的輸出", usage._iso(old14) not in hs and hs[usage._iso(edge14)] == 22 and usage._iso(hn - 30 * D) not in hs)
check("輸出一定由舊到新", [t for t, _ in HH()] == sorted(t for t, _ in HH()))
check("30 天前的還留在資料庫（只是不輸出）", usage._conn().execute("SELECT total FROM hourly WHERE provider = 'hh' AND hour = ?", (hn - 30 * D,)).fetchone()[0] == 33)
usage.put({"providers": [{"id": "hh", "hourly": [[usage._iso(hn - 59 * D), 44]]}]}, now=hn + 400)
now2 = hn + 61 * D                                     # 61 天後再收一份：超過 60 天的順手刪掉
usage.put({"providers": [{"id": "hh", "hourly": [[usage._iso(now2 - H), 55]]}]}, now=now2)
left = [r[0] for r in usage._conn().execute("SELECT hour FROM hourly WHERE provider = 'hh' ORDER BY hour")]
check("超過 60 天的被刪、沒超過的留著", left == [now2 - H])
check("剛好 61 天前的 hn 也被刪了", hn not in left and hn - 30 * D not in left)
usage.put({"providers": [{"id": "hh", "hourly": [[usage._iso(now2 - 61 * D), 66]]}]}, now=now2)
check("收進來就已經超過 60 天的不存", usage._conn().execute("SELECT COUNT(*) FROM hourly WHERE hour = ?", (now2 - 61 * D,)).fetchone()[0] == 0)

# 單次最多收 60*24 筆；每家各存各的；不能冒充 _radar
big = [[usage._iso(now2 - k * H), k + 1] for k in range(1500)]
got = usage.put({"providers": [{"id": "hbig", "hourly": big}, {"id": "hh2", "hourly": [[usage._iso(now2 - H), 7]]}]}, now=now2 + 10)
check("單次最多收 1440 筆、多的截掉", got["hourly"] == 1440 + 1)
n_big = usage._conn().execute("SELECT COUNT(*) FROM hourly WHERE provider = 'hbig'").fetchone()[0]
check("庫裡存了 1440 筆（60 天份）", n_big == 1440)
vb = {p["id"]: p for p in usage.view(now=now2 + 10)["providers"]}
check("view 只給 14 天份（336 小時）、每家各自一份", len(vb["hbig"]["hourly"]) == 14 * 24 + 1 and vb["hh2"]["hourly"] == [[usage._iso(now2 - H), 7]])
got = usage.put({"providers": [{"id": "_radar", "hourly": [[usage._iso(now2), 9]]}]}, now=now2 + 10)
check("_radar 冒充仍被擋、不寫進 hourly 表",
      got["skipped"] == ["_radar"] and usage._conn().execute("SELECT COUNT(*) FROM hourly WHERE provider = '_radar'").fetchone()[0] == 0)

# 舊資料庫（沒有 hourly 表）要能自動補表，而且不動到既有資料
import sqlite3  # noqa: E402
old_db = os.path.join(d, "old.db")
oc = sqlite3.connect(old_db)
oc.execute("CREATE TABLE meta (provider TEXT PRIMARY KEY, info TEXT, ts REAL, source TEXT)")
oc.execute("INSERT INTO meta VALUES ('codex', '{\"name\": \"舊的\"}', 1.0, 'pc')")
oc.commit()
oc.close()
usage._conn(old_db)
tables = {r[0] for r in usage._conn().execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
check("舊資料庫連上就自動補 hourly 表", "hourly" in tables)
check("舊資料庫的既有資料沒被動到", usage._conn().execute("SELECT info FROM meta WHERE provider = 'codex'").fetchone()[0] == '{"name": "舊的"}'
      and usage.view(now=hn)["providers"][0]["hourly"] == [])

print("全部通過" if not fails else f"失敗 {len(fails)} 項")
sys.exit(1 if fails else 0)
