"""一次跑完 tests/ 裡所有測試（都是 stub 版：不接 Mem0、不接 Postgres、不打 embedding）。

用法：
    uv run --no-project --with starlette --with httpx python tests/run_all.py [<新版目錄>] [--old <舊版目錄> | --old-from-git <版本>]

- 新版目錄是 server.py 所在的資料夾，預設是 tests/ 的上一層。
- 有 7 個測試是「新舊版對照」（test_identity_rename、test_memory_edit、test_memory_search、test_message_close、
  test_message_recall、test_session_inbox、test_whiteboard_card），舊版用 1.0.0 的 server.py／experience_store.py：
  - --old <目錄>：舊版兩個檔案所在的資料夾。
  - --old-from-git <版本>：從 git 歷史把那一版的兩個檔案匯到暫存資料夾當舊版（例如 1.0.0 那個 commit）。
  - 都沒給就略過這 7 個，結尾會註明。
- run_board_local*.py 是手動開本機板子看畫面用的，不在這裡跑。
- 結束碼：全部通過 0，有失敗 1。
"""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TWO_DIR = ("test_identity_rename", "test_memory_edit", "test_memory_search", "test_message_close",
           "test_message_recall", "test_session_inbox", "test_whiteboard_card")


def old_from_git(rev):
    """把 git 裡 rev 那一版的 server.py／experience_store.py 匯到暫存資料夾；拿不到就停。"""
    repo = os.path.dirname(os.path.dirname(HERE))
    srcdir = os.path.relpath(os.path.dirname(HERE), repo).replace(os.sep, "/")
    d = tempfile.mkdtemp(prefix="old-" + rev[:12] + "-")
    for f in ("server.py", "experience_store.py"):
        p = subprocess.run(["git", "-C", repo, "show", f"{rev}:{srcdir}/{f}"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            sys.exit(f"git 裡拿不到 {rev}:{srcdir}/{f}：{(p.stderr or '').strip()}")
        with open(os.path.join(d, f), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(p.stdout)
    return d


def main(argv):
    old, label = None, ""
    if "--old" in argv:
        i = argv.index("--old")
        old = os.path.abspath(argv[i + 1])
        label = f"；舊版目錄：{old}"
        argv = argv[:i] + argv[i + 2:]
    if "--old-from-git" in argv:
        i = argv.index("--old-from-git")
        old = old_from_git(argv[i + 1])
        label = f"；舊版：git {argv[i + 1]}"
        argv = argv[:i] + argv[i + 2:]
    new = os.path.abspath(argv[0]) if argv else os.path.dirname(HERE)
    names = sorted(f[:-3] for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py"))
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    results, skipped = [], []
    for name in names:
        if name in TWO_DIR:
            if not old:
                skipped.append(name)
                continue
            args = [old, new]
        else:
            args = [new]
        t0 = time.time()
        p = subprocess.run([sys.executable, os.path.join(HERE, name + ".py"), *args],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        ok = p.returncode == 0
        results.append((name, ok, time.time() - t0))
        print(f"{'通過' if ok else '失敗'}  {name}  ({time.time() - t0:.1f}s)")
        if not ok:
            print("---- 輸出 ----")
            print((p.stdout or "").rstrip())
            print((p.stderr or "").rstrip())
            print("--------------")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} 個測試通過；新版目錄：{new}{label}")
    if skipped:
        print("略過（沒有舊版可對照；用 --old 或 --old-from-git 指定）：" + "、".join(skipped))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
