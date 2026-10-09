"""Environnement Alembic utilisant les Settings et la fabrique SQLite applicative."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Engine

from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import create_sqlite_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _settings() -> Settings:
    configured = config.attributes.get("settings")
    if isinstance(configured, Settings):
        return configured
    return get_settings()


def run_migrations_offline() -> None:
    """Le schéma de référence contient du SQL SQLite natif, donc pas de mode offline."""
    raise RuntimeError("Les migrations Comptes familiaux exigent une connexion SQLite réelle")


def run_migrations_online() -> None:
    settings = _settings()
    engine: Engine = create_sqlite_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            if os.environ.get("COMPTES_ALLOW_DATA_LOSS_DOWNGRADE") == "1":
                # PRAGMA foreign_keys ne peut être modifié après BEGIN; le mode
                # destructif ne s'active qu'avec le drapeau explicite et reste
                # protégé par la vérification de sauvegarde de la révision.
                dbapi_connection = connection.connection.driver_connection
                assert dbapi_connection is not None
                dbapi_connection.execute("PRAGMA foreign_keys=OFF")
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=False,
                transactional_ddl=True,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
