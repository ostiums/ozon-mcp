"""DNS rebinding protection remains enabled for every HTTP configuration."""

import pytest

from ozon_mcp.mcp_server import _transport_security
from ozon_mcp.settings import get_settings


def test_default_boundary_accepts_only_loopback_hosts() -> None:
    security = _transport_security()
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert security.allowed_origins == ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def test_explicit_hosts_and_origins_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "allowed_hosts", ["mcp.example:*", "192.168.1.41:*"])
    monkeypatch.setattr(get_settings(), "allowed_origins", ["https://mcp.example"])
    security = _transport_security()
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["mcp.example:*", "192.168.1.41:*"]
    assert security.allowed_origins == ["https://mcp.example"]
