"""Real stdio server with a headless browser and a deterministic crash rendezvous."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ozon_mcp.__main__ import main
from ozon_mcp.session import transport
from ozon_mcp.session.shared_browser import BrowserLease, acquire_browser


def _headless_browser(profile: Path, options: dict[str, Any], timeout: float) -> BrowserLease:
    return acquire_browser(profile, {**options, "headless": True}, timeout)


def _observed_status(session: transport.OzonSession) -> str | None:
    session._ensure_browser()
    lease = session._browser_lease
    assert lease is not None
    directory = Path(os.environ["CRASH_TEST_DIRECTORY"])
    pause = directory / "pause"
    pending = pause.exists()
    user = None if pending else _signed_in_user(session)
    cdp = session._context.browser.new_browser_cdp_session()
    try:
        chrome = next(
            item["id"] for item in cdp.send("SystemInfo.getProcessInfo")["processInfo"] if item["type"] == "browser"
        )
    finally:
        cdp.detach()
    state = {
        "server": os.getpid(),
        "driver": session._playwright._impl_obj._connection._transport._proc.pid,
        "owner": lease.owner_pid,
        "chrome": chrome,
        "pending": pending,
    }
    temporary = directory / "state.tmp"
    temporary.write_text(json.dumps(state))
    temporary.replace(directory / "state.json")
    if pending:
        pause.unlink()
        session._page.evaluate("() => new Promise(() => {})")
        return _signed_in_user(session)
    return user


_signed_in_user = transport.OzonSession.signed_in_user


if __name__ == "__main__":
    transport.acquire_browser = _headless_browser
    transport.OzonSession.signed_in_user = _observed_status
    main()
