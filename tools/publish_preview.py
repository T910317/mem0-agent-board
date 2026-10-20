#!/usr/bin/env python3
"""把公告欄的新頁面放上預覽版的唯一入口：換新版號、三頁版號同步、寫一筆版本更新紀錄、留備份。

在伺服器上（安裝資料夾）：
  python3 tools/publish_preview.py --by claude \
      --note "給使用者看的白話說明" --note "第二點" \
      [--board 新的board.html] [--memory 新的memory.html] [--versions 新的versions.html] [--version X.Y.Z] [--dry-run]

- 每執行一次就是一個新版號：只是修正就不給 --version，自動把最後一碼加一（1.5.0 → 1.5.1）；
  加了新功能就用 --version 把中間那碼加一（1.4.0 → 1.5.0）；要跳第一碼先問使用者。
  給人看的地方（版本紀錄、版本列、採用／回滾提示）只寫 vX.Y.Z，不寫修訂、rc。
- 三頁的 *-revision 是程式用的內部流水號（採用、回滾、標「正式版已包含」靠它），每次一起加一，不顯示。
- --note 必填，至少一點；寫「改了什麼、使用者會看到什麼」，不寫內部代號。
- 紀錄插在 versions.html 的 <!-- releases --> 標記下面，最新在上。
- 只動 ui/preview。正式版只能由使用者在預覽頁按「採用成正式版」，AI 不 promote／rollback。
- 舊的三頁先備份到 data/preview-history/<時間>-r<舊流水號>/，還原就把那三個檔複製回 ui/preview
  （還原後預覽會顯示「有改動還沒寫進紀錄」，要再跑一次本工具登記）。
- 放完會寫 ui/preview/publish-record.json（三頁 SHA）；server 用它檢查預覽是否被直接改過，沒登記的改動不能採用。
- 只改了 server.py（沒動頁面）也要跑一次，--note 寫後端改了什麼，紀錄才不會漏。
- 這版有動到後端程式、資料庫或資料格式、依賴套件、架構，就加 --impact backend／db／deps／arch（可多個）。
  紀錄會標紅字「⚠ 這版動到…」，使用者回滾或採用時確認視窗會特別警告。
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import html
import json
import os
import re
import shutil
import sys

ROOT = os.environ.get("MEM0_MCP_ROOT", os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "data")))
PREVIEW = os.path.join(ROOT, "ui", "preview")
HISTORY = os.path.join(ROOT, "preview-history")
RECORD = os.path.join(PREVIEW, "publish-record.json")
PAGES = (("board.html", "board"), ("memory.html", "memory"), ("versions.html", "release"))
MARK = "<!-- releases -->"
IMPACT = {"backend": "後端程式", "db": "資料庫或資料格式", "deps": "依賴套件", "arch": "架構"}
def _tz_hours():
    """跟伺服器同一個時區：先看環境變數，再看安裝資料夾的 .env，都沒有就 8。
    .env 照 docker compose 的寫法讀：值可以加單／雙引號，沒加引號時「空白＋#」之後是註解，同名寫兩次以後面的為準。"""
    raw = os.environ.get("TZ_OFFSET_HOURS", "").strip()
    if not raw:
        try:
            with open(os.path.join(ROOT, os.pardir, ".env"), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("export "):
                        line = line[7:].lstrip()
                    key, eq, v = line.partition("=")
                    if key.strip() != "TZ_OFFSET_HOURS" or not eq:
                        continue
                    v = v.strip()
                    if v[:1] in ("'", '"'):
                        end = v.find(v[0], 1)
                        v = v[1:end] if end > 0 else v[1:]
                    else:
                        v = re.split(r"\s+#", v, maxsplit=1)[0]
                    raw = v.strip()
        except OSError:
            pass
    if not raw:
        return 8.0
    try:
        return float(raw)
    except ValueError:
        sys.exit("TZ_OFFSET_HOURS 不是數字：%r（例如 8、-5、5.5）" % raw)


TPE = dt.timezone(dt.timedelta(hours=_tz_hours()))


def fail(msg):
    sys.exit("放預覽失敗：" + msg)


def head_meta(text, kind, field):
    """只看 <head>，避免抓到頁面 JS 裡長得像 meta 的字串。"""
    head = text.split("</head>", 1)[0]
    m = re.search(r'<meta name="%s-%s" content="([^"]+)">' % (kind, field), head)
    return m.group(1) if m else None


def set_head_meta(text, kind, field, value):
    head, sep, rest = text.partition("</head>")
    new, n = re.subn(r'(<meta name="%s-%s" content=")[^"]+(">)' % (kind, field),
                     lambda m: m.group(1) + value + m.group(2), head, count=1)
    if n != 1:
        fail("%s 頁面 <head> 裡找不到 %s-%s" % (kind, kind, field))
    return new + sep + rest


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_atomic(path, data):
    tmp = path + ".publish-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o664)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="放公告欄預覽版並自動寫版本更新紀錄")
    ap.add_argument("--by", required=True, help="誰放的（圓桌名字，例如 claude、codex-laptop）")
    ap.add_argument("--note", action="append", default=[], help="白話說明，一點一個 --note")
    ap.add_argument("--board", help="新的 board.html（不給就沿用預覽現有的）")
    ap.add_argument("--memory", help="新的 memory.html")
    ap.add_argument("--versions", help="新的 versions.html（只有改版本頁版面時才需要）")
    ap.add_argument("--version", help="指定版號 X.Y.Z（要比現在的預覽版大）。加新功能升中間那碼；跳第一碼先問使用者。不給就把最後一碼加一（只是修正）")
    ap.add_argument("--impact", action="append", default=[], choices=sorted(IMPACT),
                    help="這版動到哪裡：backend 後端程式、db 資料庫或資料格式、deps 依賴套件、arch 架構（可多個）")
    ap.add_argument("--dry-run", action="store_true", help="只顯示會怎麼改，不寫檔")
    a = ap.parse_args()

    notes = [n.strip() for n in a.note if n.strip()]
    if not notes:
        fail("至少要一個 --note，說明這次改了什麼")
    if any(len(n) > 400 for n in notes):
        fail("每個 --note 400 字以內，太長請拆開")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", a.by):
        fail("--by 請填圓桌名字（小寫英數與 -）")
    impact = [k for k in IMPACT if k in a.impact]
    if not impact and any(re.search(r"後端|server\.py|伺服器|資料庫|套件|架構", n) for n in notes):
        print("注意：說明提到後端／伺服器／資料庫，但沒加 --impact；真的有動到請加，回滾時才會警告使用者。")

    if not a.dry_run:  # dry-run 只計算、不建資料夾也不寫鎖檔
        os.makedirs(HISTORY, exist_ok=True)
        lock = open(os.path.join(HISTORY, ".lock"), "a")  # 用 a 開，不截斷既有內容
        fcntl.flock(lock, fcntl.LOCK_EX)

    old = {}
    for name, _ in PAGES:
        with open(os.path.join(PREVIEW, name), "rb") as f:
            old[name] = f.read()

    if os.path.isfile(RECORD):
        with open(RECORD, encoding="utf-8") as f:
            rec = json.load(f)
        drift = [n for n, _ in PAGES if rec.get("sha256", {}).get(n) != sha(old[n])]
        if drift:
            print("注意：上次登記後，預覽的 %s 被直接改過；這次的 --note 要把那些改動也寫進去。" % "、".join(drift))

    new = dict(old)
    for name, src in (("board.html", a.board), ("memory.html", a.memory), ("versions.html", a.versions)):
        if src:
            with open(src, "rb") as f:
                new[name] = f.read()
    text = {n: new[n].decode("utf-8") for n, _ in PAGES}

    revs = []
    for name, kind in PAGES:
        r = head_meta(text[name], kind, "revision")
        if not r or not r.isdigit():
            fail("%s 沒有 %s-revision" % (name, kind))
        revs.append(int(r))
    # 版號只用 X.Y.Z：每放一次預覽就是新的一號，採用就是那一號上正式版
    base = re.sub(r"-rc\.\d+$", "", head_meta(old["board.html"].decode("utf-8"), "board", "version") or "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", base):
        fail("現在的預覽版號看不懂：%r" % base)
    cur = tuple(int(x) for x in base.split("."))
    if a.version:
        if not re.fullmatch(r"\d+\.\d+\.\d+", a.version):
            fail("--version 只能是 X.Y.Z 三段數字（不加 -rc）：%r" % a.version)
        if tuple(int(x) for x in a.version.split(".")) <= cur:
            fail("--version %s 要比現在的預覽版 %s 大" % (a.version, base))
        version = a.version
    else:
        version = "%d.%d.%d" % (cur[0], cur[1], cur[2] + 1)
    vt = text["versions.html"]
    if vt.count(MARK) != 1:
        fail("versions.html 要剛好有一個 %s 標記" % MARK)
    if re.search(r"<h3>v%s<" % re.escape(version), vt):
        fail("v%s 已經有紀錄，換一個版號" % version)
    listed = {int(x) for x in re.findall(r'<section class="rel[^"]*" data-rev="(\d+)"', vt)}
    # 內部流水號一路往上加；已經有人手動把某頁改成新號但沒寫紀錄，就用那個號補登
    top = max(revs)
    target = top if top not in listed else top + 1
    if target in listed:
        fail("內部流水號 %d 已經有紀錄" % target)

    now = dt.datetime.now(TPE)
    # data-rev 只給程式比對用（採用、回滾、標「正式版已包含」），畫面上只寫 vX.Y.Z
    entry = ('<section class="rel" data-rev="%d"%s id="r%d"><h3>v%s<span class="badge" data-badge></span></h3>'
             '<p>%s放上預覽 · 負責：%s</p>%s<ul>%s</ul></section>'
             % (target, ' data-impact="%s"' % ",".join(impact) if impact else "", target, html.escape(version),
                now.strftime("%Y-%m-%d %H:%M"), html.escape(a.by),
                '<p class="impact">⚠ 這版動到：%s</p>' % "、".join(IMPACT[k] for k in impact) if impact else "",
                "".join("<li>%s</li>" % html.escape(n) for n in notes)))
    vt = vt.replace(MARK, MARK + "\n" + entry, 1)
    vt, _ = re.subn(r"(<b data-now>)[^<]*(</b>)", lambda m: "%sv%s%s" % (m.group(1), html.escape(version), m.group(2)), vt, count=1)
    text["versions.html"] = vt

    for name, kind in PAGES:
        t = set_head_meta(text[name], kind, "revision", str(target))
        text[name] = set_head_meta(t, kind, "version", version)
    out = {n: text[n].encode("utf-8") for n, _ in PAGES}

    changed = [n for n, _ in PAGES if out[n] != old[n]]
    print("v%s → v%s（內部流水號 %s → %d）；會改：%s" % (base, version, "/".join(map(str, revs)), target, "、".join(changed)))
    print("紀錄：" + entry)
    if a.dry_run:
        print("（dry-run，沒有寫檔）")
        return

    backup = os.path.join(HISTORY, "%s-r%d" % (now.strftime("%Y%m%d-%H%M%S-%f"), max(revs)))
    os.makedirs(backup)
    for name, _ in PAGES:
        shutil.copy2(os.path.join(PREVIEW, name), os.path.join(backup, name))
    with open(os.path.join(backup, "publish.json"), "w", encoding="utf-8") as f:
        json.dump({"by": a.by, "at": now.isoformat(), "from": revs, "to": target, "notes": notes}, f, ensure_ascii=False, indent=1)

    for name, _ in PAGES:
        write_atomic(os.path.join(PREVIEW, name), out[name])
    record = {"revision": target, "version": version, "by": a.by, "at": now.isoformat(),
              "sha256": {n: sha(out[n]) for n, _ in PAGES}}
    write_atomic(RECORD, json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8"))

    for name, kind in PAGES:  # 讀回核對
        with open(os.path.join(PREVIEW, name), "rb") as f:
            back = f.read()
        if sha(back) != record["sha256"][name] or head_meta(back.decode("utf-8"), kind, "revision") != str(target):
            fail("%s 讀回不符，請用備份 %s 還原" % (name, backup))
    print("完成：預覽版現在是 v%s（內部流水號 %d）。備份在 %s" % (version, target, backup))
    print("還原：cp %s/{board,memory,versions}.html %s/ 之後再跑一次本工具登記" % (backup, PREVIEW))
    print("提醒：到 /board/preview 確認畫面，再請使用者決定要不要按「採用成正式版」。")


if __name__ == "__main__":
    main()
