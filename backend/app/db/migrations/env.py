"""Environnement Alembic utilisant les Settings et la fabrique SQLite applicative."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import create_sqlite_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _operation() -> str | None:
    configured = config.attributes.get("operation")
    if isinstance(configured, str):
        return configured
    cli_command = getattr(getattr(config, "cmd_opts", None), "cmd", None)
    function = cli_command[0] if isinstance(cli_command, tuple) and cli_command else cli_command
    name = getattr(function, "__name__", None)
    return name if isinstance(name, str) else None


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
            operation = _operation()
            dbapi_connection = connection.connection.driver_connection
            foreign_keys_disabled = False
            if (
                operation == "downgrade"
                and os.environ.get("COMPTES_ALLOW_DATA_LOSS_DOWNGRADE") == "1"
            ):
                # Les reconstructions SQLite des migrations exigent cette bascule
                # avant BEGIN. La migration valide ensuite foreign_key_check.
                assert dbapi_connection is not None
                dbapi_connection.execute("PRAGMA foreign_keys=OFF")
                foreign_keys_disabled = True
            elif dbapi_connection is not None:
                fk_enabled = dbapi_connection.execute("PRAGMA foreign_keys").fetchone()
                if fk_enabled != (1,):
                    raise RuntimeError("foreign_keys doit rester actif hors downgrade destructif")
            if operation == "upgrade":
                assert dbapi_connection is not None
                head_revision = ScriptDirectory.from_config(config).get_current_head()
                version_table_exists = (
                    dbapi_connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='alembic_version'"
                    ).fetchone()
                    is not None
                )
                current_revision = (
                    dbapi_connection.execute("SELECT version_num FROM alembic_version").fetchone()
                    if version_table_exists
                    else None
                )
                needs_migration = current_revision != (head_revision,)
                has_schema = (
                    dbapi_connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                    ).fetchone()
                    is not None
                )
                if has_schema and config.attributes.get("pre_upgrade_backup_checked") is not True:
                    raise RuntimeError(
                        "upgrade direct refusé sur une base non vide; utilisez "
                        "uv run python -m app.db.migrate upgrade pour la sauvegarde vérifiée"
                    )
                # Les reconstructions SQLite exigent cette bascule avant BEGIN.
                # Un upgrade déjà à la tête reste strictement en lecture seule.
                if needs_migration:
                    dbapi_connection.execute("PRAGMA foreign_keys=OFF")
                    foreign_keys_disabled = True
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=False,
                transactional_ddl=True,
                transaction_per_migration=True,
            )
            try:
                with context.begin_transaction():
                    context.run_migrations()
                    assert dbapi_connection is not None
                    integrity = dbapi_connection.execute("PRAGMA integrity_check").fetchone()
                    violations = dbapi_connection.execute("PRAGMA foreign_key_check").fetchmany(10)
                    if integrity != ("ok",):
                        raise RuntimeError(
                            f"migration SQLite refusée : integrity_check={integrity!r}"
                        )
                    if violations:
                        raise RuntimeError(
                            "migration SQLite refusée : foreign_key_check signale "
                            f"{len(violations)} violation(s)"
                        )
            finally:
                if foreign_keys_disabled and dbapi_connection is not None:
                    if dbapi_connection.in_transaction:
                        dbapi_connection.execute("ROLLBACK")
                    dbapi_connection.execute("PRAGMA foreign_keys=ON")
                    if dbapi_connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
                        raise RuntimeError("foreign_keys n'a pas été réactivé après migration")
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
