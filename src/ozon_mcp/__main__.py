"""Console entrypoint: ``python -m ozon_mcp``.

Three transports: stdio for a client that spawns the process itself, and two
long-lived HTTP ones for a remote agent — streamable HTTP at /mcp, which is what
clients use now, and the deprecated event stream at /sse. Either way the same
port serves ``/metrics``, so one published port covers both the protocol and
scraping.
"""

import logging

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route

from ozon_mcp.dependencies import shutdown_session
from ozon_mcp.http_security import HTTPAccessMiddleware
from ozon_mcp.main import mcp
from ozon_mcp.mcp_server import _transport_security
from ozon_mcp.settings import get_settings
from ozon_mcp.shutdown import install_term_stdin_unblock
from ozon_mcp.utils.observability import METRICS_PATH, metrics_endpoint

logger = logging.getLogger("ozon_mcp")


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore", "curl_cffi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _http_app() -> Starlette:
    """Construct the authenticated HTTP/SSE app before opening its listener."""
    settings = get_settings()
    settings.validate_http_security()
    security = _transport_security()
    mcp.settings.transport_security = security
    mcp.settings.host = settings.host
    mcp.settings.port = settings.port
    app = mcp.streamable_http_app() if settings.transport == "http" else mcp.sse_app()
    app.routes.append(Route(METRICS_PATH, metrics_endpoint))
    # Validation above guarantees a token. All paths share the same boundary,
    # including /messages/ for SSE and /metrics.
    token = settings.auth_token.get_secret_value() if settings.auth_token is not None else ""
    app.add_middleware(HTTPAccessMiddleware, token=token, security=security)
    return app


def main() -> None:
    _configure_logging()
    settings = get_settings()
    try:
        if settings.transport == "stdio":
            install_term_stdin_unblock()
            mcp.run()
            return

        # /mcp is streamable HTTP, what clients use now; /sse is the older transport
        # the spec has deprecated, kept for clients that still speak only that.
        streaming = settings.transport == "http"
        app = _http_app()
        logger.info(
            "serving mcp on http://%s:%s%s (metrics at %s)",
            settings.host,
            settings.port,
            "/mcp" if streaming else "/sse",
            METRICS_PATH,
        )
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    finally:
        shutdown_session()


if __name__ == "__main__":
    main()
