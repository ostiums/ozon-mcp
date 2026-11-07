"""Shutdown waits only for the configured bound and can unblock stdin."""

from __future__ import annotations

import os
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
import textwrap
import time
from typing import TYPE_CHECKING

import pytest

from ozon_mcp import dependencies
from ozon_mcp.shutdown import close_stdio_input

if TYPE_CHECKING:
    from pathlib import Path


def test_close_stdio_input_unblocks_a_pipe_reader() -> None:
    read_fd, write_fd = os.pipe()

    class _Stream:
        def fileno(self) -> int:
            return read_fd

    close_stdio_input(_Stream())
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.read(read_fd, 1)
    os.close(write_fd)


def test_shutdown_returns_while_a_close_is_still_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    finished: list[bool] = []
    lock = tmp_path / "SingletonLock"
    lock.write_text("keep", encoding="utf-8")

    class Session:
        def close(self) -> None:
            time.sleep(1.5)
            finished.append(True)
            lock.unlink(missing_ok=True)

    monkeypatch.setattr(dependencies, "_session", Session(), raising=False)
    monkeypatch.setattr(dependencies, "_executor", None, raising=False)
    executor = dependencies.get_executor()
    assert executor is not None
    started = time.monotonic()
    assert dependencies.shutdown_session(timeout=0.2) is False
    assert time.monotonic() - started < 1
    assert finished == []
    assert lock.read_text(encoding="utf-8") == "keep"
    assert dependencies._session is None
    assert dependencies._executor is None


def test_sigterm_unblocks_an_anyio_stdin_read() -> None:
    script = textwrap.dedent(
        """
        import sys
        from io import TextIOWrapper
        import anyio
        from ozon_mcp.shutdown import install_term_stdin_unblock

        install_term_stdin_unblock()

        async def main() -> None:
            stdin = anyio.wrap_file(TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"))
            sys.stdout.write("ready\\n")
            sys.stdout.flush()
            async for _line in stdin:
                pass
            sys.stdout.write("eof\\n")
            sys.stdout.flush()

        anyio.run(main)
        """
    )
    process = subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true]
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline() == "ready\n"
    process.send_signal(signal.SIGTERM)
    try:
        code = process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
        raise
    # Closing stdin unblocks AnyIO's worker read with EBADF. The process must
    # exit either way. A clean EOF is not what this library version produces.
    assert process.stderr is not None
    error = process.stderr.read()
    assert code == 0 or "Bad file descriptor" in error
