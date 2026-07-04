"""Process-wide session state for the local ops MCP bridge.

Today there is a single mutable “default working directory” shared by every
client of this server process. That is deliberately simple: the project runs
as a single-user local bridge, so per-connection sessions are overkill.

The default cwd is used by :func:`notion_local_ops_mcp.pathing.resolve_cwd`
as the fallback whenever a tool call omits ``cwd``. Resolution order is:

1. explicit ``cwd`` argument on the tool call
2. the session default (if one has been set via ``set_default_cwd``)
3. ``WORKSPACE_ROOT`` from config
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

_lock = threading.RLock()
_default_cwd: Path | None = None

# Process-wide single relay-run binding, used by the relay bridge sink to mirror
# tool calls back to a relay server. Mirrors the ``_default_cwd`` pattern: a
# single mutable value shared by every client of this process, since this server
# runs as a single-user local bridge. A new bind overwrites the previous one;
# ``set_bound_run(None)`` clears it. When ``None`` the bridge sink is a no-op and
# behavior is identical to a non-bridged server.
_bound_run: dict[str, object] | None = None


def get_default_cwd() -> Path | None:
    """Return the current session-wide default cwd, or ``None`` if unset."""
    with _lock:
        return _default_cwd


def set_default_cwd(cwd: Path | None) -> Path | None:
    """Set (or clear with ``None``) the session-wide default cwd.

    Returns the newly-active value (``None`` when cleared). Validation of the
    path (must exist and be a directory) is left to the caller so this module
    has no filesystem side effects.
    """
    global _default_cwd
    with _lock:
        _default_cwd = cwd
        return _default_cwd


def get_bound_run() -> dict[str, object] | None:
    """Return the currently bound relay run, or ``None`` if not bound."""
    with _lock:
        return _bound_run


def set_bound_run(value: dict[str, object] | None) -> dict[str, object] | None:
    """Set (or clear with ``None``) the process-wide relay-run binding.

    The dict is expected to carry ``request_id``, ``callback_token``,
    ``relay_url``, ``conversation_key`` and will be stamped with a
    ``bound_at`` ISO timestamp if missing. Returns the newly-active value
    (``None`` when cleared). No network or validation side effects happen here
    so a dead relay never blocks tool execution.
    """
    global _bound_run
    with _lock:
        if value is None:
            _bound_run = None
            return None
        normalized = dict(value)
        normalized.setdefault("bound_at", datetime.now(UTC).isoformat())
        _bound_run = normalized
        return _bound_run


def clear_bound_run() -> None:
    """Clear the process-wide relay-run binding (equivalent to ``set_bound_run(None)``)."""
    set_bound_run(None)
