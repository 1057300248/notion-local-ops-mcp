from __future__ import annotations

import asyncio

import pytest

from notion_local_ops_mcp import instrument
from notion_local_ops_mcp.instrument import (
    ToolEvent,
    clear_sinks,
    notify_sinks,
    register_sink,
    traced,
)


class _RecordingSink:
    """Minimal ToolSink that captures every event it sees."""

    def __init__(self) -> None:
        self.events: list[ToolEvent] = []

    def on_tool_event(self, event: ToolEvent) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _reset_sinks():
    clear_sinks()
    yield
    clear_sinks()


def _call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return result


def test_traced_sink_receves_successful_event() -> None:
    sink = _RecordingSink()
    register_sink(sink)

    @traced("add")
    def add(a: int, b: int) -> dict[str, object]:
        return {"sum": a + b}

    result = add(a=2, b=3)

    assert result == {"sum": 5}
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.tool == "add"
    assert event.ok is True
    assert event.error is None
    assert event.duration_ms >= 0
    assert event.started_at
    assert event.args_summary == {"a": 2, "b": 3}
    # No `success` key in the result -> not synthesized; scalar fields carried.
    assert event.result_summary == {"sum": 5, "truncated": False}


def test_traced_measures_duration() -> None:
    sink = _RecordingSink()
    register_sink(sink)

    import time

    @traced("slow")
    def slow() -> dict[str, object]:
        time.sleep(0.02)
        return {"ok": True}

    slow()
    event = sink.events[0]
    # At least 20ms slept; allow generous slack for slow CI.
    assert event.duration_ms >= 15


def test_traced_re_raises_and_records_failure() -> None:
    sink = _RecordingSink()
    register_sink(sink)

    @traced("boom")
    def boom() -> None:
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        boom()

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.ok is False
    assert event.error == "kaboom"
    assert event.result_summary is None
    # Failure still has timing + args.
    assert event.duration_ms >= 0
    assert event.args_summary == {}


def test_traced_error_message_truncated_to_500_chars() -> None:
    sink = _RecordingSink()
    register_sink(sink)

    @traced("boom")
    def boom() -> None:
        raise RuntimeError("x" * 2000)

    with pytest.raises(RuntimeError):
        boom()

    event = sink.events[0]
    assert event.ok is False
    assert len(event.error) == 500


def test_no_sink_registered_handler_still_returns_normally() -> None:
    # No sink registered (fixture cleared them).

    @traced("lonely")
    def lonely(x: int) -> dict[str, object]:
        return {"x": x}

    assert lonely(x=42) == {"x": 42}
    # notify_sinks with no sinks should be a quiet no-op.
    notify_sinks(
        ToolEvent(
            tool="lonely",
            title="lonely",
            args_summary={},
            result_summary=None,
            started_at="t",
            duration_ms=0,
            ok=True,
        )
    )


def test_default_args_fn_drops_huge_content_param() -> None:
    sink = _RecordingSink()
    register_sink(sink)

    big = "x" * 5000

    @traced("write_file")
    def write_file(path: str, content: str) -> dict[str, object]:
        return {"path": path, "bytes_written": len(content)}

    write_file(path="/tmp/a.txt", content=big)

    event = sink.events[0]
    # `content` must NOT appear verbatim; replaced with a size pointer.
    assert event.args_summary["content"] == {"bytes": 5000}
    assert event.args_summary["path"] == "/tmp/a.txt"
    # The big string never made it into the summary.
    assert big not in str(event.args_summary)


def test_default_args_fn_drops_patch_and_command_params() -> None:
    sink = _RecordingSink()
    register_sink(sink)

    @traced("run_command")
    def run_command(command: str, cwd: str | None = None) -> dict[str, object]:
        return {"exit_code": 0}

    run_command(command="echo " + "y" * 3000, cwd="/tmp")

    event = sink.events[0]
    assert event.args_summary["command"] == {"bytes": 3005}
    assert event.args_summary["cwd"] == "/tmp"


def test_default_result_fn_never_includes_full_content_for_read_result() -> None:
    sink = _RecordingSink()
    register_sink(sink)

    body = "line\n" * 1000  # large content

    @traced("read_text")
    def read_text(path: str) -> dict[str, object]:
        return {
            "success": True,
            "path": path,
            "content": body,
            "truncated": True,
            "start_line": 1,
            "end_line": 1000,
            "language": "text",
        }

    read_text(path="/tmp/big.txt")

    event = sink.events[0]
    rs = event.result_summary
    assert rs is not None
    # Content body must not appear in the summary; only its size.
    assert "content" not in rs
    assert rs["content_bytes"] == len(body.encode("utf-8"))
    assert rs["content_lines"] == 1000
    assert rs["truncated"] is True
    assert body not in str(rs)


def test_default_result_fn_surfaces_error_code() -> None:
    sink = _RecordingSink()
    register_sink(sink)

    @traced("read_text")
    def read_text(path: str) -> dict[str, object]:
        return {
            "success": False,
            "error": {"code": "file_not_found", "message": "missing"},
        }

    read_text(path="/nope")

    event = sink.events[0]
    assert event.result_summary == {"success": False, "error_code": "file_not_found", "truncated": False}


def test_bad_sink_does_not_break_tool_call() -> None:
    class _ExplodingSink:
        def on_tool_event(self, event: ToolEvent) -> None:
            raise RuntimeError("sink exploded")

    good = _RecordingSink()
    register_sink(_ExplodingSink())
    register_sink(good)

    @traced("safe")
    def safe(x: int) -> dict[str, object]:
        return {"x": x}

    # The exploding sink must not propagate; the good sink still gets the event.
    result = safe(x=1)
    assert result == {"x": 1}
    assert len(good.events) == 1
    assert good.events[0].tool == "safe"


def test_traced_preserves_signature() -> None:
    import inspect

    @traced("foo")
    def foo(a: int, b: str = "x", *, c: bool = False) -> dict[str, object]:
        return {"a": a, "b": b, "c": c}

    # functools.wraps keeps the signature visible to FastMCP / inspect.
    sig = inspect.signature(foo)
    assert list(sig.parameters) == ["a", "b", "c"]
    assert foo.__name__ == "foo"


def test_traced_supports_async_handler() -> None:
    sink = _RecordingSink()
    register_sink(sink)

    @traced("async_tool")
    async def async_tool(x: int) -> dict[str, object]:
        await asyncio.sleep(0)
        return {"x": x}

    result = _call(async_tool, x=7)
    assert result == {"x": 7}
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.tool == "async_tool"
    assert event.ok is True
    assert event.result_summary == {"x": 7, "truncated": False}


def test_traced_async_handler_records_failure_and_reraises() -> None:
    sink = _RecordingSink()
    register_sink(sink)

    @traced("async_boom")
    async def async_boom() -> None:
        await asyncio.sleep(0)
        raise ValueError("async kaboom")

    with pytest.raises(ValueError, match="async kaboom"):
        _call(async_boom)

    event = sink.events[0]
    assert event.ok is False
    assert event.error == "async kaboom"


def test_custom_title_fn_used() -> None:
    sink = _RecordingSink()
    register_sink(sink)

    @traced("git_commit", title_fn=lambda name, kw: f"COMMIT: {kw.get('message', '')[:20]}")
    def git_commit(message: str) -> dict[str, object]:
        return {"commit": "abc"}

    git_commit(message="fix: update docs")
    assert sink.events[0].title == "COMMIT: fix: update docs"


def test_default_title_fn_picks_path() -> None:
    sink = _RecordingSink()
    register_sink(sink)

    @traced("read_text")
    def read_text(path: str) -> dict[str, object]:
        return {"success": True}

    read_text(path="/repo/src/components/Button.tsx")
    # basename used for brevity.
    assert sink.events[0].title == "read_text → Button.tsx"


def test_register_sink_is_idempotent() -> None:
    sink = _RecordingSink()

    register_sink(sink)
    register_sink(sink)
    register_sink(sink)

    @traced("x")
    def x() -> dict[str, object]:
        return {}

    x()
    # Same sink registered multiple times should only fire once per event.
    assert len(sink.events) == 1


def test_clear_sinks_removes_all() -> None:
    sink = _RecordingSink()
    register_sink(sink)
    clear_sinks()

    @traced("x")
    def x() -> dict[str, object]:
        return {}

    x()
    assert sink.events == []
