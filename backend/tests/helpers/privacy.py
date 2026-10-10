"""Privacy assertions for synthetic bank account identifiers in API output."""

from __future__ import annotations

import re
from typing import Any

_CANONICAL_UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")
_CANONICAL_SECTION_ID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}:[1-9][0-9]*$")
_CANONICAL_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# These paths come from the documented Pydantic response schemas. Paths are
# explicit so an extra field named account_id cannot inherit an exemption.
_UUID_IDENTIFIER_PATHS = (
    re.compile(r"^\$\.(?:preview_id|import_file_id)$"),
    re.compile(r"^\$\.sections\[\d+\]\.account_id$"),
    re.compile(r"^\$\.sections\[\d+\]\.account_mapping\.account_id$"),
    re.compile(r"^\$\.items\[\d+\]\.(?:account_id|balance_check_id)$"),
    re.compile(r"^\$\.coverage\.sections\[\d+\]\.account_id$"),
    re.compile(r"^\$\.items\[\d+\]\.coverage\.sections\[\d+\]\.account_id$"),
    re.compile(r"^\$\.accounts\[\d+\]$"),
)
_SECTION_REFERENCE_PATHS = (
    re.compile(r"^\$\.coverage\.sections\[\d+\]\.section_id$"),
    re.compile(r"^\$\.items\[\d+\]\.coverage\.sections\[\d+\]\.section_id$"),
    re.compile(r"^\$\.items\[\d+\]\.(?:start_section_id|end_section_id)$"),
)
_SHA256_IDENTIFIER_PATHS = (
    re.compile(r"^\$\.file_sha256$"),
    re.compile(r"^\$\.items\[\d+\]\.input_snapshot_sha256$"),
)
_ACCOUNT_ID_MAP_PATHS = (
    re.compile(r"^\$\.coverage\.complete_intervals_by_account$"),
    re.compile(r"^\$\.items\[\d+\]\.coverage\.complete_intervals_by_account$"),
)


def _sensitive_account_fragments(account_number: str) -> set[str]:
    fragments = {account_number} if account_number else set()
    if len(account_number) >= 4:
        fragments.update((account_number[:4], account_number[-4:]))
    return fragments


def _assert_no_fragments(value: str, fragments: set[str], location: str) -> None:
    for fragment in fragments:
        assert fragment not in value, f"compte synthétique divulgué à {location}: {fragment!r}"


def assert_payload_has_no_account_numbers(
    payload: Any, raw_response_text: str, account_numbers: set[str]
) -> set[str]:
    """Reject account digits in payloads, exempting only canonical contract IDs."""
    fragments = set().union(*(_sensitive_account_fragments(number) for number in account_numbers))
    allowed_identifiers: set[str] = set()

    def visit(value: Any, path: str = "$", key: str | None = None) -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                key_text = str(child_key)
                is_account_key = any(
                    pattern.fullmatch(path) for pattern in _ACCOUNT_ID_MAP_PATHS
                ) and bool(_CANONICAL_UUID.fullmatch(key_text))
                if is_account_key:
                    allowed_identifiers.add(key_text)
                else:
                    _assert_no_fragments(key_text, fragments, f"clé {path}.{key_text}")
                visit(child_value, f"{path}.{key_text}", key_text)
        elif isinstance(value, list):
            for index, child_value in enumerate(value):
                visit(child_value, f"{path}[{index}]", key)
        elif isinstance(value, str):
            is_uuid_path = any(pattern.fullmatch(path) for pattern in _UUID_IDENTIFIER_PATHS)
            is_section_reference = any(
                pattern.fullmatch(path) for pattern in _SECTION_REFERENCE_PATHS
            ) and bool(_CANONICAL_SECTION_ID.fullmatch(value))
            is_sha256_path = any(pattern.fullmatch(path) for pattern in _SHA256_IDENTIFIER_PATHS)
            is_contract_identifier = (
                (is_uuid_path and bool(_CANONICAL_UUID.fullmatch(value)))
                or is_section_reference
                or (is_sha256_path and bool(_CANONICAL_SHA256.fullmatch(value)))
            )
            if is_contract_identifier:
                allowed_identifiers.add(value)
                return
            _assert_no_fragments(value, fragments, f"valeur {path}")

    visit(payload)

    # Check serialized JSON too, masking only values validated at contract paths.
    masked_raw_text = raw_response_text
    for identifier in allowed_identifiers:
        masked_raw_text = masked_raw_text.replace(identifier, "<CONTRACT_IDENTIFIER>")
    _assert_no_fragments(masked_raw_text, fragments, "JSON brut après masquage des identifiants")
    return allowed_identifiers


def assert_no_account_numbers_in_logs(
    log_text: str, account_numbers: set[str], allowed_identifiers: set[str]
) -> None:
    fragments = set().union(*(_sensitive_account_fragments(number) for number in account_numbers))
    masked_log_text = log_text
    for identifier in allowed_identifiers:
        masked_log_text = masked_log_text.replace(identifier, "<CONTRACT_IDENTIFIER>")
    _assert_no_fragments(masked_log_text, fragments, "logs")
