"""Unit tests for pure CDP helpers in chrome.py (no live Chrome needed)."""

from __future__ import annotations

from notion_local_ops_mcp.chrome import (
    extract_console_messages,
    pick_tab,
    summarize_network_events,
)


def test_pick_tab_prefers_pages_and_matches() -> None:
    tabs = [
        {"id": "A", "type": "background_page", "title": "ext", "url": "chrome-extension://x"},
        {"id": "B", "type": "page", "title": "Home", "url": "http://localhost:3000/"},
        {"id": "C", "type": "page", "title": "Docs", "url": "https://example.com/docs"},
    ]
    assert pick_tab(tabs)["id"] == "B"  # default: first page tab
    assert pick_tab(tabs, "C")["id"] == "C"  # exact tab id
    assert pick_tab(tabs, "example.com")["id"] == "C"  # url substring
    assert pick_tab(tabs, "docs")["id"] == "C"  # case-insensitive title match
    assert pick_tab(tabs, "missing") is None
    assert pick_tab([]) is None


def test_extract_console_messages() -> None:
    events = [
        {
            "method": "Runtime.consoleAPICalled",
            "params": {
                "type": "warning",
                "args": [
                    {"type": "string", "value": "careful"},
                    {"type": "number", "value": 42},
                ],
                "timestamp": 1.0,
            },
        },
        {
            "method": "Runtime.exceptionThrown",
            "params": {
                "exceptionDetails": {
                    "text": "Uncaught",
                    "exception": {"description": "Error: boom"},
                }
            },
        },
        {
            "method": "Log.entryAdded",
            "params": {
                "entry": {
                    "source": "network",
                    "level": "error",
                    "text": "404 Not Found",
                    "url": "http://x/y",
                }
            },
        },
        {"method": "Network.requestWillBeSent", "params": {}},
    ]
    messages = extract_console_messages(events)
    assert len(messages) == 3
    assert messages[0]["level"] == "warning"
    assert messages[0]["text"] == "careful 42"
    assert messages[1]["source"] == "exception"
    assert "boom" in messages[1]["text"]
    assert messages[2]["source"] == "network"
    assert messages[2]["text"] == "404 Not Found"


def test_summarize_network_events() -> None:
    events = [
        {
            "method": "Network.requestWillBeSent",
            "params": {"requestId": "1", "request": {"url": "http://a/", "method": "GET"}},
        },
        {
            "method": "Network.responseReceived",
            "params": {
                "requestId": "1",
                "response": {"status": 200, "mimeType": "text/html"},
            },
        },
        {
            "method": "Network.requestWillBeSent",
            "params": {"requestId": "2", "request": {"url": "http://a/x.js", "method": "GET"}},
        },
        {
            "method": "Network.loadingFailed",
            "params": {"requestId": "2", "errorText": "net::ERR_FAILED"},
        },
    ]
    entries = summarize_network_events(events)
    assert len(entries) == 2
    assert entries[0]["url"] == "http://a/"
    assert entries[0]["status"] == 200
    assert entries[0]["mime_type"] == "text/html"
    assert entries[1]["failed"] is True
    assert entries[1]["error"] == "net::ERR_FAILED"


def test_summarize_network_respects_limit() -> None:
    events = [
        {
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": str(i),
                "request": {"url": f"http://a/{i}", "method": "GET"},
            },
        }
        for i in range(10)
    ]
    entries = summarize_network_events(events, max_requests=3)
    assert len(entries) == 3
