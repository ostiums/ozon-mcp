"""Read one public product across parallel stdio clients, a killed client, and restart."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


class Client:
    def __init__(self, launcher: str, name: str, log_dir: Path) -> None:
        self.launcher = launcher
        self.name = name
        self.log_path = log_dir / f"{name}.stderr"
        self.process: asyncio.subprocess.Process | None = None
        self.sequence = 0

    async def start(self) -> None:
        with self.log_path.open("wb") as log:
            self.process = await asyncio.create_subprocess_exec(
                self.launcher,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=log,
                start_new_session=True,
                limit=4 * 1024 * 1024,
            )
        await self.request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "parallel-runtime-verification", "version": "1"},
            },
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = await self.request("tools/list", {})
        names = {tool["name"] for tool in result["tools"]}
        if not {"session_status", "product_details"} <= names:
            raise RuntimeError("Required tools are absent")

    def send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Client is not started")
        self.process.stdin.write((json.dumps(message) + "\n").encode())

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        self.send({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
        if self.process is None or self.process.stdout is None or self.process.stdin is None:
            raise RuntimeError("Client is not started")
        await self.process.stdin.drain()
        while True:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout=120)
            if not line:
                raise RuntimeError(f"{self.name} exited; see {self.log_path}")
            response = json.loads(line)
            if response.get("id") != self.sequence:
                continue
            if "error" in response:
                raise RuntimeError(str(response["error"]))
            return response["result"]

    async def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        result = await self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(str(result))
        return result["structuredContent"]

    async def read(self, product: str, stage: str) -> None:
        status = await self.call("session_status")
        if not status["signed_in"]:
            raise RuntimeError("The persistent profile is no longer signed in")
        if not status["cart_enabled"] or status["writes_enabled"] or status["orders_enabled"]:
            raise RuntimeError("Runtime permissions differ from CART=1 WRITES=0 ORDERS=0")
        card = await self.call("product_details", sku_or_url=product)
        if not card.get("price"):
            raise RuntimeError(f"Product has no price: {card}")

        record = {"stage": stage, "client": self.name, "signed_in": status["signed_in"]}
        record.update({key: card.get(key) for key in ("sku", "title", "price", "price_regular", "seller")})
        sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    async def kill(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()

    async def close(self) -> None:
        if self.process is None or self.process.returncode is not None:
            return
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=20)
        except TimeoutError:
            await self.kill()


async def exercise(clients: list[Client], product: str) -> None:
    first, second, restarted = clients
    await asyncio.gather(first.start(), second.start())
    await asyncio.gather(first.read(product, "parallel"), second.read(product, "parallel"))
    await first.kill()
    await second.read(product, "survives-peer-sigkill")
    await restarted.start()
    await asyncio.gather(second.read(product, "peer-restarted"), restarted.read(product, "restarted"))


async def verify(launcher: str, product: str) -> None:
    with tempfile.TemporaryDirectory(prefix="ozon-stdio-verification-") as directory:
        clients = [Client(launcher, name, Path(directory)) for name in ("first", "second", "restarted")]
        try:
            await exercise(clients, product)
        except BaseException:
            for client in clients:
                if client.log_path.exists():
                    sys.stderr.write(client.log_path.read_text())
            raise
        finally:
            await asyncio.gather(*(client.close() for client in clients))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("launcher")
    parser.add_argument("--product", default="507239415", help="Canonical product URL or SKU")
    args = parser.parse_args()
    asyncio.run(verify(args.launcher, args.product))


if __name__ == "__main__":
    main()
