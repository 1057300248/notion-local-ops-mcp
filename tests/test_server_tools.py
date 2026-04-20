from __future__ import annotations

import json
from pathlib import Path

from notion_local_ops_mcp.executors import ExecutorRegistry
from notion_local_ops_mcp.tasks import TaskStore


def test_server_apply_patch_tool_updates_file(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    target = tmp_path / "note.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")

    result = server.apply_patch(
        patch="\n".join(
            [
                "*** Begin Patch",
                f"*** Update File: {target}",
                "@@",
                " hello",
                "-world",
                "+there",
                "*** End Patch",
            ]
        )
    )

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "hello\nthere\n"


def test_server_run_command_can_dispatch_background_tasks(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    server.registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command="python3 -c \"print('codex')\"",
        claude_command="python3 -c \"print('claude')\"",
    )

    queued = server.run_command(
        command="python3 -c \"print('background')\"",
        cwd=str(tmp_path),
        timeout=5,
        run_in_background=True,
    )
    result = server.wait_task(queued["task_id"], timeout=2, poll_interval=0.05)

    assert queued["executor"] == "shell"
    assert queued["status"] == "queued"
    assert result["status"] == "succeeded"
    assert "background" in result["stdout_tail"]


def test_server_read_files_tool_returns_multiple_file_results(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("alpha\n", encoding="utf-8")
    second.write_text("beta\n", encoding="utf-8")

    result = server.read_files(paths=[str(first), str(second)])

    assert result["success"] is True
    assert [item["content"] for item in result["results"]] == ["alpha", "beta"]


def test_server_delegate_task_accepts_structured_fields(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    server.registry = ExecutorRegistry(
        store=TaskStore(tmp_path / "state"),
        codex_command="python3 -c \"print('codex')\"",
        claude_command="python3 -c \"print('claude')\"",
    )

    queued = server.delegate_task(
        task="Implement the fallback flow",
        goal="Ship a working fallback task runner",
        cwd=str(tmp_path),
        acceptance_criteria=["Tool returns structured status"],
        verification_commands=["pytest -q"],
        commit_mode="allowed",
    )
    meta = server.get_task(queued["task_id"])

    assert meta["goal"] == "Ship a working fallback task runner"
    assert meta["acceptance_criteria"] == ["Tool returns structured status"]
    assert meta["verification_commands"] == ["pytest -q"]
    assert meta["commit_mode"] == "allowed"


def test_server_run_command_stream_returns_task_polling_hint(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    old_registry = server.registry
    try:
        server.registry = ExecutorRegistry(
            store=TaskStore(tmp_path / "state"),
            codex_command="python3 -c \"print('codex')\"",
            claude_command="python3 -c \"print('claude')\"",
        )

        queued = server.run_command_stream(
            command="python3 -c \"print('stream')\"",
            cwd=str(tmp_path),
            timeout=5,
        )
        result = server.wait_task(queued["task_id"], timeout=2, poll_interval=0.05)

        assert queued["stream_mode"] == "task-polling"
        assert "next" in queued
        assert result["status"] == "succeeded"
        assert "stream" in result["stdout_tail"]
    finally:
        server.registry = old_registry


def test_server_purge_tasks_dry_run_reports_candidates(tmp_path: Path) -> None:
    from notion_local_ops_mcp import server

    old_store = server.store
    old_registry = server.registry
    try:
        server.store = TaskStore(tmp_path / "state")
        server.registry = ExecutorRegistry(
            store=server.store,
            codex_command="python3 -c \"print('codex')\"",
            claude_command="python3 -c \"print('claude')\"",
        )

        created = server.store.create(task="old", executor="shell", cwd=str(tmp_path))
        meta_path = server.store._task_dir(created["task_id"]) / "meta.json"  # noqa: SLF001
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
        payload["updated_at"] = "2000-01-01T00:00:00+00:00"
        meta_path.write_text(json.dumps(payload), encoding="utf-8")

        result = server.purge_tasks(older_than_hours=1, dry_run=True)

        assert result["success"] is True
        assert result["purged"] == 1
        assert created["task_id"] in result["task_ids"]
    finally:
        server.store = old_store
        server.registry = old_registry
