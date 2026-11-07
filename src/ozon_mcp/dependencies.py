"""Dependency-injection factories and the browser's owning worker thread."""

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from ozon_mcp.session.transport import OzonSession
from ozon_mcp.settings import get_settings

logger = logging.getLogger("ozon_mcp")

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


def shutdown_session(timeout: float | None = None) -> bool:
    """Close the session on its owning thread, waiting at most ``timeout`` seconds.

    Returns False when the close is still running. Profile locks are not removed
    on that path. The worker thread is not killed, so a stuck browser can keep
    the process alive. This was not retested on a deployed process.
    """
    global _session, _executor  # ruff: ignore[global-statement]
    if timeout is None:
        timeout = get_settings().shutdown_timeout
    with _factory_lock:
        executor = _executor
        session = _session
    closed = True
    try:
        if executor is not None:
            try:
                executor.submit(_close_captured, session).result(timeout=timeout)
            except TimeoutError:
                closed = False
                logger.warning("session close exceeded %.1fs; profile locks were not cleared", timeout)
        elif session is not None:
            session.close()
    finally:
        if executor is not None:
            executor.shutdown(wait=closed, cancel_futures=True)
        with _factory_lock:
            _session = _executor = None
    return closed


def _close_captured(session: OzonSession | None) -> None:
    if session is not None:
        session.close()


async def run_blocking[T](work: Callable[[], T]) -> T:
    """Await blocking session work off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(get_executor(), work)
