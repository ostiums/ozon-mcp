"""A public profile has one browser owner and one socket lease per client tab."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import select
import selectors
import signal
import socket
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] - starts this package with the current Python
import sys
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from playwright.sync_api import Error, sync_playwright

from ozon_mcp.errors import OzonError
from ozon_mcp.session.profile_lock import clear_dead_profile_lock

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page, Playwright

_IDLE_SECONDS = 30.0
_PROTOCOL = 1


@dataclass
class BrowserLease:
    connection: socket.socket
    endpoint: str
    token: str
    owner_pid: int

    def close(self) -> None:
        self.connection.close()

    def is_alive(self) -> bool:
        if self.connection.fileno() < 0:
            return False
        try:
            readable, _writable, _errors = select.select([self.connection], [], [], 0)
            return not readable or bool(self.connection.recv(1, socket.MSG_PEEK))
        except OSError:
            return False

    def attach(self, playwright: Playwright, timeout: float) -> tuple[BrowserContext, Page]:
        browser = playwright.chromium.connect_over_cdp(self.endpoint, timeout=timeout * 1000)
        context = browser.contexts[0]
        pages = [page for page in context.pages if page.url == f"about:blank#{self.token}"]
        if len(pages) != 1:
            raise OzonError("Shared browser did not provide the client's tab.")
        return context, pages[0]


def _runtime_dir(profile: Path) -> Path:
    root = Path("/tmp") / f"ozon-mcp-{os.getuid()}"  # ruff: ignore[hardcoded-temp-file] - common across client TMPDIR values
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise OzonError("Shared browser runtime directory must be private and owned by this user.")
    key = hashlib.sha256(os.fsencode(profile.resolve())).hexdigest()[:32]
    return root / key


def _fingerprint(options: dict[str, Any]) -> str:
    config = {k: v for k, v in options.items() if k not in {"timeout", "handle_sigterm"}}
    config["display"] = os.environ.get("DISPLAY")
    config["protocol"] = _PROTOCOL
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def acquire_browser(profile: Path, options: dict[str, Any], timeout: float) -> BrowserLease:
    """Acquire an owned tab without taking ownership of the shared context."""
    profile = profile.resolve()
    options = {**options, "user_data_dir": str(profile)}
    base = _runtime_dir(profile)
    address = str(base.with_suffix(".sock"))
    fingerprint = _fingerprint(options)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = socket.socket(socket.AF_UNIX)
        try:
            connection.settimeout(max(0.01, deadline - time.monotonic()))
            connection.connect(address)
            return _request_lease(connection, fingerprint)
        except (ConnectionError, FileNotFoundError):
            connection.close()
            _start_owner(base, options)
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        except BaseException:
            connection.close()
            raise
    raise OzonError("Timed out waiting for the shared profile browser.")


def _request_lease(connection: socket.socket, fingerprint: str) -> BrowserLease:
    connection.sendall((fingerprint + "\n").encode())
    response = bytearray()
    while b"\n" not in response and len(response) < 4096:
        chunk = connection.recv(4096)
        if not chunk:
            raise ConnectionError("Browser owner stopped before assigning a tab.")
        response.extend(chunk)
    result = json.loads(response)
    if "error" in result:
        raise OzonError(result["error"])
    return BrowserLease(connection, result["endpoint"], result["token"], result["pid"])


def _start_owner(base: Path, options: dict[str, Any]) -> None:
    fd = os.open(base.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        profile = Path(options["user_data_dir"])
        if (profile / "SingletonLock").is_symlink():
            clear_dead_profile_lock(profile)
        elif (profile / "SingletonLock").exists():
            raise OzonError("Chromium profile has an unrecognized lock; it was not removed.")
        process = subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed module, no shell
            [sys.executable, "-m", __name__, str(base), str(fd)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(fd,),
        )
        if process.stdin is None:
            raise OzonError("Could not send browser launch configuration.")
        process.stdin.write(json.dumps(options).encode())
        process.stdin.close()
        threading.Thread(target=process.wait, daemon=True, name="ozon-browser-reaper").start()
    finally:
        os.close(fd)


def _endpoint(profile: Path) -> str:
    port, path = (profile / "DevToolsActivePort").read_text().splitlines()[:2]
    if not port.isdigit() or not 0 < int(port) < 65536 or not path.startswith("/devtools/browser/"):
        raise OzonError("Browser did not publish a valid local debugging endpoint.")
    return f"ws://127.0.0.1:{port}{path}"


@dataclass
class _Client:
    connection: socket.socket
    received: bytes = b""
    page: Page | None = None

    def receive(self, context: BrowserContext, endpoint: str, fingerprint: str) -> None:
        data = self.connection.recv(1024)
        if not data or self.page is not None:
            raise ConnectionError("Lease ended.")
        self.received += data
        if len(self.received) > 128:
            raise ConnectionError("Invalid lease request.")
        if b"\n" not in self.received:
            return
        if self.received.decode().strip() != fingerprint:
            self.connection.sendall(b'{"error":"Profile browser configuration differs from this client."}\n')
            raise ConnectionError("Configuration mismatch.")
        token = uuid.uuid4().hex
        self.page = context.new_page()
        self.page.goto(f"about:blank#{token}")
        reply = {"endpoint": endpoint, "token": token, "pid": os.getpid()}
        self.connection.sendall((json.dumps(reply) + "\n").encode())

    def close(self) -> None:
        if self.page is not None:
            with suppress(Error):
                self.page.close()
        self.connection.close()


def _serve(base: Path, options: dict[str, Any], stop: threading.Event) -> None:
    address = base.with_suffix(".sock")
    fingerprint = _fingerprint(options)
    address.unlink(missing_ok=True)
    with socket.socket(socket.AF_UNIX) as listener, selectors.DefaultSelector() as selector:
        listener.bind(str(address))
        address.chmod(0o600)
        listener.listen()
        listener.settimeout(0)
        selector.register(listener, selectors.EVENT_READ)
        options = {
            **options,
            "args": [*options.get("args", []), "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"],
        }
        with sync_playwright() as playwright:
            try:
                context = playwright.chromium.launch_persistent_context(**options)
            except Error as error:
                _report_startup_failure(listener, error, options)
                return
            try:
                endpoint = _endpoint(Path(options["user_data_dir"]))
                _serve_clients(listener, selector, context, endpoint, fingerprint, stop)
            finally:
                context.close()
    address.unlink(missing_ok=True)


def _report_startup_failure(listener: socket.socket, error: Error, options: dict[str, Any]) -> None:
    detail = str(error).splitlines()[0]
    for secret in options.get("proxy", {}).values():
        if secret:
            detail = detail.replace(secret, "[proxy]")
    message = (json.dumps({"error": f"Shared browser launch failed: {detail}"}) + "\n").encode()
    listener.settimeout(0.5)
    with suppress(OSError):
        connection, _address = listener.accept()
        with connection:
            connection.sendall(message)


def _serve_clients(
    listener: socket.socket,
    selector: selectors.BaseSelector,
    context: BrowserContext,
    endpoint: str,
    fingerprint: str,
    stop: threading.Event,
) -> None:
    clients: dict[socket.socket, _Client] = {}
    idle_since = time.monotonic()
    heartbeat = context.pages[0] if context.pages else context.new_page()
    try:
        while not stop.is_set() and (clients or time.monotonic() - idle_since < _IDLE_SECONDS):
            heartbeat.wait_for_timeout(50)
            for key, _events in selector.select(0):
                if key.fileobj is listener:
                    connection, _address = listener.accept()
                    connection.settimeout(0)
                    clients[connection] = _Client(connection)
                    selector.register(connection, selectors.EVENT_READ, clients[connection])
                    continue
                client = key.data
                try:
                    client.receive(context, endpoint, fingerprint)
                except (OSError, UnicodeError, Error):
                    selector.unregister(client.connection)
                    clients.pop(client.connection)
                    client.close()
                    idle_since = time.monotonic()
    finally:
        for client in clients.values():
            client.close()


def main() -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
    base = Path(sys.argv[1])
    lock_fd = int(sys.argv[2])
    options = json.load(sys.stdin)
    try:
        _serve(base, options, stop)
    finally:
        base.with_suffix(".sock").unlink(missing_ok=True)
        os.close(lock_fd)


if __name__ == "__main__":
    main()
