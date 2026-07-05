"""Chrome DevTools Protocol (CDP) helpers.

Talks to a Chrome/Edge instance started with ``--remote-debugging-port``. The
``ensure_running`` helper launches a dedicated automation instance (with its
own user-data-dir) when no debugger is reachable, because an already-running
normal Chrome cannot be attached to retroactively.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .imaging import compress_image_bytes

DEFAULT_DEBUG_PORT = 9222
_HTTP_TIMEOUT = 5.0

IS_WINDOWS = os.name == "nt"

_WINDOWS_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)
_POSIX_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
)


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}"


def get_version(port: int, timeout: float = _HTTP_TIMEOUT) -> dict[str, Any]:
    response = httpx.get(f"{_base_url(port)}/json/version", timeout=timeout)
    response.raise_for_status()
    return response.json()


def is_debugger_up(port: int) -> bool:
    try:
        get_version(port, timeout=1.5)
        return True
    except Exception:
        return False


def find_chrome_binary(explicit: str | None = None) -> str | None:
    """Locate a Chrome/Edge binary, preferring an explicitly configured path."""
    if explicit:
        return explicit if Path(explicit).exists() else None
    candidates = _WINDOWS_CHROME_CANDIDATES if IS_WINDOWS else _POSIX_CHROME_CANDIDATES
    for candidate in candidates:
        path = Path(candidate)
        if path.is_absolute():
            if path.exists():
                return str(path)
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def launch_chrome(
    port: int,
    *,
    binary: str | None = None,
    user_data_dir: Path,
    url: str | None = None,
    headless: bool = False,
    wait_seconds: float = 20.0,
) -> dict[str, Any]:
    """Launch a debuggable Chrome instance and wait for the DevTools port."""
    chrome = find_chrome_binary(binary)
    if not chrome:
        return {
            "success": False,
            "error": {
                "code": "chrome_not_found",
                "message": (
                    "Could not find a Chrome/Edge binary. Set "
                    "NOTION_LOCAL_OPS_CHROME_BINARY to the full executable path."
                ),
            },
        }
    user_data_dir = Path(user_data_dir)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    args = [
        chrome,
        f"--remote-debugging-port={int(port)}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        args.append("--headless=new")
    args.append(url or "about:blank")
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(args, **kwargs)
    deadline = time.monotonic() + float(wait_seconds)
    while time.monotonic() < deadline:
        if is_debugger_up(port):
            try:
                info = get_version(port)
            except Exception:
                info = {}
            return {
                "success": True,
                "launched": True,
                "pid": process.pid,
                "binary": chrome,
                "port": int(port),
                "browser": info.get("Browser"),
            }
        if process.poll() is not None:
            return {
                "success": False,
                "error": {
                    "code": "chrome_exited",
                    "message": (
                        f"Chrome exited with code {process.returncode} before the "
                        "debugger came up (is another instance using the same profile?)."
                    ),
                },
            }
        time.sleep(0.3)
    return {
        "success": False,
        "error": {
            "code": "debugger_timeout",
            "message": f"Chrome did not open DevTools port {port} within {wait_seconds}s.",
        },
    }


def ensure_running(
    port: int,
    *,
    binary: str | None = None,
    user_data_dir: Path,
    url: str | None = None,
    headless: bool = False,
) -> dict[str, Any]:
    """Return debugger info, launching a debuggable Chrome if necessary."""
    if is_debugger_up(port):
        try:
            info = get_version(port)
        except Exception:
            info = {}
        return {
            "success": True,
            "launched": False,
            "port": int(port),
            "browser": info.get("Browser"),
            "note": "debugger already reachable",
        }
    return launch_chrome(
        port, binary=binary, user_data_dir=user_data_dir, url=url, headless=headless
    )


def list_tabs(port: int) -> list[dict[str, Any]]:
    response = httpx.get(f"{_base_url(port)}/json/list", timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    return [
        {
            "id": tab.get("id"),
            "type": tab.get("type"),
            "title": tab.get("title"),
            "url": tab.get("url"),
            "webSocketDebuggerUrl": tab.get("webSocketDebuggerUrl"),
        }
        for tab in response.json()
    ]


def open_tab(port: int, url: str) -> dict[str, Any]:
    endpoint = f"{_base_url(port)}/json/new?{urlencode({'url': url})}"
    response = httpx.put(endpoint, timeout=_HTTP_TIMEOUT)
    if response.status_code == 405:
        # Older Chrome versions only accept GET here.
        response = httpx.get(endpoint, timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


def activate_tab(port: int, tab_id: str) -> dict[str, Any]:
    response = httpx.get(f"{_base_url(port)}/json/activate/{tab_id}", timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    return {"success": True, "id": tab_id}


def pick_tab(tabs: list[dict[str, Any]], target: str | None = None) -> dict[str, Any] | None:
    """Pick a page tab by exact id, then url/title substring; default first page."""
    pages = [tab for tab in tabs if (tab.get("type") or "page") == "page"]
    if not target:
        return pages[0] if pages else None
    needle = str(target).strip()
    for tab in pages:
        if tab.get("id") == needle:
            return tab
    lowered = needle.lower()
    for tab in pages:
        if lowered in (tab.get("url") or "").lower() or lowered in (tab.get("title") or "").lower():
            return tab
    return None


class CdpSession:
    """Small synchronous CDP session over a tab's DevTools websocket."""

    def __init__(self, ws_url: str, *, open_timeout: float = 10.0):
        from websockets.sync.client import connect

        self._ws = connect(
            ws_url,
            max_size=128 * 1024 * 1024,
            open_timeout=open_timeout,
            close_timeout=5.0,
        )
        self._next_id = 0
        self._events: list[dict[str, Any]] = []

    def __enter__(self) -> "CdpSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        self._ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + float(timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"CDP call timed out: {method}")
            try:
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError:
                raise TimeoutError(f"CDP call timed out: {method}") from None
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if data.get("id") == message_id:
                if "error" in data:
                    raise RuntimeError(f"CDP error for {method}: {data['error']}")
                return data.get("result") or {}
            if "method" in data:
                self._events.append(data)

    def drain_events(self, duration: float) -> list[dict[str, Any]]:
        """Collect protocol events for ``duration`` seconds."""
        events = list(self._events)
        self._events = []
        deadline = time.monotonic() + max(0.0, float(duration))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError:
                break
            except Exception:
                break
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if "method" in data:
                events.append(data)
        return events


def open_session(
    port: int, target: str | None = None
) -> tuple[CdpSession | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Open a CDP session against the tab matching ``target``.

    Returns ``(session, tab, error)`` where exactly one of session/error is set.
    """
    try:
        tabs = list_tabs(port)
    except Exception as exc:
        return None, None, {
            "code": "debugger_unreachable",
            "message": f"DevTools port {port} unreachable: {exc}. Run chrome_ensure first.",
        }
    tab = pick_tab(tabs, target)
    if tab is None:
        return None, None, {
            "code": "tab_not_found",
            "message": f"No open page tab matches {target!r}. Use chrome_tabs to list tabs.",
        }
    ws_url = tab.get("webSocketDebuggerUrl")
    if not ws_url:
        return None, tab, {
            "code": "no_debug_url",
            "message": "Tab has no webSocketDebuggerUrl (another client may be attached).",
        }
    try:
        session = CdpSession(ws_url)
    except Exception as exc:
        return None, tab, {"code": "ws_connect_failed", "message": str(exc)}
    return session, tab, None


def _tab_brief(tab: dict[str, Any] | None) -> dict[str, Any] | None:
    if not tab:
        return None
    return {"id": tab.get("id"), "title": tab.get("title"), "url": tab.get("url")}


def screenshot_tab(
    port: int,
    target: str | None = None,
    *,
    full_page: bool = False,
    max_width: int = 1400,
    format: str = "jpeg",
    quality: int = 80,
) -> dict[str, Any]:
    session, tab, error = open_session(port, target)
    if error or session is None:
        return {"success": False, "error": error, "tab": _tab_brief(tab)}
    try:
        if full_page:
            metrics = session.call("Page.getLayoutMetrics")
            content = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            width = int(content.get("width") or 0)
            height = int(content.get("height") or 0)
            if width and height:
                session.call(
                    "Emulation.setDeviceMetricsOverride",
                    {
                        "width": width,
                        "height": min(height, 10000),
                        "deviceScaleFactor": 1,
                        "mobile": False,
                    },
                )
        result = session.call(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": bool(full_page)},
            timeout=60.0,
        )
        if full_page:
            try:
                session.call("Emulation.clearDeviceMetricsOverride")
            except Exception:
                pass
    except (TimeoutError, RuntimeError) as exc:
        return {
            "success": False,
            "error": {"code": "cdp_failed", "message": str(exc)},
            "tab": _tab_brief(tab),
        }
    finally:
        session.close()
    raw = base64.b64decode(result.get("data") or "")
    data, fmt, width, height = compress_image_bytes(
        raw, max_width=max_width, format=format, quality=quality
    )
    return {
        "success": True,
        "data": data,
        "format": fmt,
        "width": width,
        "height": height,
        "tab": _tab_brief(tab),
    }


def evaluate_in_tab(
    port: int,
    expression: str,
    target: str | None = None,
    *,
    await_promise: bool = True,
    timeout: float = 30.0,
) -> dict[str, Any]:
    session, tab, error = open_session(port, target)
    if error or session is None:
        return {"success": False, "error": error, "tab": _tab_brief(tab)}
    try:
        result = session.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": bool(await_promise),
                "returnByValue": True,
                "userGesture": True,
            },
            timeout=timeout,
        )
    except (TimeoutError, RuntimeError) as exc:
        return {
            "success": False,
            "error": {"code": "cdp_failed", "message": str(exc)},
            "tab": _tab_brief(tab),
        }
    finally:
        session.close()
    exception = result.get("exceptionDetails")
    remote = result.get("result") or {}
    if exception:
        description = (exception.get("exception") or {}).get("description")
        return {
            "success": False,
            "error": {
                "code": "js_exception",
                "message": description or exception.get("text") or "JavaScript exception",
            },
            "tab": _tab_brief(tab),
        }
    return {
        "success": True,
        "type": remote.get("type"),
        "value": remote.get("value"),
        "description": remote.get("description"),
        "tab": _tab_brief(tab),
    }


def extract_console_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert raw CDP events into console message dicts (pure, testable)."""
    messages: list[dict[str, Any]] = []
    for event in events:
        method = event.get("method")
        params = event.get("params") or {}
        if method == "Runtime.consoleAPICalled":
            parts: list[str] = []
            for arg in params.get("args") or []:
                if "value" in arg:
                    parts.append(str(arg.get("value")))
                elif arg.get("description"):
                    parts.append(str(arg["description"]))
                else:
                    parts.append(str(arg.get("type")))
            messages.append(
                {
                    "source": "console",
                    "level": params.get("type") or "log",
                    "text": " ".join(parts),
                    "timestamp": params.get("timestamp"),
                }
            )
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            description = (details.get("exception") or {}).get("description")
            messages.append(
                {
                    "source": "exception",
                    "level": "error",
                    "text": description or details.get("text") or "Uncaught exception",
                    "timestamp": params.get("timestamp"),
                }
            )
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            messages.append(
                {
                    "source": entry.get("source") or "log",
                    "level": entry.get("level") or "info",
                    "text": entry.get("text") or "",
                    "url": entry.get("url"),
                }
            )
    return messages


def collect_console(
    port: int,
    target: str | None = None,
    *,
    duration: float = 4.0,
    navigate: str | None = None,
) -> dict[str, Any]:
    session, tab, error = open_session(port, target)
    if error or session is None:
        return {"success": False, "error": error, "tab": _tab_brief(tab)}
    try:
        session.call("Runtime.enable")
        session.call("Log.enable")
        if navigate:
            session.call("Page.enable")
            session.call("Page.navigate", {"url": navigate})
        events = session.drain_events(duration)
    except (TimeoutError, RuntimeError) as exc:
        return {
            "success": False,
            "error": {"code": "cdp_failed", "message": str(exc)},
            "tab": _tab_brief(tab),
        }
    finally:
        session.close()
    messages = extract_console_messages(events)
    return {
        "success": True,
        "tab": _tab_brief(tab),
        "duration": float(duration),
        "message_count": len(messages),
        "messages": messages,
    }


def summarize_network_events(
    events: list[dict[str, Any]], max_requests: int = 100
) -> list[dict[str, Any]]:
    """Aggregate raw Network.* CDP events per request (pure, testable)."""
    requests: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        method = event.get("method")
        params = event.get("params") or {}
        request_id = params.get("requestId")
        if not request_id:
            continue
        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            if request_id not in requests:
                order.append(request_id)
            entry = requests.setdefault(request_id, {})
            entry["url"] = request.get("url")
            entry["method"] = request.get("method")
            entry.setdefault("status", None)
        elif method == "Network.responseReceived":
            response = params.get("response") or {}
            entry = requests.setdefault(request_id, {})
            entry["status"] = response.get("status")
            entry["mime_type"] = response.get("mimeType")
        elif method == "Network.loadingFailed":
            entry = requests.setdefault(request_id, {})
            entry["failed"] = True
            entry["error"] = params.get("errorText")
    summary = [requests[rid] for rid in order if requests.get(rid, {}).get("url")]
    return summary[: int(max_requests)]


def collect_network(
    port: int,
    target: str | None = None,
    *,
    duration: float = 6.0,
    navigate: str | None = None,
    max_requests: int = 100,
) -> dict[str, Any]:
    session, tab, error = open_session(port, target)
    if error or session is None:
        return {"success": False, "error": error, "tab": _tab_brief(tab)}
    try:
        session.call("Network.enable")
        if navigate:
            session.call("Page.enable")
            session.call("Page.navigate", {"url": navigate})
        events = session.drain_events(duration)
    except (TimeoutError, RuntimeError) as exc:
        return {
            "success": False,
            "error": {"code": "cdp_failed", "message": str(exc)},
            "tab": _tab_brief(tab),
        }
    finally:
        session.close()
    entries = summarize_network_events(events, max_requests=max_requests)
    return {
        "success": True,
        "tab": _tab_brief(tab),
        "duration": float(duration),
        "request_count": len(entries),
        "requests": entries,
    }
