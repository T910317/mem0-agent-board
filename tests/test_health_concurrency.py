"""Public health probes must not block MCP or allocate one worker per request."""
import asyncio
import contextlib
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import server
from starlette.responses import JSONResponse


async def ping_app(scope, receive, send):
    await JSONResponse({"result": {}})(scope, receive, send)


def _mcp_asgi():
    """拆掉監控外層與 AuthMiddleware，拿到裡面的 MCP app。"""
    a = server.app
    while not isinstance(a, server.AuthMiddleware):
        a = a.__closure__[a.__code__.co_freevars.index("app")].cell_contents
    return a.app


def test_health_flood_does_not_delay_authenticated_mcp_ping(monkeypatch):
    calls = []

    def slow_ping():
        calls.append(threading.get_ident())
        time.sleep(0.15)
        return True

    monkeypatch.setattr(server, "STORE", SimpleNamespace(name="mem0", ping=slow_ping))

    async def run():
        app = server.AuthMiddleware(_mcp_asgi())
        async with server.mcp.session_manager.run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                started = time.monotonic()
                healths = [asyncio.create_task(client.get("/health")) for _ in range(6)]
                await asyncio.sleep(0)
                response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                             headers={"Authorization": "Bearer test-token-claude-0123456789abcdef0123456789",
                                                      "Accept": "application/json, text/event-stream"})
                elapsed = time.monotonic() - started
                results = await asyncio.gather(*healths)
                assert response.status_code == 200
                assert elapsed < 0.4, f"MCP ping waited {elapsed:.3f}s for health probes"
                assert len(calls) == 1
                assert all(r.status_code == 200 and r.json()["store_ok"] for r in results)

    asyncio.run(run())


@pytest.mark.parametrize("store_name", ["mem0", "pg"])
def test_health_shares_inflight_probe_then_probes_again(monkeypatch, store_name):
    replies = iter([True, False])
    calls = []
    gate = threading.Event()

    def ping():
        calls.append(threading.get_ident())
        gate.wait(timeout=2)
        return next(replies)

    monkeypatch.setattr(server, "STORE", SimpleNamespace(name=store_name, ping=ping))

    async def run():
        app = server.AuthMiddleware(ping_app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            # 同時進來的請求共用同一次探測
            tasks = [asyncio.create_task(client.get("/health")) for _ in range(8)]
            await asyncio.sleep(0.05)
            gate.set()
            first = await asyncio.gather(*tasks)
            assert len(calls) == 1
            for r in first:
                body = r.json()
                assert r.status_code == 200 and body["ok"] is True and body["store"] == store_name
                assert body["store_ok"] is True and body["agents"] == sorted(set(server.TOKENS.values()))
            assert first[0].headers["x-content-type-options"] == "nosniff"
            # 上一次探測結束後，下一個請求重新探測，失敗照實回報
            again = (await client.get("/health")).json()
            assert again["store_ok"] is False and len(calls) == 2
            assert all(ident != threading.get_ident() for ident in calls)
            assert (await client.get("/mcp")).status_code == 401

    asyncio.run(run())


def test_health_times_out_without_piling_up(monkeypatch):
    release = threading.Event()
    calls = []

    def stuck_ping():
        calls.append(1)
        release.wait(timeout=2)
        return True

    monkeypatch.setattr(server, "STORE", SimpleNamespace(name="mem0", ping=stuck_ping))
    monkeypatch.setattr(server, "HEALTH_PING_S", 0.1)

    async def run():
        app = server.AuthMiddleware(ping_app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            try:
                results = await asyncio.gather(*[client.get("/health") for _ in range(16)])
                assert all(r.json()["store_ok"] is False and "store_note" in r.json() for r in results)
                assert len(calls) == 1
            finally:
                release.set()

    asyncio.run(run())


def test_cancelled_health_request_does_not_start_more_workers(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def ping():
        calls.append(threading.get_ident())
        entered.set()
        release.wait(timeout=2)
        return False

    monkeypatch.setattr(server, "STORE", SimpleNamespace(name="mem0", ping=ping))

    async def run():
        app = server.AuthMiddleware(ping_app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            first = asyncio.create_task(client.get("/health"))
            others = []
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                others = [asyncio.create_task(client.get("/health")) for _ in range(64)]
                await asyncio.sleep(0.05)
                assert len(calls) == 1
                assert not any(task.done() for task in others)
            finally:
                release.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await first
                results = await asyncio.gather(*others)
            assert all(r.status_code == 200 and r.json()["store_ok"] is False for r in results)
            assert len(calls) == 1

    asyncio.run(run())
