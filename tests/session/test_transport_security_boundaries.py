"""Keep credentials on the fixed Ozon endpoints and avoid replaying writes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest

from ozon_mcp.constants import ACTION_URL
from ozon_mcp.errors import OzonError, UpstreamError
from ozon_mcp.session.transport import OzonSession, _copy_private_profile, _is_auth_frame, _make_private
from session.test_upstream_failure_is_not_an_empty_page import PAGE, _Http, _Response, _Session

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "answer", [TimeoutError("already sent"), _Response(502, "bad gateway"), _Response(403, "blocked")]
)
def test_mutations_are_never_replayed_after_an_ambiguous_failure(answer: _Response | Exception) -> None:
    http = _Http(answer, _Response(200, PAGE))
    session = _Session(http)
    with pytest.raises(UpstreamError):
        session.action("createOrderV2", {"id": "createOrder"})
    assert http.calls == 1
    assert not session.waits
    assert not session.rebootstraps


@pytest.mark.parametrize(
    "url", ["http://www.ozon.ru/api/x", "https://evil.test/api/x", "https://www.ozon.ru@evil.test/x"]
)
def test_transport_rejects_destinations_before_sending_credentials(url: str) -> None:
    http = _Http(_Response(200, PAGE))
    session = _Session(http)
    with pytest.raises(OzonError):
        session._request("GET", url)
    assert http.calls == 0


@pytest.mark.parametrize(
    "path",
    [
        "../page/json/v2",
        "%2e%2e/page/json/v2",
        "v2/../../x",
        "/absolute",
        "https://evil.test",
        "v2\\..\\x",
        "action#fragment",
    ],
)
def test_action_links_cannot_escape_the_action_endpoint(path: str) -> None:
    http = _Http(_Response(200, PAGE))
    with pytest.raises(OzonError):
        _Session(http).action(path, {})
    assert http.calls == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/ozonid-lite",
        "https://www.ozon.ru.evil.test/otp-lite",
        "https://www.ozon.ru/?redirect=otp-lite",
        "http://www.ozon.ru/otp-lite",
    ],
)
def test_untrusted_pages_cannot_receive_one_time_codes(url: str) -> None:
    assert not _is_auth_frame(url)


@pytest.mark.parametrize("status", [200, 302, 401, 502])
def test_invalid_responses_never_echo_tokens_or_look_like_an_empty_account(status: int) -> None:
    payload = "__Secure-access-token=private-secret@example.test"
    with pytest.raises(OzonError) as raised:
        _Session(_Http(_Response(status, payload))).fetch("/my/orderlist")
    assert payload not in str(raised.value)


def test_transport_does_not_follow_redirects() -> None:
    options: list[dict[str, object]] = []

    class RecordingHttp(_Http):
        def request(self, *_args: object, **kwargs: object) -> _Response:
            options.append(kwargs)
            return super().request(*_args, **kwargs)

    session = _Session(RecordingHttp(_Response(302, PAGE)))
    with pytest.raises(UpstreamError):
        session.fetch("/my/orderlist")
    assert options[0]["allow_redirects"] is False


def test_symlinked_storage_ancestor_is_rejected_before_creation(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError, match="symbolic link"):
        _make_private(link / "profile", create=True)
    assert not (target / "profile").exists()


def test_copy_rejects_symlinked_destination_ancestor_before_removal(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "cookie").write_text("secret", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "backup" / "keep"
    sentinel.parent.mkdir()
    sentinel.write_text("keep", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError, match="symbolic link"):
        _copy_private_profile(source, link / "backup")
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("destination", ["same", "child", "parent"])
def test_copy_rejects_overlapping_profile_paths(tmp_path: Path, destination: str) -> None:
    source = tmp_path / "profile"
    source.mkdir()
    (source / "cookie").write_text("secret", encoding="utf-8")
    target = {"same": source, "child": source / "backup", "parent": tmp_path}[destination]
    with pytest.raises(OSError, match="overlap"):
        _copy_private_profile(source, target)
    assert (source / "cookie").read_text(encoding="utf-8") == "secret"


def test_action_query_is_preserved_within_the_action_endpoint() -> None:
    class RecordingHttp(_Http):
        def request(self, *args: object, **kwargs: object) -> _Response:
            assert args[1] == ACTION_URL + "selectCancelModalRms?orderNumber=123-456"
            assert urlsplit(str(args[1])).path.startswith("/api/composer-api.bx/_action/")
            return super().request(*args, **kwargs)

    _Session(RecordingHttp(_Response(200, PAGE))).action("selectCancelModalRms?orderNumber=123-456", {})


@pytest.mark.parametrize("path", ["https://evil.test/", "http://127.0.0.1/", "//evil.test/", "/\\evil.test/"])
def test_dom_fallback_rejects_external_navigation_before_browser_access(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = OzonSession()
    monkeypatch.setattr(session, "_ensure_browser", lambda: None)
    session._page = SimpleNamespace(
        goto=lambda *_args, **_kwargs: None, wait_for_timeout=lambda _: None, evaluate=lambda _: "title"
    )
    with pytest.raises(OzonError):
        session.page_extract(path, "() => document.title")


@pytest.mark.parametrize("header", ["-1", "nan", "inf", "-inf"])
def test_retry_after_cannot_create_an_invalid_sleep(header: str) -> None:
    assert OzonSession._retry_after(_Response(429, "", {"Retry-After": header})) is None


def test_login_in_progress_cannot_answer_with_the_previous_account() -> None:
    http = _Http(_Response(200, PAGE))
    session = _Session(http)
    session._logging_in = True
    with pytest.raises(OzonError, match="login"):
        session.fetch("/my/orderlist")
    assert http.calls == 0


def test_code_requires_a_started_login() -> None:
    with pytest.raises(OzonError, match="start_login"):
        OzonSession().complete_login("123456")


@pytest.mark.parametrize("code", ["", "123abc", "1" * 10000], ids=["empty", "nondigit", "unbounded"])
def test_invalid_codes_are_rejected_before_interacting_with_the_browser(code: str) -> None:
    session = OzonSession()
    session._logging_in = True
    with pytest.raises(OzonError, match="code"):
        session.complete_login(code)


def test_copy_rejects_a_dot_dot_alias_before_deleting_the_profile(tmp_path: Path) -> None:
    source = tmp_path / "profile"
    source.mkdir()
    (tmp_path / "other").mkdir()
    sentinel = source / "cookie"
    sentinel.write_text("secret", encoding="utf-8")
    with pytest.raises(OSError, match="overlap"):
        _copy_private_profile(source, tmp_path / "other" / ".." / "profile")
    assert sentinel.read_text(encoding="utf-8") == "secret"


def test_ambiguous_mutation_error_requires_read_back_before_retrying() -> None:
    with pytest.raises(UpstreamError) as raised:
        _Session(_Http(TimeoutError("already sent"))).action("createOrderV2", {})
    assert "outcome is unknown" in str(raised.value)
    assert "inspect account state before retrying" in str(raised.value)


def test_rate_limited_mutation_does_not_advise_an_unconditional_retry() -> None:
    with pytest.raises(UpstreamError) as raised:
        _Session(_Http(_Response(429, "slow down"))).action("createOrderV2", {})
    assert "inspect account state before retrying" in str(raised.value)


def test_failed_login_start_discards_the_previous_transport_and_clears_the_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _Session(_Http(_Response(200, PAGE)))
    monkeypatch.setattr(session, "_close_browser", lambda: None)

    def failed_launch() -> None:
        raise OzonError("login browser failed")

    monkeypatch.setattr(session, "_launch_browser", failed_launch)
    with pytest.raises(OzonError, match="browser failed"):
        session.begin_login("test@example.test")
    assert session._http is None
    assert not session._logging_in


@pytest.mark.parametrize("status", [400, 401, 404])
def test_json_error_pages_cannot_be_reported_as_an_empty_account(status: int) -> None:
    with pytest.raises(OzonError):
        _Session(_Http(_Response(status, '{"error":"unavailable"}'))).fetch("/my/orderlist")
