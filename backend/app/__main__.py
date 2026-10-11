"""Démarre le serveur local avec la configuration d'environnement."""

import logging

import uvicorn

from app.core.config import get_settings
from app.core.logging import uvicorn_log_config

logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    if settings.allow_non_loopback:
        logger.warning(
            "ALLOW_NON_LOOPBACK est activé ; le bind externe n'est pas supporté en v1. "
            "CORS et Origin ne fournissent pas d'authentification."
        )
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
        log_config=uvicorn_log_config(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
