"""離線測試：公告欄「回滾到指定版本」。用假的 UI 資料夾，不連任何真實服務。
用法：python test_ui_rollback_to.py <server.py 所在目錄>"""
import importlib.util
import os
import sys
import tempfile
import types

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


for n in ["httpx", "starlette", "starlette.concurrency", "starlette.requests", "starlette.responses",
          "mcp", "mcp.server", "mcp.server.transport_security"]:
    _stubmod(n)
_stubmod("mcp.server.fastmcp", FastMCP=FastMCP, Image=_Any)
_stubmod("experience_store", register=lambda *a, **k: None)
for k in [k for k in os.environ if k.startswith("MCP_TOKEN_")]:
    del os.environ[k]
ROOT = tempfile.mkdtemp(prefix="uirb-")
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MCP_TOKEN_CLAUDE_LAPTOP": "t1", "UI_DIR": ROOT})
sys.path.insert(0, SRC)
spec = importlib.util.spec_from_file_location("srv", os.path.join(SRC, "server.py"))
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

FAILS = []


def check(name, ok):
    print(("通過 " if ok else "失敗 ") + name)
    if not ok:
        FAILS.append(name)


def pages(rev, version):
    out = {}
    for name, kind in zip(S._UI_FILES, ("board", "memory", "release")):
        out[name] = ('<html><head><meta name="%s-version" content="%s"><meta name="%s-revision" content="%d"></head>'
                     '<body>%s r%d</body></html>' % (kind, version, kind, rev, name, rev)).encode()
    return out


def put(which, snap):
    os.makedirs(os.path.join(ROOT, which), exist_ok=True)
    for n, v in snap.items():
        with open(os.path.join(ROOT, which, n), "wb") as f:
            f.write(v)


def stable_rev():
    return S._ui_stable_rev(S._ui_snapshot("stable"))


def baks():
    return sorted(m[0] for m in (S._ui_bak_rev(n) for n in S._ui_backup_entries() if n.startswith("ui.bak-")) if m)


put("stable", pages(5, "1.3.0-rc.1"))
put("preview", pages(6, "1.4.0"))
S._ui_promote()
put("preview", pages(7, "1.5.0"))
S._ui_promote()
st = S._ui_state()
check("採用兩次後正式版是 7", stable_rev() == 7)
check("可回滾清單是 6、5（新到舊）", [x["rev"] for x in st["rollback_to"]] == [6, 5])
check("版號拿掉 rc", st["rollback_to"][1]["version"] == "1.3.0")
check("可以回滾、回報正式版流水號", st["can_rollback"] is True and st["stable_rev"] == 7)

S._ui_rollback(5)
check("指定回滾到 5", stable_rev() == 5)
check("回滾前的 7 也留成備份，可以再換回來", [x["rev"] for x in S._ui_state()["rollback_to"]] == [7, 6])
check("回滾到現在這一版 → 沒有變動", S._ui_rollback(5) == "already-current")

S._ui_rollback(7)
check("從更新紀錄換回 7", stable_rev() == 7)
check("同一版的備份只留一份", baks() == [5, 6, 7])

S._last_rollback.clear()
S._ui_rollback()
check("不指定就退到比現在舊的最新一版（6）", stable_rev() == 6)
check("五秒內連按不指定 → 不會再退", S._ui_rollback() == "already-current" and stable_rev() == 6)

try:
    S._ui_rollback(99)
    check("沒有備份的版本要拒絕", False)
except ValueError as e:
    check("沒有備份的版本要拒絕", "找不到這一版的備份" in str(e))
check("拒絕後正式版不變", stable_rev() == 6)

S._ui_promote()  # 預覽還是 7
check("回滾後再採用預覽 → 7", stable_rev() == 7)
check("採用後同一版備份仍只一份", baks() == [5, 6, 7])
check("可回滾清單 6、5", [x["rev"] for x in S._ui_state()["rollback_to"]] == [6, 5])

with open(os.path.join(ROOT, "stable", "ui.bak-20200101-000000-000000-deadbeef"), "w") as f:
    f.write("壞掉的備份")
check("讀不懂的備份不列進清單、不出錯", [x["rev"] for x in S._ui_state()["rollback_to"]] == [6, 5])
check("沒有留下交易紀錄檔", not os.path.exists(os.path.join(ROOT, "stable", "ui.transaction.json")))

print("全部通過" if not FAILS else "有 %d 項失敗" % len(FAILS))
sys.exit(1 if FAILS else 0)
