"""A single MCP pipe survives real server, browser-owner, and Chrome deaths."""

from __future__ import annotations

import asyncio
import json
import os
import select
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

_CHILD = Path(__file__).resolve().parents[1] / "stdio_crash_child.py"


class CrashClient:
    def __init__(self, directory: Path, *, supervised: bool) -> None:
        self.directory = directory
        self.supervised = supervised
        self.sequence = 0
        self.process: asyncio.subprocess.Process | None = None
        self.owned: dict[int, int] = {}

    async def start(self) -> None:
        profile = self.directory / "profile"
        (profile / "Default").mkdir(parents=True)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("OZON_")}
        environment.update({
            "OZON_STOREFRONT": "public",
            "OZON_PROFILE_DIR": str(profile),
            "OZON_PROFILE_BACKUP": str(self.directory / "backup"),
            "OZON_STATE_PATH": str(self.directory / "seed.json"),
            "OZON_ENABLE_CART": "1",
            "OZON_ENABLE_WRITES": "0",
            "OZON_ENABLE_ORDERS": "0",
            "OZON_PUBLIC_READ_TIMEOUT": "10",
            "OZON_SUPERVISOR_LOG": str(self.directory / "supervisor.log"),
            "CRASH_TEST_DIRECTORY": str(self.directory),
        })
        command = [sys.executable, str(_CHILD)]
        if self.supervised:
            command = [
                sys.executable,
                "-c",
                (
                    "from ozon_mcp.stdio_supervisor import Supervisor; import os, sys; "
                    "log = open(os.environ['OZON_SUPERVISOR_LOG'], 'ab', buffering=0); "
                    "Supervisor(sys.argv[1:], log).run()"
                ),
                *command,
            ]
        with (self.directory / "stderr.log").open("wb") as stderr:
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                env=environment,
                start_new_session=True,
                limit=4 * 1024 * 1024,
            )
        self.send(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "crash-matrix", "version": "1"},
            },
        )
        assert "result" in await self.receive()
        self.write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def write(self, message: dict[str, Any]) -> None:
        assert self.process is not None
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(message) + "\n").encode())

    def send(self, method: str, params: dict[str, Any]) -> None:
        self.sequence += 1
        self.write({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})

    async def receive(self) -> dict[str, Any]:
        assert self.process is not None
        assert self.process.stdout is not None
        while True:
            line = await asyncio.wait_for(self.process.stdout.readline(), 30)
            assert line, (self.directory / "stderr.log").read_text()
            response = json.loads(line)
            assert response["jsonrpc"] == "2.0"
            if "id" in response:
                assert response["id"] == self.sequence, response
                return response

    async def healthy(self) -> dict[str, Any]:
        self.send("tools/list", {})
        tools = (await self.receive())["result"]["tools"]
        assert len(tools) == 44
        assert "session_status" in {tool["name"] for tool in tools}
        self.send("tools/call", {"name": "session_status", "arguments": {}})
        result = (await self.receive())["result"]
        assert not result.get("isError"), result
        status = result["structuredContent"]
        assert status["signed_in"] is False
        assert status["cart_enabled"] is True
        assert status["writes_enabled"] is False
        assert status["orders_enabled"] is False
        state = self.state()
        for key in ("owner", "chrome"):
            pid = state[key]
            if pid not in self.owned:
                self.owned[pid] = os.pidfd_open(pid)
        return state

    def state(self) -> dict[str, Any]:
        return json.loads((self.directory / "state.json").read_text())

    async def pending_status(self) -> None:
        (self.directory / "pause").touch()
        self.send("tools/call", {"name": "session_status", "arguments": {}})
        await asyncio.to_thread(self._wait_pending)

    def _wait_pending(self) -> None:
        deadline = time.monotonic() + 10
        while not self.state()["pending"]:
            assert time.monotonic() < deadline, "session_status did not enter its browser call"
            time.sleep(0.01)

    async def kill(self, pid: int) -> None:
        descriptor = os.pidfd_open(pid)
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            readable, _, _ = await asyncio.to_thread(select.select, [descriptor], [], [], 10)
            assert readable, "Killed process did not exit"
        finally:
            os.close(descriptor)

    async def close(self) -> None:
        if self.process is not None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        for descriptor in self.owned.values():
            with suppress(ProcessLookupError):
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        for descriptor in self.owned.values():
            try:
                readable, _, _ = await asyncio.to_thread(select.select, [descriptor], [], [], 5)
                if not readable:
                    with suppress(ProcessLookupError):
                        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            finally:
                os.close(descriptor)


@pytest.mark.parametrize("supervised", [False, True], ids=["bare", "supervised"])
@pytest.mark.parametrize("target", ["owner", "chrome", "driver"])
@pytest.mark.parametrize("during_call", [False, True], ids=["between", "during"])
async def test_browser_death_preserves_the_stdio_session(
    tmp_path: Path, *, supervised: bool, target: str, during_call: bool
) -> None:
    client = CrashClient(tmp_path, supervised=supervised)
    try:
        await client.start()
        before = await client.healthy()
        if during_call:
            await client.pending_status()
        await client.kill(before[target])
        if target == "driver" and not during_call:
            client.send("tools/call", {"name": "session_status", "arguments": {}})
        if during_call or target == "driver":
            response = await client.receive()
            assert response["result"]["isError"] is True
        after = await client.healthy()
        assert after["server"] == before["server"]
        if target == "driver":
            assert after["owner"] == before["owner"]
            assert after["chrome"] == before["chrome"]
            assert after["driver"] != before["driver"]
        else:
            assert after["owner"] != before["owner"]
            assert after["chrome"] != before["chrome"]
    finally:
        await client.close()


@pytest.mark.parametrize("during_call", [False, True], ids=["between", "during"])
async def test_server_death_preserves_the_supervisor_pipe(tmp_path: Path, *, during_call: bool) -> None:
    client = CrashClient(tmp_path, supervised=True)
    try:
        await client.start()
        before = await client.healthy()
        if during_call:
            await client.pending_status()
        await client.kill(before["server"])
        if during_call:
            response = await client.receive()
            assert response["error"]["code"] == -32001
        after = await client.healthy()
        assert after["server"] != before["server"]
    finally:
        await client.close()
