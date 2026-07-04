"""Tests for the foreground time budget and wait_task clamping.

These guard the MCP-transport-timeout protections: run_command hands off to
background polling instead of blocking past the transport timeout, and
wait_task's timeout is clamped server-side.
"""

from __future__ import annotations

import time

import pytest

from notion_local_ops_mcp import server
from notion_local_ops_mcp.executors import ExecutorRegistry
from notion_local_ops_mcp.tasks import TaskStore

from .helpers import python_print_cmd, python_sleep_cmd


@pytest.fixture()
def temp_registry(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = TaskStore(state_dir)
    registry = ExecutorRegistry(store=store, codex_command="codex", claude_command="claude")
    monkeypatch.setattr(server, "store", store)
    monkeypatch.setattr(server, "registry", registry)
    return registry


def test_short_command_completes_with_classic_shape(monkeypatch, tmp_path, temp_registry):
    monkeypatch.setattr(server, "FOREGROUND_TIME_BUDGET", 30)
    result = server._run_foreground_command(python_print_cmd("hello-budget"), tmp_path, 120)
    assert result["success"] is True
    assert result["exit_code"] == 0
    assert "hello-budget" in result["stdout"]
    assert result["timed_out"] is False
    # Ran through the task registry because effective timeout > budget.
    assert result["task_id"]


def test_long_command_hands_off_to_background(monkeypatch, tmp_path, temp_registry):
    monkeypatch.setattr(server, "FOREGROUND_TIME_BUDGET", 1)
    start = time.monotonic()
    result = server._run_foreground_command(
        python_sleep_cmd(15.0, before="started"), tmp_path, 120
    )
    elapsed = time.monotonic() - start
    assert elapsed < 10
    assert result.get("auto_backgrounded") is True
    assert result["completed"] is False
    assert "task_id" in result
    # The command keeps running as a normal background task; clean it up.
    temp_registry.cancel(result["task_id"])


def test_budget_zero_disables_handoff(monkeypatch, tmp_path, temp_registry):
    monkeypatch.setattr(server, "FOREGROUND_TIME_BUDGET", 0)
    result = server._run_foreground_command(python_print_cmd("direct"), tmp_path, 120)
    assert result["success"] is True
    assert "task_id" not in result


def test_short_timeout_stays_foreground(monkeypatch, tmp_path, temp_registry):
    monkeypatch.setattr(server, "FOREGROUND_TIME_BUDGET", 30)
    result = server._run_foreground_command(python_print_cmd("fg"), tmp_path, 10)
    assert result["success"] is True
    assert "task_id" not in result


def test_wait_task_timeout_is_clamped(monkeypatch, tmp_path, temp_registry):
    monkeypatch.setattr(server, "WAIT_TASK_MAX_TIMEOUT", 1.0)
    queued = temp_registry.submit_command(
        command=python_sleep_cmd(15.0), cwd=tmp_path, timeout=60
    )
    task_id = queued["task_id"]
    start = time.monotonic()
    meta = server._wait_task_clamped(task_id, timeout=100.0, poll_interval=0.1)
    elapsed = time.monotonic() - start
    assert elapsed < 6
    assert meta["completed"] is False
    assert meta["wait_timeout_clamped_to"] == 1.0
    temp_registry.cancel(task_id)


def test_write_tools_do_not_require_confirmation():
    assert server.LOCAL_WRITE_TOOL["destructiveHint"] is False
    assert server.OPEN_WORLD_WRITE_TOOL["destructiveHint"] is False
