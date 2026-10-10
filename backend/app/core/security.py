"""Garde-fous réseau pour une API financière strictement locale."""

from collections.abc import Sequence
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse, Response
from starlette._utils import parse_host_header
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 9513
DEFAULT_ALLOWED_ORIGINS = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://[::1]:5173",
    "http://127.0.0.1:9513",
    "http://localhost:9513",
    "http://[::1]:9513",
)
_MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
_CORS_METHODS = frozenset({"GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"})
_CORS_HEADERS = frozenset({"content-type", "idempotency-key"})


def normalize_origin(value: str, *, allow_path: bool = False) -> str | None:
    """Return a canonical HTTP(S) origin, or ``None`` for malformed input."""
    try:
        parsed = urlsplit(value.strip())
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        scheme not in {"http", "https"}
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or (not allow_path and (parsed.path or parsed.query or parsed.fragment))
    ):
        return None
    host = host.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 80 if scheme == "http" else 443
    port_part = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{host}{port_part}"


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": {}}},
    )


class LocalSecurityMiddleware:
    """Validate Host and mutation sources, then apply local CORS and security headers."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_origins: Sequence[str],
        bind_host: str,
        bind_port: int,
    ) -> None:
        self.app = app
        self.allowed_origins = frozenset(
            origin for value in allowed_origins if (origin := normalize_origin(value)) is not None
        )
        self.allowed_hosts = {bind_host.strip("[]").lower(), "localhost"}
        self.allowed_ports: set[int] = {bind_port}
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if parsed.hostname is not None:
                self.allowed_hosts.add(parsed.hostname.strip("[]").lower())
            if parsed.port is not None:
                self.allowed_ports.add(parsed.port)

    def _host_is_allowed(self, host_header: str | None) -> bool:
        parsed = parse_host_header(host_header)
        if parsed is None or parsed.host.lower() not in self.allowed_hosts:
            return False
        return parsed.port is None or parsed.port in {str(port) for port in self.allowed_ports}

    def _source_origin(self, headers: Headers) -> str | None:
        origins = headers.getlist("origin")
        if len(origins) > 1:
            return None
        if origins:
            return normalize_origin(origins[0])
        referers = headers.getlist("referer")
        if len(referers) != 1:
            return None
        return normalize_origin(referers[0], allow_path=True)

    def _secure_send(self, scope: Scope, headers: Headers, send: Send) -> Send:
        request_origin: str | None = None
        origins = headers.getlist("origin")
        if len(origins) == 1:
            normalized = normalize_origin(origins[0])
            if normalized in self.allowed_origins:
                request_origin = normalized

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                response_headers = list(message.get("headers", []))

                def set_header(name: str, value: str) -> None:
                    nonlocal response_headers
                    encoded_name = name.lower().encode("latin-1")
                    encoded_value = value.encode("latin-1")
                    response_headers = [
                        (key, item) for key, item in response_headers if key.lower() != encoded_name
                    ]
                    response_headers.append((encoded_name, encoded_value))

                set_header("X-Content-Type-Options", "nosniff")
                set_header("Referrer-Policy", "no-referrer")
                set_header("X-Frame-Options", "DENY")
                if scope.get("path", "").startswith("/api/"):
                    set_header("Cache-Control", "no-store")
                content_type = next(
                    (
                        value.decode("latin-1").lower()
                        for key, value in response_headers
                        if key.lower() == b"content-type"
                    ),
                    "",
                )
                if content_type.startswith("text/html"):
                    set_header(
                        "Content-Security-Policy",
                        "default-src 'none'; script-src 'self'; style-src 'self'; "
                        "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
                        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; "
                        "object-src 'none'",
                    )
                if request_origin is not None:
                    set_header("Access-Control-Allow-Origin", request_origin)
                    vary = next(
                        (
                            value.decode("latin-1")
                            for key, value in response_headers
                            if key.lower() == b"vary"
                        ),
                        "",
                    )
                    vary_items = [item.strip() for item in vary.split(",") if item.strip()]
                    if "Origin" not in {item.title() for item in vary_items}:
                        vary_items.append("Origin")
                    set_header("Vary", ", ".join(vary_items))
                message["headers"] = response_headers
            await send(message)

        return send_with_headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        guarded_send = self._secure_send(scope, headers, send)
        if not self._host_is_allowed(headers.get("host")):
            await _error(400, "INVALID_HOST", "En-tête Host non autorisé.")(
                scope, receive, guarded_send
            )
            return

        method = scope["method"].upper()
        raw_origin = headers.get("origin")
        if method == "OPTIONS" and raw_origin is not None:
            origin = self._source_origin(headers)
            requested_method = headers.get("access-control-request-method", "").upper()
            requested_headers = {
                item.strip().lower()
                for item in headers.get("access-control-request-headers", "").split(",")
                if item.strip()
            }
            if (
                origin not in self.allowed_origins
                or requested_method not in _CORS_METHODS
                or not requested_headers.issubset(_CORS_HEADERS)
            ):
                await _error(403, "ORIGIN_NOT_ALLOWED", "Origine non autorisée.")(
                    scope, receive, guarded_send
                )
                return
            await Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": ", ".join(sorted(_CORS_METHODS)),
                    "Access-Control-Allow-Headers": ", ".join(sorted(_CORS_HEADERS)),
                    "Access-Control-Max-Age": "600",
                    "Vary": "Origin, Access-Control-Request-Method, Access-Control-Request-Headers",
                },
            )(scope, receive, guarded_send)
            return

        if method in _MUTATING_METHODS:
            origin = self._source_origin(headers)
            if origin not in self.allowed_origins:
                await _error(403, "ORIGIN_NOT_ALLOWED", "Origine de mutation non autorisée.")(
                    scope, receive, guarded_send
                )
                return

        await self.app(scope, receive, guarded_send)
