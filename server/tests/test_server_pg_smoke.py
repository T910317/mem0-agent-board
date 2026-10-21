"""pg 冒煙測試：server.py 用 RT_STORE=pg 接真的 Postgres＋pgvector（見 _pgtest.py），把常用工具走一遍，
確認 PgStore 回的資料形狀跟 server.py 的用法接得起來。不接 Mem0、不打 embedding（_embed_raw 換成玩具向量）。
用法：python test_server_pg_smoke.py <新版 server.py 目錄>
沒有 Postgres 可用時整個略過（印「注意：」那行、結束碼 0）。"""
import importlib.util
import math
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
NEW_DIR = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(HERE)
sys.path.insert(0, HERE)
from _pgtest import start_pg  # noqa: E402

VOCAB = ["資料庫", "向量", "留言", "認領", "人格", "測試", "圓桌"]
DIMS = len(VOCAB) + 1


def toy_embed(texts):
    out = []
    for t in texts:
        v = [float(t.count(w)) for w in VOCAB] + [0.1]
        n = math.sqrt(sum(x * x for x in v))
        out.append([x / n for x in v])
    return out


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


FAILS = []


def check(name, ok, detail=""):
    print(("通過  " if ok else "失敗  ") + name + (f"  → {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def first_id(res):
    return next((r.get("id") for r in (res.get("results") or []) if r.get("id")), None)


def main():
    dsn, cleanup = start_pg()
    if not dsn:
        print(f"注意：pg 冒煙測試略過（{cleanup}）")
        return 0
    try:
        return run(dsn)
    finally:
        cleanup()


def run(dsn):
    for n in ["httpx", "starlette", "starlette.concurrency", "starlette.requests", "starlette.responses",
              "mcp", "mcp.server", "mcp.server.transport_security"]:
        _stubmod(n)
    _stubmod("mcp.server.fastmcp", FastMCP=FastMCP, Image=_Any)
    _stubmod("experience_store", register=lambda *a, **k: None)
    os.environ.update({"RT_STORE": "pg", "DATABASE_URL": dsn, "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "EMB_DIMS": str(DIMS), "MSG_ARCHIVE": "0",
                       "MCP_TOKEN_CLAUDE": "t1", "MCP_TOKEN_CODEX_MAX": "t2", "MCP_TOKEN_HERMES": "t3"})
    os.environ.pop("MEM0_API_KEY", None)
    sys.path.insert(0, NEW_DIR)
    spec = importlib.util.spec_from_file_location("srv_pg", os.path.join(NEW_DIR, "server.py"))
    S = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(S)
    S._embed_raw = toy_embed  # 不打 embedding 服務
    check("RT_STORE=pg 時 STORE 是 PgStore", S.STORE.name == "pg")
    S.STORE.start()
    check("start 後 ping 通", S.STORE.ping() is True)
    S.current_agent.set("claude-laptop")

    # 記憶：新增、搜尋、讀、改、歷史
    r = S.memory_add(type="decision", project="roundtable-mcp", content="改用 資料庫 存共用記憶")
    dec = first_id(r["result"])
    check("memory_add 回 results[0].id", bool(dec) and r["by"] == "claude-laptop", repr(r))
    note = first_id(S.memory_add(type="note", project="roundtable-mcp", content="向量 用 cosine 距離換算分數")["result"])
    hits = S.memory_search(query="資料庫")
    check("memory_search 向量排序：決策排第一、有 score", bool(hits) and hits[0]["id"] == dec and "score" in hits[0], repr(hits[:2]))
    check("memory_search 只打編號回全文", S.memory_search(query=dec[:8])[0]["id"] == dec)
    got = S.memory_get(dec)
    check("memory_get 有 version、text、created", len(got) == 1 and got[0]["version"] and got[0]["text"].startswith("[決策] ") and got[0]["created"], repr(got))
    ver = got[0]["version"]
    up = S.memory_update(memory_id=dec, content="改用 資料庫 存共用記憶；向量 也放同一張表", base_version=ver)
    check("memory_update 改成功、有 archive_id", up.get("changed") is True and up.get("archive_id"), repr(up))
    check("memory_update 舊版本號會被拒絕", raises(lambda: S.memory_update(memory_id=dec, content="x", base_version=ver)))
    check("memory_get 讀到新內容", "向量 也放同一張表" in S.memory_get(dec)[0]["text"])
    hist = S.memory_history(dec)
    check("memory_history 有一筆「改之前的舊版」", len(hist) == 1 and hist[0]["kind"] == "改之前的舊版" and "存共用記憶" in hist[0]["text"], repr(hist))
    lst = S.memory_list(project="roundtable-mcp")
    check("memory_list 兩筆", lst["total"] == 2 and {x["id"] for x in lst["items"]} == {dec, note}, repr(lst))

    # 進度卡
    st = S.project_set_status(project="roundtable-mcp", summary="名稱：圓桌 測試\n現在：測試中\n- [ ] 冒煙測試", stage="implementing")
    check("project_set_status 建卡", st.get("created") is True and st.get("version"), repr(st))
    ps = S.project_status("roundtable-mcp")
    check("project_status 讀到卡", ps["status"] and ps["version"] == st["version"] and ps["count"] == 1, repr(ps))
    st2 = S.project_set_status(project="roundtable-mcp", summary="名稱：圓桌 測試\n現在：做完了\n- [x] 冒煙測試", stage="done", base_version=ps["version"])
    check("project_set_status 更新卡", st2.get("updated") and S.project_status("roundtable-mcp")["status"]["stage"] == "done", repr(st2))

    # 認領
    c = S.claim_create(project="roundtable-mcp", target="test-host", summary="冒煙 測試 認領")
    check("claim_create", first_id(c["result"]), repr(c))
    check("claim_check 看得到", len(S.claim_check(target="test-host")) == 1)
    S.current_agent.set("codex-max")
    check("別人的 claim 會擋", raises(lambda: S.claim_create(project="roundtable-mcp", target="test-host", summary="搶")))
    S.current_agent.set("claude-laptop")
    check("claim_release", S.claim_release(project="roundtable-mcp", target="test-host")["released"] == 1 and S.claim_check(target="test-host") == [])

    # 留言：寄、收、已讀、搜尋
    sent = S.message_send(to="codex-max", subject="留言 測試：請看一下", body="內文", needs_action=True, project="roundtable-mcp")
    mid = first_id(sent["result"])
    check("message_send", sent.get("sent") is True and mid, repr(sent))
    S.current_agent.set("codex-max")
    ib = S.inbox()
    check("inbox 收到一則、未讀、needs_action", len(ib) == 1 and ib[0]["id"] == mid and ib[0]["read"] is False and ib[0]["needs_action"] is True, repr(ib))
    check("message_ack", S.message_ack(message_id=mid)["acked"] == mid and S.inbox() == [])
    check("inbox include_read 列出已讀", [x["read"] for x in S.inbox(include_read=True)] == [True])
    S.current_agent.set("claude-laptop")
    check("memory_search type=message 搜得到留言", mid in {x["id"] for x in S.memory_search(query="留言", type="message")})
    check("memory_search 預設不含留言", mid not in {x["id"] for x in S.memory_search(query="留言")})

    # 刪除、回收桶、還原
    nv = S.memory_get(note)[0]["version"]
    d = S.memory_delete(memory_id=note, reason="測試刪除", base_version=nv)
    check("memory_delete 回 trash_id", d.get("deleted") == note and d.get("trash_id"), repr(d))
    check("刪了就讀不到", "error" in S.memory_get(note)[0])
    tr = S.memory_trash(project="roundtable-mcp")
    check("memory_trash 列出一筆", len(tr) == 1 and tr[0]["trash_id"] == d["trash_id"] and tr[0]["memory_id"] == note, repr(tr))
    rs = S.memory_restore(trash_id=d["trash_id"], reason="測試還原")
    check("memory_restore 回新 id、orig_id 是舊 id", rs.get("restored") and rs.get("orig_id") == note, repr(rs))
    check("還原後讀得到、內容一樣", S.memory_get(rs["restored"])[0]["text"] == tr[0]["text"])
    check("memory_history 連舊 id 的刪除紀錄一起列", any(h["kind"] == "刪除" for h in S.memory_history(rs["restored"])))
    check("memory_trash 預設不列已還原的", S.memory_trash(project="roundtable-mcp") == [] and len(S.memory_trash(project="roundtable-mcp", include_restored=True)) == 1)

    # 人格區
    S.current_agent.set("hermes-gamma")
    pp = S.persona_put(name="alpha", kind="soul", text="人格 測試：說話簡短。")
    check("persona_put", pp.get("changed") is True and pp.get("parts") == 1, repr(pp))
    check("persona_put 內容沒變就不寫", S.persona_put(name="alpha", kind="soul", text="人格 測試：說話簡短。")["changed"] is False)
    pg_ = S.persona_get(name="alpha")
    check("persona_get 讀回", pg_["text"] == "人格 測試：說話簡短。" and pg_["kind"] == "soul", repr(pg_))
    S.current_agent.set("codex-max")
    check("沒權限的身分不能寫人格", raises(lambda: S.persona_put(name="alpha", kind="soul", text="x")))

    # 各區筆數（直接問儲存層）
    counts = {u: S.STORE.count(u) for u in (S.USER_ID, S.OPS_UID, S.ARCHIVE_UID, S.PERSONA_UID)}
    check("各區筆數：原則層 3（決策、還原的筆記、進度卡）、交流層 1（留言）、封存 2（舊版、刪除）、人格 1",
          counts == {S.USER_ID: 3, S.OPS_UID: 1, S.ARCHIVE_UID: 2, S.PERSONA_UID: 1}, repr(counts))
    print("\n" + ("全部通過" if not FAILS else f"{len(FAILS)} 項失敗：" + "、".join(FAILS)))
    return 1 if FAILS else 0


def raises(fn):
    try:
        fn()
        return False
    except (ValueError, RuntimeError, KeyError):
        return True


if __name__ == "__main__":
    sys.exit(main())
