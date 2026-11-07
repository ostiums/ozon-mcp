"""One worker owns browser work through construction and final cleanup."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ozon_mcp import __main__ as entrypoint, dependencies
from ozon_mcp.settings import get_settings


def test_concurrent_first_use_creates_only_one_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    constructors: list[object] = []

    def construct(**_kwargs: object) -> object:
        executor = object()
        constructors.append(executor)
        entered.set()
        assert release.wait(2)
        return executor

    monkeypatch.setattr(dependencies, "_executor", None, raising=False)
    monkeypatch.setattr(dependencies, "ThreadPoolExecutor", construct)
    with ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(dependencies.get_executor)
        assert entered.wait(2)
        second = callers.submit(dependencies.get_executor)
        release.set()
        one, two = first.result(2), second.result(2)
    assert one is two
    assert len(constructors) == 1


def test_shutdown_closes_the_browser_on_the_thread_that_opened_it(monkeypatch: pytest.MonkeyPatch) -> None:
    threads: list[int] = []

    class Session:
        def close(self) -> None:
            threads.append(threading.get_ident())

    fake = Session()
    monkeypatch.setattr(dependencies, "_session", None, raising=False)
    monkeypatch.setattr(dependencies, "_executor", None, raising=False)
    monkeypatch.setattr(dependencies, "OzonSession", lambda: fake)

    def open_session() -> None:
        assert dependencies.get_session() is fake
        threads.append(threading.get_ident())

    asyncio.run(dependencies.run_blocking(open_session))
    dependencies.shutdown_session()
    assert len(threads) == 2
    assert threads[0] == threads[1]
    assert threads[0] != threading.get_ident()
    dependencies.shutdown_session()
    assert len(threads) == 2


def test_console_entrypoint_cleans_up_after_server_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    monkeypatch.setattr(get_settings(), "transport", "stdio")
    monkeypatch.setattr(entrypoint, "shutdown_session", lambda: closed.append(True), raising=False)

    def fail() -> None:
        raise RuntimeError("server stopped")

    monkeypatch.setattr(entrypoint.mcp, "run", fail)
    with pytest.raises(RuntimeError, match="server stopped"):
        entrypoint.main()
    assert closed == [True]
