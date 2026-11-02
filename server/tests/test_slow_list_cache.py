"""慢讀取回歸：python test_slow_list_cache.py <mcp 來源目錄>。

載入真正 server.py 與已安裝 MCP；只有 Store 換成記憶體假資料。
用事件讓八個呼叫先同時 miss，再以受控時鐘跨過 TTL；不以執行速度判輸贏。
不開 socket、不寫檔。測試範圍：共用讀取、TTL、新寫入及 Store 更換。
"""
import sys
sys.dont_write_bytecode = True
import concurrent.futures
import importlib.util
import os
from pathlib import Path
import threading
import types


def guard_io(event, args):
    if event == "open":
        mode, flags = args[1], args[2]
        if (isinstance(mode, str) and any(x in mode for x in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        ):
            raise RuntimeError("本測試禁止寫檔")
    if event in {"os.mkdir", "os.remove", "os.rename", "socket.bind", "socket.connect"}:
        raise RuntimeError("本測試禁止檔案變更及網路操作")


sys.addaudithook(guard_io)
SRC = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(SRC))
for name in list(os.environ):
    if name.startswith("MCP_TOKEN_"):
        del os.environ[name]
os.environ.update(MCP_TOKEN_TEST="synthetic-test-token", RT_STORE="mem0",
                  MEM0_API_KEY="fake", MEM0_URL="http://127.0.0.1:9",
                  BOARD_AUTH_FILE="", MONITOR_ON="0", MSG_ARCHIVE="0")


class Store:
    name = "fake-slow-cache"

    def __init__(self, empty=False):
        self.lock = threading.Lock()
        self.calls = 0
        self.rows = {} if empty else {"original": ("roundtable", {
            "id": "original", "memory": "synthetic original", "metadata": {"type": "note"}})}

    def list(self, uid):
        with self.lock:
            self.calls += 1
            return [{**r, "metadata": dict(r["metadata"])} for u, r in self.rows.values() if u == uid]

    def add(self, uid, agent, text, metadata):
        with self.lock:
            key = "new-" + str(len(self.rows))
            row = {"id": key, "memory": text, "metadata": dict(metadata), "agent_id": agent}
            self.rows[key] = uid, row
            return dict(row)


FAKE = Store()
import store
store.make_store = lambda **kwargs: FAKE
spec = importlib.util.spec_from_file_location("slow_cache_server", SRC / "server.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)
assert S.STORE is FAKE


class Clock:
    def __init__(self):
        self.now = 100.0
        self.lock = threading.Lock()

    def monotonic(self):
        with self.lock:
            return self.now

    def advance(self):
        with self.lock:
            self.now += 10.0


CLOCK = Clock()
S.time = types.SimpleNamespace(monotonic=CLOCK.monotonic)
S.LIST_CACHE_S = 5.0
FAILS = []


def check(label, ok):
    print(("通過 " if ok else "失敗 ") + label, flush=True)
    if not ok:
        FAILS.append(label)


class ObservedLock:
    """只觀測真實鎖的進入嘗試；原鎖的互斥語意不變。"""
    def __init__(self, lock, n):
        self.real, self.n = lock, n
        self.count = 0
        self.guard = threading.Lock()
        self.all_entered = threading.Event()

    def __enter__(self):
        with self.guard:
            self.count += 1
            if self.count == self.n:
                self.all_entered.set()
        self.real.acquire()
        return self

    def __exit__(self, *args):
        self.real.release()


# 1. 第一個讀者直到八個呼叫都 miss 才完成；完成時受控時鐘已跨過 TTL。
S._list_invalidate()
real_lock, real_list = S._list_fetch_lock, FAKE.list
observed = ObservedLock(real_lock, 8)
S._list_fetch_lock = observed


def slow_list(uid):
    if not observed.all_entered.wait(5):
        raise AssertionError("八個讀者未能進入同一批讀取")
    result = real_list(uid)
    if uid == S.OPS_UID:
        CLOCK.advance()
    return result


FAKE.list = slow_list
try:
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: S._list_all(), range(8)))
finally:
    S._list_fetch_lock, FAKE.list = real_lock, real_list
check("同批八讀者在讀取超過 TTL 後仍只列兩層一次（實際 %d 次）" % FAKE.calls,
      FAKE.calls == 2 and all([r["id"] for r in result] == ["original"] for result in rows))

# 2. 完成之後才來的新呼叫不能沿用已逾 TTL 的結果。
before = FAKE.calls
S._list_all()
check("完成之後的新呼叫會重抓已過期結果", FAKE.calls == before + 2)

# 3. fetch 已拿到舊列、但還未回傳時完成新寫入，舊結果不得再次進入快取。
S._list_invalidate()
captured, release = threading.Event(), threading.Event()


def held_list(uid):
    result = real_list(uid)
    if uid == S.OPS_UID:
        captured.set()
        if not release.wait(5):
            raise AssertionError("測試控制端未放行")
    return result


FAKE.list = held_list
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    future = pool.submit(S._list_all)
    try:
        assert captured.wait(5), "讀取未到指定交錯位置"
        added = S._st_add(S.OPS_UID, "test", "synthetic new", {"type": "message"})
    finally:
        release.set()
    stale = future.result(timeout=5)
FAKE.list = real_list
fresh = S._list_all()
check("讀取途中寫入後不復活舊快取", added["id"] not in {r["id"] for r in stale}
      and added["id"] in {r["id"] for r in fresh})

# 4. 換 Store 後，不能沿用上一份 Store 的有效快取。
S.STORE = Store(empty=True)
check("更換 Store 不沿用上一份 Store 的資料", S._list_all() == [])
S.STORE = FAKE
S._list_invalidate()

print("全部通過" if not FAILS else "有 %d 項失敗" % len(FAILS), flush=True)
sys.exit(1 if FAILS else 0)
