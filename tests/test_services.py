from __future__ import annotations

import time

from notion_local_ops_mcp.services import (
    ServiceManager,
    parse_netstat_listeners,
    port_status,
)

from .helpers import python_sleep_cmd

NETSTAT_SAMPLE = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1234
  TCP    127.0.0.1:8766         0.0.0.0:0              LISTENING       4321
  TCP    127.0.0.1:8766         127.0.0.1:52345        ESTABLISHED     4321
  TCP    [::]:445               [::]:0                 LISTENING       4
  UDP    0.0.0.0:500            *:*                                    5678
"""


def test_parse_netstat_listeners() -> None:
    listeners = parse_netstat_listeners(NETSTAT_SAMPLE)
    assert listeners[135] == {1234}
    assert listeners[8766] == {4321}
    assert listeners[445] == {4}
    assert 500 not in listeners


def test_port_status_reports_free_port() -> None:
    status = port_status(1)  # TCP port 1 is essentially never listening
    assert status["port"] == 1
    assert status["listening"] is False
    assert status["pids"] == []


def test_service_lifecycle(tmp_path) -> None:
    manager = ServiceManager(tmp_path)
    command = python_sleep_cmd(30, before="print('service up', flush=True)")
    result = manager.start("demo", command, tmp_path)
    assert result["success"] is True, result
    assert isinstance(result["pid"], int)

    try:
        listed = manager.list()
        assert len(listed) == 1
        assert listed[0]["name"] == "demo"
        assert listed[0]["running"] is True

        logs = manager.logs("demo")
        deadline = time.time() + 10
        while time.time() < deadline:
            logs = manager.logs("demo")
            if logs["success"] and any("service up" in line for line in logs["lines"]):
                break
            time.sleep(0.3)
        assert any("service up" in line for line in logs["lines"]), logs
    finally:
        stopped = manager.stop("demo")

    assert stopped["success"] is True
    listed = manager.list()
    assert listed[0]["running"] is False
    assert listed[0]["stopped_at"]


def test_start_duplicate_rejected(tmp_path) -> None:
    manager = ServiceManager(tmp_path)
    command = python_sleep_cmd(30)
    first = manager.start("dup", command, tmp_path)
    assert first["success"] is True, first
    try:
        second = manager.start("dup", command, tmp_path)
        assert second["success"] is False
        assert second["error"]["code"] == "already_running"
    finally:
        manager.stop("dup")


def test_start_reports_early_exit(tmp_path) -> None:
    manager = ServiceManager(tmp_path)
    result = manager.start("boom", "exit 7", tmp_path)
    assert result["success"] is False
    assert result["error"]["code"] == "exited_early"
    assert result["exit_code"] == 7


def test_stop_unknown_service(tmp_path) -> None:
    manager = ServiceManager(tmp_path)
    result = manager.stop("nope")
    assert result["success"] is False
    assert result["error"]["code"] == "not_found"


def test_bad_service_name_rejected(tmp_path) -> None:
    manager = ServiceManager(tmp_path)
    result = manager.start("bad name!", "echo hi", tmp_path)
    assert result["success"] is False
    assert result["error"]["code"] == "bad_name"


def test_logs_missing_service(tmp_path) -> None:
    manager = ServiceManager(tmp_path)
    result = manager.logs("ghost")
    assert result["success"] is False
    assert result["error"]["code"] == "not_found"
