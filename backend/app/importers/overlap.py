"""Pure reconciliation of strict transaction identities across overlapping exports."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

type IdentityKey = tuple[str, str, int, str]


@dataclass(frozen=True, slots=True)
class ExistingOccurrence:
    """An already stored occurrence of a strict identity."""

    identity_key: IdentityKey
    occurrence_rank: int
    transaction_id: str


@dataclass(frozen=True, slots=True)
class IncomingOccurrence:
    """A parsed row; its logical index only distributes provenance among duplicates."""

    identity_key: IdentityKey
    logical_row_idx: int


@dataclass(frozen=True, slots=True)
class ReconciliationAnomaly:
    """A blocking inconsistency in the pre-existing rank sequence or input rows."""

    code: str
    identity_key: IdentityKey | None
    actual_count: int
    expected_max_rank: int


@dataclass(frozen=True, slots=True)
class OccurrenceAssignment:
    """The occurrence rank assigned to one incoming row and known target if present."""

    identity_key: IdentityKey
    logical_row_idx: int
    occurrence_rank: int
    transaction_id: str | None


@dataclass(frozen=True, slots=True)
class IdentityReconciliation:
    """Maximum-multiplicity result for one identity."""

    identity_key: IdentityKey
    n_existing: int
    n_new: int
    create_ranks: tuple[int, ...]
    assignments: tuple[OccurrenceAssignment, ...]


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    """Immutable result of planning a reconciliation without accessing a database."""

    identities: tuple[IdentityReconciliation, ...]
    anomalies: tuple[ReconciliationAnomaly, ...]

    @property
    def can_commit(self) -> bool:
        return not self.anomalies

    @property
    def assignments(self) -> tuple[OccurrenceAssignment, ...]:
        return tuple(assignment for group in self.identities for assignment in group.assignments)


def identity_key_sha256(identity_key: IdentityKey) -> str:
    """Hash the four typed identity fields using unambiguous UTF-8 length framing."""

    account_id, booked_on, amount_cents, identity_label = identity_key
    values = (
        account_id.encode("utf-8"),
        booked_on.encode("utf-8"),
        str(amount_cents).encode("ascii"),
        identity_label.encode("utf-8"),
    )
    digest = hashlib.sha256()
    for value in values:
        digest.update(len(value).to_bytes(8, "big", signed=False))
        digest.update(value)
    return digest.hexdigest()


def plan_reconciliation(
    existing: Iterable[ExistingOccurrence], incoming: Iterable[IncomingOccurrence]
) -> ReconciliationPlan:
    """Plan ranks and source links for every identity using maximum multiplicity.

    Existing ranks must be exactly ``1..n``. Incoming duplicates are ordered by
    ``logical_row_idx`` solely to make source-to-rank links deterministic. The
    transaction set is independent of the order of either iterable.
    """

    existing_groups: dict[IdentityKey, list[ExistingOccurrence]] = defaultdict(list)
    incoming_groups: dict[IdentityKey, list[IncomingOccurrence]] = defaultdict(list)
    anomalies: list[ReconciliationAnomaly] = []

    for existing_occurrence in existing:
        if (
            type(existing_occurrence.occurrence_rank) is not int
            or existing_occurrence.occurrence_rank < 1
        ):
            anomalies.append(
                ReconciliationAnomaly(
                    "INVALID_EXISTING_RANK",
                    existing_occurrence.identity_key,
                    0,
                    existing_occurrence.occurrence_rank,
                )
            )
            continue
        existing_groups[existing_occurrence.identity_key].append(existing_occurrence)

    for incoming_occurrence in incoming:
        if (
            type(incoming_occurrence.logical_row_idx) is not int
            or incoming_occurrence.logical_row_idx < 1
        ):
            anomalies.append(
                ReconciliationAnomaly(
                    "INVALID_LOGICAL_ROW_INDEX", incoming_occurrence.identity_key, 0, 0
                )
            )
            continue
        if type(incoming_occurrence.identity_key[2]) is not int:
            anomalies.append(
                ReconciliationAnomaly(
                    "INVALID_AMOUNT_CENTS", incoming_occurrence.identity_key, 0, 0
                )
            )
            continue
        incoming_groups[incoming_occurrence.identity_key].append(incoming_occurrence)

    plans: list[IdentityReconciliation] = []
    all_keys = sorted(set(existing_groups) | set(incoming_groups))
    for identity_key in all_keys:
        old_occurrences = existing_groups[identity_key]
        ranks = sorted(old.occurrence_rank for old in old_occurrences)
        n_existing = max(ranks, default=0)
        if len(ranks) != n_existing or ranks != list(range(1, n_existing + 1)):
            anomalies.append(
                ReconciliationAnomaly(
                    "NON_CONTIGUOUS_EXISTING_RANKS", identity_key, len(ranks), n_existing
                )
            )

        new_occurrences = sorted(incoming_groups[identity_key], key=lambda new: new.logical_row_idx)
        logical_indexes = [new.logical_row_idx for new in new_occurrences]
        if len(logical_indexes) != len(set(logical_indexes)):
            anomalies.append(
                ReconciliationAnomaly(
                    "DUPLICATE_LOGICAL_ROW_INDEX", identity_key, len(logical_indexes), 0
                )
            )
        n_new = len(new_occurrences)
        existing_by_rank = {old.occurrence_rank: old.transaction_id for old in old_occurrences}
        assignments = tuple(
            OccurrenceAssignment(
                identity_key=identity_key,
                logical_row_idx=new.logical_row_idx,
                occurrence_rank=rank,
                transaction_id=existing_by_rank.get(rank),
            )
            for rank, new in enumerate(new_occurrences, start=1)
        )
        plans.append(
            IdentityReconciliation(
                identity_key=identity_key,
                n_existing=n_existing,
                n_new=n_new,
                create_ranks=tuple(range(n_existing + 1, max(n_existing, n_new) + 1)),
                assignments=assignments,
            )
        )

    return ReconciliationPlan(tuple(plans), tuple(anomalies))
