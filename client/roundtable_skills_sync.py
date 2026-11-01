#!/usr/bin/env python3
"""把圓桌共用技能庫同步到本機某個技能資料夾。

只管理自己裝過的技能（資料夾裡有 .rt-sha 標記的），其他技能一律不碰。
用法：python3 roundtable_skills_sync.py --dest ~/.codex/skills --token-file ~/.roundtable-token
      （或 --token-env ROUNDTABLE_MCP_TOKEN）
"""
import argparse
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request

BASE = os.environ.get("RT_SKILLS_URL", "http://localhost:8890/skills").rstrip("/")
MARK = ".rt-sha"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 技能下載的權杖也不能帶到轉址目的地。


def get(url: str, token: str) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=60) as r:
        return r.read()


NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")


def inside(root: str, path: str) -> bool:
    root, path = os.path.realpath(root), os.path.realpath(path)
    return os.path.commonpath([root, path]) == root and path != root


def safe_extract(blob: bytes, name: str, tmp: str) -> None:
    """只解出「技能名/」底下的一般檔案與資料夾；連結、裝置檔、絕對路徑、..、跑到別的根目錄的一律拒絕。"""
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            parts = m.name.split("/")
            if (m.name.startswith("/") or "\\" in m.name or ".." in parts or parts[0] != name
                    or not (m.isfile() or m.isdir()) or not inside(tmp, os.path.join(tmp, m.name))):
                raise RuntimeError(f"不安全的封包成員：{m.name!r}")
        try:
            tf.extractall(tmp, members=members, filter="data")  # Python 3.12+／修補過的 3.8+ 有這個過濾器
        except TypeError:
            tf.extractall(tmp, members=members)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True)
    ap.add_argument("--token-file")
    ap.add_argument("--token-env")
    ap.add_argument("--adopt", action="store_true",
                    help="同名但沒有標記的本機技能也改由共用庫管理（原資料夾先搬到 --backup）")
    ap.add_argument("--backup")
    a = ap.parse_args()
    token = (open(os.path.expanduser(a.token_file)).read() if a.token_file else os.environ.get(a.token_env or "", "")).strip()
    if not token:
        print("沒有權杖", file=sys.stderr)
        return 2
    dest = os.path.expanduser(a.dest)
    os.makedirs(dest, exist_ok=True)
    skills = json.loads(get(f"{BASE}/manifest.json", token))["skills"]
    want = {s["name"]: s["sha256"] for s in skills}
    added = updated = skipped = removed = 0
    for name in want:
        if not NAME_RE.match(name or ""):
            print(f"共用庫的技能名稱不合規則，整批停止：{name!r}", file=sys.stderr)
            return 2
    for name, sha in want.items():
        d = os.path.join(dest, name)
        if not inside(dest, d):
            print(f"技能路徑跑出目的資料夾，整批停止：{name!r}", file=sys.stderr)
            return 2
        mark = os.path.join(d, MARK)
        adopt = os.path.isdir(d) and not os.path.isfile(mark)
        if adopt:
            if not a.adopt:
                skipped += 1
                print(f"略過（本機已有同名、未受管）：{name}")
                continue
            if not a.backup:
                print("--adopt 需要 --backup", file=sys.stderr)
                return 2
        elif os.path.isfile(mark) and open(mark).read().strip() == sha:
            continue
        is_new = not os.path.isdir(d)
        tmp = tempfile.mkdtemp(dir=dest, prefix=".rt-")
        try:  # 先在暫存資料夾下載、檢查完新封包，成功了才動本機原本的資料夾
            safe_extract(get(f"{BASE}/{name}.tar.gz", token), name, tmp)
            if not os.path.isfile(os.path.join(tmp, name, "SKILL.md")):
                raise RuntimeError(f"封包裡沒有 {name}/SKILL.md")
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        with open(os.path.join(tmp, name, MARK), "w") as f:
            f.write(sha)
        if adopt:  # 原本未受管的同名技能搬去備份（新封包已確認可用）
            os.makedirs(os.path.expanduser(a.backup), exist_ok=True)
            shutil.move(d, os.path.join(os.path.expanduser(a.backup), name))
        elif os.path.isdir(d):
            shutil.rmtree(d)
        os.rename(os.path.join(tmp, name), d)
        shutil.rmtree(tmp, ignore_errors=True)
        added += is_new
        updated += not is_new
    for name in os.listdir(dest):
        d = os.path.join(dest, name)
        if name not in want and not os.path.islink(d) and os.path.isfile(os.path.join(d, MARK)):
            shutil.rmtree(d)
            removed += 1
    print(f"完成：新增 {added}、更新 {updated}、移除 {removed}、略過 {skipped}（共用庫 {len(want)} 個）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
