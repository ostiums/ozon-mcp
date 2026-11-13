"""A failed acquisition must leave the session worker usable for the next call."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

import pytest
from playwright.sync_api import BrowserContext, BrowserType, Error

from ozon_mcp import dependencies
from ozon_mcp.session.transport import OzonSession
from ozon_mcp.settings import get_settings

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("failure", ["launch", "page"])
def test_failed_start_can_retry_on_the_same_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    (tmp_path / "Default").mkdir()
    monkeypatch.setattr(get_settings(), "storefront", "public")
    monkeypatch.setattr(dependencies, "_executor", None)
    monkeypatch.setattr(dependencies, "_session", None)
    session = OzonSession(profile_dir=tmp_path)
    launches: list[int] = []
    closed: list[int] = []
    original_launch = BrowserType.launch_persistent_context
    original_close = BrowserContext.close
    original_pages = BrowserContext.pages

    def pages(context: BrowserContext) -> list[Any]:
        if len(launches) == 1 and failure == "page":
            raise RuntimeError("page creation failed")
        return original_pages.__get__(context)

    def close(context: BrowserContext) -> None:
        closed.append(threading.get_ident())
        original_close(context)

    def launch(browser: BrowserType, **kwargs: Any) -> BrowserContext:
        launches.append(threading.get_ident())
        kwargs["headless"] = True
        kwargs.pop("channel", None)
        if len(launches) == 1 and failure == "launch":
            kwargs["executable_path"] = str(tmp_path / "missing-chromium")
        return original_launch(browser, **kwargs)

    monkeypatch.setattr(BrowserType, "launch_persistent_context", launch)
    monkeypatch.setattr(BrowserContext, "pages", property(pages))
    monkeypatch.setattr(BrowserContext, "close", close)

    async def exercise() -> None:
        try:
            with pytest.raises((Error, RuntimeError), match=r"failed|executable doesn't exist"):
                await dependencies.run_blocking(session._launch_browser)
            await dependencies.run_blocking(session._launch_browser)
            assert await dependencies.run_blocking(lambda: session._page.evaluate("1 + 1")) == 2
        finally:
            await dependencies.run_blocking(session.close)

    try:
        asyncio.run(exercise())
    finally:
        dependencies.shutdown_session()
    assert len(launches) == 2
    assert launches[0] == launches[1] != threading.get_ident()
    assert closed == [launches[0]] * (2 if failure == "page" else 1)
