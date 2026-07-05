from __future__ import annotations

import httpx

from notion_local_ops_mcp.webtools import MAX_BODY_CHARS, http_request


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_get_returns_parsed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"hello": "world"})

    with _client(handler) as client:
        result = http_request(url="http://test/api", client=client)
    assert result["success"] is True
    assert result["status"] == 200
    assert result["ok"] is True
    assert result["json"] == {"hello": "world"}


def test_post_with_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.headers["content-type"].startswith("application/json")
        assert b'"a"' in request.content
        return httpx.Response(201, text="created")

    with _client(handler) as client:
        result = http_request(
            url="http://test/api", method="post", json_body={"a": 1}, client=client
        )
    assert result["status"] == 201
    assert result["body"] == "created"
    assert result["json"] is None


def test_large_body_is_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * (MAX_BODY_CHARS + 500))

    with _client(handler) as client:
        result = http_request(url="http://test/big", client=client)
    assert result["body_truncated"] is True
    assert len(result["body"]) == MAX_BODY_CHARS


def test_unsupported_method_rejected() -> None:
    result = http_request(url="http://test/", method="BREW")
    assert result["success"] is False
    assert result["error"]["code"] == "bad_method"


def test_body_and_json_body_conflict() -> None:
    result = http_request(url="http://test/", body="x", json_body={})
    assert result["success"] is False
    assert result["error"]["code"] == "bad_arguments"


def test_transport_error_is_structured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with _client(handler) as client:
        result = http_request(url="http://test/", client=client)
    assert result["success"] is False
    assert result["error"]["code"] == "request_failed"
    assert "boom" in result["error"]["message"]
