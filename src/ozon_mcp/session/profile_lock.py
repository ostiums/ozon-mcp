"""Remove a Chromium profile lock only when its owner is demonstrably dead."""

import os
import socket
from collections.abc import Callable
from pathlib import Path

from ozon_mcp.errors import OzonError

_LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")
_LEFTOVERS = ("SingletonCookie", "SingletonSocket")


def pid_is_live(pid: int) -> bool:
    """True when the pid exists or the check itself is inconclusive."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def clear_dead_profile_lock(
    profile: Path,
    *,
    hostname: str | None = None,
    pid_is_live_fn: Callable[[int], bool] | None = None,
) -> tuple[str, ...]:
    """Unlink stale singleton files. A live or foreign owner is left untouched."""
    host = socket.gethostname() if hostname is None else hostname
    alive = pid_is_live if pid_is_live_fn is None else pid_is_live_fn
    lock = profile / "SingletonLock"
    present = _exists(lock)
    if present and lock.is_symlink():
        names = _dead_owner_names(str(lock.readlink()), hostname=host, pid_is_live_fn=alive)
    elif present:
        message = "Chromium profile lock is not an owner symlink; it was not removed."
        raise OzonError(message)
    else:
        names = tuple(name for name in _LEFTOVERS if _exists(profile / name))
    for name in names:
        (profile / name).unlink(missing_ok=True)
    return names


def _dead_owner_names(target: str, *, hostname: str, pid_is_live_fn: Callable[[int], bool]) -> tuple[str, ...]:
    owner = _owner(target)
    if owner is None:
        message = "Chromium profile lock has no readable owner; it was not removed."
        raise OzonError(message)
    host, pid = owner
    if host != hostname:
        message = "Chromium profile lock names another machine; it was not removed."
        raise OzonError(message)
    if pid_is_live_fn(pid):
        message = "Chromium profile lock is held by a live process; it was not removed."
        raise OzonError(message)
    return _LOCK_NAMES


def _owner(target: str) -> tuple[str, int] | None:
    if not target or "/" in target or "\\" in target:
        return None
    host, separator, pid_text = target.rpartition("-")
    if not separator or not host or not pid_text.isascii() or not pid_text.isdigit():
        return None
    pid = int(pid_text)
    if pid <= 0:
        return None
    return host, pid


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True
