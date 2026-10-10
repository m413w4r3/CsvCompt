from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.integration
def test_OPS_03_private_backup_and_application_exports_fail_guard_when_ignored(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "guard-repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / ".gitignore").write_text("*.cfbackup\nexports/\n", encoding="utf-8")
    (repository / "tracked.cfbackup").write_bytes(b"synthetic archive")
    subprocess.run(
        ["git", "-C", str(repository), "add", "-f", ".gitignore", "tracked.cfbackup"],
        check=True,
    )
    (repository / "ignored.cfbackup").write_bytes(b"synthetic ignored archive")
    export_directory = repository / "exports"
    export_directory.mkdir()
    (export_directory / "family-export-synthetic.json").write_text("{}", encoding="utf-8")
    (repository / "family-export-untracked.csv").write_text("a,b\n", encoding="utf-8")
    guard = repository / "check_no_private_data.sh"
    guard.write_bytes((ROOT / "scripts" / "check_no_private_data.sh").read_bytes())

    result = subprocess.run(
        ["bash", str(guard)], cwd=repository, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert "archive .cfbackup détectée" in result.stderr
    assert "export applicatif détecté" in result.stderr
    assert "synthetic" not in result.stderr
