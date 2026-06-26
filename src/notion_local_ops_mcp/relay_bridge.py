"""Relay bridge sink: mirrors traced tool calls to a relay server.

This is the one place that knows about the relay protocol. It implements
:class:`notion_local_ops_mcp.instrument.ToolSink` and, on each tool event, reads
the process-wide relay-run binding from :mod:`notion_local_ops_mcp.session`.
When a binding is present (and the bridge is enabled) it fires a short-timeout
HTTP POST to ``{relay_url}/internal/tool-trace`` carrying the trace payload.

Failure semantics (per the bridge design):

- No binding -> immediate return, no HTTP, no error.
- Bridge disabled via ``RELAY_BRIDGE_ENABLED`` -> immediate return.
- Any HTTP failure (timeout, connection refused, 4xx/5xx, DNS, ...) -> swallowed
  silently and ``dropped_traces`` is incremented. The tool call that triggered
  the trace is never affected.
- The POST is synchronous but capped at ``RELAY_BRIDGE_TIMEOUT`` seconds (1.5s
  default). We deliberately do not spawn threads here: the cap bounds the cost
  and keeps the implementation simple.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

from . import config, session
from .instrument import ToolEvent


_dropped_lock = threading.Lock()
dropped_traces: int = 0


def get_dropped_traces() -> int:
    """Return the current count of traces dropped due to delivery failure."""
    with _dropped_lock:
        return dropped_traces


def reset_dropped_traces() -> None:
    """Reset the dropped counter (mainly for tests)."""
    global dropped_traces
    with _dropped_lock:
        dropped_traces = 0


def _bump_dropped() -> None:
    global dropped_traces
    with _dropped_lock:
        dropped_traces += 1


class RelayBridgeSink:
    """ToolSink that POSTs tool traces to a bound relay server.

    The sink is stateless beyond the dropped counter: every event re-reads the
    process-level binding, so ``bind_relay_run`` / ``clear_relay_run`` take
    effect immediately for subsequent tool calls with no sink re-registration.
    """

    name: str = "relay-bridge"

    def __init__(
        self,
        *,
        timeout: float | None = None,
        enabled: bool | None = None,
    ) -> None:
        # Resolve config at construction time for the defaults, but re-read the
        # enabled flag per-event so flipping the env var + reloading is honored
        # without restarting. ``None`` means "defer to config each call".
        self._default_timeout = timeout
        self._default_enabled = enabled

    def _enabled(self) -> bool:
        if self._default_enabled is not None:
            return self._default_enabled
        return bool(getattr(config, "RELAY_BRIDGE_ENABLED", True))

    def _timeout(self) -> float:
        if self._default_timeout is not None:
            return float(self._default_timeout)
        return float(getattr(config, "RELAY_BRIDGE_TIMEOUT", 1.5))

    def on_tool_event(self, event: ToolEvent) -> None:
        if not self._enabled():
            return
        binding = session.get_bound_run()
        if not binding:
            return
        relay_url = binding.get("relay_url")
        if not isinstance(relay_url, str) or not relay_url:
            return
        callback_token = binding.get("callback_token")
        if not isinstance(callback_token, str) or not callback_token:
            return

        payload: dict[str, Any] = {
            "request_id": binding.get("request_id"),
            "conversation_key": binding.get("conversation_key"),
            "callback_token": callback_token,
            "tool": event.tool,
            "title": event.title,
            "args_summary": event.args_summary,
            "result_summary": event.result_summary,
            "started_at": event.started_at,
            "duration_ms": event.duration_ms,
            "ok": event.ok,
            "error": event.error,
        }
        self._post_trace(relay_url, payload)

    def _post_trace(self, relay_url: str, payload: dict[str, Any]) -> None:
        url = relay_url.rstrip("/") + "/internal/tool-trace"
        body = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + str(payload.get("callback_token") or ""),
            },
        )
        timeout = self._timeout()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - localhost/controlled URL
                # Read the response so the connection is released cleanly, but
                # we do not care about the body. Any non-2xx is treated as a
                # drop (urlopen raises HTTPError for those, so this branch only
                # sees 2xx).
                response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, TimeoutError):
            _bump_dropped()
        except Exception:
            # Defensive: never let a sink failure escape. See instrument.notify_sinks
            # which also wraps this, but we keep this belt-and-suspenders so the
            # sink is safe to use directly in tests too.
            _bump_dropped()
