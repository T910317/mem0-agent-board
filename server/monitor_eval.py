"""每晚考題（監控網頁的「命中率乙」）：拿固定的 1,020 題題庫，走正式的搜尋路徑一題一題問，
算 hit@1、hit@5 和搜尋耗時 p50／p95，結果交給後端寫進 metrics.db 的 eval_daily。

題庫 eval/questions.json（由 bench/rerank-stress-20261021 的千題轉出，對 2026-10-21 匯出的記憶）：
    {"user_id": "roundtable", "n": 1020, "questions": [{"q": 題目, "a": 答案記憶 id, "style": plain|desc|code, "alt": [近似重複的 id]}]}
    每題的標準答案是「出題的那一則記憶」的 id（嚴格算法，報告主表用的就是這個）；36 題另有 alt（內容近似重複、
    cosine>=0.9 的另一則），寬鬆算法也算對，只放在 hit5_loose 給人參考，主指標不用。

後端怎麼包 search_fn（server.py「# ── 監控 ──」那段照這樣寫）：
    import monitor_eval

    def _eval_search(query: str, user_id: str, limit: int) -> list:
        # 題庫全部在 roundtable 區，也就是 server.USER_ID；memory_search 預設就只搜這一區（不帶 messages、不帶篩選），
        # 所以 user_id 只拿來核對，不用傳進去。memory_search 是 @mcp.tool() 包的，FastMCP 會把原函式原樣回傳，直接呼叫就是正式路徑：
        # STORE.search（向量）＋ _kw_scores（關鍵字）＋ TYPE_ADJ（類型微調），回傳每則精簡版 dict，裡面有 "id"。
        if user_id != USER_ID:
            raise ValueError(f"題庫的區 {user_id!r} 不是 {USER_ID!r}")
        return memory_search(query, limit=limit)

    # 「答案已不存在」怎麼判斷：開跑前列一次整區拿現存 id 集合（一次呼叫，不用 1,020 次 get）
    ids = {m.get("id") for m in _list_uid(USER_ID)}
    # 或者給 exists_fn=lambda mid: STORE.get(mid) is not None（每題多一次 get，較慢但不用先列整區）
    tok = current_agent.set("monitor-eval")   # 呼叫紀錄看得出是考題在跑，不是人用；後端統計時可把這個 agent 排除
    try:
        r = monitor_eval.run_nightly(_eval_search, monitor_eval.DEFAULT_QUESTIONS, sleep_sec=0.2, existing_ids=ids)
    finally:
        current_agent.reset(tok)
    # r 的欄位見 run_nightly 的說明；hit1／hit5 是 0～1 的比例，沒有可計分題時是 None（API 回 null，網頁畫斷線）

    Dockerfile 的 COPY 要多帶 monitor_eval.py 和 eval/questions.json（現在只 COPY server.py experience_store.py store.py）。
    要在背景執行緒跑（當地 00:00–06:00 之間挑一個時間），不要擋到工具呼叫；run_nightly 本身是同步、單執行緒、一次一題。

節流與時間估算：
    一次一題、題間 sleep_sec（預設 0.2 秒）。正式 memory_search 每題約 0.1～0.3 秒（10/19 量過中位 95.5 ms；
    關鍵字比對每 30 秒重列一次整區，約 3,000 則時多 0.1～0.2 秒），所以 1,020 題約 1,020 × (0.2 + 0.2) ≈ 7 分鐘；
    就算每題慢到 1 秒也只要 20 分鐘，遠低於 6 小時。max_sec（預設 5 小時）是保險：超過就停、結果標 aborted。
    EMB 主節點每題只算一句向量，跟一個人慢慢搜一樣，不會滿載。

錯誤處理：
    每一題的 search_fn／exists_fn 例外都接住、記進 errors、該題不計分，整輪不中斷；
    連續錯 MAX_CONSEC_ERRORS 題（服務多半掛了）就提早停，省得整晚打一台壞掉的服務。
"""
import datetime as dt
import json
import math
import os
import time
import traceback

DEFAULT_QUESTIONS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval", "questions.json")
TOP_K = 5                 # 每題向 search_fn 要幾則：hit@5 只看前 5
MAX_CONSEC_ERRORS = 25    # 連續錯這麼多題就提早停
STYLES = ("plain", "desc", "code")


def load_questions(path: str) -> tuple:
    """讀題庫，回 (user_id, [題目 dict])。題目至少要有 q 和 a；壞掉的題直接丟 ValueError（寧可整輪不跑也不要悄悄算錯）。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):  # 也接受純陣列
        data = {"questions": data}
    qs = data.get("questions") or []
    uid = data.get("user_id") or "roundtable"
    for i, q in enumerate(qs):
        if not isinstance(q, dict) or not q.get("q") or not q.get("a"):
            raise ValueError(f"題庫第 {i} 題缺 q 或 a：{q!r}")
    return uid, qs


def _norm(x) -> str:
    """結果的 id：接受 dict（取 "id"）或純字串；統一小寫、去空白，比對才不會因大小寫失誤。"""
    if isinstance(x, dict):
        x = x.get("id")
    return str(x or "").strip().lower()


def rank_of(result_ids: list, targets: set) -> int:
    """答案在結果裡排第幾（從 1 起算）；沒出現回 0。"""
    for i, rid in enumerate(result_ids, 1):
        if rid in targets:
            return i
    return 0


def _pct(vals: list, p: float):
    """最近秩百分位（跟壓測報告同一種算法）；空的回 None。"""
    if not vals:
        return None
    s = sorted(vals)
    k = min(len(s) - 1, max(0, math.ceil(p / 100.0 * len(s)) - 1))
    return round(s[k], 1)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def run_nightly(search_fn, questions_path: str = DEFAULT_QUESTIONS, sleep_sec: float = 0.2, limit=None, *,
                exists_fn=None, existing_ids=None, max_sec: float = 5 * 3600, log=None, pick=None) -> dict:
    """跑一輪考題。
    search_fn(query, user_id, limit) -> list[dict]：由後端包好的正式搜尋，每則至少有 "id"（也接受純 id 字串）。
    questions_path：題庫路徑。sleep_sec：題與題之間睡幾秒。limit：只跑前幾題（手動冒煙用），None＝全部。
    exists_fn(mid) -> bool 或 existing_ids（現存 id 的集合）：判斷答案記憶還在不在；都不給就不跳過任何題。
    max_sec：整輪時間上限，超過就停。log(msg)：進度訊息的出口（例如 print），None＝不印。
    pick：只考題庫裡這幾題（題號清單，從 0 起算；超出範圍的略過），給白天抽考用；None＝照 limit。先套 limit 再套 pick。

    回傳 dict：
      hit1、hit5    前 1／前 5 名命中的比例（0～1，四位小數；沒有可計分題時 None）
      p50_ms、p95_ms 每題搜尋耗時（只算 search_fn 那一段，不含 sleep；沒有成功的搜尋時 None）
      scored        可計分題數（搜尋成功且答案還存在）
      total         這輪嘗試的題數（limit 截後）
      skipped_missing 答案記憶已不存在而跳過的題數（不計分、不搜尋）
      started、finished  UTC 時間（ISO 8601）
      其餘參考欄位：errors（出錯的題數）、hit1_n／hit5_n（命中題數）、hit5_loose（近似重複也算對的前 5 命中比例）、
      by_style（各問法的 hit1／hit5／n）、not_run（提早停而沒跑到的題數）、aborted（提早停的原因，正常是 None）、
      missing_check（怎麼判斷答案不存在：existing_ids／exists_fn／none）、elapsed_sec、questions（題庫路徑）、user_id。
    """
    started = _now()
    t_start = time.monotonic()
    uid, qs = load_questions(questions_path)
    if limit is not None:
        qs = qs[: max(0, int(limit))]
    if pick is not None:
        qs = [qs[i] for i in pick if isinstance(i, int) and 0 <= i < len(qs)]
    if existing_ids is not None:
        existing = {str(i).strip().lower() for i in existing_ids if i}
        check = "existing_ids"
    elif exists_fn is not None:
        existing = None
        check = "exists_fn"
    else:
        existing = None
        check = "none"

    ranks = []          # (style, 嚴格名次, 寬鬆名次)
    times = []          # 每題 search_fn 耗時（毫秒）
    skipped = errors = consec = 0
    aborted = None
    done = 0
    for n, q in enumerate(qs, 1):
        if time.monotonic() - t_start >= max_sec:
            aborted = f"超過時間上限 {max_sec:.0f} 秒，停在第 {n - 1} 題"
            break
        if consec >= MAX_CONSEC_ERRORS:
            aborted = f"連續錯 {consec} 題，服務可能掛了，停在第 {n - 1} 題"
            break
        done = n
        ans = _norm(q["a"])
        alts = {_norm(x) for x in (q.get("alt") or []) if x}
        style = q.get("style") or "plain"
        qid = f"{ans[:8]}-{style}"
        try:
            if existing is not None:
                missing = ans not in existing
            elif exists_fn is not None:
                missing = not exists_fn(q["a"])
            else:
                missing = False
        except Exception as e:  # 查存在與否本身出錯：當這題出錯，不當作「已刪」
            errors += 1
            consec += 1
            if log:
                log(f"考題 {qid} 查答案是否存在時出錯：{type(e).__name__}: {e}")
            continue
        if missing:
            skipped += 1
            continue
        t0 = time.perf_counter()
        try:
            res = search_fn(q["q"], uid, TOP_K)
            ms = (time.perf_counter() - t0) * 1000.0
            ids = [_norm(r) for r in (res or [])][:TOP_K]
        except Exception as e:
            errors += 1
            consec += 1
            if log:
                log(f"考題 {qid} 搜尋出錯：{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")
            if sleep_sec > 0:
                time.sleep(sleep_sec)
            continue
        consec = 0
        times.append(ms)
        ranks.append((style, rank_of(ids, {ans}), rank_of(ids, {ans} | alts)))
        if log and n % 200 == 0:
            log(f"考題進度 {n}/{len(qs)}，目前 hit@5 {sum(1 for _, r, _ in ranks if 0 < r <= 5)}/{len(ranks)}")
        if sleep_sec > 0:
            time.sleep(sleep_sec)

    scored = len(ranks)
    hit1_n = sum(1 for _, r, _ in ranks if r == 1)
    hit5_n = sum(1 for _, r, _ in ranks if 0 < r <= 5)
    loose5 = sum(1 for _, _, r in ranks if 0 < r <= 5)
    by_style = {}
    for s in STYLES:
        rs = [r for st, r, _ in ranks if st == s]
        if rs:
            by_style[s] = {"n": len(rs), "hit1": round(sum(1 for r in rs if r == 1) / len(rs), 4),
                           "hit5": round(sum(1 for r in rs if 0 < r <= 5) / len(rs), 4)}
    return {
        "hit1": round(hit1_n / scored, 4) if scored else None,
        "hit5": round(hit5_n / scored, 4) if scored else None,
        "p50_ms": _pct(times, 50),
        "p95_ms": _pct(times, 95),
        "scored": scored,
        "total": len(qs),
        "skipped_missing": skipped,
        "started": started,
        "finished": _now(),
        "errors": errors,
        "hit1_n": hit1_n,
        "hit5_n": hit5_n,
        "hit5_loose": round(loose5 / scored, 4) if scored else None,
        "by_style": by_style,
        "not_run": len(qs) - done,
        "aborted": aborted,
        "missing_check": check,
        "elapsed_sec": round(time.monotonic() - t_start, 1),
        "questions": questions_path,
        "user_id": uid,
    }


if __name__ == "__main__":  # 手動冒煙：python monitor_eval.py 只檢查題庫讀得進來，不打任何服務
    u, qq = load_questions(DEFAULT_QUESTIONS)
    print(f"題庫 {DEFAULT_QUESTIONS}：{len(qq)} 題，區 {u}")
