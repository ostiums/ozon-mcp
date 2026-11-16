from __future__ import annotations

import json
import os
import select
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

CHILD = """
import json, os, sys, time
from pathlib import Path
state = Path(os.environ["FAKE_STATE"])
count = int(state.read_text()) + 1 if state.exists() else 1
state.write_text(str(count))
ready = False
initialized = False
mode = os.environ.get("FAKE_MODE", "")
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        if mode == "first_init_crash" and count == 1:
            os._exit(9)
        if mode == "replay_hang" and count == 2:
            continue
        ready = True
        result = {"protocolVersion": "2025-11-25", "capabilities": {}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "notifications/initialized":
        initialized = True
        continue
    elif method == "hold":
        continue
    elif method == "crash":
        with open(str(state) + ".writes", "a") as output:
            output.write("write\\n")
        os._exit(7)
    elif method == "bad_stdout":
        print("not protocol", flush=True)
        continue
    elif method == "flood":
        for _ in range(1000):
            notice = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": "x" * 100000}}
            print(json.dumps(notice), flush=True)
        continue
    else:
        result = {"pid": os.getpid(), "ready": ready, "initialized": initialized, "generation": count}
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
"""


class PipeClient:
    def __init__(self, tmp_path: Path, mode: str = "", executable: Path | None = None) -> None:
        self.script = tmp_path / "child.py"
        self.script.write_text(CHILD)
        self.state = tmp_path / "starts"
        self.log = tmp_path / "supervisor.log"
        self.buffer = bytearray()
        self.messages: list[dict[str, Any]] = []
        bootstrap = (
            "import logging,sys; from ozon_mcp.stdio_supervisor import Supervisor; "
            f"logging.basicConfig(filename={str(self.log)!r},level=logging.INFO); "
            f"log=open({str(self.log)!r},'ab',buffering=0); "
            f"Supervisor([{str(executable or sys.executable)!r},{str(self.script)!r}],"
            "log,handshake_timeout=.4,restart_delay=.03).run()"
        )
        self.process = subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
            [sys.executable, "-c", bootstrap],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "FAKE_STATE": str(self.state), "FAKE_MODE": mode},
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        self.process.stdin.flush()

    def request(self, request_id: int | str, method: str, **params: Any) -> dict[str, Any]:
        self.send({"id": request_id, "method": method, "params": params})
        return self.receive(request_id)

    def receive(self, request_id: int | str, timeout: float = 5) -> dict[str, Any]:
        assert self.process.stdout is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for index, message in enumerate(self.messages):
                if message.get("id") == request_id:
                    return self.messages.pop(index)
            readable, _, _ = select.select([self.process.stdout], [], [], max(0, deadline - time.monotonic()))
            if not readable:
                break
            data = os.read(self.process.stdout.fileno(), 65536)
            assert data, self.process.stderr.read().decode() if self.process.stderr else "supervisor exited"
            self.buffer.extend(data)
            while b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                self.messages.append(json.loads(line))
        pytest.fail(f"No response for {request_id}; log={self.log.read_text()}")

    def initialize(self) -> None:
        assert "result" in self.request(1, "initialize", protocolVersion="2025-11-25", capabilities={})
        self.send({"method": "notifications/initialized"})

    def recovered(self, request_id: int = 100) -> dict[str, Any]:
        result = self.request(request_id, "tools/list")
        assert "result" in result, result
        assert result["result"]["ready"]
        assert result["result"]["initialized"]
        status = self.request(request_id + 1, "tools/call", name="session_status")
        assert status["result"] == result["result"]
        return result["result"]

    def close(self) -> None:
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[PipeClient]:
    pipe = PipeClient(tmp_path)
    try:
        pipe.initialize()
        yield pipe
    finally:
        pipe.close()


def test_child_kill_between_calls_keeps_the_same_host_pipe(client: PipeClient) -> None:
    old = client.recovered()
    os.kill(old["pid"], signal.SIGKILL)
    time.sleep(0.2)
    new = client.recovered(200)
    assert old["pid"] != new["pid"]
    assert client.process.poll() is None


def test_child_kill_fails_all_pending_and_never_replays_work(client: PipeClient) -> None:
    client.send({"id": "pending-a", "method": "hold"})
    client.send({"id": "pending-b", "method": "hold"})
    failure = client.request(2, "crash")
    assert failure["error"]["code"] == -32001
    assert client.receive("pending-a")["error"]["code"] == -32001
    assert client.receive("pending-b")["error"]["code"] == -32001
    client.recovered()
    assert Path(str(client.state) + ".writes").read_text(encoding="utf-8") == "write\n"
    assert not client.messages


def test_requests_during_restart_wait_for_initialize_replay(client: PipeClient) -> None:
    assert "error" in client.request(2, "crash")
    client.send({"id": 10, "method": "tools/list"})
    client.send({"id": 11, "method": "tools/call", "params": {"name": "session_status"}})
    for request_id in (10, 11):
        response = client.receive(request_id)
        assert response["result"]["ready"]
        assert response["result"]["initialized"]


def test_initialize_failure_can_be_retried_without_reopening_pipe(tmp_path: Path) -> None:
    client = PipeClient(tmp_path, "first_init_crash")
    try:
        assert client.request(1, "initialize")["error"]["code"] == -32001
        client.initialize()
        client.recovered()
    finally:
        client.close()


def test_child_kill_before_initialized_replays_only_when_received(tmp_path: Path) -> None:
    client = PipeClient(tmp_path)
    try:
        assert "result" in client.request(1, "initialize")
        old = client.request(2, "ping")["result"]
        os.kill(old["pid"], signal.SIGKILL)
        time.sleep(0.2)
        client.send({"method": "notifications/initialized"})
        client.recovered()
    finally:
        client.close()


def test_replay_timeout_fails_requests_then_recovers_on_same_pipe(tmp_path: Path) -> None:
    client = PipeClient(tmp_path, "replay_hang")
    try:
        client.initialize()
        assert "error" in client.request(2, "crash")
        assert client.request(3, "tools/list")["error"]["code"] == -32001
        assert client.recovered()["generation"] == 3
    finally:
        client.close()


def test_child_invalid_stdout_never_reaches_the_host(client: PipeClient) -> None:
    assert client.request(2, "bad_stdout")["error"]["code"] == -32001
    client.recovered()


@pytest.mark.parametrize("stop", ["eof", "sigterm", "sigint"])
def test_host_shutdown_reaps_child_without_restarting(client: PipeClient, stop: str) -> None:
    old = client.recovered()
    if stop == "eof":
        assert client.process.stdin is not None
        client.process.stdin.close()
    else:
        client.process.send_signal(signal.SIGTERM if stop == "sigterm" else signal.SIGINT)
    assert client.process.wait(timeout=5) == 0
    with pytest.raises(ProcessLookupError):
        os.kill(old["pid"], 0)
    assert client.state.read_text() == "1"


def test_repeated_child_crashes_do_not_close_host_connection(client: PipeClient) -> None:
    for request_id in range(10, 14):
        assert client.request(request_id, "crash")["error"]["code"] == -32001
        client.recovered(request_id * 100)
    assert Path(str(client.state) + ".writes").read_text(encoding="utf-8").splitlines() == ["write"] * 4


def test_missing_child_executable_can_be_repaired_without_reopening_pipe(tmp_path: Path) -> None:
    executable = tmp_path / "python-missing"
    client = PipeClient(tmp_path, executable=executable)
    try:
        assert client.request(1, "initialize")["error"]["code"] == -32001
        executable.symlink_to(sys.executable)
        client.initialize()
        client.recovered()
    finally:
        client.close()


def test_sigpipe_does_not_terminate_supervisor(client: PipeClient) -> None:
    client.process.send_signal(signal.SIGPIPE)
    client.recovered()


def test_frozen_child_cannot_prevent_host_eof_shutdown(client: PipeClient) -> None:
    pid = client.recovered()["pid"]
    os.kill(pid, signal.SIGSTOP)
    assert client.process.stdin is not None
    client.process.stdin.close()
    assert client.process.wait(timeout=5) == 0
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_host_eof_stops_supervisor_even_when_host_does_not_drain_stdout(client: PipeClient) -> None:
    pid = client.recovered()["pid"]
    client.send({"id": 2, "method": "flood"})
    deadline = time.monotonic() + 5
    while "paused host reads" not in client.log.read_text(encoding="utf-8"):
        assert time.monotonic() < deadline, "Supervisor did not reach stdout backpressure"
        time.sleep(0.02)
    assert client.process.stdin is not None
    client.process.stdin.close()
    assert client.process.wait(timeout=5) == 0
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
