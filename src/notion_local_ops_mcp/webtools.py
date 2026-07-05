"""Structured HTTP request helper backing the ``http_request`` tool."""

from __future__ import annotations

import time
from typing import Any

import httpx

MAX_BODY_CHARS = 20_000

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def http_request(
    *,
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    json_body: Any = None,
    timeout: float = 30.0,
    follow_redirects: bool = True,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Send an HTTP request and return a structured, size-capped result.

    ``client`` is injectable for tests (e.g. ``httpx.MockTransport``).
    """
    verb = (method or "GET").strip().upper()
    if verb not in _ALLOWED_METHODS:
        return {
            "success": False,
            "error": {"code": "bad_method", "message": f"Unsupported HTTP method {method!r}"},
        }
    if body is not None and json_body is not None:
        return {
            "success": False,
            "error": {
                "code": "bad_arguments",
                "message": "Pass either body or json_body, not both",
            },
        }
    owns_client = client is None
    if owns_client:
        client = httpx.Client(follow_redirects=follow_redirects, timeout=float(timeout))
    started = time.monotonic()
    try:
        response = client.request(verb, url, headers=headers, content=body, json=json_body)
    except httpx.HTTPError as exc:
        return {
            "success": False,
            "error": {"code": "request_failed", "message": f"{type(exc).__name__}: {exc}"},
            "url": url,
            "method": verb,
        }
    finally:
        if owns_client:
            client.close()
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    text = response.text
    truncated = len(text) > MAX_BODY_CHARS
    parsed: Any = None
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
    return {
        "success": True,
        "status": response.status_code,
        "ok": response.is_success,
        "method": verb,
        "url": str(response.url),
        "elapsed_ms": elapsed_ms,
        "headers": dict(response.headers),
        "body": text[:MAX_BODY_CHARS],
        "body_truncated": truncated,
        "json": parsed,
    }
