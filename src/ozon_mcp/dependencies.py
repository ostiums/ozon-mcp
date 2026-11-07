"""Dependency-injection factories and the browser's owning worker thread."""

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from ozon_mcp.session.transport import OzonSession

_factory_lock = threading.RLock()
_session: OzonSession | None = None
_executor: ThreadPoolExecutor | None = None


def get_session() -> OzonSession:
    """The process-wide session; closed by shutdown_session before interpreter exit."""
    global _session  # ruff: ignore[global-statement]
    with _factory_lock:
        if _session is None:
            _session = OzonSession()
        return _session


def get_executor() -> ThreadPoolExecutor:
    """The single thread every session operation runs on.

    Playwright's sync API cannot run in an asyncio loop, and browser objects
    must be closed on the thread that created them. Serialize first-use too:
    functools.cache alone can construct competing workers on concurrent misses.
    """
    global _executor  # ruff: ignore[global-statement]
    with _factory_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ozon-session")
        return _executor


def shutdown_session() -> None:
    """Drain work and close Chromium on its owning thread after the server stops.

    A normal atexit callback runs after ThreadPoolExecutor's interpreter-exit
    hook and on the main thread, so it cannot safely flush a Playwright profile.
    Embedders must call this after stopping requests, as the CLI does.
    """
    global _session, _executor  # ruff: ignore[global-statement]
    with _factory_lock:
        executor = _executor
    try:
        if executor is not None:
            executor.submit(_close_session).result()
        else:
            _close_session()
    finally:
        if executor is not None:
            executor.shutdown(wait=True)
        with _factory_lock:
            _session = _executor = None


def _close_session() -> None:
    with _factory_lock:
        session = _session
    if session is not None:
        session.close()


async def run_blocking[T](work: Callable[[], T]) -> T:
    """Await blocking session work off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(get_executor(), work)
