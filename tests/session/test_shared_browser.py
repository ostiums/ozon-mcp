"""Real browser clients share a profile but own independent tab lifetimes."""

from __future__ import annotations

import fcntl
import multiprocessing
import os
import signal
import socket
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from playwright.sync_api import sync_playwright

from ozon_mcp.errors import OzonError
from ozon_mcp.session import transport
from ozon_mcp.session.shared_browser import _runtime_dir, acquire_browser
from ozon_mcp.settings import OzonSettings

if TYPE_CHECKING:
    from multiprocessing.connection import Connection


def _client(pipe: Connection, profile: str) -> None:

    path = Path(profile)
    options = {"user_data_dir": profile, "headless": True, "args": ["--no-sandbox"], "timeout": 10000}
    lease = acquire_browser(path, options, 15)
    try:
        with sync_playwright() as playwright:
            context, page = lease.attach(playwright, 10)
            page.set_content(f"<h1>{lease.token}</h1>")
            pipe.send((lease.owner_pid, lease.token))
            while (command := pipe.recv()) != "quit":
                if command == "read":
                    pipe.send(page.locator("h1").inner_text())
                elif command == "tabs":
                    page.wait_for_timeout(150)
                    pipe.send(len(context.pages))
    finally:
        lease.close()
        pipe.close()


def _receive(pipe: Connection) -> Any:
    assert pipe.poll(20), "Browser client did not reply."
    return pipe.recv()


def _stop_owner(pid: int, profile: Path) -> None:
    os.kill(pid, signal.SIGTERM)
    with _runtime_dir(profile).with_suffix(".lock").open("rb") as lock:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(0.05)
            else:
                return
        pytest.fail("Browser owner did not release the profile.")


def test_concurrent_clients_survive_peer_kill_and_restart(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    clients = []
    owner = None
    try:
        for _ in range(2):
            parent, child = ctx.Pipe()
            process = ctx.Process(target=_client, args=(child, str(tmp_path)))
            process.start()
            child.close()
            clients.append((process, parent))
        first_owner, first_token = _receive(clients[0][1])
        second_owner, second_token = _receive(clients[1][1])
        owner = first_owner
        assert first_owner == second_owner
        assert first_token != second_token
        clients[0][0].kill()
        clients[0][0].join(10)
        survivor = clients[1][1]
        survivor.send("read")
        assert _receive(survivor) == second_token
        survivor.send("tabs")
        assert _receive(survivor) == 2
        parent, child = ctx.Pipe()
        replacement = ctx.Process(target=_client, args=(child, str(tmp_path)))
        replacement.start()
        child.close()
        clients.append((replacement, parent))
        replacement_owner, replacement_token = _receive(parent)
        assert replacement_owner == owner
        assert replacement_token not in {first_token, second_token}
        survivor.send("read")
        assert _receive(survivor) == second_token
        parent.send("read")
        assert _receive(parent) == replacement_token
    finally:
        for process, pipe in clients:
            if process.is_alive():
                pipe.send("quit")
                process.join(10)
            if process.is_alive():
                process.kill()
                process.join(10)
            pipe.close()
        if owner is not None:
            _stop_owner(owner, tmp_path)


def test_configuration_mismatch_does_not_change_owner(tmp_path: Path) -> None:
    options = {"user_data_dir": str(tmp_path), "headless": True, "args": ["--no-sandbox"], "timeout": 10000}
    lease = acquire_browser(tmp_path, options, 15)
    try:
        with pytest.raises(OzonError, match="configuration differs"):
            acquire_browser(tmp_path, {**options, "locale": "ru-RU"}, 10)
        with sync_playwright() as playwright:
            _context, page = lease.attach(playwright, 10)
            assert page.evaluate("2 + 2") == 4
    finally:
        lease.close()
        _stop_owner(lease.owner_pid, tmp_path)


def test_live_profile_owner_is_not_disturbed(tmp_path: Path) -> None:

    target = f"{socket.gethostname()}-{os.getpid()}"
    lock = tmp_path / "SingletonLock"
    lock.symlink_to(target)
    with pytest.raises(OzonError, match="live process"):
        acquire_browser(tmp_path, {"user_data_dir": str(tmp_path)}, 1)
    assert str(lock.readlink()) == target


def test_dead_profile_owner_is_cleaned_before_start(tmp_path: Path) -> None:

    process = multiprocessing.get_context("spawn").Process(target=time.time)
    process.start()
    pid = process.pid
    process.join(10)
    (tmp_path / "SingletonLock").symlink_to(f"{socket.gethostname()}-{pid}")
    options = {"user_data_dir": str(tmp_path), "headless": True, "args": ["--no-sandbox"], "timeout": 10000}
    lease = acquire_browser(tmp_path, options, 15)
    try:
        lock = tmp_path / "SingletonLock"
        assert not lock.is_symlink() or str(lock.readlink()) != f"{socket.gethostname()}-{pid}"
        assert _runtime_dir(tmp_path).with_suffix(".sock").stat().st_mode & 0o777 == 0o600
    finally:
        lease.close()
        _stop_owner(lease.owner_pid, tmp_path)


def test_failed_launch_reports_cause_and_allows_retry(tmp_path: Path) -> None:
    options = {"user_data_dir": str(tmp_path), "headless": True, "args": ["--no-sandbox"], "timeout": 10000}
    with pytest.raises(OzonError, match=r"launch failed.*executable doesn't exist"):
        acquire_browser(tmp_path, {**options, "executable_path": str(tmp_path / "missing-browser")}, 10)
    lease = acquire_browser(tmp_path, options, 15)
    try:
        with sync_playwright() as playwright:
            _context, page = lease.attach(playwright, 10)
            assert page.evaluate("2 + 2") == 4
    finally:
        lease.close()
        _stop_owner(lease.owner_pid, tmp_path)


def test_profile_alias_uses_the_same_owner_address(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(profile, target_is_directory=True)
    assert _runtime_dir(alias) == _runtime_dir(profile)


def test_client_temp_directories_do_not_split_profile_ownership(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "first-client").mkdir()
    (tmp_path / "second-client").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "first-client"))
    monkeypatch.setattr(tempfile, "tempdir", None)
    first = _runtime_dir(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "second-client"))
    monkeypatch.setattr(tempfile, "tempdir", None)
    assert _runtime_dir(tmp_path) == first


def test_first_call_after_owner_exit_reconnects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "Default").mkdir()
    monkeypatch.setattr(transport, "get_settings", lambda: OzonSettings(storefront="public"))

    def headless(profile: Path, options: dict[str, Any], timeout: float) -> Any:
        return acquire_browser(profile, {**options, "headless": True}, timeout)

    monkeypatch.setattr(transport, "acquire_browser", headless)
    session = transport.OzonSession(profile_dir=tmp_path)
    owner = None
    try:
        session._launch_browser()
        assert session._browser_lease is not None
        old_owner = session._browser_lease.owner_pid
        _stop_owner(old_owner, tmp_path)
        assert session.signed_in_user() is None
        assert session._browser_lease is not None
        owner = session._browser_lease.owner_pid
        assert owner != old_owner
    finally:
        session.close()
        if owner is not None:
            _stop_owner(owner, tmp_path)
