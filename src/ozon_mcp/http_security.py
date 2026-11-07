"""Authentication and DNS rebinding checks for every HTTP route."""

from __future__ import annotations

from hmac import compare_digest
from typing import TYPE_CHECKING

from mcp.server.transport_security import TransportSecurityMiddleware
from starlette.requests import HTTPConnection
from starlette.responses import Response

if TYPE_CHECKING:
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.types import ASGIApp, Receive, Scope, Send


class HTTPAccessMiddleware:
    """Require a bearer token on MCP, SSE message posts, metrics and unknown paths."""

    def __init__(self, app: ASGIApp, token: str, security: TransportSecuritySettings) -> None:
        self.app = app
        self._token = token.encode("ascii")
        self._security = TransportSecurityMiddleware(security)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = HTTPConnection(scope)
        response = await self._security.validate_request(request)
        if response is None:
            # Duplicate credentials or boundary headers are ambiguous across proxies.
            headers = request.headers
            authorization = headers.getlist("authorization")
            if len(headers.getlist("host")) != 1 or len(headers.getlist("origin")) > 1:
                response = Response("Ambiguous request headers", status_code=400)
            elif len(authorization) != 1 or not self._authorized(authorization[0]):
                response = Response("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Bearer"})
        if response is not None:
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _authorized(self, authorization: str) -> bool:
        scheme, separator, token = authorization.partition(" ")
        return bool(separator and scheme.lower() == "bearer" and compare_digest(token.encode("latin-1"), self._token))
