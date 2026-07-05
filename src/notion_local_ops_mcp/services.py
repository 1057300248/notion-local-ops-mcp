"""Long-running dev-service management and TCP port utilities.

Backs the ``start_service``/``stop_service``/``list_services``/``service_logs``
and ``port_check``/``kill_port`` MCP tools. Services are detached background
processes (dev servers, watchers) with output captured to per-service logs and
metadata persisted under the state directory.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

IS_WINDOWS = os.name == "nt"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pid_alive(pid: int) -> bool:
    """Check whether a PID is alive without signalling it.

    Note: on Windows ``os.kill(pid, 0)`` would TERMINATE the process, so a
    ctypes OpenProcess/GetExitCodeProcess probe is used instead.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _kill_tree(pid: int) -> None:
    """Kill a process and its children."""
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            capture_output=True,
        )
        return
    import signal

    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass


def parse_netstat_listeners(output: str) -> dict[int, set[int]]:
    """Parse ``netstat -ano -p tcp`` output into {port: {pids}} for LISTENING rows."""
    listeners: dict[int, set[int]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if "LISTEN" not in parts[3].upper():
            continue
        port_text = parts[1].rsplit(":", 1)[-1]
        try:
            port = int(port_text)
            pid = int(parts[4])
        except ValueError:
            continue
        listeners.setdefault(port, set()).add(pid)
    return listeners


def _listening_pids(port: int) -> list[int]:
    if IS_WINDOWS:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True
        )
        return sorted(parse_netstat_listeners(proc.stdout or "").get(int(port), set()))
    proc = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
    )
    return sorted(
        {int(token) for token in (proc.stdout or "").split() if token.strip().isdigit()}
    )


def port_status(port: int) -> dict[str, Any]:
    pids = _listening_pids(port)
    return {"port": int(port), "listening": bool(pids), "pids": pids}


def kill_port(port: int) -> dict[str, Any]:
    pids = _listening_pids(port)
    if not pids:
        return {
            "success": True,
            "port": int(port),
            "killed_pids": [],
            "note": "nothing listening on this port",
        }
    for pid in pids:
        _kill_tree(pid)
    time.sleep(0.5)
    after = port_status(port)
    return {
        "success": not after["listening"],
        "port": int(port),
        "killed_pids": pids,
        "still_listening": after["listening"],
    }


class ServiceManager:
    """Start/stop/inspect named long-running background services."""

    def __init__(self, state_dir: Path):
        self._dir = Path(state_dir) / "services"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _meta_path(self, name: str) -> Path:
        return self._dir / f"{name}.json"

    def _log_path(self, name: str) -> Path:
        return self._dir / f"{name}.log"

    def _read_meta(self, name: str) -> dict[str, Any] | None:
        path = self._meta_path(name)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_meta(self, name: str, meta: dict[str, Any]) -> None:
        self._meta_path(name).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def _tail(path: Path, lines: int) -> list[str]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return text.splitlines()[-max(1, int(lines)) :]

    def start(
        self,
        name: str,
        command: str,
        cwd: Path,
        *,
        port: int | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not _NAME_RE.match(name or ""):
            return {
                "success": False,
                "error": {
                    "code": "bad_name",
                    "message": "Service name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}",
                },
            }
        cwd = Path(cwd)
        if not cwd.is_dir():
            return {
                "success": False,
                "error": {"code": "cwd_not_found", "message": f"cwd does not exist: {cwd}"},
            }
        existing = self._read_meta(name)
        if existing and _pid_alive(existing.get("pid") or 0):
            return {
                "success": False,
                "error": {
                    "code": "already_running",
                    "message": (
                        f"Service {name!r} is already running (pid {existing['pid']}). "
                        "Stop it first with stop_service."
                    ),
                },
                "pid": existing.get("pid"),
            }
        log_path = self._log_path(name)
        log_handle = open(log_path, "ab")
        log_handle.write(
            f"\n===== start {name}: {command} @ {_now_iso()} =====\n".encode("utf-8")
        )
        log_handle.flush()
        merged_env = dict(os.environ)
        if env:
            merged_env.update({str(key): str(value) for key, value in env.items()})
        kwargs: dict[str, Any] = {
            "shell": True,
            "cwd": str(cwd),
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "env": merged_env,
        }
        if IS_WINDOWS:
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            ) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(command, **kwargs)
        finally:
            log_handle.close()  # the child keeps its inherited handle
        time.sleep(0.8)
        exit_code = process.poll()
        meta = {
            "name": name,
            "command": command,
            "cwd": str(cwd),
            "pid": process.pid,
            "port": int(port) if port else None,
            "started_at": _now_iso(),
            "log": str(log_path),
        }
        self._write_meta(name, meta)
        if exit_code is not None:
            return {
                "success": False,
                "error": {
                    "code": "exited_early",
                    "message": f"Service exited immediately with code {exit_code}.",
                },
                "name": name,
                "pid": process.pid,
                "exit_code": exit_code,
                "log": str(log_path),
                "log_tail": self._tail(log_path, 40),
            }
        result: dict[str, Any] = {
            "success": True,
            "name": name,
            "pid": process.pid,
            "cwd": str(cwd),
            "log": str(log_path),
        }
        if port:
            result["port"] = int(port)
            result["port_listening"] = bool(port_status(int(port)).get("listening"))
        return result

    def stop(self, name: str) -> dict[str, Any]:
        meta = self._read_meta(name)
        if meta is None:
            return {
                "success": False,
                "error": {"code": "not_found", "message": f"No service named {name!r}"},
            }
        pid = int(meta.get("pid") or 0)
        if not _pid_alive(pid):
            meta["stopped_at"] = meta.get("stopped_at") or _now_iso()
            self._write_meta(name, meta)
            return {"success": True, "name": name, "pid": pid, "already_stopped": True}
        _kill_tree(pid)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.1)
        still_alive = _pid_alive(pid)
        meta["stopped_at"] = _now_iso()
        self._write_meta(name, meta)
        return {
            "success": not still_alive,
            "name": name,
            "pid": pid,
            "killed": not still_alive,
        }

    def list(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for meta_path in sorted(self._dir.glob("*.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pid = int(meta.get("pid") or 0)
            entries.append(
                {
                    "name": meta.get("name") or meta_path.stem,
                    "command": meta.get("command"),
                    "cwd": meta.get("cwd"),
                    "pid": pid,
                    "port": meta.get("port"),
                    "running": _pid_alive(pid),
                    "started_at": meta.get("started_at"),
                    "stopped_at": meta.get("stopped_at"),
                    "log": meta.get("log"),
                }
            )
        return entries

    def logs(self, name: str, *, tail_lines: int = 100) -> dict[str, Any]:
        log_path = self._log_path(name)
        if not log_path.exists():
            return {
                "success": False,
                "error": {"code": "not_found", "message": f"No logs for service {name!r}"},
            }
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return {
                "success": False,
                "error": {"code": "read_failed", "message": str(exc)},
            }
        meta = self._read_meta(name) or {}
        return {
            "success": True,
            "name": name,
            "log": str(log_path),
            "running": _pid_alive(int(meta.get("pid") or 0)),
            "line_count": len(lines),
            "lines": lines[-max(1, int(tail_lines)) :],
        }
