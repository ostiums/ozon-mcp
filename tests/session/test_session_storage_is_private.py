"""Session cookies stay private even when existing files have broad permissions."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING, Any

import pytest

from ozon_mcp.session import transport
from ozon_mcp.session.transport import OzonSession, _make_private
from ozon_mcp.settings import get_settings

if TYPE_CHECKING:
    from pathlib import Path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _public_tree(path: Path) -> Path:
    (path / "Default").mkdir(parents=True)
    cookies = path / "Default" / "Cookies"
    cookies.write_text("session", encoding="utf-8")
    path.chmod(0o755)
    (path / "Default").chmod(0o755)
    cookies.chmod(0o644)
    return cookies


def test_new_profile_is_private_with_a_permissive_umask(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    tmp_path.chmod(0o755)
    previous = os.umask(0)
    try:
        _make_private(profile, create=True)
    finally:
        os.umask(previous)

    assert _mode(profile) == 0o700
    # Restrict the requested storage, never the directory containing it.
    assert _mode(tmp_path) == 0o755


def test_existing_tree_is_restricted_without_following_links(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    cookies = _public_tree(profile)
    outside = tmp_path / "outside"
    outside_cookies = _public_tree(outside)
    (profile / "linked-directory").symlink_to(outside, target_is_directory=True)
    (profile / "linked-file").symlink_to(outside_cookies)
    (profile / "SingletonLock").symlink_to("missing-browser-lock")

    _make_private(profile)

    assert _mode(profile) == _mode(profile / "Default") == 0o700
    assert _mode(cookies) == 0o600
    assert _mode(outside) == _mode(outside / "Default") == 0o755
    assert _mode(outside_cookies) == 0o644
    assert (profile / "SingletonLock").is_symlink()


def test_a_symbolic_link_cannot_be_the_storage_root(tmp_path: Path) -> None:
    cookies = _public_tree(tmp_path / "outside")
    linked = tmp_path / "profile"
    linked.symlink_to(cookies.parent.parent, target_is_directory=True)

    with pytest.raises(OSError, match="must not be a symbolic link"):
        _make_private(linked, create=True)

    assert _mode(cookies) == 0o644


def test_profile_backup_and_restore_keep_private_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "profile"
    cookies = _public_tree(profile)
    backup = tmp_path / "backup"
    monkeypatch.setattr(get_settings(), "profile_backup", backup)
    session = OzonSession(profile_dir=profile)
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    outside.chmod(0o644)
    (profile / "external").symlink_to(outside)
    (profile / "SingletonLock").symlink_to("unused-lock")

    session.back_up_profile()

    assert _mode(cookies) == 0o600
    assert _mode(backup) == _mode(backup / "Default") == 0o700
    assert _mode(backup / "Default" / "Cookies") == 0o600
    assert (backup / "external").is_symlink()
    assert not (backup / "SingletonLock").is_symlink()

    assert session.restore_profile() is True
    assert _mode(profile) == _mode(profile / "Default") == 0o700
    assert _mode(cookies) == 0o600
    assert (profile / "external").is_symlink()
    assert _mode(outside) == 0o644


def test_existing_session_paths_are_restricted_before_browser_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    cookies = _public_tree(profile)
    backup = tmp_path / "backup"
    backup_cookies = _public_tree(backup)
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    state.chmod(0o644)
    monkeypatch.setattr(get_settings(), "profile_backup", backup)
    monkeypatch.setattr(get_settings(), "state_path", state)

    def stop_before_browser() -> Any:
        assert _mode(cookies) == _mode(backup_cookies) == _mode(state) == 0o600
        assert _mode(profile) == _mode(backup) == 0o700
        raise RuntimeError("browser not started")

    monkeypatch.setattr(transport, "sync_playwright", stop_before_browser)
    with pytest.raises(RuntimeError, match="browser not started"):
        OzonSession(profile_dir=profile)._launch_browser()
