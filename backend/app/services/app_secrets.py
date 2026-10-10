"""Gestion des secrets locaux stockés dans ``app_settings``."""

from __future__ import annotations

import base64
import secrets

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import AppSetting
from app.db.session import write_transaction

HMAC_SETTING_KEY = "import.account_hmac_key.v1"


def ensure_hmac_key(engine: Engine) -> bytes:
    """Return the unique local 256-bit HMAC key, creating it once under IMMEDIATE lock."""
    with write_transaction(engine) as session:
        setting = session.get(AppSetting, HMAC_SETTING_KEY)
        if setting is None:
            candidate = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
            setting = AppSetting(setting_key=HMAC_SETTING_KEY, setting_value=candidate, is_secret=1)
            session.add(setting)
            session.flush()
        if setting.is_secret != 1:
            raise RuntimeError("secret_setting_invalid")
        return base64.urlsafe_b64decode(setting.setting_value.encode("ascii"))


def read_hmac_key(session: Session) -> bytes | None:
    """Read the key from an existing settings row without revealing it to callers."""
    setting = session.scalar(select(AppSetting).where(AppSetting.setting_key == HMAC_SETTING_KEY))
    if setting is None or setting.is_secret != 1:
        return None
    return base64.urlsafe_b64decode(setting.setting_value.encode("ascii"))
