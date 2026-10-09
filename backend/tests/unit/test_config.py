import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings, is_loopback_host


@pytest.mark.unit
def test_config_defaults_to_local_bind_and_llm_disabled() -> None:
    settings = Settings(_env_file=None)
    assert settings.app_host == "127.0.0.1"
    assert settings.app_port == 9513
    assert settings.llm_enabled is False
    keyed_settings = Settings(llm_api_key="private-test-key", _env_file=None)
    assert "private-test-key" not in repr(keyed_settings)


@pytest.mark.unit
def test_non_loopback_host_requires_explicit_override() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings(app_host="0.0.0.0", _env_file=None)
    assert (
        Settings(app_host="0.0.0.0", allow_non_loopback=True, _env_file=None).app_host == "0.0.0.0"
    )


@pytest.mark.unit
def test_loopback_addresses_and_settings_cache() -> None:
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("192.0.2.1")
    assert get_settings() is get_settings()
