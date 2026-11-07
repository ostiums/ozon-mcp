"""Runtime configuration (pydantic-settings). Everything tunable is here;
fixed values live in ``constants``.
"""

from functools import cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ozon_mcp.session.proxy import validate_proxy_url


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

    browser_channel: Literal["chrome"] | None = None
    """Optional installed stable Chrome channel. Unset keeps Playwright's Chromium."""

    storefront: Literal["composer", "public"] = "composer"
    """Search and product cards. ``public`` reads the open browser page and does not call the composer API."""

    proxy_url: SecretStr | None = None
    """Explicit proxy for the browser and the HTTP client. Unset means a direct connection."""

    proxy_remote_dns: bool = False
    """When set with proxy_url, resolve destinations at the proxy and exclude only that host."""

    impersonate: str = "chrome124"
    """curl_cffi TLS-impersonation profile for direct HTTP requests."""

    enable_cart: bool = False
    """Allow cart quantity and selection changes without other account writes."""

    enable_writes: bool = False
    """Allow broad account writes, including cart, checkout options and payments."""

    enable_orders: bool = False
    """Allow place_order. Separate from enable_writes: this one spends money."""

    @property
    def cart_enabled(self) -> bool:
        """Effective cart permission, preserving the existing broad write flag."""
        return self.enable_cart or self.enable_writes

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

    public_read_timeout: float = Field(default=20.0, gt=0)
    """Shared seconds for one public page: navigation, tile readiness, and the snapshot.

    The steps share this clock. They do not each get ``browser_timeout``.
    Keep it shorter than the MCP client's request deadline so a diagnostic
    error can be returned. Live client deadlines were not measured.
    """

    shutdown_timeout: float = Field(default=5.0, gt=0)
    """Seconds to wait for the session thread to close. A timeout does not delete profile locks."""

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

    @field_validator("proxy_url", mode="before")
    @classmethod
    def _blank_proxy_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_proxy(self) -> Self:
        if self.proxy_url is not None:
            validate_proxy_url(self.proxy_url.get_secret_value())
        if self.proxy_remote_dns and self.proxy_url is None:
            message = "OZON_PROXY_REMOTE_DNS requires OZON_PROXY_URL"
            raise ValueError(message)
        return self


@cache
def get_settings() -> OzonSettings:
    return OzonSettings()
