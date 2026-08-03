"""The health endpoint shares the WebSocket port, so a platform routing one $PORT can reach it.

It previously listened on port+1, which Railway could never route to — leaving "the process is
running" as the only liveness signal. A crash-on-import therefore took three restart cycles to
surface, and a server that was up but doing nothing looked identical to a healthy one.
"""
import json
from types import SimpleNamespace

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
    assert set(payload) == {"status", "tick", "agents", "observers"}


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
