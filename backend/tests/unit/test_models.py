from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import Integer

from app.db.base import ISODateText, UTCDateTimeText
from app.db.models import (
    Account,
    Allocation,
    AuditEvent,
    BackupManifest,
    BalanceCheck,
    Base,
    Budget,
    ClassificationDecision,
    ClassificationSuggestion,
    ImportFile,
    ImportReconciliationCandidate,
    ImportRejection,
    Insight,
    InsightFact,
    LlmRun,
    Payee,
    PayeeAlias,
    Rule,
    RuleExecution,
    SourceRow,
    Transaction,
    TransactionLink,
    uuid4_text,
)


def test_T06_ORM_02_entity_primary_keys_use_application_generated_uuid4() -> None:
    uuid_models = (
        Account,
        Allocation,
        AuditEvent,
        BackupManifest,
        BalanceCheck,
        Budget,
        ClassificationDecision,
        ClassificationSuggestion,
        ImportFile,
        ImportReconciliationCandidate,
        ImportRejection,
        Insight,
        InsightFact,
        LlmRun,
        Payee,
        PayeeAlias,
        Rule,
        RuleExecution,
        SourceRow,
        Transaction,
        TransactionLink,
    )
    generated = [uuid4_text() for _ in uuid_models]
    assert len(set(generated)) == len(uuid_models)
    assert all(UUID(value).version == 4 for value in generated)
    for model in uuid_models:
        primary_key = next(iter(model.__table__.primary_key.columns))
        assert callable(primary_key.default.arg)
        assert UUID(primary_key.default.arg(None)).version == 4


def test_INV_02_ORM_money_columns_have_integer_affinity() -> None:
    money_fields = (
        (Transaction, "amount_cents"),
        (Allocation, "amount_cents"),
        (Budget, "amount_cents"),
        (ImportRejection, "logical_row_idx"),
    )
    for model, field in money_fields:
        assert model.__table__.c[field].type._type_affinity is Integer
    for table in (Transaction.__table__, Allocation.__table__, Budget.__table__):
        for column in table.columns:
            if column.name.endswith("_cents"):
                assert column.type._type_affinity is Integer


def test_T06_ORM_03_text_dates_and_timestamps_are_typed_and_utc() -> None:
    date_type = ISODateText()
    assert date_type.process_bind_param(date(2026, 10, 10), None) == "2026-10-10"
    assert date_type.process_result_value("2026-10-10", None) == date(2026, 10, 10)

    timestamp_type = UTCDateTimeText()
    local_time = datetime(2026, 10, 10, 14, 30, tzinfo=timezone(timedelta(hours=2)))
    stored = timestamp_type.process_bind_param(local_time, None)
    assert stored == "2026-10-10T12:30:00.000Z"
    restored = timestamp_type.process_result_value(stored, None)
    assert restored is not None and restored.tzinfo is UTC
    assert restored == local_time.astimezone(UTC)
    with pytest.raises(ValueError, match="fuseau"):
        timestamp_type.process_bind_param(datetime(2026, 10, 10), None)
    with pytest.raises(ValueError, match="millisecondes"):
        timestamp_type.process_result_value("2026-10-10T12:30:00.000000Z", None)
    with pytest.raises(ValueError, match="millisecondes"):
        timestamp_type.process_result_value("2026-10-10T12:30:00Z", None)
    precise = datetime(2026, 10, 10, 14, 30, 0, 123456, tzinfo=timezone(timedelta(hours=2)))
    assert timestamp_type.process_bind_param(precise, None) == "2026-10-10T12:30:00.123Z"


def test_T06_ORM_04_versioned_rows_enable_optimistic_locking() -> None:
    for model in (Account, ImportFile, Transaction, Allocation, Rule, Budget, Insight):
        assert model.__mapper__.version_id_col is model.__table__.c.version


def test_ADR_027_allocations_and_refund_links_use_allocation_identity() -> None:
    allocation_columns = Allocation.__table__.c
    assert isinstance(allocation_columns.retracted_at_utc.type, UTCDateTimeText)
    assert allocation_columns.retracted_by_decision_id.foreign_keys
    link_columns = TransactionLink.__table__.c
    assert link_columns.source_allocation_id.foreign_keys
    assert link_columns.target_allocation_id.foreign_keys
    assert TransactionLink.__mapper__.relationships.source_allocation.mapper.class_ is Allocation
    assert TransactionLink.__mapper__.relationships.target_allocation.mapper.class_ is Allocation


def test_T06_ORM_05_all_reference_tables_have_a_typed_model() -> None:
    assert len(Base.metadata.tables) == 28
    assert "flow_kind" not in Transaction.__table__.columns
    assert "expires_at_utc" not in Base.metadata.tables["idempotency_records"].columns
    assert all(
        not column.nullable
        for table in Base.metadata.tables.values()
        for column in table.primary_key.columns
    )
