"""Base SQLAlchemy commune et types texte ISO conformes au schéma SQLite."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import MetaData, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    """Base des modèles; le DDL de référence reste appliqué par Alembic."""

    metadata = MetaData()


class ISODateText(TypeDecorator[date]):
    """Expose ``date`` en Python tout en conservant l'affinité TEXT de SQLite."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: date | None, _dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            raise TypeError("une date métier ne peut pas être un datetime")
        if not isinstance(value, date):
            raise TypeError("une date métier doit être un datetime.date")
        return value.isoformat()

    def process_result_value(self, value: str | None, _dialect: Any) -> date | None:
        return None if value is None else date.fromisoformat(value)


class UTCDateTimeText(TypeDecorator[datetime]):
    """Expose des instants conscients du fuseau au format UTC canonique milliseconde."""

    impl = Text
    cache_ok = True
    _format = "%Y-%m-%dT%H:%M:%S.%fZ"
    _pattern = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")

    def process_bind_param(self, value: datetime | None, _dialect: Any) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("un horodatage doit inclure un fuseau horaire")
        utc_value = value.astimezone(UTC)
        return utc_value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def process_result_value(self, value: str | None, _dialect: Any) -> datetime | None:
        if value is None:
            return None
        if self._pattern.fullmatch(value) is None:
            raise ValueError(
                "l'horodatage SQLite doit respecter le format UTC avec millisecondes "
                "YYYY-MM-DDTHH:MM:SS.mmmZ"
            )
        return datetime.strptime(value, self._format).replace(tzinfo=UTC)


class CreatedAtMixin:
    created_at_utc: Mapped[datetime] = mapped_column(
        UTCDateTimeText(),
        nullable=False,
        server_default=text("(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"),
    )


class CreatedUpdatedAtMixin(CreatedAtMixin):
    updated_at_utc: Mapped[datetime] = mapped_column(
        UTCDateTimeText(),
        nullable=False,
        server_default=text("(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"),
    )


class VersionedMixin:
    """Active la vérification optimistic-lock SQLAlchemy pour les lignes versionnées."""

    version: Mapped[int] = mapped_column(nullable=False, default=1, server_default=text("1"))

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:
        return {"version_id_col": cls.version}
