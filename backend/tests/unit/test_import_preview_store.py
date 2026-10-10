from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.errors import AppError
from app.services.import_preview_store import ImportPreviewStore
from app.services.imports import set_coverage, set_multiplicity


@pytest.mark.unit
def test_preview_store_ttl_atomic_private_files_and_capacity(tmp_path) -> None:
    now = datetime(2026, 10, 10, tzinfo=UTC)
    store = ImportPreviewStore(tmp_path / "private", ttl_seconds=5, max_bytes=4, clock=lambda: now)
    preview = store.create({"status": "previewed"}, b"1234")
    assert store.raw_bytes(preview["preview_id"]) == b"1234"
    assert (tmp_path / "private").stat().st_mode & 0o777 == 0o700
    for path in (tmp_path / "private").iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(AppError, match="plein"):
        store.create({"status": "previewed"}, b"x")
    now += timedelta(seconds=6)
    with pytest.raises(AppError):
        store.get(preview["preview_id"])
    assert list((tmp_path / "private").iterdir()) == []


@pytest.mark.unit
def test_multiplicity_resolution_is_scoped_to_section_when_row_indexes_repeat() -> None:
    preview = {
        "_resolutions": {"account_mapping": {}, "coverage": {}, "multiplicity": {}},
        "_out_of_period_rows": [{"section": 1, "row": 1}, {"section": 2, "row": 1}],
        "_period_missing_sections": [],
        "_structural_rejection": False,
        "sections": [
            {
                "section_index": 1,
                "account_mapping": {"status": "known", "account_id": "a"},
            },
            {
                "section_index": 2,
                "account_mapping": {"status": "known", "account_id": "b"},
            },
        ],
        "rejections": [
            {"code": "OUT_OF_PERIOD", "severity": "blocking", "section": 1, "logical_row_idx": 1},
            {"code": "OUT_OF_PERIOD", "severity": "blocking", "section": 2, "logical_row_idx": 1},
        ],
    }
    with pytest.raises(AppError) as ambiguous:
        set_multiplicity(preview, 1, "create_occurrence")
    assert ambiguous.value.code == "MULTIPLICITY_AMBIGUOUS"
    set_multiplicity(preview, 1, "create_occurrence", section_index=1)
    assert len(preview["rejections"]) == 1
    assert preview["can_commit"] is False
    set_multiplicity(preview, 1, "link_existing", section_index=2)
    assert preview["rejections"] == []
    assert preview["can_commit"] is True


@pytest.mark.unit
def test_empty_section_coverage_does_not_invent_complete_through() -> None:
    preview = {
        "sections": [
            {
                "section_index": 1,
                "account_mapping": {"status": "known", "account_id": "synthetic-account"},
                "operation_count": 0,
                "max_date": None,
                "complete_through": None,
            }
        ],
        "_resolutions": {"account_mapping": {}, "coverage": {}, "multiplicity": {}},
        "_period_missing_sections": [1],
        "_out_of_period_rows": [],
        "_structural_rejection": False,
        "rejections": [],
    }
    set_coverage(
        preview,
        1,
        coverage_from=date(2026, 3, 1),
        coverage_through=date(2026, 3, 31),
        generated_on=None,
    )
    assert preview["sections"][0]["complete_through"] is None
