from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from notion_local_ops_mcp import instrument, relay_bridge, session
from notion_local_ops_mcp.instrument import ToolEvent, clear_sinks, register_sink
from notion_local_ops_mcp.relay_bridge import RelayBridgeSink


def _call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return result


@pytest.fixture(autouse=True)
def _isolate_binding_and_sinks():
    session.clear_bound_run()
    relay_bridge.reset_dropped_traces()
    clear_sinks()
    yield
    session.clear_bound_run()
    relay_bridge.reset_dropped_traces()
    clear_sinks()


# ---------------------------------------------------------------------------
# bind_relay_run / clear_relay_run (session binding + tool surface)
# ---------------------------------------------------------------------------


def test_bind_relay_run_sets_session_binding() -> None:
    from notion_local_ops_mcp import server

    result = _call(
        server.bind_relay_run,
        request_id="run_abc",
        callback_token="tok_secret",
        relay_url="http://127.0.0.1:8799",
        conversation_key="conv_1",
    )
    assert result["success"] is True
    assert result["bound"] is True
    assert result["request_id"] == "run_abc"
    assert result["relay_url"] == "http://127.0.0.1:8799"

    binding = session.get_bound_run()
    assert binding is not None
    assert binding["request_id"] == "run_abc"
    assert binding["callback_token"] == "tok_secret"
    assert binding["relay_url"] == "http://127.0.0.1:8799"
    assert binding["conversation_key"] == "conv_1"
    assert binding["bound_at"]  # timestamp stamped


def test_bind_relay_run_overwrites_previous_binding() -> None:
    from notion_local_ops_mcp import server

    _call(
        server.bind_relay_run,
        request_id="run_1",
        callback_token="t1",
        relay_url="http://127.0.0.1:8799",
    )
    _call(
        server.bind_relay_run,
        request_id="run_2",
        callback_token="t2",
        relay_url="http://127.0.0.1:9000",
    )
    binding = session.get_bound_run()
    assert binding is not None
    assert binding["request_id"] == "run_2"
    assert binding["callback_token"] == "t2"
    assert binding["relay_url"] == "http://127.0.0.1:9000"


def test_bind_relay_run_with_null_request_id_unbinds() -> None:
    from notion_local_ops_mcp import server

    _call(
        server.bind_relay_run,
        request_id="run_1",
        callback_token="t1",
        relay_url="http://127.0.0.1:8799",
    )
    assert session.get_bound_run() is not None

    result = _call(server.bind_relay_run, request_id=None)
    assert result["success"] is True
    assert result["bound"] is False
    assert session.get_bound_run() is None


def test_bind_relay_run_requires_callback_token() -> None:
    from notion_local_ops_mcp import server

    result = _call(
        server.bind_relay_run,
        request_id="run_x",
        callback_token=None,
        relay_url="http://127.0.0.1:8799",
    )
    assert result["success"] is False
    assert result["error"]["code"] == "missing_callback_token"
    assert session.get_bound_run() is None


def test_bind_relay_run_defaults_relay_url_when_omitted() -> None:
    from notion_local_ops_mcp import config, server

    try:
        result = _call(
            server.bind_relay_run,
            request_id="run_x",
            callback_token="tok",
            relay_url=None,
        )
        assert result["success"] is True
        assert result["bound"] is True
        # Omitting relay_url falls back to the configured default, so the agent
        # never has to know the relay host/port.
        assert result["relay_url"] == config.RELAY_URL
        binding = session.get_bound_run()
        assert binding is not None
        assert binding["relay_url"] == config.RELAY_URL
    finally:
        session.clear_bound_run()


def test_clear_relay_run_unbinds() -> None:
    from notion_local_ops_mcp import server

    _call(
        server.bind_relay_run,
        request_id="run_1",
        callback_token="t1",
        relay_url="http://127.0.0.1:8799",
    )
    result = _call(server.clear_relay_run)
    assert result["success"] is True
    assert result["bound"] is False
    assert session.get_bound_run() is None


def test_clear_relay_run_safe_when_unbound() -> None:
    from notion_local_ops_mcp import server

    result = _call(server.clear_relay_run)
    assert result["success"] is True
    assert result["bound"] is False


# ---------------------------------------------------------------------------
# RelayBridgeSink HTTP behavior via a tiny local stub server
# ---------------------------------------------------------------------------


class _StubServer:
    """Minimal HTTP server that records POSTed trace bodies."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url: str = ""
        # When set, the handler returns this status instead of 200.
        self.fail_status: int | None = None

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # silence
                pass

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    body = {"_raw": raw.decode("utf-8", errors="replace")}
                auth = self.headers.get("Authorization", "")
                with outer._lock:
                    outer.requests.append({"path": self.path, "auth": auth, "body": body})
                status = outer.fail_status if outer.fail_status is not None else 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    @property
    def captured(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.requests)


@pytest.fixture
def stub_relay():
    srv = _StubServer()
    srv.start()
    yield srv
    srv.stop()


def test_sink_with_binding_posts_correct_payload(stub_relay: _StubServer) -> None:
    session.set_bound_run(
        {
            "request_id": "run_123",
            "callback_token": "tok_abc",
            "relay_url": stub_relay.url,
            "conversation_key": "conv_xyz",
        }
    )
    sink = RelayBridgeSink()
    sink.on_tool_event(
        ToolEvent(
            tool="apply_patch",
            title="apply_patch → Button.tsx",
            args_summary={"path": "src/Button.tsx", "patch": {"bytes": 100, "hunks": 2}},
            result_summary={"success": True, "files_len": 1, "applied": True},
            started_at="2026-06-26T12:00:00+00:00",
            duration_ms=42,
            ok=True,
            error=None,
        )
    )

    assert len(stub_relay.captured) == 1
    req = stub_relay.captured[0]
    assert req["path"] == "/internal/tool-trace"
    assert req["auth"] == "Bearer tok_abc"
    body = req["body"]
    assert body["request_id"] == "run_123"
    assert body["conversation_key"] == "conv_xyz"
    assert body["callback_token"] == "tok_abc"
    assert body["tool"] == "apply_patch"
    assert body["title"] == "apply_patch → Button.tsx"
    assert body["args_summary"]["path"] == "src/Button.tsx"
    assert body["result_summary"]["success"] is True
    assert body["started_at"] == "2026-06-26T12:00:00+00:00"
    assert body["duration_ms"] == 42
    assert body["ok"] is True
    assert body["error"] is None


def test_sink_no_binding_makes_no_http(stub_relay: _StubServer) -> None:
    # No binding set (fixture cleared it).
    sink = RelayBridgeSink()
    sink.on_tool_event(
        ToolEvent(
            tool="read_text",
            title="read_text",
            args_summary={},
            result_summary=None,
            started_at="t",
            duration_ms=0,
            ok=True,
        )
    )
    assert stub_relay.captured == []


def test_sink_http_failure_does_not_raise_and_increments_dropped(stub_relay: _StubServer) -> None:
    session.set_bound_run(
        {
            "request_id": "run_x",
            "callback_token": "tok",
            "relay_url": stub_relay.url,
        }
    )
    stub_relay.fail_status = 500
    relay_bridge.reset_dropped_traces()
    before = relay_bridge.get_dropped_traces()

    sink = RelayBridgeSink()
    # Must not raise.
    sink.on_tool_event(
        ToolEvent(
            tool="read_text",
            title="read_text",
            args_summary={},
            result_summary=None,
            started_at="t",
            duration_ms=1,
            ok=True,
        )
    )

    assert relay_bridge.get_dropped_traces() == before + 1
    assert session.get_bound_run() is not None


def test_sink_stale_binding_http_status_clears_binding(stub_relay: _StubServer) -> None:
    session.set_bound_run(
        {
            "request_id": "run_superseded",
            "callback_token": "tok",
            "relay_url": stub_relay.url,
            "conversation_key": "conv",
        }
    )
    stub_relay.fail_status = 409
    relay_bridge.reset_dropped_traces()

    sink = RelayBridgeSink()
    sink.on_tool_event(
        ToolEvent(
            tool="run_command",
            title="run_command",
            args_summary={},
            result_summary=None,
            started_at="t",
            duration_ms=1,
            ok=True,
        )
    )

    assert relay_bridge.get_dropped_traces() == 1
    assert session.get_bound_run() is None


def test_sink_connection_refused_does_not_raise_and_increments_dropped() -> None:
    # Bind to a port that is not listening. Use a fresh bound socket to find a
    # definitely-free port, then close it so nothing listens there.
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    session.set_bound_run(
        {
            "request_id": "run_x",
            "callback_token": "tok",
            "relay_url": f"http://127.0.0.1:{port}",
        }
    )
    relay_bridge.reset_dropped_traces()
    sink = RelayBridgeSink(timeout=1.0)

    sink.on_tool_event(
        ToolEvent(
            tool="run_command",
            title="run_command",
            args_summary={},
            result_summary=None,
            started_at="t",
            duration_ms=1,
            ok=True,
        )
    )
    assert relay_bridge.get_dropped_traces() >= 1


def test_sink_disabled_does_not_post(stub_relay: _StubServer) -> None:
    session.set_bound_run(
        {
            "request_id": "run_x",
            "callback_token": "tok",
            "relay_url": stub_relay.url,
        }
    )
    sink = RelayBridgeSink(enabled=False)
    sink.on_tool_event(
        ToolEvent(
            tool="read_text",
            title="read_text",
            args_summary={},
            result_summary=None,
            started_at="t",
            duration_ms=0,
            ok=True,
        )
    )
    assert stub_relay.captured == []


def test_traced_tool_with_binding_triggers_exactly_one_post(stub_relay: _StubServer, tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    session.set_bound_run(
        {
            "request_id": "run_42",
            "callback_token": "tok_42",
            "relay_url": stub_relay.url,
            "conversation_key": "conv",
        }
    )
    # Make sure the real RelayBridgeSink (registered at server import time) is
    # the only sink, and that it points at our binding.
    clear_sinks()
    register_sink(RelayBridgeSink())

    # Drive a real traced tool end-to-end: write_file is simple and local.
    target = tmp_path / "out.txt"
    _call(server.write_file, path=str(target), content="hello")

    captured = stub_relay.captured
    assert len(captured) == 1
    body = captured[0]["body"]
    assert body["tool"] == "write_file"
    assert body["ok"] is True
    assert body["request_id"] == "run_42"
    assert body["callback_token"] == "tok_42"
    # write_file result summary should include bytes_written, not the content.
    assert body["result_summary"]["success"] is True
    assert body["result_summary"]["bytes_written"] == 5
    assert "hello" not in str(body["args_summary"]) + str(body["result_summary"])


def test_dropped_traces_counter_is_observable_in_server_info() -> None:
    from notion_local_ops_mcp import server

    relay_bridge.reset_dropped_traces()
    info = _call(server.server_info)
    assert info["success"] is True
    rb = info["relay_bridge"]
    assert rb["enabled"] is True
    assert rb["dropped_traces"] == 0
    assert "bind_relay_run" in info["tools"]
    assert "clear_relay_run" in info["tools"]


def test_server_info_reports_bound_state() -> None:
    from notion_local_ops_mcp import server

    info = _call(server.server_info)
    assert info["relay_bridge"]["bound"] is False
    assert info["relay_bridge"]["request_id"] is None

    session.set_bound_run(
        {
            "request_id": "run_z",
            "callback_token": "t",
            "relay_url": "http://127.0.0.1:8799",
            "conversation_key": "c",
        }
    )
    info = _call(server.server_info)
    assert info["relay_bridge"]["bound"] is True
    assert info["relay_bridge"]["request_id"] == "run_z"
    assert info["relay_bridge"]["conversation_key"] == "c"
    assert info["relay_bridge"]["bound_at"]


def test_urllib_mock_path_posts_payload_and_swallows_error() -> None:
    """Exercise the _post_trace path with a mocked urllib to assert payload
    shape and error swallowing without spinning a real server."""
    session.set_bound_run(
        {
            "request_id": "run_m",
            "callback_token": "tok_m",
            "relay_url": "http://127.0.0.1:8799",
        }
    )
    sink = RelayBridgeSink()

    captured: dict[str, Any] = {}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["data"] = json.loads(request.data.decode("utf-8"))
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return _FakeResponse()

    with patch("notion_local_ops_mcp.relay_bridge.urllib.request.urlopen", side_effect=fake_urlopen):
        sink.on_tool_event(
            ToolEvent(
                tool="git_commit",
                title="git_commit → fix",
                args_summary={},
                result_summary={"commit": "abc123"},
                started_at="t",
                duration_ms=5,
                ok=True,
            )
        )

    assert captured["url"] == "http://127.0.0.1:8799/internal/tool-trace"
    assert captured["method"] == "POST"
    assert captured["data"]["tool"] == "git_commit"
    assert captured["data"]["request_id"] == "run_m"
    assert captured["timeout"] == 1.5
    # Authorization header carries the callback token.
    auth_header = next(v for k, v in captured["headers"].items() if k.lower() == "authorization")
    assert auth_header == "Bearer tok_m"
    assert relay_bridge.get_dropped_traces() == 0


def test_urllib_mock_failure_increments_dropped() -> None:
    import urllib.error

    session.set_bound_run(
        {
            "request_id": "run_m",
            "callback_token": "tok_m",
            "relay_url": "http://127.0.0.1:8799",
        }
    )
    sink = RelayBridgeSink()
    relay_bridge.reset_dropped_traces()

    with patch(
        "notion_local_ops_mcp.relay_bridge.urllib.request.urlopen",
        side_effect=urllib.error.URLError("nope"),
    ):
        sink.on_tool_event(
            ToolEvent(
                tool="read_text",
                title="read_text",
                args_summary={},
                result_summary=None,
                started_at="t",
                duration_ms=1,
                ok=True,
            )
        )
    assert relay_bridge.get_dropped_traces() == 1
