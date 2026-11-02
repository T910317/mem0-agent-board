"""離線測試：白天抽考（monitor.py 的 eval_spot，random／idle 兩種）。
用假時鐘、假的 calls_raw／emb_raw、真的 monitor_eval（搜尋函式是假的），不連任何真實服務、不打 embedding。
檢查：預設關著；預定表只落在 06:00–23:45、每種 2～4 次、彼此隔 60 分鐘、重啟不重抽；離峰不碰；
random 到點不管忙不忙都考、記下當時多忙；idle 忙就等、閒了才考、等太久記 skipped；每次 20 題、不重複；
series() 的 eval_spot 數列與摘要；和每晚考題不會同時跑。
用法：python test_monitor_spot.py [<monitor.py 所在目錄>]"""
import datetime as dt
import json
import os
import random
import sys
import tempfile
import threading

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = tempfile.mkdtemp(prefix="spot-")
os.environ["MONITOR_DIR"] = os.path.join(ROOT, "metrics")
os.environ["TZ_OFFSET_HOURS"] = "8"
for k in list(os.environ):
    if k.startswith("MONITOR_SPOT_"):
        del os.environ[k]
sys.path.insert(0, SRC)
import monitor  # noqa: E402
import monitor_eval  # noqa: E402

FAILS = []


def check(name, ok):
    print(("通過 " if ok else "失敗 ") + name)
    if not ok:
        FAILS.append(name)


TZ = dt.timezone(dt.timedelta(hours=8))
DAY0 = int(dt.datetime(2026, 10, 28, tzinfo=TZ).timestamp())   # 當地 10/28 00:00


def at(h, m=0, day=0):
    return DAY0 + day * 86400 + h * 3600 + m * 60


# 假題庫 100 題
QPATH = os.path.join(ROOT, "questions.json")
qs = [{"q": f"問題{i}", "a": f"{i:08x}-0000-4000-8000-{i:012x}", "style": ("plain", "desc", "code")[i % 3]} for i in range(100)]
with open(QPATH, "w", encoding="utf-8") as f:
    json.dump({"user_id": "roundtable", "questions": qs}, f, ensure_ascii=False)
ANS = {q["q"]: q["a"] for q in qs}
ASKED = []


def search(query, uid, limit):
    ASKED.append(query)
    i = int(query[2:])
    return [{"id": ANS[query]}] if i % 2 == 0 else [{"id": "x"}, {"id": ANS[query]}]  # 偶數題第 1 名、奇數題第 2 名


clock = {"t": at(5, 50)}
col = monitor.Collector(db_path=os.path.join(ROOT, "m.db"), now=lambda: clock["t"])
col.rand = random.Random(7)
col.search_fn = search
col.questions = QPATH
col.eval_setup_fn = lambda: {"existing_ids": set(ANS.values()), "log": print}


def wait_threads():
    for th in threading.enumerate():
        if th.name.startswith("monitor-eval"):
            th.join(20)


def busy(n_calls, n_emb, now):
    c = col.db()
    c.execute("DELETE FROM calls_raw")
    c.execute("DELETE FROM emb_raw")
    c.executemany("INSERT INTO calls_raw VALUES (?, 'a', 'memory_search', 10, 1, '', 0)", [(now - 30,)] * n_calls)
    c.executemany("INSERT INTO calls_raw VALUES (?, 'monitor-eval', 'memory_search', 10, 1, '', 1)", [(now - 30,)] * 50)  # 輪詢不算
    c.executemany("INSERT INTO emb_raw VALUES (?, 5, 200, 'x', 1, 1)", [(now - 30,)] * n_emb)
    c.commit()


def rows():
    return col.db().execute("SELECT kind, status, n, load_calls, load_emb, hit1, hit5, scored, started, planned FROM eval_spot ORDER BY id").fetchall()


# 1. 預設關著
col.maybe_spot(at(10))
check("沒設 MONITOR_SPOT_ON：不排、不考", col._state("eval_spot_plan") is None and not rows())
os.environ["MONITOR_SPOT_ON"] = "1"
os.environ["MONITOR_SPOT_SLEEP"] = "0"  # 測試不用等

# 2. 離峰 05:50 不碰（連預定表都不排）
col.maybe_spot(at(5, 50))
check("離峰 05:50：不排、不考", col._state("eval_spot_plan") is None and not rows())
os.environ["MONITOR_SPOT_WINDOW"] = "00:00-23:59"
check("時段設錯成 00:00 開始也會被夾回 06:00、最晚 23:45", monitor.Collector.spot_cfg()["lo"] == 360 and monitor.Collector.spot_cfg()["hi"] == 1425)
del os.environ["MONITOR_SPOT_WINDOW"]

# 3. 預定表
clock["t"] = at(6, 0)
col.maybe_spot(clock["t"])
plan = col._state("eval_spot_plan")
slots = plan["slots"]
kinds = [s["kind"] for s in slots]
ts = sorted(s["at"] for s in slots)
check("預定表：當天、random 與 idle 各 2～4 次", plan["day"] == "2026-10-28" and 2 <= kinds.count("random") <= 4 and 2 <= kinds.count("idle") <= 4)
check("預定表：全部落在 06:00–23:31、彼此隔 >= 60 分鐘", all(at(6) <= t < at(23, 31) for t in ts) and all(b - a >= 3600 - 59 for a, b in zip(ts, ts[1:])))
col.db().commit()  # 正式環境由 tick() 結尾 commit
col2 = monitor.Collector(db_path=os.path.join(ROOT, "m.db"), now=lambda: clock["t"])
check("重啟（新 Collector 同一個 db）不重抽", col2.spot_plan(clock["t"], col2.spot_cfg()) == plan)
# 多抽幾天看範圍
okr = True
for d in range(1, 40):
    p = col.spot_plan(at(6, 0, d), col.spot_cfg())
    tt = sorted(s["at"] for s in p["slots"])
    okr &= all(at(6, 0, d) <= t < at(23, 31, d) for t in tt) and 4 <= len(tt) <= 8
check("連抽 39 天：每天 4～8 次、全在 06:00–23:31", okr)
# 後面的行為測試用固定的預定表（抽出來的順序每次不同，固定才測得準）
plan = {"day": "2026-10-28", "slots": [{"kind": "random", "at": at(9, 0), "state": "wait"}, {"kind": "idle", "at": at(11, 0), "state": "wait"},
                                       {"kind": "idle", "at": at(14, 0), "state": "wait"}, {"kind": "random", "at": at(20, 0), "state": "wait"}]}
slots = plan["slots"]
kinds = [x["kind"] for x in slots]
col._set_state("eval_spot_plan", plan)
r_slot, i_slot = slots[0], slots[1]

# 4. random：到點前不考；到點時很忙也照考
clock["t"] = r_slot["at"] - 120
busy(0, 0, clock["t"])
col.maybe_spot(clock["t"])
check("random 還沒到點：不考", not [r for r in rows() if r[0] == "random"])
clock["t"] = r_slot["at"] + 30
busy(80, 200, clock["t"])
ASKED.clear()
col.maybe_spot(clock["t"])
wait_threads()
rr = [r for r in rows() if r[0] == "random"]
check("random 到點、很忙也照考，記下當時人用 80 次、EMB 200 次", len(rr) == 1 and rr[0][1] == "done" and rr[0][3] == 80 and rr[0][4] == 200)
check("random 考 20 題、題目不重複、hit@1 是偶數題比例、hit@5=1", rr[0][2] == 20 and len(ASKED) == 20 and len(set(ASKED)) == 20
      and rr[0][7] == 20 and rr[0][6] == 1.0 and abs(rr[0][5] - sum(1 for q in ASKED if int(q[2:]) % 2 == 0) / 20) < 1e-9)
check("考完 _eval_running 放回 False", not col._eval_running)

# 5. idle：到點但很忙 → 等；閒了才考
clock["t"] = i_slot["at"] + 60
busy(11, 0, clock["t"])
col.maybe_spot(clock["t"])
check("idle 到點但人用 11 次（門檻 10）：先不考", not [r for r in rows() if r[0] == "idle"])
busy(0, 31, clock["t"])
col.maybe_spot(clock["t"])
check("idle 到點但 EMB 31 次（門檻 30）：先不考", not [r for r in rows() if r[0] == "idle"])
clock["t"] = i_slot["at"] + 600
busy(3, 5, clock["t"])
col.maybe_spot(clock["t"])
wait_threads()
ir = [r for r in rows() if r[0] == "idle"]
check("idle 閒下來（人用 3、EMB 5）就考，記下當時忙碌度", len(ir) == 1 and ir[0][1] == "done" and ir[0][3] == 3 and ir[0][4] == 5 and ir[0][8] > ir[0][9])

# 6. idle 等太久 → skipped
rest = [s for s in col._state("eval_spot_plan")["slots"] if s["kind"] == "idle" and s["state"] == "wait"]
if rest:
    s2 = rest[0]
    clock["t"] = s2["at"] + 181 * 60
    busy(50, 0, clock["t"])
    col.maybe_spot(clock["t"])
    sk = [r for r in rows() if r[1] == "skipped"]
    check("idle 等超過 180 分鐘都很忙：記一筆 skipped、不考", len(sk) == 1 and sk[0][0] == "idle" and sk[0][3] == 50 and sk[0][5] is None)
else:
    check("idle 至少 2 次（才測得到 skipped）", False)

# 7. 和每晚考題互斥：_eval_running 時不開考
p = col._state("eval_spot_plan")
waiting = [s for s in p["slots"] if s["state"] == "wait"]
if waiting:
    clock["t"] = max(s["at"] for s in waiting) + 10
    busy(0, 0, clock["t"])
    n0 = len(rows())
    col._eval_running = True
    col.maybe_spot(clock["t"])
    check("別的考試在跑：不開考", len(rows()) == n0)
    col._eval_running = False

# 8. series 輸出
col._cache.clear()
clock["t"] = at(23, 50)
s = col.series("24h")
es = s["eval_spot"]
check("series.eval_spot 欄位齊、長度一致", set(es) == {"t", "kind", "hit1", "hit5", "p50_ms", "scored", "load_calls", "load_emb"}
      and len({len(v) for v in es.values()}) == 1)
check("series.eval_spot 只列考完的、照時間排、kind 標 random／idle", es["kind"][:2] in (["random", "idle"], ["idle", "random"])
      and es["t"] == sorted(es["t"]) and len(es["t"]) == len([r for r in rows() if r[1] == "done"]))
sm = s["summary"]["eval_spot"]
check("摘要：兩種都有 at／hit1／今天預定、完成、略過", set(sm) == {"random", "idle"} and sm["random"]["today_done"] >= 1
      and sm["idle"]["today_skipped"] == (1 if rest else 0) and sm["random"]["today_planned"] == kinds.count("random")
      and sm["idle"]["at"].endswith("Z"))
check("eval_daily 還在、沒被影響", s["eval_daily"] == {"t": [], "hit1": [], "hit5": [], "p50_ms": [], "scored": []})

# 9. 真題庫成本估算：20 題、題間 1 秒、假搜尋 0.9 秒（照 VM119 每晚考題實測 p50 約 870 毫秒）
real = monitor_eval.DEFAULT_QUESTIONS
if os.path.exists(real):
    import time as _t
    _, rq = monitor_eval.load_questions(real)
    pick = sorted(random.Random(1).sample(range(len(rq)), 20))
    t0 = _t.monotonic()
    r = monitor_eval.run_nightly(lambda q, u, l: (_t.sleep(0.05), [{"id": "x"}])[1], real, 0.05, pick=pick)
    check("真題庫抽 20 題能跑、total=20", r["total"] == 20 and r["scored"] == 20)
    print(f"注意：真題庫 20 題假跑 {(_t.monotonic() - t0):.1f} 秒（搜尋 0.05＋停 0.05）；正式估 20×(0.9＋1)≈38 秒")

print("全部通過" if not FAILS else "有 %d 項失敗" % len(FAILS))
sys.exit(1 if FAILS else 0)
