"""A live or foreign Chromium lock is not deleted."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from ozon_mcp.errors import OzonError
from ozon_mcp.session.profile_lock import clear_dead_profile_lock

if TYPE_CHECKING:
    from pathlib import Path


def _dead_pid() -> int:
    pid = 1_000_000_000
    for candidate in range(pid, pid + 50):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except OSError:
            continue
    message = "could not find an unused pid"
    raise RuntimeError(message)


def test_dead_owner_on_this_host_is_removed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="utf-8")
    lock = tmp_path / "SingletonLock"
    lock.symlink_to(f"this-host-{_dead_pid()}")
    (tmp_path / "SingletonCookie").write_text("c", encoding="utf-8")
    (tmp_path / "SingletonSocket").symlink_to(outside)
    removed = clear_dead_profile_lock(tmp_path, hostname="this-host", pid_is_live_fn=lambda _pid: False)
    assert removed == ("SingletonLock", "SingletonCookie", "SingletonSocket")
    assert not lock.exists()
    assert not lock.is_symlink()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_live_owner_is_kept(tmp_path: Path) -> None:
    lock = tmp_path / "SingletonLock"
    lock.symlink_to(f"this-host-{os.getpid()}")
    (tmp_path / "SingletonCookie").write_text("c", encoding="utf-8")
    with pytest.raises(OzonError, match="live process"):
        clear_dead_profile_lock(tmp_path, hostname="this-host")
    assert lock.is_symlink()
    assert (tmp_path / "SingletonCookie").read_text(encoding="utf-8") == "c"


def test_foreign_host_is_kept_even_if_the_pid_is_dead(tmp_path: Path) -> None:
    lock = tmp_path / "SingletonLock"
    lock.symlink_to(f"other-host-{_dead_pid()}")
    with pytest.raises(OzonError, match="another machine"):
        clear_dead_profile_lock(tmp_path, hostname="this-host", pid_is_live_fn=lambda _pid: False)
    assert lock.is_symlink()


def test_unreadable_owner_is_kept(tmp_path: Path) -> None:
    lock = tmp_path / "SingletonLock"
    lock.symlink_to("outside/not-an-owner")
    with pytest.raises(OzonError, match="no readable owner"):
        clear_dead_profile_lock(tmp_path, hostname="this-host")
    assert lock.is_symlink()


def test_regular_lock_file_is_kept(tmp_path: Path) -> None:
    lock = tmp_path / "SingletonLock"
    lock.write_text("locked", encoding="utf-8")
    with pytest.raises(OzonError, match="not an owner symlink"):
        clear_dead_profile_lock(tmp_path, hostname="this-host")
    assert lock.read_text(encoding="utf-8") == "locked"


def test_leftover_sockets_without_a_lock_are_removed(tmp_path: Path) -> None:
    (tmp_path / "SingletonSocket").write_text("stale", encoding="utf-8")
    removed = clear_dead_profile_lock(tmp_path, hostname="this-host")
    assert removed == ("SingletonSocket",)
    assert not (tmp_path / "SingletonSocket").exists()
