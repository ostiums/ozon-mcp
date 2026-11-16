"""Worker exits fail one MCP request while real request cancellation still cancels."""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import TYPE_CHECKING

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ozon_mcp import dependencies

if TYPE_CHECKING:
    from pathlib import Path

_CHILD = """
import asyncio
import sys
from ozon_mcp.__main__ import main
from ozon_mcp.models.session import SessionStatus
from ozon_mcp.services import session

failures = [getattr(asyncio, sys.argv[1]) if sys.argv[1] == 'CancelledError' else
            getattr(__import__('builtins'), sys.argv[1])]

def status():
    if failures:
        raise failures.pop()('injected worker interruption')
    return SessionStatus(signed_in=False, backup_available=False)

session.session_status = status
main()
"""


@pytest.mark.parametrize("failure", ["SystemExit", "KeyboardInterrupt", "CancelledError"])
async def test_worker_interruption_fails_one_tool_and_keeps_stdio_alive(tmp_path: Path, failure: str) -> None:
    server = StdioServerParameters(
        command=sys.executable, args=["-c", _CHILD, failure], env={"OZON_TRANSPORT": "stdio"}
    )
    with (tmp_path / "server.log").open("w") as log:
        async with asyncio.timeout(15), stdio_client(server, errlog=log) as streams, ClientSession(*streams) as client:
            await client.initialize()
            failed = await client.call_tool("session_status", {})
            assert failed.isError is True
            assert "not retried" in str(failed.content)
            assert len((await client.list_tools()).tools) == 44
            recovered = await client.call_tool("session_status", {})
            assert recovered.isError is False
            assert recovered.structuredContent is not None
            assert recovered.structuredContent["signed_in"] is False


async def test_request_cancellation_is_not_converted_into_a_worker_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies, "_executor", None)
    monkeypatch.setattr(dependencies, "_session", None)
    entered = threading.Event()
    release = threading.Event()

    def work() -> None:
        entered.set()
        assert release.wait(5)

    task = asyncio.create_task(dependencies.run_blocking(work))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await dependencies.run_blocking(lambda: 7) == 7
    finally:
        release.set()
        dependencies.shutdown_session()
