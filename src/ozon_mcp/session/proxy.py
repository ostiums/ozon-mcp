"""Explicit browser and HTTP proxy settings.

The package never inherits an ambient proxy and never embeds a deployment
endpoint. Callers pass ``OZON_PROXY_URL`` when they want one.
"""

from dataclasses import dataclass
from typing import Final
from urllib.parse import unquote, urlsplit

_SCHEMES: Final = frozenset({"http", "https", "socks5"})
_STORE_SUFFIX: Final = ".ozon.ru"


@dataclass(frozen=True)
class HttpProxyChoice:
    """What the HTTP client should use instead of the process environment."""

    proxy: str | None
    proxy_auth: tuple[str, str] | None
    disable_env_proxy: bool


def validate_proxy_url(value: str) -> str:
    """Reject a proxy URL that is not a single explicit endpoint."""
    if any(char.isspace() for char in value) or "," in value:
        message = "OZON_PROXY_URL must be one proxy endpoint without spaces or commas"
        raise ValueError(message)
    parts = urlsplit(value)
    host = parts.hostname
    if parts.scheme not in _SCHEMES or not host or parts.port is None or parts.query or parts.fragment:
        message = "OZON_PROXY_URL must be http, https or socks5 with an explicit host and port"
        raise ValueError(message)
    lowered = host.lower()
    if lowered == "ozon.ru" or lowered.endswith(_STORE_SUFFIX):
        message = "OZON_PROXY_URL must name the proxy, not an Ozon host"
        raise ValueError(message)
    return value


def http_proxy_choice(proxy_url: str | None) -> HttpProxyChoice:
    """Use the configured proxy, or disable ambient proxy inheritance."""
    if proxy_url is None:
        return HttpProxyChoice(proxy=None, proxy_auth=None, disable_env_proxy=True)
    server, user, password = _endpoint(proxy_url)
    auth = (user or "", password or "") if user is not None or password is not None else None
    return HttpProxyChoice(proxy=server, proxy_auth=auth, disable_env_proxy=False)


def playwright_proxy(proxy_url: str | None) -> dict[str, str] | None:
    """Playwright proxy settings. Credentials stay out of the server URL."""
    if proxy_url is None:
        return None
    server, user, password = _endpoint(proxy_url)
    proxy = {"server": server}
    if user is not None:
        proxy["username"] = user
    if password is not None:
        proxy["password"] = password
    return proxy


def browser_network_args(*, proxy_url: str | None, remote_dns: bool) -> list[str]:
    """Chromium resolver rules for one configured proxy, or nothing.

    ``MAP * ~NOTFOUND`` stops local destination lookups. ``EXCLUDE`` names only
    the proxy host so that host can still be reached. It is not a list of
    destinations to connect to directly.
    """
    if not remote_dns:
        return []
    if proxy_url is None:
        message = "OZON_PROXY_REMOTE_DNS requires OZON_PROXY_URL"
        raise ValueError(message)
    host = urlsplit(proxy_url).hostname or ""
    if not host or any(char in host for char in " ,\"'"):
        message = "proxy host cannot be excluded safely"
        raise ValueError(message)
    return [
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        f"--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE {host}",
    ]


def _endpoint(proxy_url: str) -> tuple[str, str | None, str | None]:
    parts = urlsplit(proxy_url)
    host = parts.hostname or ""
    server_host = f"[{host}]" if ":" in host else host
    user = unquote(parts.username) if parts.username is not None else None
    password = unquote(parts.password) if parts.password is not None else None
    return f"{parts.scheme}://{server_host}:{parts.port}", user, password
