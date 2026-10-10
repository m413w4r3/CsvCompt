"""Vocabulaire immuable et types stricts du domaine financier."""

from __future__ import annotations

from enum import StrEnum


class Cents(int):
    """Montant entier signé en centimes; les conversions implicites sont refusées."""

    def __new__(cls, value: int) -> Cents:
        if type(value) is not int:
            raise TypeError("Cents exige un int strict; les booléens et flottants sont interdits")
        return int.__new__(cls, value)


class FlowKind(StrEnum):
    EXPENSE = "expense"
    INCOME_RECURRING = "income_recurring"
    INCOME_OTHER = "income_other"
    TRANSFER_SAVINGS = "transfer_savings"
    TRANSFER_OWNED = "transfer_owned"
    ALLOCATION_PERSONAL = "allocation_personal"
    LOAN_DISBURSEMENT = "loan_disbursement"
    LOAN_REPAYMENT = "loan_repayment"
    CAPITAL_EVENT = "capital_event"
    CASH_WITHDRAWAL_PENDING = "cash_withdrawal_pending"
    REFUND = "refund"
    INTERMEDIARY_PENDING = "intermediary_pending"
    RECONCILIATION = "reconciliation"
    UNKNOWN = "unknown"


class Origin(StrEnum):
    MANUAL = "manual"
    RULE = "rule"
    HISTORY = "history"
    LLM = "llm"
    UNCLASSIFIED = "unclassified"


class Decision(StrEnum):
    CONFIRMED = "confirmed"
    REVIEW = "review"
    UNKNOWN = "unknown"


class Beneficiary(StrEnum):
    HOUSEHOLD = "household"
    ADULT_A = "adult_a"
    ADULT_B = "adult_b"
    CHILD_A = "child_a"
    CHILD_B = "child_b"
    UNKNOWN = "unknown"


class Cadence(StrEnum):
    RECURRING = "recurring"
    OCCASIONAL = "occasional"
    EXCEPTIONAL = "exceptional"
    UNKNOWN = "unknown"


class Flexibility(StrEnum):
    FIXED = "fixed"
    VARIABLE_ESSENTIAL = "variable_essential"
    VARIABLE_OPTIONAL = "variable_optional"
    UNKNOWN = "unknown"


class Acquisition(StrEnum):
    NEW = "new"
    SECOND_HAND = "second_hand"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"
