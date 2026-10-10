"""Constantes et contrôles minimaux de bind local (la garde Origin/CSRF viendra en T42)."""

from collections.abc import Sequence

from fastapi.responses import JSONResponse
from starlette._utils import parse_host_header
from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware as StarletteTrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 9513


class LocalTrustedHostMiddleware(StarletteTrustedHostMiddleware):
    """Garde Host loopback et normalise le rejet dans l'enveloppe API officielle."""

    def __init__(self, app: ASGIApp, allowed_hosts: Sequence[str]) -> None:
        super().__init__(app, allowed_hosts=allowed_hosts, www_redirect=False)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            parsed_host = parse_host_header(Headers(scope=scope).get("host"))
            host = parsed_host.host.lower() if parsed_host is not None else None
            if host not in self.allowed_hosts:
                response = JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "code": "INVALID_HOST",
                            "message": "En-tête Host non autorisé.",
                            "details": {},
                        }
                    },
                )
                await response(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)
