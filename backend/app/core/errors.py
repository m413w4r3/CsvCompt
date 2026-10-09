"""Erreurs API uniformes sans écho des entrées utilisateur."""

import logging
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("comptes_familiaux.api")


class AppError(Exception):
    """Erreur applicative exposable avec un code stable et des détails sûrs."""

    def __init__(
        self,
        code: str,
        status_code: int,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.details = dict(details or {})


def _error_response(
    status_code: int,
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": dict(details or {}),
            }
        },
        headers=dict(headers or {}),
    )


class InternalErrorBoundary:
    """Intercepte les erreurs HTTP avant le serveur ASGI pour éviter son traceback."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self.app(scope, receive, send)
        except Exception:
            if scope["type"] != "http":
                raise
            correlation_id = str(uuid4())
            logger.error("internal_error correlation_id=%s", correlation_id)
            response = _error_response(
                500,
                "INTERNAL_ERROR",
                "Une erreur interne est survenue.",
                {"correlation_id": correlation_id},
                {"X-Correlation-ID": correlation_id},
            )
            await response(scope, receive, send)


def install_error_handlers(app: FastAPI) -> None:
    """Installe les réponses 4xx/5xx normalisées."""

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            code, message = "NOT_FOUND", "Ressource introuvable."
        elif exc.status_code == 405:
            code, message = "METHOD_NOT_ALLOWED", "Méthode non autorisée."
        else:
            code, message = "HTTP_ERROR", "La requête ne peut pas être traitée."
        return _error_response(exc.status_code, code, message, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Ne jamais inclure `input`, `ctx` ou le message brut de Pydantic.
        fields = [
            {"field": ".".join(str(part) for part in error["loc"]), "type": error["type"]}
            for error in exc.errors()
        ]
        return _error_response(
            422,
            "VALIDATION_ERROR",
            "La requête contient des champs invalides.",
            {"fields": fields},
        )

    @app.exception_handler(Exception)
    async def internal_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
        correlation_id = str(uuid4())
        logger.error("internal_error correlation_id=%s", correlation_id)
        return _error_response(
            500,
            "INTERNAL_ERROR",
            "Une erreur interne est survenue.",
            {"correlation_id": correlation_id},
            {"X-Correlation-ID": correlation_id},
        )
