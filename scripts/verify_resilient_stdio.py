"""Check an installed launcher's recovery using only tools/list and session_status."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

from scripts.verify_parallel_stdio import Client


def _child_pid(supervisor_pid: int) -> int | None:
    children = Path(f"/proc/{supervisor_pid}/task/{supervisor_pid}/children").read_text(encoding="utf-8").split()
    if len(children) != 1:
        return None
    child = int(children[0])
    command = Path(f"/proc/{child}/cmdline").read_bytes().split(b"\0")
    if command[1:3] != [b"-m", b"ozon_mcp"]:
        raise RuntimeError("Launcher child is not the expected Python MCP server")
    return child


async def _replacement(supervisor_pid: int, previous: int) -> int:
    async with asyncio.timeout(20):
        while True:
            try:
                current = _child_pid(supervisor_pid)
            except FileNotFoundError:
                current = None
            if current is not None and current != previous:
                return current
            await asyncio.sleep(0.05)


async def verify(launcher: str) -> None:
    with tempfile.TemporaryDirectory(prefix="ozon-resilience-check-") as directory:
        client = Client(launcher, "installed", Path(directory))
        try:
            await client.start()
            before = await client.call("session_status")
            if client.process is None:
                raise RuntimeError("Launcher did not start")
            supervisor_pid = client.process.pid
            child = _child_pid(supervisor_pid)
            if child is None:
                raise RuntimeError("Launcher does not have exactly one MCP child")
            descriptor = os.pidfd_open(child)
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            finally:
                os.close(descriptor)
            replacement = await _replacement(supervisor_pid, child)
            listed = await client.request("tools/list", {})
            after = await client.call("session_status")
            for name in ("signed_in", "cart_enabled", "writes_enabled", "orders_enabled"):
                if after[name] != before[name]:
                    raise RuntimeError(f"Session field changed after child restart: {name}")
            if not after["cart_enabled"] or after["writes_enabled"] or after["orders_enabled"]:
                raise RuntimeError("Expected CART=1 WRITES=0 ORDERS=0")
            launcher_bytes = await asyncio.to_thread(Path(launcher).read_bytes)
            sys.stdout.write(
                json.dumps({
                    "result": "PASS",
                    "supervisor_pid": supervisor_pid,
                    "killed_child": child,
                    "replacement_child": replacement,
                    "tools": len(listed["tools"]),
                    "signed_in": after["signed_in"],
                    "cart_enabled": after["cart_enabled"],
                    "writes_enabled": after["writes_enabled"],
                    "orders_enabled": after["orders_enabled"],
                    "launcher_sha256": hashlib.sha256(launcher_bytes).hexdigest(),
                })
                + "\n"
            )
        finally:
            await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launcher")
    args = parser.parse_args()
    asyncio.run(verify(args.launcher))


if __name__ == "__main__":
    main()
