"""Redirects are classified and never followed.

A sanctioned search 302 was host=same-canonical-ozon and path=api-path. That is
not the public /search/ page. The destination text stays out of the error.
"""

from __future__ import annotations

import pytest

from ozon_mcp.errors import SessionExpiredError, UpstreamError
from ozon_mcp.session.transport import _redirect_detail
from ozon_mcp.settings import get_settings
from session.test_upstream_failure_is_not_an_empty_page import PAGE, _Http, _Response, _Session

MARKER = "DO_NOT_EMIT"
REQUEST = "https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/search/"


@pytest.mark.parametrize(
    ("location", "host", "path"),
    [
        (None, "missing", "unknown"),
        ("", "missing", "unknown"),
        (f"/search/?secret={MARKER}#frag", "same-canonical-ozon", "/search/"),
        ("https://www.ozon.ru/", "same-canonical-ozon", "/"),
        ("https://ozon.ru/search/", "other-ozon", "/search/"),
        (f"https://sub.ozon.ru/api/{MARKER}", "other-ozon", "api-path"),
        (f"https://www.ozon.ru/account/{MARKER}", "same-canonical-ozon", "other-path"),
        (f"https://{MARKER}.example.invalid/search/", "external", "/search/"),
        (f"//example.invalid/api/{MARKER}", "external", "api-path"),
        ("https://ozon.ru.example.invalid/", "external", "/"),
        ("https://evilozon.ru/", "external", "/"),
        (f"https://{MARKER}:{MARKER}@www.ozon.ru/", "malformed", "unknown"),
        (f"https://www.ozon.ru:{MARKER}/", "malformed", "unknown"),
        ("https://www.ozon.ru:99999/", "malformed", "unknown"),
        (f"https://[{MARKER}/", "malformed", "unknown"),
        (f"javascript:{MARKER}", "malformed", "unknown"),
        (f"data:{MARKER}", "malformed", "unknown"),
        (f"https://www.ozon.ru/\\{MARKER}", "malformed", "unknown"),
        (f"https://www.ozon.ru/\n{MARKER}", "malformed", "unknown"),
        (" https://www.ozon.ru/", "malformed", "unknown"),
        (f"/search/%2F{MARKER}", "same-canonical-ozon", "other-path"),
        (f"?{MARKER}={MARKER}", "same-canonical-ozon", "api-path"),
        (object(), "malformed", "unknown"),
    ],
)
def test_redirect_labels_hide_the_destination(location: object, host: str, path: str) -> None:
    result = _redirect_detail(location, REQUEST)
    assert result == f"Redirect destination: host={host}; path={path}."
    assert MARKER not in result


class _RecordingHttp(_Http):
    def __init__(self, *answers: _Response | Exception) -> None:
        super().__init__(*answers)
        self.urls: list[str] = []
        self.options: list[dict[str, object]] = []

    def request(self, *args: object, **kwargs: object) -> _Response:
        self.urls.append(str(args[1]))
        self.options.append(kwargs)
        return super().request(*args, **kwargs)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_every_redirect_is_rejected_once(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "request_attempts", 1)
    http = _RecordingHttp(_Response(status, "{}", {"Location": f"/search/?secret={MARKER}"}))
    with pytest.raises(UpstreamError) as raised:
        _Session(http).fetch("/search/")
    assert raised.value.status == status
    assert raised.value.detail == "Redirect destination: host=same-canonical-ozon; path=/search/."
    assert MARKER not in str(raised.value)
    assert http.calls == 1
    assert http.options[0]["allow_redirects"] is False


def test_api_path_redirect_is_not_a_public_search_and_is_not_followed() -> None:
    location = f"https://www.ozon.ru/api/composer-api.bx/page/json/v2?url=/search/&token={MARKER}"
    http = _RecordingHttp(_Response(302, "{}", {"Location": location}))
    with pytest.raises(UpstreamError, match="host=same-canonical-ozon; path=api-path") as raised:
        _Session(http).fetch("/search/?text=example")
    assert MARKER not in str(raised.value)
    assert http.calls == 1
    assert len(http.urls) == 1


def test_missing_location_is_still_rejected() -> None:
    http = _RecordingHttp(_Response(302, "{}"))
    with pytest.raises(UpstreamError, match="host=missing; path=unknown") as raised:
        _Session(http).fetch("/my/orderlist")
    assert raised.value.status == 302
    assert http.calls == 1


@pytest.mark.parametrize(
    ("status", "body"),
    [(403, "{}"), (307, "{}"), (302, "abt-challenge"), (200, "incidentId")],
)
def test_challenge_responses_do_not_save_state(status: int, body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    saved: list[bool] = []
    session = _Session(_RecordingHttp(_Response(status, body, {"Location": "https://www.ozon.ru/api/x"})))
    monkeypatch.setattr(session, "save_state", lambda: saved.append(True))
    with pytest.raises(UpstreamError):
        session.fetch("/my/orderlist")
    assert saved == []


def test_non_redirect_errors_have_no_destination_class() -> None:
    for status in (403, 500):
        with pytest.raises(UpstreamError) as raised:
            _Session(_RecordingHttp(_Response(status, "no"))).fetch("/my/orderlist")
        assert not raised.value.detail


def test_success_still_requires_a_json_object() -> None:
    page = _Session(_RecordingHttp(_Response(200, PAGE))).fetch("/my/orderlist")
    assert page["_httpStatus"] == 200
    for body in ("<html>fixture</html>", "[]"):
        with pytest.raises(UpstreamError):
            _Session(_RecordingHttp(_Response(200, body))).fetch("/my/orderlist")


def test_unauthorized_is_session_expired_without_a_redirect_class() -> None:
    with pytest.raises(SessionExpiredError):
        _Session(_RecordingHttp(_Response(401, PAGE, {"Location": f"https://example.invalid/{MARKER}"}))).fetch(
            "/my/orderlist"
        )


def test_guarded_redirect_retries_once_and_still_does_not_follow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "request_attempts", 2)
    http = _RecordingHttp(_Response(307, "{}", {"Location": f"https://www.ozon.ru/api/{MARKER}"}))
    session = _Session(http)
    with pytest.raises(UpstreamError, match="api-path") as raised:
        session.fetch("/my/orderlist")
    assert MARKER not in str(raised.value)
    assert http.calls == 2
    assert session.rebootstraps == 1
    assert all(call["allow_redirects"] is False for call in http.options)
