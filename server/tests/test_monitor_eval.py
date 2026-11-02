"""離線測試：每晚考題的計分（monitor_eval.run_nightly），用假的 search_fn，不連任何真實服務、不打 embedding。
用法：python test_monitor_eval.py [<新版 server.py 目錄>]（run_all.py 會帶目錄；不帶就用 tests/ 的上一層）
檢查：全中、全不中、答案排第 3、答案被刪（existing_ids 與 exists_fn 兩種判法）、單題例外不中斷、連續錯提早停、
limit、純字串 id、大小寫、寬鬆答案（alt）、時間上限、真實題庫讀得進來且沒有金鑰樣子的字串。"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import time

NEW_DIR = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("monitor_eval", os.path.join(NEW_DIR, "monitor_eval.py"))
ME = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ME)

fails = []


def check(name, cond):
    print(("通過 " if cond else "失敗 ") + name)
    if not cond:
        fails.append(name)


def uuid_like(n):
    return f"{n:08x}-0000-4000-8000-{n:012x}"


ANS = [uuid_like(i) for i in range(1, 7)]        # 6 題的答案
OTHER = [uuid_like(100 + i) for i in range(10)]  # 無關的記憶
TMP = tempfile.mkdtemp(prefix="eval-")
QPATH = os.path.join(TMP, "questions.json")
qs = []
for i, a in enumerate(ANS):
    style = ("plain", "desc", "code")[i % 3]
    row = {"q": f"第 {i} 題的問法", "a": a, "style": style}
    if i == 5:
        row["alt"] = [OTHER[0]]  # 第 6 題有近似重複
    qs.append(row)
with open(QPATH, "w", encoding="utf-8") as f:
    json.dump({"user_id": "roundtable", "n": len(qs), "questions": qs}, f, ensure_ascii=False)
BY_Q = {q["q"]: q["a"] for q in qs}
CALLS = []


def fn_all_hit(query, user_id, limit):
    CALLS.append((query, user_id, limit))
    return [{"id": BY_Q[query]}] + [{"id": o} for o in OTHER[: limit - 1]]


def fn_none(query, user_id, limit):
    CALLS.append(query)
    return [{"id": o} for o in OTHER[:limit]]


def fn_third(query, user_id, limit):
    return [{"id": OTHER[1]}, {"id": OTHER[2]}, {"id": BY_Q[query]}, {"id": OTHER[3]}, {"id": OTHER[4]}]


REQUIRED = {"hit1", "hit5", "p50_ms", "p95_ms", "scored", "total", "skipped_missing", "started", "finished"}

# 1. 全中
CALLS.clear()
r = ME.run_nightly(fn_all_hit, QPATH, sleep_sec=0)
check("回傳含約定的 9 個欄位", REQUIRED <= set(r))
check("全中：hit1=hit5=1.0", r["hit1"] == 1.0 and r["hit5"] == 1.0)
check("全中：scored=total=6、沒跳過、沒錯", (r["scored"], r["total"], r["skipped_missing"], r["errors"]) == (6, 6, 0, 0))
check("search_fn 收到 user_id=roundtable、limit=5", all(c[1] == "roundtable" and c[2] == 5 for c in CALLS) and len(CALLS) == 6)
check("耗時是數字且 p95>=p50>=0", isinstance(r["p50_ms"], float) and r["p95_ms"] >= r["p50_ms"] >= 0)
check("started/finished 是 UTC ISO 字串", r["started"].endswith("+00:00") and r["finished"] >= r["started"])
check("by_style 三種問法都有、各自 hit5=1.0", set(r["by_style"]) == {"plain", "desc", "code"} and all(v["hit5"] == 1.0 for v in r["by_style"].values()))
check("沒給存在判法時 missing_check=none", r["missing_check"] == "none" and r["aborted"] is None and r["not_run"] == 0)

# 2. 全不中
r = ME.run_nightly(fn_none, QPATH, sleep_sec=0)
check("全不中：hit1=hit5=0.0、scored=6", r["hit1"] == 0.0 and r["hit5"] == 0.0 and r["scored"] == 6)
check("全不中：寬鬆也只有有 alt 的那題算對（1/6）", r["hit5_loose"] == round(1 / 6, 4))

# 3. 答案排第 3：hit1=0、hit5=1
r = ME.run_nightly(fn_third, QPATH, sleep_sec=0)
check("答案排第 3：hit1=0.0、hit5=1.0", r["hit1"] == 0.0 and r["hit5"] == 1.0)

# 4. 答案被刪：existing_ids 少兩個 → 跳過 2 題、不呼叫 search_fn
CALLS.clear()
alive = set(ANS[:4])
r = ME.run_nightly(fn_all_hit, QPATH, sleep_sec=0, existing_ids=alive)
check("existing_ids：跳過 2 題、計分 4 題、total 仍 6", (r["skipped_missing"], r["scored"], r["total"]) == (2, 4, 6))
check("被刪的題沒去搜（省流量）", len(CALLS) == 4 and all(BY_Q[c[0]] in alive for c in CALLS))
check("跳過的題不影響命中率", r["hit1"] == 1.0 and r["missing_check"] == "existing_ids")

# 5. exists_fn 判法，且 exists_fn 出錯算 errors、不算已刪
def exists(mid):
    if mid == ANS[2]:
        raise RuntimeError("資料庫暫時連不上")
    return mid != ANS[0]
r = ME.run_nightly(fn_all_hit, QPATH, sleep_sec=0, exists_fn=exists)
check("exists_fn：1 題已刪、1 題查存在出錯、4 題計分", (r["skipped_missing"], r["errors"], r["scored"]) == (1, 1, 4))

# 6. 單題例外：接住、記數、整輪繼續
def fn_boom(query, user_id, limit):
    if query == qs[1]["q"]:
        raise ConnectionError("EMB 閘道 502")
    return fn_all_hit(query, user_id, limit)
r = ME.run_nightly(fn_boom, QPATH, sleep_sec=0)
check("單題例外：errors=1、scored=5、hit1 仍 1.0、沒 aborted", (r["errors"], r["scored"], r["hit1"], r["aborted"]) == (1, 5, 1.0, None))

# 7. 連續錯太多題提早停
big = os.path.join(TMP, "big.json")
with open(big, "w", encoding="utf-8") as f:
    json.dump({"questions": [{"q": f"q{i}", "a": uuid_like(1000 + i)} for i in range(100)]}, f)
def fn_dead(query, user_id, limit):
    raise ConnectionError("掛了")
r = ME.run_nightly(fn_dead, big, sleep_sec=0)
check("全部出錯：提早停、errors=25、not_run=75、hit 都是 None", r["aborted"] is not None and r["errors"] == ME.MAX_CONSEC_ERRORS
      and r["not_run"] == 100 - ME.MAX_CONSEC_ERRORS and r["hit1"] is None and r["hit5"] is None and r["p50_ms"] is None)
check("提早停也有 finished", "finished" in r and r["scored"] == 0)

# 8. limit 只跑前 2 題
r = ME.run_nightly(fn_all_hit, QPATH, sleep_sec=0, limit=2)
check("limit=2：total=2、scored=2", r["total"] == 2 and r["scored"] == 2)

# 9. 結果是純字串 id、或大小寫不同，也算中
def fn_str(query, user_id, limit):
    return [BY_Q[query].upper()] + OTHER[:4]
r = ME.run_nightly(fn_str, QPATH, sleep_sec=0)
check("純字串 id、大寫也算中", r["hit1"] == 1.0)

# 10. 只看前 5：答案排第 6 不算
def fn_sixth(query, user_id, limit):
    return [{"id": o} for o in OTHER[:5]] + [{"id": BY_Q[query]}]
r = ME.run_nightly(fn_sixth, QPATH, sleep_sec=0)
check("答案排第 6：hit5=0", r["hit5"] == 0.0)

# 11. sleep_sec 有生效（6 題 × 0.02 秒至少 0.1 秒）
t0 = time.monotonic()
ME.run_nightly(fn_all_hit, QPATH, sleep_sec=0.02)
check("題間有睡 sleep_sec", time.monotonic() - t0 >= 0.1)

# 12. 時間上限：max_sec=0 → 第 1 題就停
r = ME.run_nightly(fn_all_hit, QPATH, sleep_sec=0, max_sec=0)
check("max_sec 超過就停、標 aborted", r["aborted"] and r["not_run"] == 6)

# 12b. 百分位用最近秩：1..100 的 p50=50、p95=95；單一值兩個都是它
check("百分位最近秩算法", ME._pct(list(range(1, 101)), 50) == 50 and ME._pct(list(range(1, 101)), 95) == 95
      and ME._pct([7.0], 50) == 7.0 and ME._pct([7.0], 95) == 7.0 and ME._pct([], 50) is None)

# 13. 壞題庫直接報錯，不悄悄算
bad = os.path.join(TMP, "bad.json")
with open(bad, "w", encoding="utf-8") as f:
    json.dump({"questions": [{"q": "沒有答案"}]}, f)
try:
    ME.run_nightly(fn_all_hit, bad, sleep_sec=0)
    check("缺答案的題庫要報錯", False)
except ValueError:
    check("缺答案的題庫要報錯", True)

# 13b. pick：只考指定題號（白天抽考用），超出範圍的略過
CALLS.clear()
r = ME.run_nightly(fn_all_hit, QPATH, sleep_sec=0, pick=[4, 0, 99, -1])
check("pick：只考第 0、4 題，超出範圍略過", r["total"] == 2 and r["scored"] == 2 and sorted(c[0] for c in CALLS) == ["第 0 題的問法", "第 4 題的問法"])

# 14. 真實題庫：1,020 題、答案都是 uuid、區是 roundtable、沒有金鑰樣子的值
real = ME.DEFAULT_QUESTIONS
if os.path.exists(real):
    uid, rq = ME.load_questions(real)
    UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    check("真實題庫 1,020 題、區 roundtable", len(rq) == 1020 and uid == "roundtable")
    check("真實題庫答案都是 uuid、問法三種各 340", all(UUID.match(q["a"]) for q in rq)
          and all(sum(1 for q in rq if q["style"] == s) == 340 for s in ("plain", "desc", "code")))
    check("真實題庫有 alt 的 36 題", sum(1 for q in rq if q.get("alt")) == 36)
    SECRET = re.compile(r"(?i)(sk-[a-z0-9]{16,}|ghp_[a-z0-9]{20,}|xox[abp]-[a-z0-9-]{10,}|bearer\s+[a-z0-9._-]{16,}|eyJ[a-zA-Z0-9_-]{20,})")
    check("真實題庫沒有金鑰樣子的字串", not any(SECRET.search(q["q"]) for q in rq))
    check("真實題庫每題文字 <= 500 字（memory_search 的上限）", all(len(q["q"]) <= 500 for q in rq))
    # 用真題庫跑一次假搜尋，確認整輪流程沒問題、速度夠快
    t0 = time.monotonic()
    r = ME.run_nightly(lambda q, u, l: [{"id": "00000000-0000-4000-8000-000000000000"}], real, sleep_sec=0)
    check("真題庫假跑：total=scored=1020、hit 0", r["total"] == 1020 and r["scored"] == 1020 and r["hit5"] == 0.0 and time.monotonic() - t0 < 5)
else:
    print("注意：找不到 eval/questions.json，略過真實題庫的檢查")

print("\n全部通過" if not fails else "\n有 %d 項失敗：%s" % (len(fails), "、".join(fails)))
sys.exit(1 if fails else 0)
