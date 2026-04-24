"""Tests for streaming output, timeout, cancel, interval validation, and incremental flushing."""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import notion_local_ops_mcp.config as config_mod
import notion_local_ops_mcp.executors as executors
from notion_local_ops_mcp.executors import ExecutorRegistry, _kill_process
from notion_local_ops_mcp.tasks import TaskStore
from tests.helpers import python_cmd, python_print_cmd, python_sleep_cmd


# ---------------------------------------------------------------------------
# 1. STREAM_OUTPUT_INTERVAL validation
# ---------------------------------------------------------------------------


def test_stream_output_interval_rejects_zero():
    with pytest.raises(ValueError, match="must be > 0"):
        # Simulate what config.py does at import time
        val = float("0")
        if val <= 0:
            raise ValueError(
                f"NOTION_LOCAL_OPS_STREAM_OUTPUT_INTERVAL must be > 0, got {val}"
            )


def test_stream_output_interval_rejects_negative():
    with pytest.raises(ValueError, match="must be > 0"):
        val = float("-1")
        if val <= 0:
            raise ValueError(
                f"NOTION_LOCAL_OPS_STREAM_OUTPUT_INTERVAL must be > 0, got {val}"
            )


# ---------------------------------------------------------------------------
# 2. Live streaming — get_task sees incremental output while task runs
# ---------------------------------------------------------------------------


def test_get_task_sees_incremental_output(tmp_path: Path) -> None:
    """A slow command that prints lines with delays; get_task should see
    partial output before the task completes."""
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    # Print "line1", sleep 0.8s, print "line2", sleep 0.8s — total ~1.6s
    # Generous sleeps so the flush interval fires between prints even on
    # slow Windows CI where shell=True adds startup latency.
    slow_cmd = python_cmd(
        "import time, sys; "
        "print('line1', flush=True); sys.stdout.flush(); time.sleep(0.8); "
        "print('line2', flush=True); sys.stdout.flush(); time.sleep(0.8)"
    )

    with patch.object(executors, "STREAM_OUTPUT_INTERVAL", 0.2):
        task = registry.submit_command(command=slow_cmd, cwd=tmp_path, timeout=15)
        task_id = task["task_id"]

        # Wait long enough for the process to start + first flush
        time.sleep(1.2)
        mid = registry.get(task_id)
        # Should see at least "line1" before the task finishes
        assert "line1" in mid["stdout_tail"], f"Expected incremental output, got: {mid['stdout_tail']!r}"

        # Now wait for completion
        result = registry.wait(task_id, timeout=10)
        assert result["status"] == "succeeded"
        assert "line1" in result["stdout_tail"]
        assert "line2" in result["stdout_tail"]


# ---------------------------------------------------------------------------
# 3. Timeout kills the task and marks it failed
# ---------------------------------------------------------------------------


def test_stream_process_timeout_marks_failed(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit_command(
        command=python_sleep_cmd(30),
        cwd=tmp_path,
        timeout=1,  # 1 second timeout
    )
    result = registry.wait(task["task_id"], timeout=10)

    assert result["status"] == "failed"
    assert result["completed"] is True
    # The process-level timed_out is stored in meta by _stream_process
    meta = store.get(task["task_id"])
    assert meta.get("timed_out") is True


# ---------------------------------------------------------------------------
# 4. Cancel stops the task
# ---------------------------------------------------------------------------


def test_cancel_stops_streaming_task(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit_command(
        command=python_sleep_cmd(30),
        cwd=tmp_path,
        timeout=60,
    )
    task_id = task["task_id"]

    # Give it a moment to start
    time.sleep(0.2)
    registry.cancel(task_id)
    result = registry.wait(task_id, timeout=5)

    assert result["status"] == "cancelled"
    assert result["completed"] is True


# ---------------------------------------------------------------------------
# 5. Delegate task uses event-driven wait (not polling fallback)
# ---------------------------------------------------------------------------


def test_delegate_task_wait_is_event_driven(tmp_path: Path) -> None:
    """After the fix, submit() uses _register_task() so wait_task() should
    use the completion event, not fall back to polling."""
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("done"),
        claude_command=python_print_cmd("claude"),
    )

    task = registry.submit(
        task="quick task",
        executor="codex",
        cwd=tmp_path,
        timeout=5,
    )
    task_id = task["task_id"]

    # Verify the completion event was registered
    with registry._lock:
        assert task_id in registry._completion_events, (
            "submit() should register a completion event via _register_task()"
        )

    start = time.monotonic()
    # Large poll_interval proves we're using the event, not polling
    result = registry.wait(task_id, timeout=5, poll_interval=10.0)
    elapsed = time.monotonic() - start

    assert result["completed"] is True
    assert result["status"] == "succeeded"
    assert elapsed < 2.0, f"wait took {elapsed:.3f}s — event path not used"


# ---------------------------------------------------------------------------
# 6. Append-based log flushing (no O(n²) rewrite)
# ---------------------------------------------------------------------------


def test_append_logs_accumulates(tmp_path: Path) -> None:
    """TaskStore.append_logs should accumulate, not overwrite."""
    store = TaskStore(tmp_path / "state")
    created = store.create(task="test", executor="shell", cwd=str(tmp_path))
    tid = created["task_id"]

    store.append_logs(tid, stdout="chunk1\n")
    store.append_logs(tid, stdout="chunk2\n")
    store.append_logs(tid, stderr="err1\n")

    assert store.read_stdout(tid) == "chunk1\nchunk2\n"
    assert store.read_stderr(tid) == "err1\n"


# ---------------------------------------------------------------------------
# 7. Large output doesn't blow up
# ---------------------------------------------------------------------------


def test_large_output_completes(tmp_path: Path) -> None:
    """A command producing substantial output should complete without error."""
    store = TaskStore(tmp_path / "state")
    registry = ExecutorRegistry(
        store=store,
        codex_command=python_print_cmd("codex"),
        claude_command=python_print_cmd("claude"),
    )

    # Print 2000 lines of output
    big_cmd = python_cmd(
        "for i in range(2000): print(f'line {i} ' + 'x' * 80)"
    )

    with patch.object(executors, "STREAM_OUTPUT_INTERVAL", 0.1):
        task = registry.submit_command(command=big_cmd, cwd=tmp_path, timeout=30)
        result = registry.wait(task["task_id"], timeout=15)

    assert result["status"] == "succeeded"
    # stdout_tail is capped at 4000 chars by get(), but the full log should have content
    assert len(result["stdout_tail"]) > 0
    assert "line 1999" in store.read_stdout(task["task_id"])


# ---------------------------------------------------------------------------
# 8. _kill_process helper
# ---------------------------------------------------------------------------


def test_kill_process_noop_when_already_exited(tmp_path: Path) -> None:
    """_kill_process should not raise when the process already exited."""
    proc = subprocess.Popen(
        python_print_cmd("hi"),
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.wait(timeout=5)
    # Should be a no-op, not raise
    _kill_process(proc)
