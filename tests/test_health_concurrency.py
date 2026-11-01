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


def test_health_flood_does_not_delay_authenticated_mcp_ping(monkeypatch):
    calls = []

    def slow_ping():
        calls.append(threading.get_ident())
        time.sleep(0.15)
        return True

    monkeypatch.setattr(server, "STORE", SimpleNamespace(name="mem0", ping=slow_ping))

    async def run():
        app = server.AuthMiddleware(server.app.app)
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
def test_health_caches_success_and_failure_then_refreshes(monkeypatch, store_name):
    clock = [100.0]
    replies = iter([True, False, True])
    calls = []

    def ping():
        calls.append(threading.get_ident())
        return next(replies)

    monkeypatch.setattr(server, "STORE", SimpleNamespace(name=store_name, ping=ping))
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def run():
        app = server.AuthMiddleware(ping_app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            first = await client.get("/health")
            expected = {"ok": True, "agents": sorted(set(server.TOKENS.values())), "store": store_name, "store_ok": True}
            assert first.status_code == 200 and first.json() == expected
            assert first.headers["x-content-type-options"] == "nosniff"
            clock[0] += 4.9
            assert (await client.get("/health")).json() == expected
            assert len(calls) == 1
            clock[0] += 0.2
            expected["store_ok"] = False
            assert (await client.get("/health")).json() == expected
            assert (await client.get("/health")).json() == expected
            assert len(calls) == 2
            clock[0] += 5.1
            assert (await client.get("/health")).json()["store_ok"] is True
            assert len(calls) == 3
            assert all(ident != threading.get_ident() for ident in calls)
            assert (await client.get("/mcp")).status_code == 401

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
