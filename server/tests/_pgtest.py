"""測試用的拋棄式 Postgres＋pgvector（不是測試檔，run_all.py 不會跑它）。

優先順序：
1. 環境變數 RT_TEST_PG_DSN：用現成的資料庫（例如 docker 起的 pgvector/pgvector:0.8.7-pg17：
       docker run --rm -d --name rt-test-pg -e POSTGRES_PASSWORD=test -p 54329:5432 pgvector/pgvector:0.8.7-pg17
       RT_TEST_PG_DSN=postgresql://postgres:test@127.0.0.1:54329/postgres
   測試只動 rt_memories／rt_history 兩張表，而且只用自己這次造的 user_id，跑完把那些列刪掉。
2. 沒設就用 pgserver 套件在暫存資料夾起一個（內建 PostgreSQL 16＋pgvector，約 3 秒；要 Python 3.12）：
       uv run --no-project --python 3.12 --with "psycopg[binary]" --with pgserver ...
3. 兩個都沒有：start_pg() 回 (None, 原因)，測試自己決定要略過還是失敗。
"""
import os
import shutil
import tempfile


def start_pg():
    """回 (dsn, cleanup) 或 (None, 原因字串)。cleanup() 跑完要呼叫。"""
    dsn = os.environ.get("RT_TEST_PG_DSN", "").strip()
    if dsn:
        return dsn, (lambda: None)
    try:
        import pgserver  # noqa: F401
    except ImportError:
        return None, "沒設 RT_TEST_PG_DSN，也沒裝 pgserver（uv run --python 3.12 --with pgserver --with \"psycopg[binary]\"）"
    d = tempfile.mkdtemp(prefix="rt-pg-")
    try:
        db = pgserver.get_server(d)
    except Exception as e:  # pgserver 起不來（例如系統沒有對應的二進位檔）
        shutil.rmtree(d, ignore_errors=True)
        return None, f"pgserver 起不來：{e}"

    def cleanup():
        try:
            db.cleanup()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    return db.get_uri(), cleanup
