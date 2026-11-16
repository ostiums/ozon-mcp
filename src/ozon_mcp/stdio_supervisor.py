"""Keep a host's JSON-lines stdio transport alive across server process exits."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import select
import selectors
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

logger = logging.getLogger("ozon_mcp.supervisor")
Json = dict[str, Any]
MAX_BUFFER = 8 * 1024 * 1024
MAX_PENDING = 1024


@dataclass
class Child:
    process: subprocess.Popen[bytes]
    incoming: bytearray = field(default_factory=bytearray)
    outgoing: bytearray = field(default_factory=bytearray)
    replay_id: str | None = None
    deadline: float = 0
    server_requests: set[str] = field(default_factory=set)


class Supervisor:
    """Own the host connection independently of the current child generation."""

    def __init__(
        self,
        command: list[str],
        log: BinaryIO,
        *,
        handshake_timeout: float = 15,
        restart_delay: float = 0.25,
    ) -> None:
        self.command = command
        self.log = log
        self.handshake_timeout = handshake_timeout
        self.restart_delay = restart_delay
        self.selector = selectors.DefaultSelector()
        self.child: Child | None = None
        self.incoming = bytearray()
        self.outgoing = bytearray()
        self.pending: dict[str, Json] = {}
        self.queued: deque[Json] = deque()
        self.queued_bytes = 0
        self.initialize: Json | None = None
        self.initialized: Json | None = None
        self.restart_at = 0.0
        self.failures = 0
        self.stopping = False

    def run(self) -> None:
        os.set_blocking(sys.stdin.fileno(), False)
        os.set_blocking(sys.stdout.fileno(), False)
        self.selector.register(sys.stdin.fileno(), selectors.EVENT_READ, "host")
        host_hangup = select.poll()
        host_hangup.register(sys.stdin.fileno(), select.POLLHUP | select.POLLERR)
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, self._stop)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        try:
            while not self.stopping:
                if len(self.outgoing) >= MAX_BUFFER and host_hangup.poll(0):
                    logger.info("host stdin closed while stdout was backpressured")
                    break
                self._tick()
                self._read_interest(sys.stdin.fileno(), "host")
                if self.child is not None and self.child.process.stdout is not None:
                    self._read_interest(self.child.process.stdout.fileno(), "child")
                for key, mask in self.selector.select(timeout=0.1):
                    self._event(key.fd, key.data, mask)
                    if self.stopping:
                        break
        finally:
            self._close_child()
            self.selector.close()

    def _read_interest(self, fd: int, source: str) -> None:
        registered = fd in self.selector.get_map()
        if len(self.outgoing) >= MAX_BUFFER:
            if registered:
                self.selector.unregister(fd)
                logger.info("paused %s reads while host stdout is backpressured", source)
        elif not registered:
            self.selector.register(fd, selectors.EVENT_READ, source)

    def _stop(self, signum: int, _frame: object) -> None:
        logger.info("host signal %s", signum)
        self.stopping = True

    def _tick(self) -> None:
        child = self.child
        if child is not None:
            if child.process.poll() is not None:
                while len(self.outgoing) < MAX_BUFFER and self._read_child():
                    pass
                if self.child is not None:
                    self._failed(f"child exited with status {child.process.returncode}")
            elif child.deadline and time.monotonic() >= child.deadline:
                self._failed("initialize timed out")
        if self.child is None and time.monotonic() >= self.restart_at:
            self._start_child()

    def _start_child(self) -> None:
        try:
            process = subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.log,
                start_new_session=True,
                bufsize=0,
            )
        except OSError:
            logger.exception("could not start child")
            self._failed("child could not start")
            return
        child = self.child = Child(process)
        logger.info("started child pid=%s", process.pid)
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("child pipes were not created")
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        self.selector.register(process.stdout.fileno(), selectors.EVENT_READ, "child")
        if self.initialize is not None:
            child.replay_id = f"ozon-supervisor-{uuid.uuid4().hex}"
            child.deadline = time.monotonic() + self.handshake_timeout
            self._to_child({**self.initialize, "id": child.replay_id})
        else:
            self._release_queue()

    def _event(self, fd: int, source: str, mask: int) -> None:
        if source == "host":
            data = os.read(fd, 65536)
            if not data:
                logger.info("host stdin EOF")
                self.stopping = True
                return
            self.incoming.extend(data)
            if len(self.incoming) > MAX_BUFFER:
                self.incoming.clear()
                self._error(None, -32600, "Request exceeds supervisor buffer limit")
                return
            while b"\n" in self.incoming:
                line, _, rest = self.incoming.partition(b"\n")
                self.incoming = bytearray(rest)
                self._host_line(line)
        elif source == "child":
            self._read_child()
        elif mask & selectors.EVENT_WRITE:
            self._write(fd, source)

    def _write(self, fd: int, source: str) -> None:
        buffer = self.outgoing if source == "output" else self.child.outgoing if self.child else bytearray()
        try:
            written = os.write(fd, buffer)
        except BlockingIOError:
            return
        except OSError:
            if source == "output":
                self.stopping = True
            else:
                self._failed("child stdin closed")
            return
        del buffer[:written]
        if not buffer:
            with contextlib.suppress(KeyError):
                self.selector.unregister(fd)

    def _host_line(self, line: bytes | bytearray) -> None:
        try:
            message = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            self._error(None, -32700, "Invalid JSON")
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self._error(None, -32600, "Invalid JSON-RPC message")
            return
        request_id = message.get("id")
        if not isinstance(request_id, (str, int, type(None))) or isinstance(request_id, bool):
            self._error(None, -32600, "Invalid request id")
            return
        if "method" in message and not isinstance(message["method"], str):
            self._error(request_id, -32600, "Invalid method")
            return
        self._host_message(message)

    def _host_message(self, message: Json) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if method is None:
            key = json.dumps(request_id)
            if self.child is not None and key in self.child.server_requests:
                self.child.server_requests.remove(key)
                self._to_child(message)
            return
        if len(self.pending) >= MAX_PENDING:
            if "id" in message:
                self._error(request_id, -32000, "Too many pending requests")
            return
        if "id" in message:
            key = json.dumps(request_id)
            if key in self.pending:
                self._error(request_id, -32600, "Request id already in use")
                return
            self.pending[key] = message
        if method == "notifications/initialized":
            self.initialized = message
        child = self.child
        if child is None or child.replay_id is not None:
            self._queue(message)
        else:
            if method == "initialize":
                child.deadline = time.monotonic() + self.handshake_timeout
            self._to_child(message)

    def _queue(self, message: Json) -> None:
        size = len(json.dumps(message).encode())
        if self.queued_bytes + size > MAX_BUFFER:
            if "id" in message:
                self.pending.pop(json.dumps(message["id"]), None)
                self._error(message["id"], -32000, "Restart queue is full")
            return
        self.queued.append(message)
        self.queued_bytes += size

    def _read_child(self) -> bool:
        child = self.child
        if child is None or child.process.stdout is None:
            return False
        try:
            data = os.read(child.process.stdout.fileno(), 65536)
        except BlockingIOError:
            return False
        if not data:
            self._failed(f"child stdout closed; status={child.process.poll()}")
            return False
        child.incoming.extend(data)
        if len(child.incoming) > MAX_BUFFER:
            self._failed("child response exceeds supervisor buffer limit")
            return False
        while b"\n" in child.incoming:
            line, _, rest = child.incoming.partition(b"\n")
            child.incoming = bytearray(rest)
            self._child_line(child, line)
            if self.child is not child:
                return False
        return True

    def _child_line(self, child: Child, line: bytes | bytearray) -> None:
        try:
            message = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            self._failed("child emitted invalid JSON")
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self._failed("child emitted invalid JSON-RPC")
            return
        self._child_message(child, message)

    def _child_message(self, child: Child, message: Json) -> None:
        request_id = message.get("id")
        if child.replay_id is not None and request_id == child.replay_id and "method" not in message:
            if "result" not in message:
                self._failed("initialize replay failed")
                return
            child.replay_id = None
            child.deadline = 0
            if self.initialized is not None:
                self._to_child(self.initialized)
            self._release_queue()
            logger.info("child pid=%s restored initialization", child.process.pid)
            return
        if "method" in message:
            if "id" in message:
                child.server_requests.add(json.dumps(request_id))
        else:
            original = self.pending.pop(json.dumps(request_id), None)
            if original is None:
                logger.warning("discarded response without pending host request")
                return
            if original.get("method") == "initialize":
                child.deadline = 0
                if "result" in message:
                    self.initialize = original
            self.failures = 0
        self._to_host(message)

    def _release_queue(self) -> None:
        self.queued_bytes = 0
        while self.queued:
            message = self.queued.popleft()
            if message.get("method") == "notifications/initialized" and self.initialize is not None:
                continue
            if message.get("method") == "initialize" and self.child is not None:
                self.child.deadline = time.monotonic() + self.handshake_timeout
            self._to_child(message)

    def _to_child(self, message: Json) -> None:
        child = self.child
        if child is None or child.process.stdin is None:
            return
        child.outgoing.extend(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        if len(child.outgoing) > MAX_BUFFER:
            self._failed("child is not consuming input")
            return
        self._watch_write(child.process.stdin.fileno(), "input")

    def _to_host(self, message: Json) -> None:
        self.outgoing.extend(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        self._watch_write(sys.stdout.fileno(), "output")

    def _watch_write(self, fd: int, source: str) -> None:
        if fd not in self.selector.get_map():
            self.selector.register(fd, selectors.EVENT_WRITE, source)

    def _error(self, request_id: object, code: int, message: str) -> None:
        self._to_host({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def _failed(self, reason: str) -> None:
        logger.warning("%s; pending=%s", reason, len(self.pending))
        self._close_child()
        for request in self.pending.values():
            self._error(
                request["id"], -32001, "Ozon server restarted; request outcome is unknown. Do not retry writes blindly."
            )
        self.pending.clear()
        self.queued.clear()
        self.queued_bytes = 0
        self.failures += 1
        self.restart_at = time.monotonic() + min(5.0, self.restart_delay * 2 ** min(self.failures - 1, 5))

    def _close_child(self) -> None:
        child, self.child = self.child, None
        if child is None:
            return
        process = child.process
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                with contextlib.suppress(KeyError):
                    self.selector.unregister(stream.fileno())
                stream.close()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            logger.warning("child pid=%s ignored SIGTERM", process.pid)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        logger.info("reaped child pid=%s status=%s", process.pid, process.returncode)


def main() -> None:
    directory = (
        Path(sys.prefix).parent / "logs" if sys.prefix != sys.base_prefix else Path.home() / ".local/state/ozon-mcp"
    )
    path = Path(os.environ.get("OZON_SUPERVISOR_LOG", str(directory / "stdio-supervisor.log")))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.close(descriptor)
    with path.open("ab", buffering=0) as child_log:
        handler = logging.FileHandler(path)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            Supervisor([sys.executable, "-m", "ozon_mcp", *sys.argv[1:]], child_log).run()
        finally:
            handler.close()
            logger.removeHandler(handler)


if __name__ == "__main__":
    main()
