"""Bounded shutdown helpers. A stuck browser thread is not killed."""

import os
import signal
import sys
from typing import IO, Any


def close_stdio_input(stream: IO[Any] | None = None) -> None:
    """Close one input stream so a blocked read can return."""
    target = sys.stdin if stream is None else stream
    try:
        os.close(target.fileno())
    except OSError:
        return


def install_term_stdin_unblock() -> None:
    """On SIGTERM, unblock a stdio read. Do not raise from the handler.

    AnyIO reads stdin on a worker thread. Raising KeyboardInterrupt on the main
    thread does not cancel that read. Closing the descriptor does. This was not
    retested against a deployed process.
    """

    def _handler(signum: int, frame: object) -> None:
        del signum, frame
        close_stdio_input()

    signal.signal(signal.SIGTERM, _handler)
