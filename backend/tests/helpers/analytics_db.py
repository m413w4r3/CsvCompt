"""Synthetic database builders for analytics tests: services write, analytics read."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from sqlalchemy.engine import Engine

from app.core.config import Settings
from app.db.migrate import upgrade_to_head
from app.db.models import Account, ImportAccountSection, ImportFile, Payee, Transaction
from app.db.session import create_sqlite_engine, write_transaction
from app.domain.taxonomy import load_taxonomy
from app.services.allocations import AllocationPart, allocate
from app.services.app_secrets import ensure_hmac_key
from app.services.categories import sync_taxonomy


def make_settings(tmp_path: Path, name: str = "analytics") -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / f'{name}.sqlite'}",
        data_dir=tmp_path / f"{name}-data",
        _env_file=None,
    )


def make_engine(settings: Settings) -> Engine:
    upgrade_to_head(settings)
    engine = create_sqlite_engine(settings.database_url)
    sync_taxonomy(engine, load_taxonomy())
    ensure_hmac_key(engine)
    return engine


def add_account(engine: Engine, alias: str, role: str = "current") -> str:
    with write_transaction(engine) as session:
        row = Account(alias=alias, account_role=role)
        session.add(row)
        session.flush()
        return row.account_id


def add_payee(engine: Engine, name: str) -> str:
    with write_transaction(engine) as session:
        row = Payee(display_name=name, normalized_name=name.lower())
        session.add(row)
        session.flush()
        return row.payee_id


def add_transaction(
    engine: Engine,
    account_id: str,
    booked_on: date,
    amount_cents: int,
    *,
    label: str = "LIBELLE SYNTHETIQUE CONFIDENTIEL",
    payee_id: str | None = None,
) -> str:
    identity = f"analytics-{uuid4()}"
    with write_transaction(engine) as session:
        row = Transaction(
            account_id=account_id,
            booked_on=booked_on,
            amount_cents=amount_cents,
            raw_label=label,
            identity_label=identity,
            identity_key_sha256=sha256(identity.encode()).hexdigest(),
            occurrence_rank=1,
            payee_id=payee_id,
        )
        session.add(row)
        session.flush()
        return row.transaction_id


def classify(
    engine: Engine,
    transaction_id: str,
    *parts: tuple[str, str, int, str],
    version: int = 1,
) -> dict[str, str]:
    """Confirm allocations `(flow_kind, category_id, amount_cents, beneficiary)` via the service."""
    state = allocate(
        engine,
        transaction_id,
        [
            AllocationPart(flow_kind, category_id, amount_cents, beneficiary)
            for flow_kind, category_id, amount_cents, beneficiary in parts
        ],
        mode="complete",
        origin="manual",
        actor="analytics-fixture",
        expected_version=version,
    )
    return {item.category_id: item.allocation_id for item in state.allocations}


def commit_section(
    engine: Engine,
    account_id: str,
    coverage_from: date,
    coverage_through: date,
    *,
    generated_on: date | None = None,
) -> None:
    """Record a committed import section so the covered days count as complete."""
    generation = generated_on or coverage_through + timedelta(days=1)
    import_file_id = str(uuid4())
    with write_transaction(engine) as session:
        session.add(
            ImportFile(
                import_file_id=import_file_id,
                file_sha256=sha256(f"synthetic-{import_file_id}".encode()).hexdigest(),
                archive_relpath=f"data/originals/{import_file_id}.bin",
                archive_storage_mode="raw",
                archive_permissions_octal="0600",
                file_size_bytes=1,
                received_on=generation,
                generation_date=generation,
                generation_date_source="header_download",
                parsed_operation_count=0,
                status="previewed",
            )
        )
        session.flush()
        session.add(
            ImportAccountSection(
                import_file_id=import_file_id,
                section_index=1,
                account_id=account_id,
                coverage_from=coverage_from,
                coverage_through=coverage_through,
                complete_through=coverage_through,
                section_operation_count=0,
            )
        )
        session.flush()
        file_row = session.get(ImportFile, import_file_id)
        assert file_row is not None
        file_row.status = "committed"
        file_row.version = 2
        file_row.finished_at_utc = datetime(
            generation.year, generation.month, generation.day, tzinfo=UTC
        )


def build_family_database(engine: Engine, *, classify_unallocated: bool = False) -> dict[str, str]:
    """A February + March family ledger written through the allocation/refund services.

    Returns the account and transaction IDs by readable key. Amounts are synthetic.
    """
    from app.services.refund_links import confirm_refund_link, create_refund_link

    ids: dict[str, str] = {}
    ids["cur"] = add_account(engine, "Compte courant synthétique", "current")
    ids["per"] = add_account(engine, "Compte personnel synthétique", "personal")
    ids["sav"] = add_account(engine, "Compte épargne synthétique", "savings")
    for key in ("cur", "per", "sav"):
        commit_section(engine, ids[key], date(2026, 2, 1), date(2026, 3, 31))
    shop = add_payee(engine, "Magasin synthétique")
    march = date(2026, 3, 1)

    def book(
        key: str,
        account: str,
        day: int,
        amount: int,
        *parts: tuple[str, str, int, str],
        month: date = march,
        payee_id: str | None = None,
    ) -> None:
        transaction_id = add_transaction(
            engine, ids[account], month.replace(day=day), amount, payee_id=payee_id
        )
        ids[key] = transaction_id
        if parts:
            classify(engine, transaction_id, *parts)

    feb = date(2026, 2, 1)
    book(
        "feb-salary",
        "cur",
        5,
        280_000,
        ("income_recurring", "income_salary_a", 280_000, "adult_a"),
        month=feb,
    )
    book(
        "feb-food",
        "cur",
        10,
        -15_000,
        ("expense", "groceries", -15_000, "household"),
        month=feb,
        payee_id=shop,
    )
    book("salary", "cur", 2, 300_000, ("income_recurring", "income_salary_a", 300_000, "adult_a"))
    book("aid", "cur", 3, 18_000, ("income_other", "income_benefits", 18_000, "household"))
    book(
        "loan",
        "cur",
        3,
        5_000_000,
        ("loan_disbursement", "flow_loan_disbursement", 5_000_000, "unknown"),
    )
    book(
        "purchase",
        "cur",
        4,
        -10_000,
        ("expense", "groceries", -6_000, "household"),
        ("expense", "restaurants", -4_000, "household"),
        payee_id=shop,
    )
    book(
        "credit-prorated",
        "cur",
        5,
        3_001,
        ("refund", "income_refund", 3_001, "unknown"),
        payee_id=shop,
    )
    book(
        "credit-targeted",
        "cur",
        6,
        1_000,
        ("refund", "income_refund", 1_000, "unknown"),
        payee_id=shop,
    )
    book(
        "savings-out",
        "cur",
        6,
        -20_000,
        ("transfer_savings", "flow_savings_out", -20_000, "unknown"),
    )
    book(
        "savings-leg", "sav", 6, 20_000, ("transfer_savings", "flow_savings_in", 20_000, "unknown")
    )
    book(
        "savings-return", "cur", 7, 5_000, ("transfer_savings", "flow_savings_in", 5_000, "unknown")
    )
    book(
        "allowance",
        "cur",
        8,
        -25_000,
        ("allocation_personal", "flow_personal_a", -25_000, "adult_a"),
    )
    book(
        "allowance-in",
        "per",
        8,
        25_000,
        ("transfer_owned", "flow_owned_transfer", 25_000, "unknown"),
    )
    book("personal-purchase", "per", 9, -18_000, ("expense", "restaurants", -18_000, "child_a"))
    book(
        "owned-out", "cur", 9, -7_000, ("transfer_owned", "flow_owned_transfer", -7_000, "unknown")
    )
    book("owned-in", "per", 9, 7_000, ("transfer_owned", "flow_owned_transfer", 7_000, "unknown"))
    book(
        "cash",
        "cur",
        10,
        -10_000,
        ("cash_withdrawal_pending", "flow_cash_unallocated", -10_000, "unknown"),
    )
    book(
        "loan-principal",
        "cur",
        11,
        -9_826_561,
        ("loan_repayment", "flow_loan_principal", -9_826_561, "unknown"),
    )
    book(
        "loan-unsplit",
        "cur",
        12,
        -98_265,
        ("loan_repayment", "flow_loan_payment_unknown_split", -98_265, "unknown"),
    )
    if classify_unallocated:
        book("unallocated", "cur", 13, -1_234, ("expense", "groceries", -1_234, "household"))
    else:
        book("unallocated", "cur", 13, -1_234)

    from sqlalchemy import select

    from app.db.models import Allocation

    with write_transaction(engine) as session:
        rows = session.execute(
            select(Allocation.allocation_id, Allocation.transaction_id, Allocation.flow_kind)
        ).all()
    refund_allocation = {tid: aid for aid, tid, flow in rows if flow == "refund"}
    with write_transaction(engine) as session:
        grocery_allocation = session.scalar(
            select(Allocation.allocation_id).where(
                Allocation.transaction_id == ids["purchase"], Allocation.category_id == "groceries"
            )
        )
    assert grocery_allocation is not None
    prorated = create_refund_link(
        engine,
        ids["credit-prorated"],
        ids["purchase"],
        3_001,
        actor="analytics-fixture",
        source_allocation_id=refund_allocation[ids["credit-prorated"]],
    )
    confirm_refund_link(
        engine,
        prorated.transaction_link_id,
        actor="analytics-fixture",
        expected_version=prorated.version,
    )
    targeted = create_refund_link(
        engine,
        ids["credit-targeted"],
        ids["purchase"],
        1_000,
        actor="analytics-fixture",
        source_allocation_id=refund_allocation[ids["credit-targeted"]],
        target_allocation_id=grocery_allocation,
    )
    confirm_refund_link(
        engine,
        targeted.transaction_link_id,
        actor="analytics-fixture",
        expected_version=targeted.version,
    )
    return ids
