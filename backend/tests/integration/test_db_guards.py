from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings
from app.db.migrate import upgrade_to_head

VERIFY_SCHEMA = Path(__file__).resolve().parents[3] / "scripts" / "verify_schema.py"
SCHEMA_ORACLE_SPEC = importlib.util.spec_from_file_location("verify_schema_oracle", VERIFY_SCHEMA)
assert SCHEMA_ORACLE_SPEC is not None and SCHEMA_ORACLE_SPEC.loader is not None
schema_oracle = importlib.util.module_from_spec(SCHEMA_ORACLE_SPEC)
SCHEMA_ORACLE_SPEC.loader.exec_module(schema_oracle)


@pytest.mark.integration
def test_IMP_06_IMP_07_and_SEC_06_schema_contracts_replay_on_migrated_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "guard-contracts.sqlite"
    settings = Settings(
        database_url=f"sqlite:///{database}", data_dir=tmp_path / "data", _env_file=None
    )
    upgrade_to_head(settings)
    original_open_connection = schema_oracle.open_connection

    def migrated_open_connection(
        target: str | Path = ":memory:",
        _schema_path: Path | None = None,
        *,
        check_same_thread: bool = True,
    ) -> sqlite3.Connection:
        if _schema_path is None:
            return original_open_connection(target, check_same_thread=check_same_thread)
        connection = sqlite3.connect(
            str(target), isolation_level=None, timeout=5, check_same_thread=check_same_thread
        )
        source = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        try:
            source.backup(connection)
        finally:
            source.close()
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA recursive_triggers=ON")
        return connection

    monkeypatch.setattr(schema_oracle, "open_connection", migrated_open_connection)
    schema_oracle.verify_schema(Path("ignored-reference-path.sql"), prove_mutations=False)
    output = capsys.readouterr().out
    assert "IMP-06 erreur SQL au milieu du commit" in output
    assert "IMP-07 deux connexions" in output
    assert "SEC-06 chaque connexion vérifiée" in output
    assert "ADR-025 PK non-nulles/UUIDv4" in output
    assert "m-12 version inchangée refusée par trigger" in output
    assert "allocation confirmée au-delà du montant rejetée" in output
    assert "plafonds source/cible" in output
    assert "retrait audité, immuable et exclu du plafond/somme actifs" in output
    assert "allocation liée ne peut pas être retirée" in output
    assert "plafond de source par allocation refund" in output
    assert "lien rejected conservé sans bloquer une proposition active" in output
