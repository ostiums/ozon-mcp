"""Runtime configuration (pydantic-settings). Everything tunable is here;
fixed values live in ``constants``.
"""

from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class OzonSettings(BaseSettings):
    """Session, transport and feature flags, read from ``OZON_*`` env vars."""

    model_config = SettingsConfigDict(env_prefix="OZON_", extra="ignore")

    profile_dir: Path = Path("/data/profile")
    """Persistent Chromium profile holding the session, including OzonID state
    that a storageState snapshot cannot carry (IndexedDB, device trust)."""

    profile_backup: Path = Path("/data/profile.backup")
    """Copy of a known-good profile, restored if the live one gets logged out."""

    state_path: Path = Path("/data/state.json")
    """Legacy storageState, imported once to seed a brand-new profile."""

    impersonate: str = "chrome124"
    """curl_cffi TLS-impersonation profile for direct HTTP requests."""

    enable_writes: bool = False
    """Allow account-mutating tools (cart / favorites / lists)."""

    enable_orders: bool = False
    """Allow place_order. Separate from enable_writes: this one spends money."""

    monitor_store: Path = Path("/data/price_history.json")
    """Where favorites price-monitoring snapshots are persisted."""

    request_timeout: float = 30.0
    """Seconds to wait for one HTTP call to Ozon."""

    request_attempts: int = 3
    """How many times one call is attempted before it is reported as failed."""

    retry_backoff_seconds: float = 1.0
    """Base wait between attempts; doubled per attempt and jittered."""

    retry_cap_seconds: float = 20.0
    """Longest this server waits before a retry, including a Retry-After Ozon
    asked for — an hour-long Retry-After must not hang a tool call."""

    browser_timeout: float = 60.0
    """Seconds to wait for a browser navigation or a login step."""

    idle_seconds: int = 600
    """Close the idle browser after this long; HTTP session stays alive."""

    transport: Literal["stdio", "http", "sse"] = "stdio"
    """How clients reach the server.

    ``stdio`` for a client that spawns the process. ``http`` serves streamable
    HTTP at /mcp, which is what MCP clients use now. ``sse`` serves the older
    event-stream transport at /sse; it still works and the spec has deprecated
    it, so prefer ``http`` for anything new."""

    host: str = "127.0.0.1"
    """Bind address used by the http and sse transports."""

    port: int = 8084
    """Port serving both the MCP endpoint (/mcp or /sse) and /metrics."""

    auth_token: SecretStr | None = None
    """Bearer token required for HTTP/SSE, including /metrics. Use at least 32
    printable ASCII characters without spaces, generated at random."""

    allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    """Accepted Host headers. Required for HTTP/SSE; DNS rebinding protection
    is always enabled. An explicit hostname with :* accepts any port."""

    allowed_origins: list[str] = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    """Accepted browser Origin headers. Requests without Origin are accepted;
    an empty list rejects every request that supplies an Origin."""

    def validate_http_security(self) -> None:
        """Fail before opening a network listener with missing security settings."""
        token = self.auth_token.get_secret_value() if self.auth_token is not None else ""
        if len(token) < 32 or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError(
                "HTTP/SSE requires OZON_AUTH_TOKEN with at least 32 printable ASCII characters without spaces"
            )
        if not self.allowed_hosts or any(
            not host or "*" in host.removesuffix(":*") or any(char.isspace() for char in host)
            for host in self.allowed_hosts
        ):
            raise ValueError("HTTP/SSE requires a nonempty OZON_ALLOWED_HOSTS list of explicit hosts")
        if any(
            not origin.startswith(("http://", "https://"))
            or "*" in origin.removesuffix(":*")
            or any(char.isspace() for char in origin)
            for origin in self.allowed_origins
        ):
            raise ValueError("OZON_ALLOWED_ORIGINS must contain explicit HTTP(S) origins")


@cache
def get_settings() -> OzonSettings:
    return OzonSettings()
