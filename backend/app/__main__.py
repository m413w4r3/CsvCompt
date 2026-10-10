"""Démarre le serveur local avec la configuration d'environnement."""

import uvicorn

from app.core.config import get_settings
from app.core.logging import uvicorn_log_config


def main() -> None:
    settings = get_settings()
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
