"""Proxy and browser channel stay configurable. The deployment endpoint is not a default."""

from __future__ import annotations

from pathlib import Path

import pytest
from curl_cffi import Curl, CurlOpt
from curl_cffi.requests import Session
from curl_cffi.requests.utils import set_curl_options
from pydantic import ValidationError

from ozon_mcp.constants import LAUNCH_ARGS
from ozon_mcp.session.proxy import browser_network_args, http_proxy_choice, playwright_proxy
from ozon_mcp.session.transport import OzonSession
from ozon_mcp.settings import OzonSettings

PROXY = "http://proxy.example:9"
AUTH = "pw-example"


def test_package_source_has_no_deployment_proxy_literal() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in Path("src/ozon_mcp").rglob("*.py"))
    assert "11084" not in text
    assert "1831385" not in text
    assert "1831401" not in text


def test_channel_defaults_to_playwright_chromium(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OZON_BROWSER_CHANNEL", raising=False)
    assert OzonSettings().browser_channel is None
    assert OzonSettings(browser_channel="chrome").browser_channel == "chrome"
    for bad in ("chromium", "chrome-beta", "arbitrary-executable", ""):
        with pytest.raises(ValidationError):
            OzonSettings(browser_channel=bad)


def test_proxy_defaults_off_and_rejects_storefront_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OZON_PROXY_URL", raising=False)
    monkeypatch.delenv("OZON_PROXY_REMOTE_DNS", raising=False)
    assert OzonSettings().proxy_url is None
    assert OzonSettings(proxy_url="").proxy_url is None
    with pytest.raises(ValidationError):
        OzonSettings(proxy_remote_dns=True)
    for bad in ("http://www.ozon.ru:8080", "http://proxy.example", "socks5://proxy.example:9?x=1", "http://a,b:9"):
        with pytest.raises(ValidationError):
            OzonSettings(proxy_url=bad)


def test_remote_dns_excludes_only_the_proxy_host() -> None:
    assert browser_network_args(proxy_url=None, remote_dns=False) == []
    assert browser_network_args(proxy_url=PROXY, remote_dns=False) == []
    args = browser_network_args(proxy_url=PROXY, remote_dns=True)
    assert args[:2] == ["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    assert args[2] == "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE proxy.example"
    assert "www.ozon.ru" not in args[2]
    assert "automationcontrolled" not in " ".join(args).lower()
    joined = [*LAUNCH_ARGS, *args]
    assert joined[: len(LAUNCH_ARGS)] == list(LAUNCH_ARGS)


def test_playwright_proxy_keeps_the_password_out_of_the_server() -> None:
    proxy = playwright_proxy(f"http://user:{AUTH}@proxy.example:9")
    assert proxy is not None
    assert proxy["server"] == PROXY
    assert proxy["username"] == "user"
    assert proxy["password"] == AUTH
    assert AUTH not in proxy["server"]
    assert playwright_proxy(None) is None


def test_ipv6_proxy_keeps_brackets_on_the_server() -> None:
    proxy = playwright_proxy("http://[2001:db8::1]:8080")
    assert proxy == {"server": "http://[2001:db8::1]:8080"}


def test_http_choice_disables_env_proxy_when_unset() -> None:
    choice = http_proxy_choice(None)
    assert choice.disable_env_proxy is True
    assert choice.proxy is None
    chosen = http_proxy_choice(f"http://user:{AUTH}@proxy.example:9")
    assert chosen.proxy == PROXY
    assert chosen.proxy_auth == ("user", AUTH)
    assert chosen.disable_env_proxy is False


def test_session_kwargs_match_the_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OZON_PROXY_URL", raising=False)
    configured = OzonSettings(proxy_url=PROXY)
    monkeypatch.setattr("ozon_mcp.session.transport.get_settings", lambda: configured)
    kwargs = OzonSession()._http_client_kwargs()
    assert kwargs["trust_env"] is False
    assert kwargs["proxy"] == PROXY
    assert not kwargs["curl_options"][CurlOpt.NOPROXY]
    direct = OzonSettings()
    monkeypatch.setattr("ozon_mcp.session.transport.get_settings", lambda: direct)
    bare = OzonSession()._http_client_kwargs()
    assert bare["trust_env"] is False
    assert "proxy" not in bare
    assert not bare["curl_options"][CurlOpt.PROXY]


def _recorded(url: str, proxies: dict[str, str]) -> dict[int, object]:
    curl = Curl()
    seen: dict[int, object] = {}
    original = curl.setopt

    def wrapped(option: object, value: object) -> object:
        seen[int(option)] = value  # type: ignore[arg-type]
        return original(option, value)

    curl.setopt = wrapped  # type: ignore[method-assign]
    set_curl_options(
        curl,
        "GET",
        url,
        params_list=[None, None],
        headers_list=[None, None],
        cookies_list=[None, None],
        proxies_list=[proxies, None],
        verify_list=[True, None],
        impersonate="chrome124",
        curl_options={CurlOpt.NOPROXY: ""},
        timeout=1,
        allow_redirects=False,
    )
    return seen


def test_installed_library_uses_explicit_proxy_for_https_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "www.ozon.ru,ozon.ru")
    monkeypatch.setenv("no_proxy", "www.ozon.ru")
    monkeypatch.setenv("https_proxy", "http://env-proxy.example:1")
    session = Session(proxy=PROXY, trust_env=False, impersonate="chrome124")
    assert session.proxies == {"all": PROXY}
    assert session.trust_env is False
    https = _recorded("https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/search/", session.proxies)
    assert https[int(CurlOpt.PROXY)] == PROXY
    assert https[int(CurlOpt.HTTPPROXYTUNNEL)] == 1
    assert not https[int(CurlOpt.NOPROXY)]
    http = _recorded("http://www.ozon.ru/search/", session.proxies)
    assert http[int(CurlOpt.PROXY)] == PROXY
    assert int(CurlOpt.HTTPPROXYTUNNEL) not in http
