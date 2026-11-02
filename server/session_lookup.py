"""找收件人。

外來的 AI 不知道某件事該寄給哪一串時，用關鍵字比對兩個來源：
- 路由表（session-routes.json）：每一串的 label、title、projects、topics；
- 公告欄白板的專案卡：專案代號、「名稱」「做事」兩行。
回最像的 1～3 個收件地址（claude-laptop:<label>、codex-laptop 這種）、負責的專案、一句說明。
只做純比對，不碰網路、不讀寫記憶；伺服器把路由表和專案卡傳進來。
"""
import re

ASCII_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?")
CJK_RE = re.compile(r"[㐀-鿿豈-﫿]+")
FIELD_RE = re.compile(r"^\s*(名稱|主管|做事|現在)\s*[:：]\s*(.+?)\s*$")
TAG_RE = re.compile(r"^(\s*\[[^\]]{1,6}\]\s*)+")  # 專案卡開頭的「[狀態] 」
STOP = {"的", "了", "和", "與", "跟", "要", "請", "誰", "給", "寄", "信", "哪", "一", "個", "串", "是", "在", "有", "我"}
MIN_SCORE = 1.0  # 至少要有一個詞對上


def terms(text: str) -> list:
    """問句拆成比對用的詞：英數整個詞，中文切成兩字一組（只有一個字就用那個字）。"""
    s = (text or "").lower()
    out = ASCII_RE.findall(s)
    for run in CJK_RE.findall(s):
        if len(run) == 1:
            if run not in STOP:
                out.append(run)
            continue
        out += [run[i:i + 2] for i in range(len(run) - 1)]
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def card_fields(text: str) -> dict:
    """專案卡內文裡的「名稱／主管／做事／現在」。"""
    out = {}
    for ln in TAG_RE.sub("", text or "").splitlines():
        m = FIELD_RE.match(ln)
        if m and m.group(1) not in out:
            out[m.group(1)] = m.group(2)
    return out


def _hits(qterms: list, hay: str) -> list:
    return [t for t in qterms if t in hay]


def _workers(doer: str, agents: list, claude_base: str) -> list:
    """「做事」那行提到誰：回 ('claude', '') 或 ('codex', 地址) 的清單，照提到的先後。"""
    s = (doer or "").lower()
    found = []
    if "codex" in s:  # 「Codex laptop」「codex-pc」都認；只寫 Codex 沒寫哪台就寄 codex（全部 Codex）
        for a in agents:
            dev = a.split("-", 1)[1] if a.startswith("codex-") else ""
            if dev and dev in s:
                found.append((s.find(dev), "codex", a))
        if not found:
            found.append((s.find("codex"), "codex", "codex"))
    if "claude" in s or "cc" in re.findall(r"[a-z]+", s):
        found.append((s.find("claude") if "claude" in s else s.find("cc"), "claude", claude_base))
    found.sort(key=lambda x: x[0])
    return [(kind, addr) for _, kind, addr in found]


def rank(query: str, routes: dict, cards: list, agents: list, claude_base: str = "claude-laptop",
         exclude: set | None = None, limit: int = 3) -> list:
    """routes：session-routes.json 的內容；cards：[{project, text}]；agents：目前有權杖的身分。
    回 [{to, projects, why, score}]，分數高的在前，最多 limit 個；都沒對上回 []。"""
    exclude = exclude or set()
    qterms = terms(query)
    if not qterms:
        return []
    qwords = set(ASCII_RE.findall((query or "").lower()))
    cand = {}

    def put(to, score, projects, why):
        if not to or to in exclude or score < MIN_SCORE:
            return
        c = cand.get(to)
        if not c or score > c["score"]:
            cand[to] = {"to": to, "projects": list(projects), "why": why, "score": round(score, 2)}
        elif c:
            for p in projects:
                if p not in c["projects"]:
                    c["projects"].append(p)

    by_project = {}  # 專案 → 訂閱它的串（只算不是 * 的）
    for v in (routes or {}).values():
        if not isinstance(v, dict):
            continue
        label = str(v.get("label") or "")
        if not label or label.startswith("inbox-test"):
            continue
        projects = [str(p) for p in (v.get("projects") or []) if p and p != "*"]
        for p in projects:
            by_project.setdefault(p, []).append(label)
        title = str(v.get("title") or "")
        topics = [str(t) for t in (v.get("topics") or [])]
        score = 0.0
        score += 3.0 * len(qwords & set(projects))
        score += 2.0 * (label in qwords)
        th = _hits(qterms, " ".join(topics).lower())
        score += 1.5 * len(th)
        score += 1.0 * len([t for t in _hits(qterms, title.lower()) if t not in th])
        short = re.split(r"[（(]", title, 1)[0].strip() or label
        why = f"{short}" + (f"；負責 {', '.join(projects)}" if projects else "") + (f"；管 {'、'.join(topics[:3])}" if topics else "")
        put(f"{claude_base}:{label}", score, projects, why)

    for c in cards or []:
        p = str(c.get("project") or "")
        f = card_fields(c.get("text") or "")
        name, doer = f.get("名稱", ""), f.get("做事", "")
        score = 3.0 * (p in qwords)
        score += 1.0 * len(_hits(qterms, (p + " " + name).lower()))
        if score < MIN_SCORE:
            continue
        for kind, addr in _workers(doer, agents, claude_base) or [("claude", claude_base)]:
            if kind == "claude":
                subs = by_project.get(p) or []
                to = f"{claude_base}:{subs[0]}" if len(subs) == 1 else claude_base
            else:
                to = addr
            why = f"專案卡「{name or p}」的做事：{doer or '沒寫'}"
            put(to, score, [p], why)

    out = sorted(cand.values(), key=lambda x: (-x["score"], x["to"]))
    return out[:max(1, min(3, int(limit or 3)))]


def navigator(routes: dict) -> str:
    """預設收件人串的 label（projects 有 * 的那一串）；沒有就回空字串。"""
    for v in (routes or {}).values():
        if isinstance(v, dict) and "*" in (v.get("projects") or []) and v.get("label"):
            return str(v["label"])
    return ""
