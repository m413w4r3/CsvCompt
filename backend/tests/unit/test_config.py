from pathlib import Path

import pytest
from pydantic import ValidationError

import app.core.config as config
from app.core.config import PROJECT_ROOT, Settings, is_loopback_host


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
def test_config_validation_error_never_discloses_supplied_llm_key() -> None:
    fake_key = "fake-llm-key-should-not-leak"
    with pytest.raises(ValidationError) as caught:
        Settings(
            app_host="0.0.0.0",
            llm_api_key=fake_key,
            _env_file=None,
        )
    assert fake_key not in str(caught.value)
    assert fake_key[-8:] not in str(caught.value)


@pytest.mark.unit
def test_config_paths_are_anchored_to_project_root_from_other_cwd(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        database_url="sqlite:///./data/db/comptes.sqlite",
        data_dir=Path("data"),
        _env_file=None,
    )
    assert Settings.model_config["env_file"] == PROJECT_ROOT / ".env"
    assert settings.data_dir == PROJECT_ROOT / "data"
    assert config.make_url(settings.database_url).database == str(
        PROJECT_ROOT / "data/db/comptes.sqlite"
    )


@pytest.mark.unit
def test_loopback_addresses_and_settings_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("192.0.2.1")
    monkeypatch.setattr(config, "Settings", lambda: Settings(_env_file=None))
    config.get_settings.cache_clear()
    assert config.get_settings() is config.get_settings()
