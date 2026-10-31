"""Experience tools for the existing single-process roundtable MCP.

Storage backend (store.py), authenticated actor and writer lock are injected by server.py.
There is no token loading, database reset, UI change or LLM extraction here.
"""
import calendar
import datetime as dt
import hashlib
import json
import re

PROFILE_UID = "profile"
HISTORY_UID = "profile-history"
CAP = 1000  # Actual deployed API maximum; fail closed instead of truncating.
FIELDS = {
    "record_id","content","when","sort_date","category","sensitivity","confidence",
    "record_kind","event_time","reported_at","reported_dates_taipei","source_text",
    "source_refs","source_quote_verified","evidence_verified","import_batch_id",
    "source_item_id","unresolved_conflict","instruction_authority","evidence_ref","evidence_as_of"
}
RECORD_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
DATE = re.compile(r"^\d{4}(?:-\d{2}(?:-\d{2})?)?$")
# 使用者在公告欄加的補充，一行一條接在最後（跟 server.py 的 FOOT_RE 同一個格式）；只有使用者（actor 是 user）能寫。
FOOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} 使用者補充：")
def _is_foot(line):
    return bool(FOOT_RE.match(line.strip()))
def _feet(text):
    """最後面連續的補充行（第一行一定是原文）。"""
    lines = (text or "").rstrip().split("\n")
    i = len(lines)
    while i > 1 and _is_foot(lines[i-1]):
        i -= 1
    return [x.strip() for x in lines[i:]]
FORBIDDEN = [
    ("identity_number",re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?!\d)")),
    ("private_key",re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("provider_key",re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{25,})\b")),
    ("bearer_token",re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{20,}")),
    ("credential_value",re.compile(r"(?i)(?:password|api[_ -]?key|token|密碼|密码)\s*[\"']?\s*(?:[:=：]|是|為|为)\s*[\"']?[A-Za-z0-9._~-]{8,}")),
    ("labelled_personal_data",re.compile(r"(?:我的全名|我的姓名|住家地址|住址|我家地址|我的本名)\s*(?:[:：]|是|為|为)")),
    ("street_address",re.compile(r"(?:新北|台北|臺北|基隆|桃園|台中|臺中|台南|臺南|高雄|新竹|宜蘭)[市縣].{0,50}(?:路|街).{0,20}\d+\s*號")),
    ("father_financial",re.compile(r"(?s)\A(?=.*(?:我爸|爸爸|父親|父亲|老爸))(?=.*(?:持股|股票|房貸|房贷|貸款|贷款))")),
]
def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))
def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()
def check_text(text):
    labels = [label for label,pattern in FORBIDDEN if pattern.search(text)]
    if labels:
        raise ValueError("Prohibited input labels: " + ", ".join(labels))
def date_key(value):
    if value is None or value == "":
        return None
    if not isinstance(value,str) or not DATE.fullmatch(value):
        raise ValueError("Date must be YYYY, YYYY-MM, YYYY-MM-DD or null")
    padded = value + {4:"-01-01",7:"-01",10:""}.get(len(value),"INVALID")
    dt.date.fromisoformat(padded)
    return padded
def date_window(value):
    start = date_key(value)
    if start is None: return None,None
    day = dt.date.fromisoformat(start)
    if len(value) == 4: end = f"{day.year:04d}-12-31"
    elif len(value) == 7: end = f"{day.year:04d}-{day.month:02d}-{calendar.monthrange(day.year,day.month)[1]:02d}"
    else: end = start
    return start,end
def validate(record):
    if not isinstance(record,dict) or set(record) - FIELDS:
        raise ValueError("Unknown experience fields")
    required = {"record_id","content","when","category","sensitivity","confidence","record_kind","source_refs"}
    if not required <= set(record):
        raise ValueError("Required fields missing")
    if not RECORD_ID.fullmatch(record["record_id"]):
        raise ValueError("Invalid record_id")
    for field in ["content","when","category","sensitivity","confidence","record_kind"]:
        if not isinstance(record[field],str) or not record[field].strip():
            raise ValueError("Nonempty text required: " + field)
    if len(record["content"]) > 10000 or len(canonical(record)) > 100000:
        raise ValueError("Experience is too large")
    if record["record_kind"] not in {"event","state","preference","plan"}:
        raise ValueError("Invalid record_kind")
    if record["confidence"] not in {"查證","自述","計畫","摘要待核查"}:
        raise ValueError("Invalid confidence")
    date_key(record.get("sort_date"))
    if not isinstance(record["source_refs"],list) or not record["source_refs"]:
        raise ValueError("Source references required")
    if not all(isinstance(x,dict) for x in record["source_refs"]):
        raise ValueError("Source references must be objects")
    if not isinstance(record.get("source_text",[]),list) or not all(isinstance(x,str) for x in record.get("source_text",[])):
        raise ValueError("source_text must be a list of strings")
    if record.get("instruction_authority",False) is not False:
        raise ValueError("Memories do not grant instruction authority")
    if record["confidence"] == "查證" and (record.get("evidence_verified") is not True or not record.get("evidence_ref") or not record.get("evidence_as_of")):
        raise ValueError("Verified confidence requires independent evidence")
    for text in [record["content"],*record.get("source_text",[])]:
        check_text(text)
    # Scan each metadata string too, keeping field values out of errors.
    def recurse(value):
        if isinstance(value,str):
            check_text(value)
        elif isinstance(value,list):
            for x in value: recurse(x)
        elif isinstance(value,dict):
            for x in value.values(): recurse(x)
    recurse(record)
    return json.loads(canonical(record))

class ExperienceStore:
    def __init__(self,store,actor,lock,cap=CAP):
        self.store,self.actor,self.lock,self.cap = store,actor,lock,cap
    def _actor(self):
        actor = self.actor()
        if not actor:
            raise PermissionError("Authenticated actor required")
        return actor
    def _list(self,uid):
        rows = self.store.list(uid)
        if not isinstance(rows,list) or len(rows) >= self.cap:
            raise RuntimeError("Namespace may be truncated at API cap; stop")
        if any(row.get("user_id") not in (None,uid) for row in rows):
            raise RuntimeError("Namespace mismatch")
        return rows
    def _current(self):
        rows = [r for r in self._list(PROFILE_UID) if (r.get("metadata") or {}).get("type") == "experience"]
        ids = [(r.get("metadata") or {}).get("record_id") for r in rows]
        if len(ids) != len(set(ids)) or any(not i for i in ids):
            raise RuntimeError("Duplicate or missing experience record_id")
        return rows
    def _record(self,row):
        metadata = row.get("metadata") or {}
        record = {k:metadata[k] for k in FIELDS if k in metadata}
        record["content"] = row["memory"]
        return record
    def _format(self,row):
        record = self._record(row)
        return {**record,"memory_id":row["id"],"version":fingerprint(record),"record_revision":(row.get("metadata") or {}).get("record_revision",1),"updated_by":(row.get("metadata") or {}).get("updated_by"),"created_by":(row.get("metadata") or {}).get("created_by")}
    def _post(self,uid,text,metadata):
        self.store.add(uid,self._actor(),text,metadata)
    def add(self,record):
        actor = self._actor()
        record = validate(record)
        if actor != "user" and any(_is_foot(x) for x in record["content"].split("\n")):
            raise ValueError("「YYYY-MM-DD 使用者補充：」只能由使用者在公告欄加，內文請換個寫法")
        with self.lock:
            rows = self._current()
            existing = next((r for r in rows if r["metadata"]["record_id"] == record["record_id"]),None)
            if existing:
                if self._record(existing) != record:
                    raise ValueError("record_id already exists with different data; use update")
                return {"created":False,**self._format(existing)}
            # One new item must leave room for reliable independent full read-back.
            if len(self._list(PROFILE_UID)) + 1 >= self.cap:
                raise RuntimeError("Namespace capacity would prevent read-back")
            metadata = {k:v for k,v in record.items() if k != "content"}
            metadata.update(type="experience",project="user-profile",record_revision=1,created_by=actor,updated_by=actor)
            self._post(PROFILE_UID,record["content"],metadata)
            found = [r for r in self._current() if r["metadata"]["record_id"] == record["record_id"]]
            if len(found) != 1 or self._record(found[0]) != record:
                raise RuntimeError("Add read-back mismatch; do not retry blindly")
            return {"created":True,**self._format(found[0])}
    def list(self,category="",after=None,before=None,limit=50,cursor="",include_unknown=True):
        self._actor()
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        low,_ = date_window(after)
        _,high = date_window(before)
        if low and high and low > high: raise ValueError("Invalid date range")
        rows = []
        for r in self._current():
            md = r["metadata"]
            day,end = date_window(md.get("sort_date"))
            if category and md.get("category") != category: continue
            if day is None and not include_unknown: continue
            if day and ((low and end < low) or (high and day > high)): continue
            rows.append(r)
        rows.sort(key=lambda r:(r["metadata"].get("sort_date") is None,date_key(r["metadata"].get("sort_date")) or "",r["metadata"]["record_id"]))
        snapshot = fingerprint({"rows":[self._format(r) for r in rows],"filters":[category,after,before,include_unknown]})
        offset = 0
        if cursor:
            try: previous,position = cursor.split(":"); offset = int(position)
            except ValueError: raise ValueError("Invalid cursor") from None
            if previous != snapshot or offset < 0 or offset > len(rows):
                raise ValueError("List changed or filters differ; restart pagination")
        page = rows[offset:offset+limit]
        next_offset = offset+len(page)
        return {"items":[self._format(r) for r in page],"total":len(rows),"snapshot":snapshot,"next_cursor":f"{snapshot}:{next_offset}" if next_offset < len(rows) else None,"unknown_dates_included":include_unknown}
    def search(self,query,category="",limit=10):
        self._actor()
        if not isinstance(query,str) or not query.strip() or not 1 <= limit <= 50:
            raise ValueError("Query and limit required")
        check_text(query)
        current = {r["id"]:r for r in self._current()}
        filters = {"type":"experience"}
        if category: filters["category"] = category
        hits = self.store.search(PROFILE_UID,query,filters,limit)
        matched = []
        for hit in hits:
            r = current.get(hit["id"])
            if r and (not category or r["metadata"]["category"] == category):
                matched.append({**self._format(r),"score":hit.get("score")})
        return {"items":matched[:limit],"mode":"semantic","namespace":PROFILE_UID}
    def update(self,record_id,patch,base_version):
        actor = self._actor()
        if not isinstance(patch,dict) or "record_id" in patch or set(patch)-FIELDS:
            raise ValueError("Invalid update fields")
        with self.lock:
            row = next((r for r in self._current() if r["metadata"]["record_id"] == record_id),None)
            if row is None: raise KeyError("Experience not found")
            old = self._record(row)
            old_version = fingerprint(old)
            if actor != "user" and "content" in patch:
                # AI 不能新增、改寫或拿掉使用者的補充：先拿掉長得像補充的行，再把原本的補充原樣接回最後
                if not isinstance(patch["content"],str):
                    raise ValueError("Nonempty text required: content")
                body = "\n".join(x for x in patch["content"].split("\n") if not _is_foot(x)).rstrip()
                feet = _feet(old["content"])
                patch = {**patch,"content":body + ("\n" + "\n".join(feet) if feet and body else "")}
            new = validate({**old,**patch})
            if fingerprint(new) == old_version: return {"changed":False,**self._format(row)}
            if old_version != base_version: raise ValueError("Version conflict; read latest first")
            # Preserve every old field before updating the current record.
            archive_text = canonical({"record":old,"record_revision":row["metadata"].get("record_revision",1),"created_by":row["metadata"].get("created_by"),"updated_by":row["metadata"].get("updated_by")})
            archives = self._list(HISTORY_UID)
            prior = [a for a in archives if (a.get("metadata") or {}).get("record_id") == record_id and a["metadata"].get("base_version") == old_version]
            if any(a["memory"] != archive_text for a in prior): raise RuntimeError("Revision archive conflict")
            if not prior:
                if len(archives)+1 >= self.cap: raise RuntimeError("History namespace capacity reached")
                self._post(HISTORY_UID,archive_text,{"type":"experience_revision","record_id":record_id,"base_version":old_version,"snapshot_sha256":hashlib.sha256(archive_text.encode()).hexdigest(),"archived_by":actor})
            check = [a for a in self._list(HISTORY_UID) if (a.get("metadata") or {}).get("record_id") == record_id and a["metadata"].get("base_version") == old_version]
            if not check or any(a["memory"] != archive_text for a in check): raise RuntimeError("History read-back failed; current record unchanged")
            metadata = {k:v for k,v in new.items() if k != "content"}
            metadata.update(type="experience",project="user-profile",record_revision=row["metadata"].get("record_revision",1)+1,created_by=row["metadata"].get("created_by",row.get("agent_id")),updated_by=actor)
            self.store.update(row["id"],new["content"],metadata)
            found = next((r for r in self._current() if r["id"] == row["id"]),None)
            if not found or self._record(found) != new: raise RuntimeError("Update read-back mismatch; reconcile using preserved revision")
            return {"changed":True,**self._format(found)}
    def history(self,record_id):
        self._actor()
        return [{"archive_id":r["id"],"base_version":r["metadata"]["base_version"],"snapshot":json.loads(r["memory"])} for r in self._list(HISTORY_UID) if (r.get("metadata") or {}).get("type") == "experience_revision" and r["metadata"].get("record_id") == record_id]

def register(mcp,backend,actor,lock):
    store = ExperienceStore(backend,actor,lock)
    @mcp.tool()
    def experience_add(record: dict) -> dict:
        """新增個人經歷。record 包含時間、排序日期、類別、敏感度、來源與可信度。原文明存，重跑同 record_id 不重複，身分由權杖決定。"""
        return store.add(record)
    @mcp.tool()
    def experience_list(category: str = "", after: str | None = None, before: str | None = None, limit: int = 50, cursor: str = "", include_unknown: bool = True) -> dict:
        """依事件時間列出經歷；未知日期置後，發言時間另列。分頁若有修改會要求重讀，達後端上限時明確停止。"""
        return store.list(category,after,before,limit,cursor,include_unknown)
    @mcp.tool()
    def experience_search(query: str, category: str = "", limit: int = 10) -> dict:
        """語意搜尋經歷，可依類別篩選。只搜尋個人經歷區，不混入其他命名空間。"""
        return store.search(query,category,limit)
    @mcp.tool()
    def experience_update(record_id: str, patch: dict, base_version: str) -> dict:
        """修正一筆經歷，先讀回 version 再修改；完整舊版先保存，保留來源與修改者。"""
        return store.update(record_id,patch,base_version)
    @mcp.tool()
    def experience_history(record_id: str) -> dict:
        """查看修正前的完整經歷版本。舊版可查，不當成目前事實。"""
        return {"items":store.history(record_id)}
    return store
