"""離線測試：白板專案卡「卡在等使用者」（blocked_on_user／blocked_why，乒乓 DESIGN 9.2）、小目標沒勾完不准標完成，
以及公告欄回覆的原留言被 14 天封存搬走時，改從封存區帶原主旨（公告欄串 10/19 回報）。
判斷規則的例子取自 10/19 線上 23 張專案卡的「現在」與第一個沒勾的小目標。
不連任何真實服務。用法：python test_blocked.py <server.py 目錄>"""
import importlib.util
import os
import sys
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
os.environ.update({"RT_STORE": "mem0", "MEM0_API_KEY": "fake", "MEM0_URL": "http://fake", "MCP_ALIASES": "claude,claude-laptop", "PERSONA_WRITERS": "hermes-gamma,claude", "PERSONA_NAMES": "alpha,beta,gamma", "MSG_ARCHIVE": "0",
                   "MCP_TOKEN_CLAUDE_LAPTOP": "t1", "MCP_TOKEN_CODEX_LAPTOP": "t2"})


class R:
    def __init__(self, d=None): self.d = d if d is not None else {}
    def raise_for_status(self): pass
    def json(self): return self.d


class FakeClient:  # 白板會查 status 的改版紀錄（更新幾次）；回空的就好
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def get(self, path, **k): return R([])


spec = importlib.util.spec_from_file_location("srv", os.path.join(SRC, "server.py"))
S = importlib.util.module_from_spec(spec)
sys.path.insert(0, SRC)
spec.loader.exec_module(S)
S.STORE.http = lambda: FakeClient()
results = []


def check(name, ok):
    results.append((name, bool(ok)))


def raises_ve(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


def card(now="", first="", others=(), done=()):
    lines = ["名稱：測試專案"] + ([f"現在：{now}"] if now else [])
    lines += [f"- [x] {d}" for d in done]
    lines += [f"- [ ] {x}" for x in ([first] if first else []) + list(others)]
    return "\n".join(lines + ["", "技術細節：等使用者決定（這行在細節區，不算）"])


# 1. 判斷規則：10/19 線上專案卡（現在、第一個沒勾的、後面沒勾的）→ 預期
CASES = [
    ("update-cache", "方案寫好了，等使用者決定要不要建快取機", "使用者決定：做不做、要不要 Docker 映像快取",
     ["建快取 LXC（要使用者批准）"], "方案寫好了，等使用者決定要不要建快取機"),
    ("board", "正式版 2.0.0；預覽 2.1.0 等使用者決定要不要採用，也在等圓桌 MCP 串把「卡在使用者」的欄位做上線",
     "圓桌 MCP 串讓 feed 帶 blocked_on_user／blocked_why", ["使用者決定 2.1.0 採用或不採用"],
     "預覽 2.1.0 等使用者決定要不要採用，也在等圓桌 MCP 串把「卡在使用者」的欄位做上線"),
    ("windows-workstation", "第五版安裝卡在最後清捷徑一步，第六版修好正在做 USB，之後乾淨重測",
     "第六版 USB 做好，先 game 再 prod", [], ""),
    ("memory-tidy", "這一輪做完了；下一輪等每週一盤點", "審核頁上傳方式寫進 README", [], ""),
    ("host-a-backup", "由備份串負責，細節以它的交接檔為準", "每晚排程與保留規則",
     ["CT 223（聽歌）加進每晚備份（改宿主機，要使用者批准）"], ""),
    ("pingpong", "第一批試跑收完（做完 1、沒驗證 1、失敗 1）；1 件排隊等使用者批（CT301 入口）；等信器掛著，等下一批",
     "使用者批 QUEUE 第 1 項（CT301 入口 A／B／C）", [], "1 件排隊等使用者批（CT301 入口）"),
    ("host-a-alerts", "中文樣張做好，等使用者看；改走 Gmail 也還在等使用者批准", "使用者看樣張決定要不要用", [],
     "中文樣張做好，等使用者看"),
    ("apple-music-discord", "擱置中，等使用者做兩件本人才能做的事",
     "使用者：discord.com/developers 建應用程式", [], "擱置中，等使用者做兩件本人才能做的事"),
    ("secaudit", "Kali 掃描機還沒裝系統，等使用者從 PVE 管理頁安裝", "使用者從 PVE 管理頁裝好 Kali", [],
     "Kali 掃描機還沒裝系統，等使用者從 PVE 管理頁安裝"),
    ("aria2", "還沒開始做，等使用者回答 6 個問題", "使用者回答 6 個問題", [], "還沒開始做，等使用者回答 6 個問題"),
    ("llm-gateway", "調查寫好了，等使用者看結論再決定方向", "使用者看結論、決定方向", [], "調查寫好了，等使用者看結論再決定方向"),
    ("docker-vm", "提案寫好了，等使用者回答四個決定", "使用者回答四個決定", ["建 VM（要使用者批准）"],
     "提案寫好了，等使用者回答四個決定"),
    ("homelab-network", "Kuma 53 項監控正常；自動告警只做了一半，還缺驗收和備援路線",
     "自動告警驗收：DOWN、RECOVERY 各一則（Codex）", ["Laya 3 項監控暫停中，要不要恢復等使用者"], ""),
    ("pve", "三台宿主機只准唯讀；虛擬機照常運作，VM301 顯示卡問題等使用者決定",
     "VM301 換新核心後抓不到 T4：改回舊核心或裝新驅動，等使用者決定", [], "虛擬機照常運作，VM301 顯示卡問題等使用者決定"),
    ("memory-map", "Claude 版 0.1.2 已裝；Codex 版 0.1.2 審過，等使用者決定裝不裝", "使用者決定要不要裝 Codex 版", [],
     "Codex 版 0.1.2 審過，等使用者決定裝不裝"),
    ("dakimakura-gallery", "接手串在做樹外備份；Fable 在評估「自訂改動怎麼搬到 2.24.2」", "樹外備份（NAS＋G 槽各一份），讀回驗證",
     ["一次問完使用者要決定的事", "部署到 DEV／正式（每關先問使用者）"], ""),
    ("mem0", "分兩層已上線、量測 11/12；Hermes VM113 查舊址等使用者貼給 Codex；4B＋Laya 等 Codex 開測試機",
     "Hermes 主串去 VM113 唯讀確認舊址", [], "Hermes VM113 查舊址等使用者貼給 Codex"),
    ("evalscope", "裁判校準失敗，目標已標記阻擋；等本人批准單題錄製及CC回覆", "本人批准單題錄製", [], "等本人批准單題錄製及CC回覆"),
    ("ha", "使用者剛新增一個裝置，等 Claude 接手；乒乓寫好的兩份方案等 HA 串看要不要套",
     "弄清楚使用者 新增的裝置，接好", [], ""),
    ("backup", "", "", [], ""),
]
for proj, now, first, others, why in CASES:
    got = S._blocked(card(now, first, others))
    check(f"{proj}：{'卡在使用者' if why else '沒卡'}", got == (bool(why), why))

# 只靠小目標、邊界情況
check("「現在」沒寫、第一個沒勾的主詞是使用者", S._blocked(card(first="使用者回答 6 個問題")) == (True, "使用者回答 6 個問題"))
check("第一個沒勾的是「本人批准…」", S._blocked(card(first="本人批准單題錄製")) == (True, "本人批准單題錄製"))
check("第一個沒勾的寫「要使用者確認」", S._blocked(card(first="合併前要使用者確認畫面")) == (True, "合併前要使用者確認畫面"))
check("後面幾步要使用者批准不算", S._blocked(card(first="寫程式", others=["上線（要使用者批准）"])) == (False, ""))
check("已勾的小目標不算", S._blocked(card(done=["使用者決定做"], first="寫程式")) == (False, ""))
check("「照使用者決定的方案」是過去的決定，不算", S._blocked(card(now="照使用者決定的方案實作")) == (False, ""))
check("「使用者批准後上線」不算（是之後的關卡）", S._blocked(card(now="寫好了，使用者批准後上線")) == (False, ""))
check("「等你回覆」算", S._blocked(card(now="等你回覆")) == (True, "等你回覆"))
check("「等待使用者」算", S._blocked(card(now="等待使用者確認")) == (True, "等待使用者確認"))
check("細節區寫等使用者不算", S._blocked(card(now="做到一半")) == (False, ""))
check("空卡", S._blocked("") == (False, ""))
check("✗ 也算沒勾", S._blocked("現在：做到一半\n✗ 使用者回答問題") == (True, "使用者回答問題"))
long = S._blocked(card(now="等使用者" + "字" * 100))
check("超過 80 字截斷並加刪節號", long[0] and len(long[1]) == 80 and long[1].endswith("…"))

# 2. 白板（feed 的 statuses）：兩個欄位都在，那一句跟名稱一樣 html 跳脫過
items = [
    {"id": "11111111-1111-1111-1111-111111111111", "memory": "[狀態] 名稱：甲\n現在：等使用者決定 <b>A</b> 還是 B",
     "metadata": {"type": "status", "project": "jia", "stage": "planning"}, "agent_id": "claude-laptop",
     "created_at": "2026-10-19T01:00:00+00:00", "updated_at": "2026-10-19T02:00:00+00:00"},
    {"id": "22222222-2222-2222-2222-222222222222", "memory": "[狀態] 名稱：乙\n現在：Codex 在做\n- [ ] 寫程式",
     "metadata": {"type": "status", "project": "yi", "stage": "implementing"}, "agent_id": "claude-laptop",
     "created_at": "2026-10-19T01:00:00+00:00", "updated_at": "2026-10-19T01:30:00+00:00"},
]
st = {s["project"]: s for s in S._whiteboard(items)[0]}
check("白板：卡住的專案 blocked_on_user=true", st["jia"]["blocked_on_user"] is True)
check("白板：blocked_why 跳脫過", st["jia"]["blocked_why"] == "等使用者決定 &lt;b&gt;A&lt;/b&gt; 還是 B")
check("白板：沒卡的專案 false、空字串", st["yi"]["blocked_on_user"] is False and st["yi"]["blocked_why"] == "")
check("白板：原本的欄位都還在", all(k in st["jia"] for k in ("name", "now", "steps", "pingpong", "updates")))

# 3. project_status：同樣兩個欄位，那一句是純文字（給 AI 看）
S._list_all = lambda: list(items)
ps = S.project_status("jia")
check("project_status：卡住", ps["blocked_on_user"] is True and ps["blocked_why"] == "等使用者決定 <b>A</b> 還是 B")
check("project_status：沒卡", S.project_status("yi")["blocked_on_user"] is False)
check("project_status：沒有卡片", S.project_status("nothing") == {**S.project_status("nothing"), "status": None,
                                                                 "blocked_on_user": False, "blocked_why": ""})
check("project_status：原本的欄位都還在", all(k in ps for k in ("project", "status", "count", "version", "pingpong")))

# 4. 公告欄回覆：原留言被封存 → 帶原主旨和封存日；封存區也沒有 → 寫不存在
ARC = "aaaaaaaa-0000-0000-0000-000000000001"
GONE = "bbbbbbbb-0000-0000-0000-000000000002"
BOOM = "cccccccc-0000-0000-0000-000000000003"
LIVE = "dddddddd-0000-0000-0000-000000000004"
calls = []


def fake_archived_copy(oid):
    calls.append(oid)
    if oid == BOOM:
        raise RuntimeError("封存區連不上")
    if oid == ARC:
        return {"id": "copy-1", "metadata": {"type": "message", "from": "codex-laptop", "from_session": "",
                                             "subject": "舊的 <問題>", "orig_id": ARC,
                                             "archived_at": "2026-10-05T04:30:01+08:00"}}
    return None


S._archived_copy = fake_archived_copy
S._reply_cache.clear()


def reply_of(parent_id, pool=()):
    m = {"id": "eeeeeeee-0000-0000-0000-000000000005", "memory": "回覆",
         "metadata": {"type": "message", "from": "user", "to": "codex-laptop", "subject": "回覆", "reply_to": parent_id},
         "created_at": "2026-10-19T03:00:00+00:00"}
    return S._message_feed(m, [m, *pool])["reply"]


r = reply_of(ARC)
check("被封存：帶原主旨（跳脫過）", r["subject"] == "舊的 &lt;問題&gt;" and r["id"] == ARC)
check("被封存：帶寄件人和封存日", r["from"] == "codex-laptop" and r["archived"] == "2026-10-05")
reply_of(ARC)
check("被封存：第二次不再查封存區", calls.count(ARC) == 1)
r = reply_of(GONE)
check("封存區也沒有：寫原留言已不存在、沒有 archived", r["subject"] == "原留言已不存在" and "archived" not in r)
reply_of(GONE)
check("找不到的 10 分鐘內不重查", calls.count(GONE) == 1)
S._reply_cache[GONE] = (S._reply_cache[GONE][0] - 601, None)
reply_of(GONE)
check("找不到的過 10 分鐘再查一次", calls.count(GONE) == 2)
r = reply_of(BOOM)
check("封存區出錯：不讓公告欄壞掉", r["subject"] == "原留言已不存在")
reply_of(BOOM)
check("封存區出錯：不記住，下次再查", calls.count(BOOM) == 2)
n = len(calls)
r = reply_of("不是編號")
check("編號格式不對：不去翻封存區", r["subject"] == "原留言已不存在" and len(calls) == n)
parent = {"id": LIVE, "memory": "原留言", "metadata": {"type": "message", "from": "claude-laptop", "subject": "還在的"},
          "created_at": "2026-10-19T02:00:00+00:00"}
r = reply_of(LIVE, [parent])
check("原留言還在：照舊，不查封存區", r["subject"] == "還在的" and "archived" not in r and len(calls) == n)

# 5. 小目標沒勾完不准標完成
def refused(stage, text):
    try:
        S._check_done(stage, text)
        return ""
    except ValueError as e:
        return str(e)


TWO_LEFT = "名稱：壓縮\n現在：做完了\n- [x] 改設定\n- [ ] 驗證觸發點\n☐ 寫報告\n\n細節"
check("done 還有沒勾的：拒絕、講幾個", "還有 2 個小目標沒勾" in refused("done", TWO_LEFT))
check("拒絕訊息舉出第一個沒勾的、教怎麼改", "驗證觸發點" in refused("done", TWO_LEFT) and "verifying" in refused("done", TWO_LEFT))
check("done 全部勾完：放行", refused("done", "現在：完成\n- [x] 改設定\n- [x] 驗證") == "")
check("done 沒有小目標：放行", refused("done", "現在：完成\n細節") == "")
check("verifying 有沒勾的：放行", refused("verifying", TWO_LEFT) == "")
check("「現在：」那行像勾選框也不算小目標", refused("done", "現在：[ ] 不是小目標\n- [x] 做完") == "")

S.current_agent.set("claude-laptop")
added, puts = [], []
S._add = lambda text, md: added.append((text, md)) or {"id": "new"}


class PutClient(FakeClient):
    def put(self, path, json=None, **k):
        puts.append((path, json))
        return R()


S.STORE.http = lambda: PutClient()
S._list_all = lambda: []
check("新建時標 done 但沒勾完：拒絕、沒寫入", raises_ve(lambda: S.project_set_status("newp", TWO_LEFT, "done")) and not added)
S.project_set_status("newp", TWO_LEFT, "verifying")
check("新建時標 verifying：照寫", len(added) == 1 and added[0][1]["stage"] == "verifying")
old = {"id": "33333333-3333-3333-3333-333333333333", "memory": "[狀態] 名稱：舊\n- [x] 做完",
       "metadata": {"type": "status", "project": "oldp", "stage": "done"}, "agent_id": "claude-laptop",
       "created_at": "2026-10-19T01:00:00+00:00", "updated_at": "2026-10-19T01:00:00+00:00"}
S._list_all = lambda: [old]
v = S._version(old["memory"])
check("更新時不填 stage、原本是 done、又加了沒勾的：拒絕、沒寫入",
      raises_ve(lambda: S.project_set_status("oldp", TWO_LEFT, "", v)) and not puts)
S.project_set_status("oldp", TWO_LEFT, "verifying", v)
check("更新時改 verifying：照寫", len(puts) == 1 and puts[0][1]["metadata"]["stage"] == "verifying")
S.project_set_status("oldp", "名稱：舊\n- [x] 做完\n- [x] 驗證", "done", v)
check("更新時全部勾完標 done：照寫", len(puts) == 2 and puts[1][1]["metadata"]["stage"] == "done")

fail = [n for n, ok in results if not ok]
for n, ok in results:
    print(("通過 " if ok else "失敗 ") + n)
print(f"共 {len(results)} 項，失敗 {len(fail)} 項")
sys.exit(1 if fail else 0)
