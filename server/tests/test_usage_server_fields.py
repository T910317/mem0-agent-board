"""額度頁伺服器新欄位（usage.py）：m5／credits／hook error 存得到並隨 view() 回傳、
hook 的 version 依機器記成 codex_version＋hook_last_seen（伺服器時間）、只帶 hook 的回報不動「更新時間」也不清舊額度。
資料庫在暫存資料夾，不連任何真實服務。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import usage  # noqa: E402

T0 = 1_793_000_000.0
FULL = {"id": "codex", "name": "Codex", "windows": [{"key": "weekly", "label": "每週", "used": 40,
                                                      "resets_at": T0 + 86400, "minutes": 10080}]}


def fresh_db():
    usage._conn(os.path.join(tempfile.mkdtemp(prefix="usage-fields-"), "usage.db"))


def prov(v, pid="codex"):
    return next(p for p in v["providers"] if p["id"] == pid)


def test_new_fields_stored_and_returned():
    fresh_db()
    now = T0
    m5 = [[usage._iso(now - 600), 1200], [usage._iso(now - 300), 800], [usage._iso(now - 300), 900]]  # 同一格後面蓋前面
    cr = {"balance": 57032.84, "has_credits": True, "unlimited": False, "seen_at": "2026-10-28T19:44:00Z",
          "changes": [{"at": "2026-10-28T19:00:00Z", "from": 57035.84, "to": 57032.84}]}
    r = usage.put({"source": "pc", "providers": [dict(FULL, m5=m5, credits=cr,
                   hook={"name": "StopFailure", "error": "rate_limit"})]}, "codex-laptop", now=now)
    assert r["m5"] == 2 and r["credits"] == 1
    p = prov(usage.view(now=now + 10))
    fl = lambda t: usage._iso(t // 300 * 300)  # 時間 floor 到 5 分鐘
    assert p["m5"] == [[fl(now - 600), 1200], [fl(now - 300), 900]]
    assert p["credits"]["balance"] == 57032.84 and p["credits"]["changes"][0]["to"] == 57032.84
    assert p["credits"]["has_credits"] is True and p["credits"]["unlimited"] is False
    assert p["hook_error"] == "rate_limit" and p["hook_error_ts"] == usage._iso(now)


def test_new_fields_kept_when_not_sent_and_junk_dropped():
    fresh_db()
    now = T0
    usage.put({"providers": [dict(FULL, m5=[[usage._iso(now - 300), 5]], credits={"balance": 10})]}, "x", now=now)
    usage.put({"providers": [dict(FULL)]}, "x", now=now + 60)                      # 沒帶 → 沿用
    usage.put({"providers": [dict(FULL, m5="亂", credits=[1, 2])]}, "x", now=now + 120)  # 格式不對 → 沿用
    usage.put({"providers": [dict(FULL, m5=[["不是時間", 1], [usage._iso(now), -5], [usage._iso(now), "x"]])]}, "x", now=now + 180)
    p = prov(usage.view(now=now + 200))
    assert p["credits"]["balance"] == 10
    assert p["m5"] == []  # 最後一次是合法陣列但格子全不合格 → 空


def test_version_per_machine_and_hook_last_seen_is_server_time():
    fresh_db()
    now = T0
    usage.put({"providers": [dict(FULL)]}, "codex-pc", now=now)
    usage.put({"providers": [{"id": "codex", "hook": {"name": "Codex Stop hook", "version": "0.165.0", "ts": now - 99999}}]},
              "codex-laptop", now=now + 100)
    usage.put({"providers": [{"id": "codex", "hook": {"name": "Codex Stop hook", "version": "0.170.1"}}]},
              "codex-max:thread-1", now=now + 200)
    v = usage.view(now=now + 300)
    m = v["codex_machines"]
    assert m["codex-laptop"] == {"codex_version": "0.165.0", "hook_last_seen": usage._iso(now + 100), "hook_ms": []}  # 不信回報裡的 ts
    assert m["codex-max"]["codex_version"] == "0.170.1" and m["codex-max"]["hook_last_seen"] == usage._iso(now + 200)
    assert "codex-mini" not in m
    assert prov(v)["codex_machines"].keys() == {"codex-laptop", "codex-max"}
    # 之後的 ping 沒帶 version：版本沿用，時間更新
    usage.put({"providers": [{"id": "codex", "hook": {"name": "Codex Stop hook"}}]}, "codex-laptop", now=now + 400)
    m = usage.view(now=now + 500)["codex_machines"]
    assert m["codex-laptop"] == {"codex_version": "0.165.0", "hook_last_seen": usage._iso(now + 400), "hook_ms": []}


def test_non_codex_sender_does_not_get_a_codex_version():
    fresh_db()
    usage.put({"providers": [{"id": "claude", "hook": {"name": "Stop hook", "version": "9.9"}}]}, "claude-laptop", now=T0)
    assert usage.view(now=T0 + 1)["codex_machines"] == {}


def test_hook_only_report_keeps_updated_time_and_old_quota():
    fresh_db()
    usage.put({"providers": [dict(FULL)]}, "pc", now=T0)
    before = prov(usage.view(now=T0 + 10))
    assert before["updated"] == usage._iso(T0) and before["windows"][0]["used"] == 40
    usage.put({"providers": [{"id": "codex", "hook": {"name": "Codex Stop hook", "version": "0.165.0"}}]}, "codex-laptop", now=T0 + 3600)
    after = prov(usage.view(now=T0 + 3700))
    assert after["updated"] == usage._iso(T0)             # 多久前更新不變
    assert after["windows"][0]["used"] == 40 and after["source"] == before["source"]
    assert after["hook_ts"] == usage._iso(T0 + 3600)
    # 第一次就只收到 ping：沒有更新時間，不是資料
    usage.put({"providers": [{"id": "claude", "hook": {"name": "Stop hook"}}]}, "claude-laptop", now=T0 + 5000)
    c = prov(usage.view(now=T0 + 5001), "claude")
    assert c["updated"] is None and c["windows"] == []


def test_hook_ms_kept_per_machine_max_30_junk_dropped_and_light_report_unchanged():
    fresh_db()
    usage.put({"providers": [dict(FULL)]}, "pc", now=T0)
    before = prov(usage.view(now=T0 + 1))
    hook = lambda ms: {"providers": [{"id": "codex", "hook": {"name": "Codex Stop hook", "version": "0.165.0", "ms": ms}}]}
    for i in range(35):                                   # 35 次 → 只留最後 30 次
        usage.put(hook(100 + i), "codex-laptop", now=T0 + 10 + i)
    usage.put(hook(250.6), "codex-max:t1", now=T0 + 100)  # 小數四捨五入、帶串名也算同一台
    for bad in ("亂", None, True, -5, 4_000_000, float("nan"), [1]):  # 壞值不存
        usage.put(hook(bad), "codex-laptop", now=T0 + 200)
    usage.put({"providers": [{"id": "codex", "hook": {"name": "Codex Stop hook"}}]}, "codex-laptop", now=T0 + 300)  # 沒帶 ms → 沿用
    v = usage.view(now=T0 + 400)
    a = v["codex_machines"]["codex-laptop"]["hook_ms"]
    assert a == list(range(105, 135)) and len(a) == 30
    assert v["codex_machines"]["codex-max"]["hook_ms"] == [251]
    assert prov(v)["codex_machines"]["codex-laptop"]["hook_ms"] == a        # 該家也回傳
    after = prov(v)
    assert after["updated"] == before["updated"] and after["windows"] == before["windows"]  # 只帶 hook 不動更新時間、不清額度


if __name__ == "__main__":  # 沒裝 pytest 也能直接跑：python test_usage_server_fields.py
    bad = 0
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("test_") and callable(v)):
        try:
            fn()
            print("通過", name)
        except Exception as e:  # noqa: BLE001
            bad += 1
            print("失敗", name, repr(e))
    sys.exit(1 if bad else 0)


def test_non_hook_provider_hook_ping_ignored():
    """Antigravity 沒有自己的 hook；有人手動帶 hook 也不記 hook_ts，頁面才不會畫 hook 晶片、誤判久未回報。"""
    fresh_db()
    usage.put({"providers": [{"id": "agy", "name": "Antigravity", "hook": {"name": "Stop hook"}}]}, "pc", now=T0)
    assert not prov(usage.view(now=T0), "agy").get("hook_ts")
    usage.put({"providers": [{"id": "claude", "hook": {"name": "Stop hook"}}]}, "claude-laptop", now=T0)
    assert prov(usage.view(now=T0), "claude")["hook_ts"] == usage._iso(T0)
