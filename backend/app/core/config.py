"""Configuration locale du backend, chargée depuis l'environnement ou `.env`."""

from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.core.security import (
    DEFAULT_ALLOWED_ORIGINS,
    DEFAULT_BIND_HOST,
    DEFAULT_BIND_PORT,
    normalize_origin,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def is_loopback_host(host: str) -> bool:
    """Renvoie si host désigne une adresse loopback connue."""
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    """Paramètres applicatifs sans divulgation des secrets dans leur repr."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_default=True,
        hide_input_in_errors=True,
    )

    app_host: str = DEFAULT_BIND_HOST
    app_port: int = Field(default=DEFAULT_BIND_PORT, ge=1, le=65535)
    allow_non_loopback: bool = False
    cors_allowed_origins: list[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_ORIGINS))
    frontend_dist_dir: Path | None = Field(default=None, repr=False)
    enable_api_docs: bool = False
    database_url: str = Field(default="sqlite:///./data/db/comptes.sqlite", repr=False)
    llm_enabled: bool = False
    llm_base_url: str | None = Field(default=None, repr=False)
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    data_dir: Path = Field(default=Path("data"), repr=False)
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    max_preview_anomalies: int = Field(default=1000, ge=1)
    preview_ttl_seconds: int = Field(default=1800, ge=1)
    preview_dir: Path | None = Field(default=None, repr=False)
    version: str = Field(default="0.1.0", validation_alias="APP_VERSION")

    @field_validator("database_url")
    @classmethod
    def anchor_database_url(cls, value: str) -> str:
        url = make_url(value)
        database = url.database
        if (
            url.get_backend_name() == "sqlite"
            and database is not None
            and database not in ("", ":memory:")
            and not Path(database).is_absolute()
            and not database.startswith("file:")
        ):
            anchored = (PROJECT_ROOT / database).resolve()
            return url.set(database=str(anchored)).render_as_string(hide_password=False)
        return value

    @field_validator("data_dir", mode="before")
    @classmethod
    def anchor_data_dir(cls, value: str | Path) -> Path:
        path = Path(value).expanduser()
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    @field_validator("frontend_dist_dir", mode="before")
    @classmethod
    def anchor_frontend_dist_dir(cls, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        path = Path(value).expanduser()
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    @field_validator("preview_dir", mode="before")
    @classmethod
    def anchor_preview_dir(cls, value: str | Path | None) -> Path | None:
        if value is None:
            return None
        return Path(value).expanduser()

    @property
    def effective_preview_dir(self) -> Path:
        """Return the private preview directory anchored below ``data_dir`` by default."""
        if self.preview_dir is None:
            return self.data_dir / "import-previews"
        return (
            self.preview_dir if self.preview_dir.is_absolute() else self.data_dir / self.preview_dir
        )

    @field_validator("app_host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        host = value.strip()
        if not host:
            raise ValueError("APP_HOST doit être une adresse loopback non vide")
        return host

    @field_validator("cors_allowed_origins")
    @classmethod
    def validate_cors_origins(cls, value: list[str]) -> list[str]:
        """CORS trusts only explicitly listed HTTP(S) origins on loopback."""
        normalized_origins: list[str] = []
        for candidate in value:
            origin = normalize_origin(candidate)
            if origin is None:
                raise ValueError("CORS_ALLOWED_ORIGINS doit contenir des origines HTTP(S) valides")
            parsed = urlsplit(origin)
            if parsed.hostname is None or not is_loopback_host(parsed.hostname):
                raise ValueError("CORS_ALLOWED_ORIGINS accepte uniquement des origines loopback")
            if origin not in normalized_origins:
                normalized_origins.append(origin)
        if not normalized_origins:
            raise ValueError("CORS_ALLOWED_ORIGINS ne peut pas être vide")
        return normalized_origins

    @model_validator(mode="after")
    def enforce_loopback(self) -> "Settings":
        if not self.allow_non_loopback and not is_loopback_host(self.app_host):
            raise ValueError(
                "APP_HOST doit être loopback (127.0.0.1, ::1 ou localhost); "
                "définir ALLOW_NON_LOOPBACK=true uniquement pour autoriser un bind externe"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retourne les paramètres mis en cache; vider le cache en test après surcharge."""
    return Settings()
