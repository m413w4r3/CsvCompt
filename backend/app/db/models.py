"""Modèles SQLAlchemy typés pour toutes les tables du schéma SQLite v1.

Les CHECK, index partiels et triggers métier restent définis dans la migration
initiale copiée depuis ``docs/examples/schema.sql``; ``metadata.create_all`` ne
constitue pas un mécanisme de création de base supporté.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql.elements import TextClause

from app.db.base import (
    Base,
    CreatedAtMixin,
    CreatedUpdatedAtMixin,
    ISODateText,
    UTCDateTimeText,
    VersionedMixin,
)


def uuid4_text() -> str:
    """Génère un identifiant d'entité UUIDv4 au format texte canonique."""
    return str(uuid.uuid4())


def _utc_default() -> TextClause:
    return text("(strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))")


class Account(VersionedMixin, CreatedUpdatedAtMixin, Base):
    __tablename__ = "accounts"

    account_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    account_role: Mapped[str] = mapped_column(Text, nullable=False)
    bank_fingerprint: Mapped[str | None] = mapped_column(Text, unique=True)
    fingerprint_key_version: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    transactions: Mapped[list[Transaction]] = relationship(back_populates="account")
    import_sections: Mapped[list[ImportAccountSection]] = relationship(back_populates="account")


class ImportFile(VersionedMixin, Base):
    __tablename__ = "import_files"

    import_file_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    file_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    archive_relpath: Mapped[str | None] = mapped_column(Text, unique=True)
    archive_storage_mode: Mapped[str | None] = mapped_column(Text)
    archive_permissions_octal: Mapped[str | None] = mapped_column(Text)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    received_on: Mapped[date] = mapped_column(ISODateText(), nullable=False)
    generation_date: Mapped[date] = mapped_column(ISODateText(), nullable=False)
    generation_date_source: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_operation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    detected_encoding: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    duplicate_of_import_file_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("import_files.import_file_id", ondelete="RESTRICT")
    )
    failure_code: Mapped[str | None] = mapped_column(Text)
    failure_summary: Mapped[str | None] = mapped_column(Text)
    started_at_utc: Mapped[datetime] = mapped_column(
        UTCDateTimeText(), nullable=False, server_default=_utc_default()
    )
    finished_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTimeText())

    sections: Mapped[list[ImportAccountSection]] = relationship(back_populates="import_file")
    rejections: Mapped[list[ImportRejection]] = relationship(back_populates="import_file")


class ImportAccountSection(Base):
    __tablename__ = "import_account_sections"

    import_file_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("import_files.import_file_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    section_index: Mapped[int] = mapped_column(Integer, primary_key=True, nullable=False)
    account_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("accounts.account_id", ondelete="RESTRICT")
    )
    account_fingerprint: Mapped[str | None] = mapped_column(Text)
    coverage_from: Mapped[date | None] = mapped_column(ISODateText())
    coverage_through: Mapped[date | None] = mapped_column(ISODateText())
    complete_through: Mapped[date | None] = mapped_column(ISODateText())
    balance_as_of: Mapped[date | None] = mapped_column(ISODateText())
    header_balance_cents: Mapped[int | None] = mapped_column(Integer)
    section_operation_count: Mapped[int | None] = mapped_column(Integer)

    import_file: Mapped[ImportFile] = relationship(back_populates="sections")
    account: Mapped[Account | None] = relationship(back_populates="import_sections")


class BalanceCheck(VersionedMixin, CreatedAtMixin, Base):
    __tablename__ = "balance_checks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["start_import_file_id", "start_section_index"],
            ["import_account_sections.import_file_id", "import_account_sections.section_index"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["end_import_file_id", "end_section_index"],
            ["import_account_sections.import_file_id", "import_account_sections.section_index"],
            ondelete="RESTRICT",
        ),
    )

    balance_check_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    account_id: Mapped[str] = mapped_column(
        Text, ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False
    )
    start_import_file_id: Mapped[str] = mapped_column(Text, nullable=False)
    start_section_index: Mapped[int] = mapped_column(Integer, nullable=False)
    end_import_file_id: Mapped[str] = mapped_column(Text, nullable=False)
    end_section_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    recalculated_balance_cents: Mapped[int | None] = mapped_column(Integer)
    delta_cents: Mapped[int | None] = mapped_column(Integer)
    input_snapshot_sha256: Mapped[str] = mapped_column(Text, nullable=False)


class ImportRejection(CreatedAtMixin, Base):
    __tablename__ = "import_rejections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["import_file_id", "section_index"],
            ["import_account_sections.import_file_id", "import_account_sections.section_index"],
            ondelete="RESTRICT",
        ),
    )

    import_rejection_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    import_file_id: Mapped[str] = mapped_column(
        Text, ForeignKey("import_files.import_file_id", ondelete="RESTRICT"), nullable=False
    )
    section_index: Mapped[int | None] = mapped_column(Integer)
    logical_row_idx: Mapped[int | None] = mapped_column(Integer)
    field_name: Mapped[str | None] = mapped_column(Text)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    requires_resolution: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    resolution_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="not_required", server_default=text("'not_required'")
    )
    resolution_action: Mapped[str | None] = mapped_column(Text)
    resolved_transaction_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("transactions.transaction_id", ondelete="RESTRICT")
    )
    resolved_by: Mapped[str | None] = mapped_column(Text)
    resolved_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTimeText())

    import_file: Mapped[ImportFile] = relationship(back_populates="rejections")


class Payee(VersionedMixin, CreatedUpdatedAtMixin, Base):
    __tablename__ = "payees"

    payee_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True, default=uuid4_text)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    is_active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    aliases: Mapped[list[PayeeAlias]] = relationship(back_populates="payee")
    transactions: Mapped[list[Transaction]] = relationship(back_populates="payee")


class PayeeAlias(VersionedMixin, CreatedAtMixin, Base):
    __tablename__ = "payee_aliases"

    payee_alias_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    payee_id: Mapped[str] = mapped_column(
        Text, ForeignKey("payees.payee_id", ondelete="RESTRICT"), nullable=False
    )
    alias_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    match_kind: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(Text)

    payee: Mapped[Payee] = relationship(back_populates="aliases")


class Category(VersionedMixin, CreatedUpdatedAtMixin, Base):
    __tablename__ = "categories"

    category_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    parent_category_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("categories.category_id", ondelete="RESTRICT")
    )
    label: Mapped[str] = mapped_column(Text, nullable=False)
    taxonomy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    parent: Mapped[Category | None] = relationship(
        remote_side=lambda: [Category.category_id], back_populates="children"
    )
    children: Mapped[list[Category]] = relationship(back_populates="parent")
    flow_kinds: Mapped[list[CategoryFlowKind]] = relationship(back_populates="category")


class CategoryFlowKind(Base):
    __tablename__ = "category_flow_kinds"

    category_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("categories.category_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    flow_kind: Mapped[str] = mapped_column(Text, primary_key=True, nullable=False)

    category: Mapped[Category] = relationship(back_populates="flow_kinds")


class Transaction(VersionedMixin, Base):
    __tablename__ = "transactions"

    transaction_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    account_id: Mapped[str] = mapped_column(
        Text, ForeignKey("accounts.account_id", ondelete="RESTRICT"), nullable=False
    )
    booked_on: Mapped[date] = mapped_column(ISODateText(), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_label: Mapped[str] = mapped_column(Text, nullable=False)
    identity_label: Mapped[str] = mapped_column(Text, nullable=False)
    identity_key_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    occurrence_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    payee_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("payees.payee_id", ondelete="RESTRICT")
    )
    imported_at_utc: Mapped[datetime] = mapped_column(
        UTCDateTimeText(), nullable=False, server_default=_utc_default()
    )

    account: Mapped[Account] = relationship(back_populates="transactions")
    payee: Mapped[Payee | None] = relationship(back_populates="transactions")
    source_rows: Mapped[list[SourceRow]] = relationship(back_populates="transaction")
    allocations: Mapped[list[Allocation]] = relationship(back_populates="transaction")


class SourceRow(CreatedAtMixin, Base):
    __tablename__ = "source_rows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["import_file_id", "section_index"],
            ["import_account_sections.import_file_id", "import_account_sections.section_index"],
            ondelete="RESTRICT",
        ),
    )

    source_row_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    import_file_id: Mapped[str] = mapped_column(Text, nullable=False)
    section_index: Mapped[int] = mapped_column(Integer, nullable=False)
    logical_row_idx: Mapped[int] = mapped_column(Integer, nullable=False)
    physical_line_start: Mapped[int | None] = mapped_column(Integer)
    physical_line_end: Mapped[int | None] = mapped_column(Integer)
    row_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    identity_key_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    transaction_id: Mapped[str] = mapped_column(
        Text, ForeignKey("transactions.transaction_id", ondelete="RESTRICT"), nullable=False
    )

    transaction: Mapped[Transaction] = relationship(back_populates="source_rows")


class ImportReconciliationCandidate(VersionedMixin, CreatedAtMixin, Base):
    __tablename__ = "import_reconciliation_candidates"

    reconciliation_candidate_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    source_row_id: Mapped[str] = mapped_column(
        Text, ForeignKey("source_rows.source_row_id", ondelete="RESTRICT"), nullable=False
    )
    candidate_transaction_id: Mapped[str] = mapped_column(
        Text, ForeignKey("transactions.transaction_id", ondelete="RESTRICT"), nullable=False
    )
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="open", server_default=text("'open'")
    )
    reviewed_by: Mapped[str | None] = mapped_column(Text)
    reviewed_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTimeText())


class Allocation(VersionedMixin, CreatedAtMixin, Base):
    __tablename__ = "allocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["category_id", "flow_kind"],
            ["category_flow_kinds.category_id", "category_flow_kinds.flow_kind"],
            ondelete="RESTRICT",
        ),
    )

    allocation_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    transaction_id: Mapped[str] = mapped_column(
        Text, ForeignKey("transactions.transaction_id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[str] = mapped_column(Text, nullable=False)
    flow_kind: Mapped[str] = mapped_column(Text, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(
        Text, nullable=False, default="confirmed", server_default=text("'confirmed'")
    )
    beneficiary: Mapped[str] = mapped_column(
        Text, nullable=False, default="unknown", server_default=text("'unknown'")
    )
    cadence: Mapped[str] = mapped_column(
        Text, nullable=False, default="unknown", server_default=text("'unknown'")
    )
    flexibility: Mapped[str] = mapped_column(
        Text, nullable=False, default="unknown", server_default=text("'unknown'")
    )
    acquisition: Mapped[str] = mapped_column(
        Text, nullable=False, default="unknown", server_default=text("'unknown'")
    )
    superseded_by_allocation_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "allocations.allocation_id", ondelete="RESTRICT", deferrable=True, initially="DEFERRED"
        ),
    )
    superseded_by_audit_event_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "audit_events.audit_event_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    transaction: Mapped[Transaction] = relationship(back_populates="allocations")


class ClassificationSuggestion(VersionedMixin, CreatedAtMixin, Base):
    __tablename__ = "classification_suggestions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["category_id", "proposed_flow_kind"],
            ["category_flow_kinds.category_id", "category_flow_kinds.flow_kind"],
            ondelete="RESTRICT",
        ),
    )

    suggestion_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    transaction_id: Mapped[str] = mapped_column(
        Text, ForeignKey("transactions.transaction_id", ondelete="RESTRICT"), nullable=False
    )
    proposed_flow_kind: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("categories.category_id", ondelete="RESTRICT")
    )
    suggested_amount_cents: Mapped[int | None] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_label: Mapped[str] = mapped_column(
        Text, nullable=False, default="doute", server_default=text("'doute'")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="review", server_default=text("'review'")
    )
    llm_run_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("llm_runs.llm_run_id", ondelete="RESTRICT")
    )


class ClassificationDecision(VersionedMixin, CreatedAtMixin, Base):
    __tablename__ = "classification_decisions"

    decision_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    transaction_id: Mapped[str] = mapped_column(
        Text, ForeignKey("transactions.transaction_id", ondelete="RESTRICT"), nullable=False
    )
    suggestion_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("classification_suggestions.suggestion_id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    undone_by_decision_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("classification_decisions.decision_id", ondelete="RESTRICT")
    )


class Rule(VersionedMixin, CreatedUpdatedAtMixin, Base):
    __tablename__ = "rules"
    __table_args__ = (
        ForeignKeyConstraint(
            ["category_id", "flow_kind"],
            ["category_flow_kinds.category_id", "category_flow_kinds.flow_kind"],
            ondelete="RESTRICT",
        ),
    )

    rule_id: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True, default=uuid4_text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    scope_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scope_json: Mapped[str] = mapped_column(Text, nullable=False)
    flow_kind: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[str] = mapped_column(Text, nullable=False)
    effect_json: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default=text("100")
    )
    is_approved: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    approved_by: Mapped[str | None] = mapped_column(Text)


class RuleExecution(Base):
    __tablename__ = "rule_executions"

    rule_execution_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    rule_id: Mapped[str] = mapped_column(
        Text, ForeignKey("rules.rule_id", ondelete="RESTRICT"), nullable=False
    )
    transaction_id: Mapped[str] = mapped_column(
        Text, ForeignKey("transactions.transaction_id", ondelete="RESTRICT"), nullable=False
    )
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    dry_run: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    audit_event_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "audit_events.audit_event_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    executed_at_utc: Mapped[datetime] = mapped_column(
        UTCDateTimeText(), nullable=False, server_default=_utc_default()
    )


class TransactionLink(VersionedMixin, CreatedAtMixin, Base):
    __tablename__ = "transaction_links"

    transaction_link_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    source_transaction_id: Mapped[str] = mapped_column(
        Text, ForeignKey("transactions.transaction_id", ondelete="RESTRICT"), nullable=False
    )
    target_transaction_id: Mapped[str] = mapped_column(
        Text, ForeignKey("transactions.transaction_id", ondelete="RESTRICT"), nullable=False
    )
    link_kind: Mapped[str] = mapped_column(Text, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    target_allocation_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("allocations.allocation_id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="proposed", server_default=text("'proposed'")
    )
    superseded_by_transaction_link_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "transaction_links.transaction_link_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    superseded_by_audit_event_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "audit_events.audit_event_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
    )


class AuditEvent(CreatedAtMixin, Base):
    __tablename__ = "audit_events"

    audit_event_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    import_file_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("import_files.import_file_id", ondelete="RESTRICT")
    )
    target_type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)


class AppSetting(VersionedMixin, Base):
    __tablename__ = "app_settings"

    setting_key: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    setting_value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    updated_at_utc: Mapped[datetime] = mapped_column(
        UTCDateTimeText(), nullable=False, server_default=_utc_default()
    )


class LlmRun(Base):
    __tablename__ = "llm_runs"

    llm_run_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    recovered_line_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    redacted_error_code: Mapped[str | None] = mapped_column(Text)
    started_at_utc: Mapped[datetime] = mapped_column(UTCDateTimeText(), nullable=False)
    finished_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTimeText())


class Budget(VersionedMixin, CreatedUpdatedAtMixin, Base):
    __tablename__ = "budgets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["category_id", "flow_kind"],
            ["category_flow_kinds.category_id", "category_flow_kinds.flow_kind"],
            ondelete="RESTRICT",
        ),
    )

    budget_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    category_id: Mapped[str] = mapped_column(
        Text, ForeignKey("categories.category_id", ondelete="RESTRICT"), nullable=False
    )
    flow_kind: Mapped[str] = mapped_column(
        Text, nullable=False, default="expense", server_default=text("'expense'")
    )
    period_start: Mapped[date] = mapped_column(ISODateText(), nullable=False)
    period_end: Mapped[date] = mapped_column(ISODateText(), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )


class BudgetAccount(Base):
    __tablename__ = "budget_accounts"

    budget_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("budgets.budget_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    account_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("accounts.account_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )


class Insight(VersionedMixin, CreatedUpdatedAtMixin, Base):
    __tablename__ = "insights"

    insight_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default=text("'active'")
    )
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    generated_by: Mapped[str] = mapped_column(Text, nullable=False)


class InsightFact(CreatedAtMixin, Base):
    __tablename__ = "insight_facts"

    insight_fact_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    insight_id: Mapped[str] = mapped_column(
        Text, ForeignKey("insights.insight_id", ondelete="RESTRICT"), nullable=False
    )
    fact_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_definition_id: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[date] = mapped_column(ISODateText(), nullable=False)
    period_end: Mapped[date] = mapped_column(ISODateText(), nullable=False)
    value_kind: Mapped[str] = mapped_column(Text, nullable=False)
    raw_cents: Mapped[int | None] = mapped_column(Integer)
    raw_basis_points: Mapped[int | None] = mapped_column(Integer)
    raw_count: Mapped[int | None] = mapped_column(Integer)
    state_value: Mapped[str | None] = mapped_column(Text)
    filters_json: Mapped[str] = mapped_column(Text, nullable=False)
    coverage_json: Mapped[str] = mapped_column(Text, nullable=False)


class InsightFactTransaction(Base):
    __tablename__ = "insight_fact_transactions"

    insight_fact_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("insight_facts.insight_fact_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    transaction_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("transactions.transaction_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
    )
    contribution_cents: Mapped[int | None] = mapped_column(Integer)


class BackupManifest(CreatedAtMixin, Base):
    __tablename__ = "backup_manifests"

    backup_manifest_id: Mapped[str] = mapped_column(
        Text, primary_key=True, nullable=True, default=uuid4_text
    )
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    archive_relpath: Mapped[str] = mapped_column(Text, nullable=False)
    archive_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    archive_permissions_octal: Mapped[str] = mapped_column(
        Text, nullable=False, default="0600", server_default=text("'0600'")
    )
    includes_originals: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    includes_hmac_key: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTimeText())


class IdempotencyRecord(CreatedAtMixin, Base):
    __tablename__ = "idempotency_records"

    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True, nullable=True)
    http_method: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_path: Mapped[str] = mapped_column(Text, nullable=False)
    request_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
