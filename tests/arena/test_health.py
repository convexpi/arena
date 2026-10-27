"""The health endpoint shares the WebSocket port, so a platform routing one $PORT can reach it.

It previously listened on port+1, which Railway could never route to — leaving "the process is
running" as the only liveness signal. A crash-on-import therefore took three restart cycles to
surface, and a server that was up but doing nothing looked identical to a healthy one.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from convexpi.arena.server import ArenaServer, _default_background


@pytest.fixture
def server():
    return ArenaServer(background_agents=_default_background(), n_ticks=5, port=8765)


def _get(server, path):
    return server._process_request(None, SimpleNamespace(path=path))


@pytest.mark.parametrize("path", ["/health", "/healthz", "/health/", "/health?probe=1"])
def test_health_paths_answer_http(server, path):
    resp = _get(server, path)
    assert resp is not None, f"{path} fell through to the WebSocket handshake"
    assert resp.status_code == 200


def test_health_body_is_json_and_describes_the_arena(server):
    resp = _get(server, "/health")
    assert resp.headers["Content-Type"] == "application/json"
    payload = json.loads(resp.body)
    assert payload["status"] == "ok"
    assert set(payload) == {"status", "tick", "agents", "observers", "paused"}
    assert payload["paused"] is False


def test_content_length_matches_the_body(server):
    """A wrong length hangs the client rather than failing loudly."""
    resp = _get(server, "/health")
    assert int(resp.headers["Content-Length"]) == len(resp.body)


@pytest.mark.parametrize("path", ["/", "/ws", "/healthcheck", "/health/extra"])
def test_other_paths_proceed_to_the_websocket_handshake(server, path):
    """Returning a response here would break every agent connecting on that path."""
    assert _get(server, path) is None


def test_health_reports_live_tick(server):
    server.market.snapshots.append({"tick": 42})
    assert json.loads(_get(server, "/health").body)["tick"] == 42


def test_server_telemetry_histories_are_bounded():
    server = ArenaServer(
        background_agents=_default_background(),
        n_ticks=5,
        telemetry_ticks=3,
        trade_history_limit=4,
    )
    for i in range(10):
        server.market.snapshots.append({"tick": i})
        server.market.engine.trades.append(i)
    assert [row["tick"] for row in server.market.snapshots] == [7, 8, 9]
    assert list(server.market.engine.trades) == [6, 7, 8, 9]


@pytest.mark.asyncio
async def test_rankings_pushes_are_rate_limited(monkeypatch):
    server = ArenaServer(
        background_agents=_default_background(),
        n_ticks=5,
        rankings_interval=30,
    )
    pushed = []

    async def fake_push(tick):
        pushed.append(tick)

    monkeypatch.setattr(server, "_push_rankings", fake_push)
    assert server._schedule_rankings_push(1, 0.0)
    await server._rankings_push_task
    assert not server._schedule_rankings_push(2, 29.9)
    assert server._schedule_rankings_push(3, 30.0)
    await server._rankings_push_task
    assert pushed == [1, 3]


@pytest.mark.asyncio
async def test_admin_access_is_disabled_without_a_token(server):
    websocket = AsyncMock()
    await server._handle_admin(websocket, {"token": "anything"})
    websocket.close.assert_awaited_once_with(1008, "admin access is disabled")


@pytest.mark.asyncio
async def test_admin_rejects_an_invalid_token():
    server = ArenaServer(
        background_agents=_default_background(),
        n_ticks=5,
        admin_token="correct-secret",
    )
    websocket = AsyncMock()
    await server._handle_admin(websocket, {"token": "wrong-secret"})
    websocket.close.assert_awaited_once_with(1003, "invalid admin token")


def test_idle_timeout_cannot_be_negative():
    with pytest.raises(ValueError, match="idle_timeout"):
        ArenaServer(background_agents=_default_background(), idle_timeout=-1)


@pytest.mark.asyncio
async def test_infinite_server_pauses_and_wakes_for_an_observer():
    server = ArenaServer(
        background_agents=_default_background(),
        n_ticks=None,
        idle_timeout=0.01,
    )
    wait = asyncio.create_task(server._wait_for_active_clients())
    await asyncio.sleep(0.02)
    assert server._paused
    assert not wait.done()

    observer = object()
    server._observers.add(observer)
    server._activity_event.set()
    await asyncio.wait_for(wait, timeout=0.1)
    assert not server._paused


@pytest.mark.asyncio
async def test_finite_simulation_never_waits_for_clients():
    server = ArenaServer(
        background_agents=_default_background(),
        n_ticks=2,
        idle_timeout=0.01,
    )
    await asyncio.wait_for(server._wait_for_active_clients(), timeout=0.1)
    assert not server._paused
