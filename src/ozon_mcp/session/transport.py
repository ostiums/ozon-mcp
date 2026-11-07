"""Authenticated OZON session — persistent browser profile, HTTP-served.

OZON's Variti antibot blocks non-browser clients, but a real Chromium only has
to pass the challenge **once**: harvest its cookies plus the exact client-hint
header set, and thereafter direct HTTP via curl_cffi (Chrome TLS impersonation)
reaches the authenticated composer-api / _action endpoints without a live
browser per call.

The browser runs on a **persistent profile** rather than a Playwright
``storage_state`` snapshot. storage_state only carries cookies and
localStorage; OzonID — the auth realm guarding checkout — keeps its session and
device trust outside both, so a snapshot silently drops it and checkout falls
back to a login prompt. A real profile directory keeps everything (IndexedDB,
service workers, device fingerprint), so one interactive login stays valid.

That makes the profile the single source of truth for the session, which is why
cookies rotated on the HTTP side are pushed back into the live context: Chrome
then flushes them to disk, and the refresh chain survives a restart.
"""

import logging
import math
import re
import secrets
import shutil
import stat
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Final, Self
from urllib.parse import quote, urljoin, urlsplit

from curl_cffi import CurlOpt, requests as curl_requests
from playwright.sync_api import sync_playwright

from ozon_mcp.constants import (
    ACTION_URL,
    COMPOSER_URL,
    ENTRYPOINT_URL,
    HARVEST_HEADERS,
    HOME_URL,
    LAUNCH_ARGS,
    WIDGET_URL,
)
from ozon_mcp.errors import OzonError, RateLimitedError, SessionExpiredError, UpstreamError
from ozon_mcp.parsing.public_storefront import (
    PUBLIC_PAGE_READER,
    format_public_diagnostics,
    snapshot_counts,
    url_class,
)
from ozon_mcp.parsing.visible_cards import EMPTY_RESULT_PHRASES, PUBLIC_TILE_SELECTOR
from ozon_mcp.session.page_health import classify_page_title, health_message
from ozon_mcp.session.phone_form import (
    has_separate_russian_prefix,
    login_code_channel,
    normalize_phone_input,
    raise_for_phone_format_error,
    select_phone_input,
)
from ozon_mcp.session.profile_lock import clear_dead_profile_lock
from ozon_mcp.session.proxy import browser_network_args, http_proxy_choice, playwright_proxy
from ozon_mcp.settings import get_settings
from ozon_mcp.utils.observability import BROWSER_ACTIVE, SESSION_BOOTSTRAPS, UPSTREAM_LATENCY, UPSTREAM_REQUESTS
from ozon_mcp.utils.serde import dumps, loads

logger = logging.getLogger("ozon_mcp")

# Cookies that carry the login; only these are worth syncing back to the browser.
_SESSION_COOKIES: Final[frozenset[str]] = frozenset({
    "__Secure-access-token",
    "__Secure-refresh-token",
    "__Secure-user-id",
    "ozonIdAuthResponseToken",
})

Backend = str  # "composer" | "entrypoint" | "action"

# OZON hands out tokens shaped "<ver>.<userId>.<secret>"; userId 0 means guest.
_GUEST_USER_ID: Final = "0"
_TOO_MANY_REQUESTS: Final = 429
_CLIENT_ERROR: Final = 400
_SERVER_ERROR: Final = 500
# Ozon serves each login step from its own "-lite" iframe: the email or phone
# form from /ozonid-lite, the one-time code from /otp-lite — so the frame's URL
# is what tells the two steps apart.
_OTP_FRAME: Final = "otp-lite"
_AUTH_FRAME_PATHS: Final = frozenset({"/ozonid-lite", "/otp-lite"})


def _is_auth_frame(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc == "www.ozon.ru" and parsed.path in _AUTH_FRAME_PATHS


def _validate_api_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "www.ozon.ru" or not parsed.path.startswith("/api/"):
        raise OzonError("refusing a request outside the fixed HTTPS Ozon API")


def _outcome(status: int) -> str:
    """Bucket the status so the metric label stays low-cardinality."""
    if status == 0:
        return "error"
    if 200 <= status < 300:
        return "ok"
    return f"{status // 100}xx"


def _reject_symlink_ancestors(path: Path) -> None:
    """Validate configured storage before mkdir, chmod or recursive removal."""
    for entry in (path.absolute(), *path.absolute().parents):
        if entry.is_symlink():
            raise OSError(f"session storage must not be a symbolic link: {entry}")


def _make_private(path: Path, *, create: bool = False) -> None:
    """Restrict stored session data without traversing symbolic links.

    Chromium's Singleton files can be links outside the profile. Keep those
    links intact and never change the permissions of their targets.
    """
    _reject_symlink_ancestors(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode):
        raise OSError(f"session storage must not be a symbolic link: {path}")
    if stat.S_ISREG(mode):
        path.chmod(0o600, follow_symlinks=False)
    elif stat.S_ISDIR(mode):
        path.chmod(0o700, follow_symlinks=False)
        for parent, directories, files in path.walk(follow_symlinks=False):
            for name in directories:
                (parent / name).chmod(0o700, follow_symlinks=False)
            for name in files:
                entry = parent / name
                if stat.S_ISREG(entry.lstat().st_mode):
                    entry.chmod(0o600, follow_symlinks=False)


def _copy_private_profile(source: Path, destination: Path) -> None:
    """Keep the destination private while the copy is being written, too."""
    _reject_symlink_ancestors(source)
    _reject_symlink_ancestors(destination)
    source_path, destination_path = source.resolve(), destination.resolve()
    if source_path.is_relative_to(destination_path) or destination_path.is_relative_to(source_path):
        raise OSError("session profile and backup paths must not overlap")
    _make_private(source)
    if destination.exists():
        shutil.rmtree(destination)
    _make_private(destination, create=True)
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("Singleton*"),
    )
    _make_private(destination)


def _redirect_detail(location: object, request_url: str) -> str:
    """Return fixed labels only. Destination text is never included."""
    prefix = "Redirect destination: "
    if location is None or (isinstance(location, str) and not location):
        return prefix + "host=missing; path=unknown."
    if not isinstance(location, str):
        return prefix + "host=malformed; path=unknown."
    return prefix + _redirect_classes(location, request_url)


def _redirect_classes(location: str, request_url: str) -> str:
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in location) or "\\" in location:
        return "host=malformed; path=unknown."
    try:
        target = urlsplit(urljoin(request_url, location))
        host = target.hostname
        port = target.port
    except (ValueError, UnicodeError):
        return "host=malformed; path=unknown."
    if port is not None and port <= 0:
        return "host=malformed; path=unknown."
    if target.scheme not in {"https", "http"} or not host or target.username is not None or target.password is not None:
        return "host=malformed; path=unknown."
    if host == "www.ozon.ru":
        host_class = "same-canonical-ozon"
    elif host == "ozon.ru" or host.endswith(".ozon.ru"):
        host_class = "other-ozon"
    else:
        host_class = "external"
    path = target.path
    if path in {"/", "/search/"}:
        path_class = path
    elif path == "/api" or path.startswith("/api/"):
        path_class = "api-path"
    else:
        path_class = "other-path"
    return f"host={host_class}; path={path_class}."


def _page_is_closed(page: Any) -> bool:
    closed = getattr(page, "is_closed", None)
    if closed is None:
        return False
    try:
        return bool(closed())
    except Exception:
        return False


def _bring_owned_page_forward(page: Any) -> None:
    """Show the page we own. Other tabs are left open and are not read."""
    bring = getattr(page, "bring_to_front", None)
    if bring is None:
        return
    try:
        bring()
    except Exception:
        return


def _public_tile_ready(page: Any, timeout_ms: int) -> bool:
    """True when the inspected tile is visible before the budget ends."""
    try:
        page.locator(PUBLIC_TILE_SELECTOR).first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        return False
    return True


def _phrase_visible(page: Any, phrase: str) -> bool:
    getter = getattr(page, "get_by_text", None)
    if getter is None:
        return False
    try:
        located = getter(phrase, exact=False)
    except Exception:
        return False
    try:
        return bool(located.count() and located.first.is_visible())
    except Exception:
        return False


def _empty_phrase_visible(page: Any) -> bool:
    return any(_phrase_visible(page, phrase) for phrase in EMPTY_RESULT_PHRASES)


def _status(page: Any, *, ready: bool, data: dict[str, Any]) -> dict[str, Any]:
    title = data.get("title")
    if not isinstance(title, str):
        try:
            title = page.title()
        except Exception:
            title = ""
    return {
        "title": title if isinstance(title, str) else "",
        "url_class": url_class(getattr(page, "url", "")),
        "tile_selector_ready": ready,
        "counts": snapshot_counts(data.get("html")),
    }


def _public_catalog_path(path: str) -> str:
    """Allow only a same-site search, category or product path."""
    if not path.startswith("/") or path.startswith("//") or "\\" in path or any(ord(char) < 32 for char in path):
        message = "public storefront reads only an on-site catalog path"
        raise OzonError(message)
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc:
        message = "public storefront reads only an on-site catalog path"
        raise OzonError(message)
    route = parsed.path
    if route == "/search" or route.startswith(("/search/", "/category/", "/product/")):
        return path
    message = "public storefront reads only /search, /category and /product"
    raise OzonError(message)


def _response_redirect_detail(response: Any, status: int, url: str) -> str:
    if response is None or not 300 <= status < _CLIENT_ERROR:
        return ""
    location = None
    with suppress(Exception):
        location = response.headers.get("Location")
    return _redirect_detail(location, url)


class OzonSession:
    """Persistent-profile browser plus a curl_cffi transport (thread-safe)."""

    def __init__(self, profile_dir: Path | None = None) -> None:
        settings = get_settings()
        self._profile = profile_dir or settings.profile_dir
        self._backup = settings.profile_backup
        self._seed_state = settings.state_path
        self._impersonate = settings.impersonate
        self._lock = threading.RLock()
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None
        self._http: Any = None
        self._headers: dict[str, str] = {}
        self._synced: dict[str, str] = {}
        self._logging_in = False
        self._last_used = 0.0

    # -- browser lifecycle ---------------------------------------------------
    def _launch_browser(self) -> None:
        if self._page is not None:
            self._raise_for_antibot_challenge()
            return
        fresh = not (self._profile / "Default").exists()
        _make_private(self._profile, create=True)
        _make_private(self._backup)
        _make_private(self._seed_state)
        self._clear_stale_lock()
        settings = get_settings()
        proxy_url = settings.proxy_url.get_secret_value() if settings.proxy_url is not None else None
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(self._profile),
            "headless": False,
            "args": [*LAUNCH_ARGS, *browser_network_args(proxy_url=proxy_url, remote_dns=settings.proxy_remote_dns)],
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
            "viewport": {"width": 1366, "height": 900},
            # Keep SIGTERM for this process. Playwright's default handler would replace it.
            "handle_sigterm": False,
        }
        proxy = playwright_proxy(proxy_url)
        if proxy is not None:
            launch_kwargs["proxy"] = proxy
        if settings.browser_channel is not None:
            launch_kwargs["channel"] = settings.browser_channel
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        if fresh:
            self._seed_from_state()
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        BROWSER_ACTIVE.set(1)
        self._page.goto(HOME_URL, wait_until="domcontentloaded", timeout=self._browser_ms(1.5))
        self._page.wait_for_timeout(12_000)  # let Variti set cookies
        self._raise_for_antibot_challenge()

    def _raise_for_antibot_challenge(self) -> None:
        """Stop on an unhealthy title. A network title is not a CAPTCHA."""
        message = health_message(classify_page_title(self._page.title() or ""))
        if message is None:
            return
        self._logging_in = False
        logger.warning("%s", message)
        raise RuntimeError(message)

    def back_up_profile(self) -> None:
        """Snapshot a profile that is known to be signed in.

        Some Ozon pages sign the session out — the payment-confirmation page
        does — and with a persistent profile Chrome writes that logged-out state
        straight to disk, where no HTTP-side guard can intercept it. Without a
        copy, recovering costs another one-time code from the account owner.

        The browser is closed first, and that is the whole point: Chromium keeps
        its cookie jar in memory and writes it out when it exits, so copying the
        directory while it runs snapshots the session as it was *before* the
        login — which is how a fresh sign-in ended up backed up as a guest one.
        """
        self._close_browser()
        if not self._profile.exists():
            return
        try:
            _copy_private_profile(self._profile, self._backup)
            logger.info("backed up the signed-in profile to %s", self._backup)
        except OSError:
            logger.warning("could not back up the profile", exc_info=True)

    def restore_profile(self) -> bool:
        """Put the last signed-in profile back, if one was kept."""
        try:
            _make_private(self._backup)
            if not (self._backup / "Default").exists():
                return False
            self._close_browser()
            _copy_private_profile(self._backup, self._profile)
        except OSError:
            logger.warning("could not restore the profile", exc_info=True)
            return False
        logger.info("restored the signed-in profile from %s", self._backup)
        return True

    def _clear_stale_lock(self) -> None:
        """Drop a lock whose owner is dead. A live or foreign owner blocks startup."""
        for name in clear_dead_profile_lock(self._profile):
            logger.info("removed stale profile lock %s", name)

    def _seed_from_state(self) -> None:
        """Import a legacy ``state.json`` into a brand-new profile.

        Enough to keep the read tools working without a fresh login; it cannot
        restore OzonID, so checkout still needs one interactive sign-in.
        """
        try:
            _make_private(self._seed_state)
            saved = loads(self._seed_state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.info("no session to seed at %s; interactive login required", self._seed_state)
            return
        cookies = [c for c in saved.get("cookies") or [] if c.get("name") and c.get("domain")]
        by_name = {c["name"]: c.get("value") for c in cookies}
        if by_name.get("__Secure-user-id") == _GUEST_USER_ID:
            logger.warning("seed session at %s is anonymous; interactive login required", self._seed_state)
            return
        if cookies:
            self._context.add_cookies(cookies)
            logger.info("seeded %d cookies from %s into a fresh profile", len(cookies), self._seed_state)

    def _close_browser(self) -> None:
        for obj, method in ((self._context, "close"), (self._playwright, "stop")):
            try:
                if obj is not None:
                    getattr(obj, method)()
            except Exception:
                logger.debug("browser teardown error", exc_info=True)
        self._playwright = self._context = self._page = None
        BROWSER_ACTIVE.set(0)
        _make_private(self._profile)

    def _ensure_browser(self) -> None:
        if self._page is None:
            self._launch_browser()

    # -- bootstrap: harvest cookies + headers, build the HTTP session --------
    def _bootstrap(self, reason: str = "initial") -> None:
        SESSION_BOOTSTRAPS.labels(reason=reason).inc()
        self._launch_browser()
        if not self._browser_is_signed_in():
            # A page signed the profile out; put the kept copy back rather than
            # asking the account owner for another code.
            if self.restore_profile():
                SESSION_BOOTSTRAPS.labels(reason="restored").inc()
                self._launch_browser()
            if not self._browser_is_signed_in() and not self._logging_in:
                # Answering anyway would look like an empty account.
                raise SessionExpiredError
        captured: dict[str, str] = {}

        def on_request(request: Any) -> None:
            if "composer-api.bx/page/json/v2" in request.url and not captured:
                captured.update(request.headers)

        self._page.on("request", on_request)
        self._page.evaluate(
            "async () => { await fetch('/api/composer-api.bx/page/json/v2?url=/my/orderlist',"
            " {headers: {accept: 'application/json'}, credentials: 'include'}); }"
        )
        self._page.wait_for_timeout(500)
        self._headers = {k: v for k, v in captured.items() if k.lower() in HARVEST_HEADERS}
        self._http = curl_requests.Session(**self._http_client_kwargs())
        for cookie in self._context.cookies():
            self._http.cookies.set(cookie["name"], cookie["value"], domain=".ozon.ru")
        self._synced = {c["name"]: c["value"] for c in self._context.cookies() if c["name"] in _SESSION_COOKIES}
        self._last_used = time.time()

    def _http_client_kwargs(self) -> dict[str, Any]:
        """Explicit proxy, or an empty proxy so libcurl ignores the environment.

        curl_cffi stores ``trust_env`` but does not apply it. An explicit proxy
        is used for every scheme, and HTTPS uses CONNECT. ``NO_PROXY`` does not
        replace that proxy. With no configured proxy, an empty proxy string
        stops libcurl from reading ``http_proxy``.
        """
        settings = get_settings()
        proxy_url = settings.proxy_url.get_secret_value() if settings.proxy_url is not None else None
        choice = http_proxy_choice(proxy_url)
        kwargs: dict[str, Any] = {"impersonate": self._impersonate, "trust_env": False}
        if choice.disable_env_proxy:
            kwargs["curl_options"] = {CurlOpt.PROXY: ""}
            return kwargs
        kwargs["proxy"] = choice.proxy
        if choice.proxy_auth is not None:
            kwargs["proxy_auth"] = choice.proxy_auth
        return kwargs

    def _browser_is_signed_in(self) -> bool:
        """Whether the profile still holds a session, judged by its own cookie."""
        try:
            user = next((c for c in self._context.cookies() if c["name"] == "__Secure-user-id"), None)
        except Exception:
            return False
        return bool(user and user.get("value") not in {"", _GUEST_USER_ID})

    def _ensure_http(self) -> None:
        # The browser is deliberately kept alive: it owns the profile, so closing
        # it would leave HTTP-side token rotations with nowhere to be persisted.
        if self._http is None or not self._headers:
            self._bootstrap()

    def _rebootstrap(self) -> None:
        self._http = None
        self._close_browser()
        self._bootstrap(reason="rechallenge")

    # -- persistence ---------------------------------------------------------
    def save_state(self) -> None:
        """Push HTTP-rotated session cookies back into the browser profile."""
        with self._lock:
            if self._context is None or self._http is None:
                return
            jar = {c.name: c.value for c in self._http.cookies.jar}
            # Visiting the checkout login flow downgrades the session to a guest
            # one. Persisting that would overwrite a working login with an
            # anonymous token and silently lock the account out.
            if jar.get("__Secure-user-id") == _GUEST_USER_ID:
                logger.warning("session went anonymous upstream; refusing to persist it")
                return
            rotated = [
                {"name": name, "value": value, "domain": ".ozon.ru", "path": "/"}
                for name, value in jar.items()
                if name in _SESSION_COOKIES and self._synced.get(name) != value
            ]
            if not rotated:
                return
            try:
                self._context.add_cookies(rotated)
                self._synced.update({c["name"]: str(c["value"]) for c in rotated})
                logger.info("synced %d rotated cookie(s) into the profile", len(rotated))
            except Exception:
                logger.debug("cookie sync error", exc_info=True)

    def close(self) -> None:
        with self._lock:
            self.save_state()
            self._close_browser()
            self._http = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- HTTP transport ------------------------------------------------------
    @staticmethod
    def _blocked(status: int, text: str) -> bool:
        return status in {403, 307} or "incidentId" in text or "abt-challenge" in text

    @staticmethod
    def _browser_ms(share: float = 1.0) -> int:
        """A Playwright timeout in milliseconds, from the configured budget.

        Playwright counts in milliseconds and the setting is in seconds; the
        share scales one wait against the others, so tuning one number moves
        them all together instead of leaving eight literals to chase.
        """
        return int(get_settings().browser_timeout * share * 1000)

    def _sleep_before_retry(self, attempt: int, retry_after: float | None) -> None:
        """Wait before the next attempt: what Ozon asked for, or a backoff.

        The wait is capped and jittered — capped because a Retry-After of an
        hour must not hang a tool call, jittered because several calls that
        failed together would otherwise come back in lockstep.
        """
        settings = get_settings()
        base = retry_after if retry_after is not None else settings.retry_backoff_seconds * (2 ** (attempt - 1))
        delay = min(base, settings.retry_cap_seconds)
        # secrets, not random: the module is used for nothing else here, and one
        # source of randomness is one less thing to get wrong.
        time.sleep(delay * (0.5 + secrets.randbelow(500) / 1000))

    @staticmethod
    def _retry_after(response: Any) -> float | None:
        """The wait Ozon asked for, if it asked in seconds."""
        header = ""
        with suppress(Exception):
            header = str((response.headers or {}).get("Retry-After") or "")
        try:
            seconds = float(header.strip())
        except ValueError:
            return None
        return seconds if math.isfinite(seconds) and seconds >= 0 else None

    def _request(self, method: str, url: str, body: object = None, backend: Backend = "composer") -> dict[str, Any]:
        """One call to Ozon, retried, and never answered with an empty page.

        A failure used to come back as ``{"widgetStates": {}}``, which every
        parser turns into an empty list — so a 502, a timeout or a rate limit
        read exactly like an account with no orders. Anything that is not a page
        now raises instead.

        A command's 4xx carrying JSON is passed through on purpose: Ozon
        reports refused actions that way, and the caller needs the reason.
        Reads always reject error statuses, including JSON error pages.
        """
        _validate_api_url(url)
        settings = get_settings()
        # A timeout or a 5xx can arrive after the action has already committed.
        # Without an upstream idempotency key, replaying it can order twice.
        attempts = max(1, settings.request_attempts) if method.upper() in {"GET", "HEAD"} else 1
        with self._lock:
            if getattr(self, "_page", None) is not None and getattr(self, "_context", None) is not None:
                self._reconcile_completed_manual_login()
            if self._logging_in:
                raise OzonError("login is in progress; finish submit_login_code() before reading account data")
            status, text, retry_after = 0, "", None
            started = time.monotonic()
            response: Any = None
            redirect_detail = ""
            for attempt in range(1, attempts + 1):
                redirect_detail = ""
                self._ensure_http()
                headers = dict(self._headers)
                headers["accept"] = "application/json"
                if body is not None:
                    headers["content-type"] = "application/json"
                try:
                    response = self._http.request(
                        method,
                        url,
                        headers=headers,
                        timeout=settings.request_timeout,
                        allow_redirects=False,
                        data=dumps(body) if body is not None else None,
                    )
                    status, text = response.status_code, response.text
                    retry_after = self._retry_after(response) if status == _TOO_MANY_REQUESTS else None
                # A transport failure carries no status; it is retried like a 5xx.
                except Exception:
                    response = None
                    status, text, retry_after = 0, "", None
                    logger.debug("upstream transport error (%s)", backend)

                redirect_detail = _response_redirect_detail(response, status, url)
                action = self._retry_action(status, text, attempt, attempts, backend)
                if action == "rebootstrap":
                    self._rebootstrap()
                    continue
                if action == "sleep":
                    self._sleep_before_retry(attempt, retry_after)
                    continue
                if action == "page":
                    return self._page_from(
                        text,
                        status,
                        backend,
                        started,
                        allow_client_error=method.upper() != "GET",
                        redirect_detail=redirect_detail,
                    )
            UPSTREAM_LATENCY.labels(backend=backend).observe(time.monotonic() - started)
            UPSTREAM_REQUESTS.labels(backend=backend, outcome="failed").inc()
            if status == _TOO_MANY_REQUESTS:
                raise RateLimitedError(retry_after)
            raise UpstreamError(status, redirect_detail)

    def _retry_action(self, status: int, text: str, attempt: int, attempts: int, backend: Backend) -> str:
        if self._blocked(status, text):
            UPSTREAM_REQUESTS.labels(backend=backend, outcome="rechallenge").inc()
            return "rebootstrap" if attempt < attempts else "stop"
        if status == _TOO_MANY_REQUESTS or status >= _SERVER_ERROR or status == 0:
            UPSTREAM_REQUESTS.labels(backend=backend, outcome=_outcome(status)).inc()
            return "sleep" if attempt < attempts else "stop"
        return "page"

    def _reconcile_completed_manual_login(self) -> None:
        """Clear login-in-progress only after a signed-in browser with no auth frame.

        A changed title or a finished wait is not enough. The code form and a
        signed-out profile leave the flag set.
        """
        if not self._logging_in or self._page is None or self._context is None:
            return
        try:
            frames = list(self._page.frames)
        except Exception:
            return
        if any(_is_auth_frame(getattr(frame, "url", "") or "") for frame in frames):
            return
        if not self._browser_is_signed_in():
            return
        self._logging_in = False

    def _page_from(
        self,
        text: str,
        status: int,
        backend: Backend,
        started: float,
        *,
        allow_client_error: bool = False,
        redirect_detail: str = "",
    ) -> dict[str, Any]:
        """Turn a served response into a page, or refuse to call it one.

        Error pages cannot stand in for an empty account. JSON refusals are
        retained only for commands, where callers inspect their error fields.
        """
        UPSTREAM_LATENCY.labels(backend=backend).observe(time.monotonic() - started)
        UPSTREAM_REQUESTS.labels(backend=backend, outcome=_outcome(status)).inc()
        if status == 401:
            raise SessionExpiredError
        if status >= _CLIENT_ERROR and not allow_client_error:
            raise UpstreamError(status)
        self._last_used = time.time()
        self.save_state()
        try:
            data = loads(text)
        except ValueError:
            data = None
        if 300 <= status < _CLIENT_ERROR or not isinstance(data, dict):
            raise UpstreamError(status, redirect_detail)
        data.setdefault("_httpStatus", status)
        return data

    def fetch(self, path: str, backend: Backend = "composer") -> dict[str, Any]:
        """Fetch a page's JSON by on-site path. backend: composer | entrypoint.

        ``%`` is left alone, so a caller that had to escape a value — a search
        phrase, which may hold ``&`` — hands over an already-encoded path and
        gets it through intact. Escaping it again turned "тунец" into
        ``%25D1%2582…``, and Ozon answered that nonsense query with whatever it
        felt like: car accessories, for canned tuna. ``?``, ``&`` and ``=`` stay
        raw so the page query remains the page query. A redirect is refused.
        That refusal is why a live search 302 is ``upstream_unavailable``; the
        server reason for that 302 was not established.
        """
        base = ENTRYPOINT_URL if backend == "entrypoint" else COMPOSER_URL
        return self._request("GET", base + quote(path, safe="/?=&%"), backend=backend)

    def post_page(self, path: str, body: object, backend: Backend = "composer") -> dict[str, Any]:
        """POST a page-level command and get the refreshed page back.

        Some controls are not ``_action`` endpoints: the site posts to the
        page's own JSON URL and re-renders from the response. Which backend
        serves it depends on the page — the cart posts to composer, the checkout
        to entrypoint — and posting to the wrong one silently changes nothing.
        """
        base = ENTRYPOINT_URL if backend == "entrypoint" else COMPOSER_URL
        # The inner path is a *value* of the endpoint's url= parameter, so its
        # own query has to be escaped: left raw, its parameters detach and act
        # on the endpoint instead, and the command silently does nothing.
        return self._request("POST", base + quote(path, safe="/"), body=body, backend=backend)

    def widget_state(self, state_id: str, async_data: str) -> dict[str, Any]:
        """Fill one lazily-loaded widget by naming its state.

        The page ships such widgets empty; the site then posts a base64
        descriptor of the component to this endpoint to get the real content.
        """
        return self._request("POST", WIDGET_URL + quote(state_id, safe=""), body={"asyncData": async_data})

    def action(self, action_path: str, body: object) -> dict[str, Any]:
        """POST a composer ``_action/<action_path>`` (body sent as JSON verbatim)."""
        path = action_path.partition("?")[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", path) or any(
            ord(char) < 32 or char in "\\#" for char in action_path
        ):
            raise OzonError("invalid Ozon action path")
        return self._request("POST", ACTION_URL + action_path, body=body, backend="action")

    def refresh(self) -> bool:
        """Silent session refresh (no 2FA): re-bootstrap so OZON mints a fresh
        access-token on navigation, then persist. Returns the auth state.
        """
        with self._lock:
            self._rebootstrap()
            return self.is_authenticated(self.fetch("/my/orderlist"))

    # -- DOM reads (need the browser) ----------------------------------------
    def read_public_page(self, path: str) -> dict[str, Any]:
        """Open one public catalog path on the owned page and read that page only.

        Restored tabs and popups are not adopted. ``goto`` and ``evaluate`` use
        the same page object. A foreground about:blank tab is not evidence of
        what this page contained. The tile selector is waited for within the
        browser budget; a fixed sleep is not used. Live timing is unmeasured.
        """
        checked = _public_catalog_path(path)
        with self._lock:
            if self._logging_in:
                message = "login is in progress; finish it before reading the storefront"
                raise OzonError(message)
            self._ensure_browser()
            if self._page is None or _page_is_closed(self._page):
                message = "Public read has no open owned page. Nothing was read. This is not an empty search result."
                raise OzonError(message)
            _bring_owned_page_forward(self._page)
            self._page.goto(HOME_URL.rstrip("/") + checked, wait_until="domcontentloaded", timeout=self._browser_ms())
            self._raise_for_antibot_challenge()
            owned_class = url_class(getattr(self._page, "url", ""))
            if owned_class == "about:blank":
                message = (
                    "Public read navigated the owned page and it is still about:blank. "
                    "Nothing was read. This is not an empty search result. "
                    + format_public_diagnostics(_status(self._page, ready=False, data={}))
                )
                raise OzonError(message)
            ready = _public_tile_ready(self._page, self._browser_ms())
            if not ready and not _empty_phrase_visible(self._page):
                message = (
                    "Public tile selector was not ready before the deadline. "
                    "Nothing was read. This is not an empty search result. "
                    + format_public_diagnostics(_status(self._page, ready=False, data={}))
                )
                raise OzonError(message)
            self._raise_for_antibot_challenge()
            self._last_used = time.time()
            data = self._page.evaluate(PUBLIC_PAGE_READER)
        if not isinstance(data, dict):
            message = "Public storefront page did not return a document. Nothing was read."
            raise OzonError(message)
        result = dict(data)
        result["diagnostics"] = _status(self._page, ready=ready, data=result)
        return result

    def page_extract(self, path: str, js: str, *, scroll: bool = False) -> Any:
        """Navigate to ``path`` and evaluate ``js`` against the rendered DOM.

        Kept only as a fallback: everything is served over HTTP now, but the
        delivery estimate rides a layout-pinned widget endpoint, so reading the
        rendered page is the safety net if Ozon changes that layout.
        """
        if not path.startswith("/") or path.startswith("//") or "\\" in path or any(ord(char) < 32 for char in path):
            raise OzonError("DOM extraction requires an on-site Ozon path")
        with self._lock:
            self._ensure_browser()
            url = HOME_URL.rstrip("/") + path
            self._page.goto(url, wait_until="domcontentloaded", timeout=self._browser_ms())
            self._page.wait_for_timeout(6_000)
            if scroll:  # trigger lazy widgets
                for delta in (4000, 9000, 16000):
                    self._page.mouse.wheel(0, delta)
                    self._page.wait_for_timeout(1500)
            self._last_used = time.time()
            return self._page.evaluate(js)

    # -- interactive login ---------------------------------------------------
    def _auth_frame(self) -> Any:
        """The iframe holding the current login step; wait for it to appear.

        The step changes the iframe: the email form is served from
        ``/ozonid-lite`` and, once the address is submitted, Ozon swaps in
        ``/otp-lite`` for the code. Matching only the first URL made the code
        step look like a vanished login form, so a login begun in one call could
        never be completed in another.
        """
        for _ in range(30):
            frame = next((frame for frame in self._page.frames if _is_auth_frame(frame.url or "")), None)
            if frame is not None:
                return frame
            self._page.wait_for_timeout(1000)
        msg = "OzonID login frame never appeared"
        raise OzonError(msg)

    def begin_login(self, login: str) -> str:
        """Open the login form and ask Ozon to send a one-time code.

        The code is delivered out of band, so this returns as soon as Ozon has
        moved on to asking for it; ``complete_login`` finishes the job.

        Reaching that step is checked rather than assumed. Clicking through the
        form and reporting "a code was sent" regardless is how a login that Ozon
        never accepted — an address it does not know, a form that wanted a phone,
        a challenge in the way — became "waiting for a code that never arrives",
        with nothing to say why.
        """
        with self._lock:
            self._logging_in = True
            self._http = None
            try:
                channel = self._begin_login_form(login)
            except Exception:
                self._logging_in = False
                raise
            return channel

    def _begin_login_form(self, login: str) -> str:
        # Reuse a healthy context. _launch_browser checks the open page and
        # starts a browser only when none exists.
        self._launch_browser()
        self._page.goto(HOME_URL + "my/orderlist", wait_until="domcontentloaded", timeout=self._browser_ms())
        self._page.wait_for_timeout(6_000)
        self._raise_for_antibot_challenge()
        self._page.get_by_role("button", name="войти", exact=False).first.click(timeout=self._browser_ms(0.25))
        frame = self._auth_frame()
        self._page.wait_for_timeout(2_500)
        by_mail = "@" in login
        if by_mail:
            frame.get_by_text("Войти по почте", exact=False).first.click(timeout=self._browser_ms(0.15))
            self._page.wait_for_timeout(2_500)
            frame = self._auth_frame()
            frame.locator("input[type=email]").first.fill(login)
        else:
            phone_input = select_phone_input(frame)
            phone_value = normalize_phone_input(login, separate_russian_prefix=has_separate_russian_prefix(frame))
            phone_input.fill(phone_value)
        frame.get_by_role("button", name="Войти", exact=True).first.click(timeout=self._browser_ms(0.15))
        self._page.wait_for_timeout(5_000)
        self._require_code_step()
        return login_code_channel(by_mail=by_mail)

    def _require_code_step(self) -> None:
        """Prove Ozon is now asking for the code, or say what it is asking instead."""
        for _ in range(20):
            if any(
                _is_auth_frame(frame.url or "") and urlsplit(frame.url).path == "/otp-lite"
                for frame in self._page.frames
            ):
                return
            for frame in self._page.frames:
                if _is_auth_frame(frame.url or ""):
                    raise_for_phone_format_error(frame)
            self._page.wait_for_timeout(1_000)
        # The form can echo an email, phone or auth token. Keep its text out
        # of caller-visible errors instead of trying to redact every format.
        raise OzonError("Ozon did not move on to the code step; check the login form and request a new code")

    def complete_login(self, code: str) -> bool:
        """Type the one-time code and report whether the account is signed in.

        The code field can arrive ``disabled`` and updates from a React handler
        on that very input, so digits only register if the input is the event
        target: it is enabled and focused first, then real keystrokes are sent.
        """
        with self._lock:
            if not self._logging_in:
                raise OzonError("call start_login() before submitting a code")
            if not re.fullmatch(r"[0-9]{4,8}", code):
                raise OzonError("the login code must contain 4 to 8 ASCII digits")
            frame = self._auth_frame()
            frame.evaluate(
                "() => { const el = document.querySelector('input');"
                " if (el) { el.disabled = false; el.removeAttribute('disabled'); el.focus(); } }"
            )
            self._page.keyboard.type(re.sub(r"\D", "", code), delay=140)
            self._page.wait_for_timeout(1_500)
            with suppress(Exception):
                button = frame.get_by_role("button", name="Войти", exact=True).first
                if button.is_enabled(timeout=self._browser_ms(0.03)):
                    button.click(timeout=self._browser_ms(0.05))
            for _ in range(20):
                self._page.wait_for_timeout(1_000)
                if not any(_is_auth_frame(frame.url or "") for frame in self._page.frames):
                    break
            self._page.goto(HOME_URL, wait_until="domcontentloaded", timeout=self._browser_ms())
            self._page.wait_for_timeout(4_000)
            signed_in = self._browser_is_signed_in()
            if signed_in:
                # Closes the browser, which flushes the new session to the
                # profile; the next call relaunches against it.
                self.back_up_profile()
                self._http = None
            self._logging_in = not signed_in
            return signed_in

    def signed_in_user(self) -> str | None:
        """The account id the profile currently holds, if any."""
        with self._lock:
            self._ensure_browser()
            cookie = next((c for c in self._context.cookies() if c["name"] == "__Secure-user-id"), None)
            value = (cookie or {}).get("value")
            return str(value) if value and value != _GUEST_USER_ID else None

    def has_backup(self) -> bool:
        return (self._backup / "Default").exists()

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def is_authenticated(data: dict[str, Any]) -> bool:
        """Look for a positive signal.

        The anonymous widgets (``profileMenuAnonymous``, ``loginButton``) ship in
        the payload even for a signed-in account, so their presence proves
        nothing; a personal widget does.
        """
        blob = str(data.get("widgetStates") or {})
        return any(marker in blob for marker in ("myOrdersList", "orderList", "profileMenuUser", "userAdultModal"))
