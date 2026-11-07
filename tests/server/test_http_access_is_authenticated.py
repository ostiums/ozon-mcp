"""The account is never served on an unauthenticated network endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import anyio
import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from pydantic import SecretStr
from starlette.testclient import TestClient

from ozon_mcp import __main__ as entrypoint
from ozon_mcp.mcp_server import _transport_security
from ozon_mcp.settings import OzonSettings, get_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.types import Message, Scope

TOKEN = "a-randomly-generated-test-token-0123456789"  # ruff: ignore[hardcoded-password-string]
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(params=["http", "sse"])
def client(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = get_settings()
    monkeypatch.setattr(settings, "transport", request.param)
    monkeypatch.setattr(settings, "auth_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "allowed_hosts", ["localhost:8084"])
    monkeypatch.setattr(settings, "allowed_origins", ["http://localhost:8084"])
    monkeypatch.setattr(entrypoint, "mcp", FastMCP("access-test"))
    with TestClient(entrypoint._http_app(), base_url="http://localhost:8084") as test_client:
        yield test_client


@pytest.mark.parametrize("path", ["/mcp", "/sse", "/messages/?session_id=unknown", "/metrics", "/missing"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic secret"])
def test_every_http_route_requires_the_token(client: TestClient, path: str, authorization: str | None) -> None:
    headers = {"Authorization": authorization} if authorization is not None else {}
    response = client.get(path, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert TOKEN not in response.text


def test_query_parameter_does_not_authenticate(client: TestClient) -> None:
    assert client.get("/metrics", params={"access_token": TOKEN}).status_code == 401


def test_metrics_accepts_the_token_without_an_origin(client: TestClient) -> None:
    response = client.get("/metrics", headers=AUTH)
    assert response.status_code == 200
    assert "ozon_mcp_upstream_requests" in response.text


@pytest.mark.parametrize("path", ["/mcp", "/sse", "/messages/", "/metrics"])
def test_rebinding_host_is_rejected_on_every_route(client: TestClient, path: str) -> None:
    assert client.get(path, headers={**AUTH, "Host": "attacker.example:8084"}).status_code == 421


def test_foreign_origin_is_rejected_even_with_the_token(client: TestClient) -> None:
    assert client.get("/metrics", headers={**AUTH, "Origin": "https://attacker.example"}).status_code == 403
    assert client.get("/metrics", headers={**AUTH, "Origin": "http://localhost:8084"}).status_code == 200


def test_duplicate_authorization_is_rejected(client: TestClient) -> None:
    response = client.get("/metrics", headers=[("Authorization", f"Bearer {TOKEN}"), ("Authorization", "Bearer other")])
    assert response.status_code == 401


def test_token_comparison_accepts_non_ascii_input_without_crashing(client: TestClient) -> None:
    response = client.get("/metrics", headers=[(b"authorization", b"Bearer \xff")])
    assert response.status_code == 401


@pytest.mark.parametrize("transport", ["http", "sse"])
def test_network_startup_requires_authentication(
    monkeypatch: pytest.MonkeyPatch, transport: Literal["http", "sse"]
) -> None:
    monkeypatch.setattr(get_settings(), "transport", transport)
    monkeypatch.setattr(get_settings(), "auth_token", None)
    with pytest.raises(ValueError, match="OZON_AUTH_TOKEN"):
        entrypoint._http_app()


@pytest.mark.parametrize("token", ["", "short", "x" * 31, "x" * 31 + " ", "x" * 31 + "\n", "я" * 32])
def test_invalid_tokens_fail_startup_without_disclosing_the_token(token: str) -> None:
    settings = OzonSettings(auth_token=SecretStr(token))
    with pytest.raises(ValueError, match="OZON_AUTH_TOKEN") as error:
        settings.validate_http_security()
    if token:
        assert token not in str(error.value)
    assert TOKEN not in repr(OzonSettings(auth_token=SecretStr(TOKEN)))


@pytest.mark.parametrize("hosts", [[], ["*"], ["*.example:*"], ["example bad"]])
def test_empty_or_wildcard_hosts_cannot_disable_the_boundary(hosts: list[str]) -> None:
    settings = OzonSettings(auth_token=SecretStr(TOKEN), allowed_hosts=hosts)
    with pytest.raises(ValueError, match="OZON_ALLOWED_HOSTS"):
        settings.validate_http_security()


def test_secure_defaults_and_explicit_remote_configuration() -> None:
    settings = OzonSettings()
    assert settings.host == "127.0.0.1"
    settings = OzonSettings(
        auth_token=SecretStr(TOKEN),
        host="0.0.0.0",  # ruff: ignore[hardcoded-bind-all-interfaces]
        allowed_hosts=["mcp.example:443"],
        allowed_origins=["https://mcp.example"],
    )
    settings.validate_http_security()


def test_http_initialize_reaches_mcp_with_a_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "transport", "http")
    monkeypatch.setattr(settings, "auth_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "allowed_hosts", ["localhost:8084"])
    monkeypatch.setattr(entrypoint, "mcp", FastMCP("initialize-test"))
    with TestClient(entrypoint._http_app(), base_url="http://localhost:8084") as test_client:
        response = test_client.post(
            "/mcp",
            headers={**AUTH, "Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
    assert response.status_code == 200
    assert "initialize-test" in response.text


def test_sse_message_post_is_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "transport", "sse")
    monkeypatch.setattr(settings, "auth_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "allowed_hosts", ["localhost:8084"])
    monkeypatch.setattr(entrypoint, "mcp", FastMCP("sse-test"))
    with TestClient(entrypoint._http_app(), base_url="http://localhost:8084") as test_client:
        assert test_client.post("/messages/?session_id=unknown", json={}).status_code == 401
        response = test_client.post("/messages/?session_id=unknown", headers=AUTH, json={})
    assert response.status_code == 400
    assert "session" in response.text.lower()


def test_stdio_does_not_require_a_network_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "transport", "stdio")
    monkeypatch.setattr(get_settings(), "auth_token", None)
    called = []
    monkeypatch.setattr(entrypoint.mcp, "run", lambda: called.append(True))
    entrypoint.main()
    assert called == [True]


def test_transport_security_stays_enabled_with_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "allowed_hosts", [])
    assert _transport_security().enable_dns_rebinding_protection is True


async def test_sse_initialize_roundtrip_requires_the_token_on_every_request(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "transport", "sse")
    monkeypatch.setattr(settings, "auth_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "allowed_hosts", ["localhost:8084"])
    monkeypatch.setattr(entrypoint, "mcp", FastMCP("sse-connect-test"))
    app = entrypoint._http_app()
    disconnected = anyio.Event()
    connected = anyio.Event()
    initialized = anyio.Event()
    endpoints: list[str] = []
    messages: list[Message] = []

    async def receive() -> Message:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:  # ruff: ignore[unused-async]
        messages.append(message)
        if message["type"] == "http.response.body":
            body = message.get("body", b"")
            if b"event: endpoint" in body:
                endpoints.append(body.split(b"data: ", 1)[1].splitlines()[0].decode())
                connected.set()
            if b'"id":1' in body:
                initialized.set()

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/sse",
        "raw_path": b"/sse",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"localhost:8084"), (b"authorization", f"Bearer {TOKEN}".encode())],
        "server": ("localhost", 8084),
        "client": ("127.0.0.1", 12345),
    }
    with anyio.fail_after(2):
        async with anyio.create_task_group() as group:
            group.start_soon(app, scope, receive, send)
            await connected.wait()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://localhost:8084"
            ) as post_client:
                initialization = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                }
                assert (await post_client.post(endpoints[0], json=initialization)).status_code == 401
                assert (await post_client.post(endpoints[0], headers=AUTH, json=initialization)).status_code == 202
            await initialized.wait()
            disconnected.set()
    assert any(message["type"] == "http.response.start" and message["status"] == 200 for message in messages)
    assert any(
        message["type"] == "http.response.body" and b"/messages/?session_id=" in message.get("body", b"")
        for message in messages
    )
    assert any(
        message["type"] == "http.response.body" and b"sse-connect-test" in message.get("body", b"")
        for message in messages
    )
