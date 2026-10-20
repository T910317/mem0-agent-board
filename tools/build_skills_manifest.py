"""重新產生共用技能庫的 manifest.json。

用法：python3 tools/build_skills_manifest.py data/skills
技能放在 data/skills/skills/<名字>/SKILL.md，開頭要有 YAML 前言（name、description）。
sha256 取整個技能資料夾內容的雜湊前 16 碼，只當版本標記用：AI 端的同步工具看到不一樣才重新下載。
"""
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "data/skills")
rows = []
for d in sorted((root / "skills").iterdir() if (root / "skills").is_dir() else []):
    md = d / "SKILL.md"
    if not md.is_file() or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,80}", d.name):
        continue
    text = md.read_text(encoding="utf-8", errors="replace")
    front = re.match(r"^---\n(.*?)\n---", text, re.S)
    desc = ""
    if front:
        m = re.search(r"^description:\s*(.+)$", front.group(1), re.M)
        desc = m.group(1).strip().strip('"\'') if m else ""
    h = hashlib.sha256()
    for f in sorted(p for p in d.rglob("*") if p.is_file()):
        h.update(str(f.relative_to(d)).encode() + b"\0" + f.read_bytes())
    rows.append({"name": d.name, "description": desc, "sha256": h.hexdigest()[:16]})
out = {"generated": dt.date.today().isoformat(), "skills": rows}
(root / "manifest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print(f"已寫入 {root / 'manifest.json'}：{len(rows)} 個技能")
