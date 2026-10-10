-- Schéma de tête SQLite — Comptes familiaux v1, révisions 0001 + 0002 (T17)
-- À poser sur CHAQUE connexion SQLite (PRAGMA locaux à la connexion) :
-- foreign_keys pour toutes les FK, recursive_triggers pour rendre REPLACE visible
-- aux triggers d'interdiction de suppression. REPLACE reste interdit dans le code.
PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;

CREATE TABLE accounts (
    account_id TEXT NOT NULL PRIMARY KEY CHECK (length(account_id) = 36 AND account_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    alias TEXT NOT NULL CHECK (length(trim(alias)) > 0),
    account_role TEXT NOT NULL CHECK (account_role IN (
        'current', 'savings', 'pea', 'personal', 'cash', 'other'
    )),
    bank_fingerprint TEXT UNIQUE,
    fingerprint_key_version INTEGER CHECK (fingerprint_key_version IS NULL OR fingerprint_key_version > 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) = updated_at_utc)),
    CHECK ((bank_fingerprint IS NULL AND fingerprint_key_version IS NULL)
        OR (length(bank_fingerprint) = 64 AND fingerprint_key_version IS NOT NULL))
);

CREATE TRIGGER trg_accounts_version_increment
BEFORE UPDATE ON accounts
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;


CREATE TABLE import_files (
    import_file_id TEXT NOT NULL PRIMARY KEY CHECK (length(import_file_id) = 36 AND import_file_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    file_sha256 TEXT NOT NULL CHECK (
        length(file_sha256) = 64 AND file_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    archive_relpath TEXT UNIQUE,
    archive_storage_mode TEXT,
    archive_permissions_octal TEXT,
    file_size_bytes INTEGER NOT NULL CHECK (typeof(file_size_bytes) = 'integer' AND file_size_bytes >= 0),
    received_on TEXT NOT NULL CHECK (date(received_on) = received_on),
    generation_date TEXT NOT NULL CHECK (date(generation_date) = generation_date),
    generation_date_source TEXT NOT NULL
        CHECK (generation_date_source IN ('header_download', 'received_on_fallback')),
    parsed_operation_count INTEGER NOT NULL DEFAULT 0
        CHECK (typeof(parsed_operation_count) = 'integer' AND parsed_operation_count >= 0),
    detected_encoding TEXT CHECK (detected_encoding IS NULL OR detected_encoding IN ('cp1252', 'utf-8', 'utf-8-sig')),
    status TEXT NOT NULL CHECK (status IN ('previewed', 'committed', 'rejected', 'no_op')),
    duplicate_of_import_file_id TEXT REFERENCES import_files(import_file_id) ON DELETE RESTRICT,
    failure_code TEXT,
    failure_summary TEXT,
    started_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', started_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', started_at_utc) = started_at_utc)),
    finished_at_utc TEXT CHECK (finished_at_utc IS NULL OR (strftime('%Y-%m-%dT%H:%M:%fZ', finished_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', finished_at_utc) = finished_at_utc)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    CHECK ((status = 'no_op' AND duplicate_of_import_file_id IS NOT NULL)
        OR (status <> 'no_op' AND duplicate_of_import_file_id IS NULL)),
    CHECK (duplicate_of_import_file_id IS NULL OR duplicate_of_import_file_id <> import_file_id),
    CHECK (generation_date_source <> 'received_on_fallback' OR generation_date = received_on),
    CHECK (
        (status = 'no_op' AND archive_relpath IS NULL
            AND archive_storage_mode IS NULL AND archive_permissions_octal IS NULL)
        OR (status <> 'no_op' AND archive_relpath IS NOT NULL
            AND archive_relpath = 'data/originals/' || import_file_id || '.bin'
            AND archive_storage_mode = 'raw' AND archive_permissions_octal = '0600')
    )
);

CREATE TRIGGER trg_import_files_version_increment
BEFORE UPDATE ON import_files
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;


-- Une empreinte ne peut être committée qu'une fois. Les tentatives répétées sont
-- conservées comme status=no_op, rattachées au commit initial, sans source_rows.
CREATE UNIQUE INDEX uq_import_files_committed_sha256
    ON import_files(file_sha256) WHERE status = 'committed';
CREATE INDEX ix_import_files_started_at ON import_files(started_at_utc DESC, import_file_id DESC);

CREATE TABLE import_account_sections (
    import_file_id TEXT NOT NULL REFERENCES import_files(import_file_id) ON DELETE RESTRICT CHECK (length(import_file_id) = 36 AND import_file_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    section_index INTEGER NOT NULL CHECK (section_index > 0),
    account_id TEXT REFERENCES accounts(account_id) ON DELETE RESTRICT,
    account_fingerprint TEXT CHECK (account_fingerprint IS NULL OR length(account_fingerprint) = 64),
    coverage_from TEXT CHECK (coverage_from IS NULL OR date(coverage_from) = coverage_from),
    coverage_through TEXT CHECK (coverage_through IS NULL OR date(coverage_through) = coverage_through),
    complete_through TEXT CHECK (complete_through IS NULL OR date(complete_through) = complete_through),
    balance_as_of TEXT CHECK (balance_as_of IS NULL OR date(balance_as_of) = balance_as_of),
    header_balance_cents INTEGER CHECK (header_balance_cents IS NULL OR typeof(header_balance_cents) = 'integer'),
    section_operation_count INTEGER CHECK (section_operation_count IS NULL OR
        (typeof(section_operation_count) = 'integer' AND section_operation_count >= 0)),
    PRIMARY KEY (import_file_id, section_index),
    CHECK (coverage_from IS NULL OR coverage_through IS NULL OR coverage_from <= coverage_through)
);
CREATE INDEX ix_import_sections_account_coverage
    ON import_account_sections(account_id, coverage_from, coverage_through);
-- IMP-13 : un même compte ne peut être représenté que par une section d'un fichier.
CREATE UNIQUE INDEX uq_import_sections_file_account
    ON import_account_sections(import_file_id, account_id) WHERE account_id IS NOT NULL;

-- Snapshot append-only : les résultats sont liés à une empreinte de l'ensemble
-- d'entrées et recalculés quand cet ensemble change; l'ordre d'import est sans effet.
CREATE TABLE balance_checks (
    balance_check_id TEXT NOT NULL PRIMARY KEY CHECK (length(balance_check_id) = 36 AND balance_check_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE RESTRICT,
    start_import_file_id TEXT NOT NULL,
    start_section_index INTEGER NOT NULL,
    end_import_file_id TEXT NOT NULL,
    end_section_index INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('match', 'mismatch', 'not_comparable')),
    recalculated_balance_cents INTEGER CHECK (recalculated_balance_cents IS NULL OR
        typeof(recalculated_balance_cents) = 'integer'),
    delta_cents INTEGER CHECK (delta_cents IS NULL OR typeof(delta_cents) = 'integer'),
    input_snapshot_sha256 TEXT NOT NULL CHECK (
        length(input_snapshot_sha256) = 64 AND input_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    FOREIGN KEY (start_import_file_id, start_section_index)
        REFERENCES import_account_sections(import_file_id, section_index) ON DELETE RESTRICT,
    FOREIGN KEY (end_import_file_id, end_section_index)
        REFERENCES import_account_sections(import_file_id, section_index) ON DELETE RESTRICT,
    UNIQUE (account_id, start_import_file_id, start_section_index,
            end_import_file_id, end_section_index, input_snapshot_sha256),
    CHECK ((status = 'not_comparable' AND recalculated_balance_cents IS NULL AND delta_cents IS NULL)
        OR (status IN ('match', 'mismatch') AND recalculated_balance_cents IS NOT NULL AND delta_cents IS NOT NULL)),
    CHECK (start_import_file_id <> end_import_file_id OR start_section_index <> end_section_index)
);

CREATE TRIGGER trg_balance_checks_version_increment
BEFORE UPDATE ON balance_checks
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;

CREATE INDEX ix_balance_checks_account_created
    ON balance_checks(account_id, created_at_utc DESC, balance_check_id DESC);

CREATE TRIGGER trg_balance_checks_validate_insert
BEFORE INSERT ON balance_checks
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM import_account_sections a
        JOIN import_files af ON af.import_file_id = a.import_file_id
        JOIN import_account_sections b
        JOIN import_files bf ON bf.import_file_id = b.import_file_id
        WHERE a.import_file_id = NEW.start_import_file_id
          AND a.section_index = NEW.start_section_index
          AND b.import_file_id = NEW.end_import_file_id
          AND b.section_index = NEW.end_section_index
          AND a.account_id = NEW.account_id AND b.account_id = NEW.account_id
          AND af.status = 'committed' AND bf.status = 'committed'
    ) THEN RAISE(ABORT, 'balance_check_sections_not_committed_same_account') END;
    SELECT CASE WHEN NEW.status IN ('match', 'mismatch') AND NOT EXISTS (
        SELECT 1 FROM import_account_sections a
        JOIN import_account_sections b ON b.import_file_id = NEW.end_import_file_id
            AND b.section_index = NEW.end_section_index
        WHERE a.import_file_id = NEW.start_import_file_id
          AND a.section_index = NEW.start_section_index
          AND a.balance_as_of IS NOT NULL AND b.balance_as_of IS NOT NULL
          AND a.header_balance_cents IS NOT NULL AND b.header_balance_cents IS NOT NULL
          AND a.complete_through IS NOT NULL AND b.complete_through IS NOT NULL
          AND a.balance_as_of <= a.complete_through AND b.balance_as_of <= b.complete_through
          AND a.balance_as_of < b.balance_as_of
          AND NEW.delta_cents = b.header_balance_cents - NEW.recalculated_balance_cents
          AND ((NEW.status = 'match' AND NEW.delta_cents = 0)
               OR (NEW.status = 'mismatch' AND NEW.delta_cents <> 0))
    ) THEN RAISE(ABORT, 'balance_check_not_comparable_inputs') END;
END;
CREATE TRIGGER trg_balance_checks_immutable_update
BEFORE UPDATE ON balance_checks
BEGIN
    SELECT RAISE(ABORT, 'balance_check_append_only');
END;
CREATE TRIGGER trg_balance_checks_immutable_delete
BEFORE DELETE ON balance_checks
BEGIN
    SELECT RAISE(ABORT, 'balance_check_append_only');
END;

CREATE TABLE import_rejections (
    import_rejection_id TEXT NOT NULL PRIMARY KEY CHECK (length(import_rejection_id) = 36 AND import_rejection_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    import_file_id TEXT NOT NULL REFERENCES import_files(import_file_id) ON DELETE RESTRICT,
    section_index INTEGER,
    logical_row_idx INTEGER CHECK (logical_row_idx IS NULL OR
        (typeof(logical_row_idx) = 'integer' AND logical_row_idx > 0)),
    field_name TEXT,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('warning', 'error')),
    requires_resolution INTEGER NOT NULL DEFAULT 0 CHECK (requires_resolution IN (0, 1)),
    resolution_status TEXT NOT NULL DEFAULT 'not_required'
        CHECK (resolution_status IN ('not_required', 'pending', 'resolved')),
    resolution_action TEXT CHECK (resolution_action IS NULL OR
        resolution_action IN ('link_existing', 'create_occurrence', 'map_account', 'set_coverage')),
    resolved_transaction_id TEXT REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    resolved_by TEXT,
    resolved_at_utc TEXT CHECK (resolved_at_utc IS NULL OR (strftime('%Y-%m-%dT%H:%M:%fZ', resolved_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', resolved_at_utc) = resolved_at_utc)),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    FOREIGN KEY (import_file_id, section_index)
        REFERENCES import_account_sections(import_file_id, section_index) ON DELETE RESTRICT,
    CHECK ((requires_resolution = 0 AND resolution_status = 'not_required')
        OR (requires_resolution = 1 AND resolution_status IN ('pending', 'resolved'))),
    CHECK ((resolution_status <> 'resolved')
        OR (resolution_action IS NOT NULL AND resolved_by IS NOT NULL AND resolved_at_utc IS NOT NULL)),
    CHECK (code <> 'OUT_OF_PERIOD' OR requires_resolution = 1)
);
CREATE INDEX ix_import_rejections_file
    ON import_rejections(import_file_id, severity, section_index, logical_row_idx, import_rejection_id);

CREATE TRIGGER trg_import_rejections_insert_previewed_only
BEFORE INSERT ON import_rejections
WHEN NOT EXISTS (
    SELECT 1 FROM import_files f
    WHERE f.import_file_id = NEW.import_file_id AND f.status = 'previewed'
)
BEGIN
    SELECT RAISE(ABORT, 'import_rejection_requires_previewed_file');
END;

CREATE TRIGGER trg_import_rejections_committed_immutable_update
BEFORE UPDATE ON import_rejections
WHEN EXISTS (
    SELECT 1 FROM import_files f
    WHERE f.import_file_id = OLD.import_file_id AND f.status = 'committed'
)
BEGIN
    SELECT RAISE(ABORT, 'committed_import_rejection_immutable');
END;
CREATE TRIGGER trg_import_rejections_committed_immutable_delete
BEFORE DELETE ON import_rejections
WHEN EXISTS (
    SELECT 1 FROM import_files f
    WHERE f.import_file_id = OLD.import_file_id AND f.status = 'committed'
)
BEGIN
    SELECT RAISE(ABORT, 'committed_import_rejection_immutable');
END;

CREATE TABLE payees (
    payee_id TEXT NOT NULL PRIMARY KEY CHECK (length(payee_id) = 36 AND payee_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    normalized_name TEXT NOT NULL CHECK (length(trim(normalized_name)) > 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) = updated_at_utc)),
    UNIQUE (normalized_name)
);

CREATE TRIGGER trg_payees_version_increment
BEFORE UPDATE ON payees
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;


CREATE TABLE payee_aliases (
    payee_alias_id TEXT NOT NULL PRIMARY KEY CHECK (length(payee_alias_id) = 36 AND payee_alias_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    payee_id TEXT NOT NULL REFERENCES payees(payee_id) ON DELETE RESTRICT,
    alias_text TEXT NOT NULL CHECK (length(trim(alias_text)) > 0),
    normalized_alias TEXT NOT NULL CHECK (length(trim(normalized_alias)) > 0),
    match_kind TEXT NOT NULL CHECK (match_kind IN ('exact', 'approved_pattern')),
    approved_by TEXT,
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    UNIQUE (normalized_alias)
);

CREATE TRIGGER trg_payee_aliases_version_increment
BEFORE UPDATE ON payee_aliases
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;

CREATE INDEX ix_payee_aliases_payee ON payee_aliases(payee_id);

CREATE TABLE categories (
    category_id TEXT NOT NULL PRIMARY KEY,
    parent_category_id TEXT REFERENCES categories(category_id) ON DELETE RESTRICT,
    label TEXT NOT NULL CHECK (length(trim(label)) > 0),
    taxonomy_version INTEGER NOT NULL CHECK (taxonomy_version > 0),
    sort_order INTEGER NOT NULL DEFAULT 0 CHECK (typeof(sort_order) = 'integer'),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) = updated_at_utc)),
    CHECK (parent_category_id IS NULL OR parent_category_id <> category_id)
);

CREATE TRIGGER trg_categories_version_increment
BEFORE UPDATE ON categories
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;

CREATE INDEX ix_categories_parent ON categories(parent_category_id, sort_order, category_id);

-- Table d'admissibilité normalisée : elle rend les combinaisons issues de
-- DOMAIN_MODEL.md vérifiables par une FK composite, y compris income_benefits.
CREATE TABLE category_flow_kinds (
    category_id TEXT NOT NULL REFERENCES categories(category_id) ON DELETE RESTRICT,
    flow_kind TEXT NOT NULL CHECK (flow_kind IN (
        'expense', 'income_recurring', 'income_other', 'transfer_savings',
        'transfer_owned', 'allocation_personal', 'loan_disbursement',
        'loan_repayment', 'capital_event', 'cash_withdrawal_pending', 'refund',
        'intermediary_pending', 'reconciliation'
    )),
    PRIMARY KEY (category_id, flow_kind)
);

CREATE TABLE transactions (
    transaction_id TEXT NOT NULL PRIMARY KEY CHECK (length(transaction_id) = 36 AND transaction_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE RESTRICT,
    booked_on TEXT NOT NULL CHECK (date(booked_on) = booked_on),
    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents <> 0),
    raw_label TEXT NOT NULL,
    identity_label TEXT NOT NULL CHECK (
        length(identity_label) > 0 AND identity_label = trim(identity_label)
        AND instr(identity_label, '  ') = 0
    ),
    identity_key_sha256 TEXT NOT NULL CHECK (
        length(identity_key_sha256) = 64 AND identity_key_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    occurrence_rank INTEGER NOT NULL CHECK (typeof(occurrence_rank) = 'integer' AND occurrence_rank >= 1),
    payee_id TEXT REFERENCES payees(payee_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    imported_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', imported_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', imported_at_utc) = imported_at_utc)),
    UNIQUE (account_id, booked_on, amount_cents, identity_label, occurrence_rank)
);

CREATE TRIGGER trg_transactions_version_increment
BEFORE UPDATE ON transactions
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;

CREATE INDEX ix_transactions_account_booked
    ON transactions(account_id, booked_on DESC, transaction_id DESC);
CREATE INDEX ix_transactions_payee_booked ON transactions(payee_id, booked_on DESC);

CREATE TRIGGER trg_transactions_occurrence_contiguous
BEFORE INSERT ON transactions
WHEN NEW.occurrence_rank > 1 AND NOT EXISTS (
    SELECT 1 FROM transactions prior
    WHERE prior.account_id = NEW.account_id
      AND prior.booked_on = NEW.booked_on
      AND prior.amount_cents = NEW.amount_cents
      AND prior.identity_label = NEW.identity_label
      AND prior.occurrence_rank = NEW.occurrence_rank - 1
)
BEGIN
    SELECT RAISE(ABORT, 'occurrence_rank_not_contiguous');
END;

CREATE TABLE source_rows (
    source_row_id TEXT NOT NULL PRIMARY KEY CHECK (length(source_row_id) = 36 AND source_row_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    import_file_id TEXT NOT NULL,
    section_index INTEGER NOT NULL,
    logical_row_idx INTEGER NOT NULL CHECK (typeof(logical_row_idx) = 'integer' AND logical_row_idx > 0),
    physical_line_start INTEGER CHECK (physical_line_start IS NULL OR physical_line_start > 0),
    physical_line_end INTEGER CHECK (physical_line_end IS NULL OR physical_line_end >= physical_line_start),
    row_sha256 TEXT NOT NULL CHECK (length(row_sha256) = 64 AND row_sha256 NOT GLOB '*[^0-9a-f]*'),
    identity_key_sha256 TEXT NOT NULL CHECK (
        length(identity_key_sha256) = 64 AND identity_key_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    FOREIGN KEY (import_file_id, section_index)
        REFERENCES import_account_sections(import_file_id, section_index) ON DELETE RESTRICT,
    UNIQUE (import_file_id, section_index, logical_row_idx),
    UNIQUE (import_file_id, transaction_id)
);
CREATE INDEX ix_source_rows_transaction ON source_rows(transaction_id, import_file_id);

CREATE TRIGGER trg_source_rows_identity_matches_transaction
BEFORE INSERT ON source_rows
WHEN NOT EXISTS (
    SELECT 1 FROM transactions t
    WHERE t.transaction_id = NEW.transaction_id
      AND t.identity_key_sha256 = NEW.identity_key_sha256
)
BEGIN
    SELECT RAISE(ABORT, 'source_row_identity_key_mismatch');
END;

CREATE TRIGGER trg_source_rows_import_status
BEFORE INSERT ON source_rows
WHEN NOT EXISTS (
    SELECT 1 FROM import_files f
    WHERE f.import_file_id = NEW.import_file_id AND f.status IN ('previewed', 'committed')
)
BEGIN
    SELECT RAISE(ABORT, 'source_row_import_status_invalid');
END;

CREATE TRIGGER trg_source_rows_account_matches_section
BEFORE INSERT ON source_rows
WHEN NOT EXISTS (
    SELECT 1
    FROM import_account_sections s
    JOIN transactions t ON t.transaction_id = NEW.transaction_id
    WHERE s.import_file_id = NEW.import_file_id
      AND s.section_index = NEW.section_index
      AND s.account_id IS NOT NULL
      AND s.account_id = t.account_id
)
BEGIN
    SELECT RAISE(ABORT, 'source_row_account_mismatch');
END;

CREATE TRIGGER trg_source_rows_import_capacity
BEFORE INSERT ON source_rows
WHEN EXISTS (
    SELECT 1 FROM import_files f
    WHERE f.import_file_id = NEW.import_file_id AND f.status = 'committed'
      AND (SELECT count(*) FROM source_rows s WHERE s.import_file_id = NEW.import_file_id)
          >= f.parsed_operation_count
)
BEGIN
    SELECT RAISE(ABORT, 'source_row_import_already_complete');
END;

CREATE TABLE import_reconciliation_candidates (
    reconciliation_candidate_id TEXT NOT NULL PRIMARY KEY CHECK (length(reconciliation_candidate_id) = 36 AND reconciliation_candidate_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    source_row_id TEXT NOT NULL REFERENCES source_rows(source_row_id) ON DELETE RESTRICT,
    candidate_transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    reason_code TEXT NOT NULL CHECK (reason_code IN (
        'case_difference', 'accent_difference', 'punctuation_difference', 'near_label',
        'coverage_boundary_ambiguous', 'other'
    )),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'reviewed_distinct', 'resolved_by_explicit_action')),
    reviewed_by TEXT,
    reviewed_at_utc TEXT CHECK (reviewed_at_utc IS NULL OR (strftime('%Y-%m-%dT%H:%M:%fZ', reviewed_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', reviewed_at_utc) = reviewed_at_utc)),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    CHECK (status = 'open' OR (reviewed_by IS NOT NULL AND reviewed_at_utc IS NOT NULL)),
    CHECK (source_row_id IS NOT NULL AND candidate_transaction_id IS NOT NULL),
    UNIQUE (source_row_id, candidate_transaction_id)
);

CREATE TRIGGER trg_import_reconciliation_candidates_version_increment
BEFORE UPDATE ON import_reconciliation_candidates
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;

CREATE INDEX ix_reconciliation_candidates_review
    ON import_reconciliation_candidates(status, created_at_utc, reconciliation_candidate_id);

CREATE TABLE allocations (
    allocation_id TEXT NOT NULL PRIMARY KEY CHECK (length(allocation_id) = 36 AND allocation_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    category_id TEXT NOT NULL,
    flow_kind TEXT NOT NULL CHECK (flow_kind IN (
        'expense', 'income_recurring', 'income_other', 'transfer_savings',
        'transfer_owned', 'allocation_personal', 'loan_disbursement',
        'loan_repayment', 'capital_event', 'cash_withdrawal_pending', 'refund',
        'intermediary_pending', 'reconciliation'
    )),
    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents <> 0),
    origin TEXT NOT NULL CHECK (origin IN ('manual', 'rule', 'history', 'llm')),
    decision TEXT NOT NULL DEFAULT 'confirmed' CHECK (decision = 'confirmed'),
    beneficiary TEXT NOT NULL DEFAULT 'unknown'
        CHECK (beneficiary IN ('household', 'adult_a', 'adult_b', 'child_a', 'child_b', 'unknown')),
    cadence TEXT NOT NULL DEFAULT 'unknown'
        CHECK (cadence IN ('recurring', 'occasional', 'exceptional', 'unknown')),
    flexibility TEXT NOT NULL DEFAULT 'unknown'
        CHECK (flexibility IN ('fixed', 'variable_essential', 'variable_optional', 'unknown')),
    acquisition TEXT NOT NULL DEFAULT 'unknown'
        CHECK (acquisition IN ('new', 'second_hand', 'not_applicable', 'unknown')),
    superseded_by_allocation_id TEXT REFERENCES allocations(allocation_id)
        DEFERRABLE INITIALLY DEFERRED,
    superseded_by_audit_event_id TEXT REFERENCES audit_events(audit_event_id)
        DEFERRABLE INITIALLY DEFERRED,
    retracted_at_utc TEXT
        CHECK (retracted_at_utc IS NULL OR
            (strftime('%Y-%m-%dT%H:%M:%fZ', retracted_at_utc) IS NOT NULL
             AND strftime('%Y-%m-%dT%H:%M:%fZ', retracted_at_utc) = retracted_at_utc)),
    retracted_by_decision_id TEXT REFERENCES classification_decisions(decision_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    FOREIGN KEY (category_id, flow_kind)
        REFERENCES category_flow_kinds(category_id, flow_kind) ON DELETE RESTRICT,
    CHECK (
        (flow_kind IN ('expense', 'allocation_personal', 'loan_repayment', 'capital_event',
                       'cash_withdrawal_pending', 'intermediary_pending') AND amount_cents < 0)
        OR (flow_kind IN ('income_recurring', 'income_other', 'loan_disbursement', 'refund') AND amount_cents > 0)
        OR flow_kind IN ('transfer_savings', 'transfer_owned', 'reconciliation')
    ),
    CHECK ((superseded_by_allocation_id IS NULL AND superseded_by_audit_event_id IS NULL)
        OR (superseded_by_allocation_id IS NOT NULL AND superseded_by_audit_event_id IS NOT NULL
            AND superseded_by_allocation_id <> allocation_id)),
    CHECK ((retracted_at_utc IS NULL AND retracted_by_decision_id IS NULL)
        OR (retracted_at_utc IS NOT NULL AND retracted_by_decision_id IS NOT NULL
            AND superseded_by_allocation_id IS NULL AND superseded_by_audit_event_id IS NULL))
);

CREATE TRIGGER trg_allocations_version_increment
BEFORE UPDATE ON allocations
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;

CREATE INDEX ix_allocations_transaction ON allocations(transaction_id, decision);
CREATE INDEX ix_allocations_review ON allocations(decision, origin, transaction_id);
CREATE INDEX ix_allocations_active_transaction ON allocations(transaction_id)
    WHERE superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL;

CREATE TABLE classification_suggestions (
    suggestion_id TEXT NOT NULL PRIMARY KEY CHECK (length(suggestion_id) = 36 AND suggestion_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    proposed_flow_kind TEXT NOT NULL CHECK (proposed_flow_kind IN (
        'expense', 'income_recurring', 'income_other', 'transfer_savings',
        'transfer_owned', 'allocation_personal', 'loan_disbursement',
        'loan_repayment', 'capital_event', 'cash_withdrawal_pending', 'refund',
        'intermediary_pending', 'reconciliation', 'unknown'
    )),
    category_id TEXT REFERENCES categories(category_id) ON DELETE RESTRICT,
    suggested_amount_cents INTEGER CHECK (suggested_amount_cents IS NULL OR
        (typeof(suggested_amount_cents) = 'integer' AND suggested_amount_cents <> 0)),
    method TEXT NOT NULL CHECK (method IN ('rule', 'history', 'heuristic', 'llm', 'manual')),
    confidence_label TEXT NOT NULL DEFAULT 'doute'
        CHECK (confidence_label IN ('certain', 'doute', 'inconnu')),
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'review' CHECK (status IN ('review', 'accepted', 'rejected', 'superseded')),
    llm_run_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    FOREIGN KEY (category_id, proposed_flow_kind)
        REFERENCES category_flow_kinds(category_id, flow_kind) ON DELETE RESTRICT,
    FOREIGN KEY (llm_run_id) REFERENCES llm_runs(llm_run_id) ON DELETE RESTRICT
);

CREATE TRIGGER trg_classification_suggestions_version_increment
BEFORE UPDATE ON classification_suggestions
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;

CREATE INDEX ix_suggestions_review ON classification_suggestions(status, method, created_at_utc, suggestion_id);
CREATE INDEX ix_suggestions_transaction ON classification_suggestions(transaction_id, status);

CREATE TABLE classification_decisions (
    decision_id TEXT NOT NULL PRIMARY KEY CHECK (length(decision_id) = 36 AND decision_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    suggestion_id TEXT REFERENCES classification_suggestions(suggestion_id) ON DELETE RESTRICT,
    action TEXT NOT NULL CHECK (action IN ('confirm', 'edit', 'split', 'reject', 'clear', 'undo')),
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    before_json TEXT,
    after_json TEXT,
    undone_by_decision_id TEXT REFERENCES classification_decisions(decision_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc))
);

CREATE TRIGGER trg_classification_decisions_version_increment
BEFORE UPDATE ON classification_decisions
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;

CREATE INDEX ix_classification_decisions_transaction ON classification_decisions(transaction_id, created_at_utc);

CREATE TABLE rules (
    rule_id TEXT NOT NULL PRIMARY KEY CHECK (length(rule_id) = 36 AND rule_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    scope_kind TEXT NOT NULL CHECK (scope_kind IN ('exact_payee', 'label_pattern', 'payee_plus_context')),
    scope_json TEXT NOT NULL,
    flow_kind TEXT NOT NULL CHECK (flow_kind IN (
        'expense', 'income_recurring', 'income_other', 'transfer_savings',
        'transfer_owned', 'allocation_personal', 'loan_disbursement',
        'loan_repayment', 'capital_event', 'cash_withdrawal_pending', 'refund',
        'intermediary_pending', 'reconciliation'
    )),
    category_id TEXT NOT NULL,
    effect_json TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100 CHECK (typeof(priority) = 'integer'),
    is_approved INTEGER NOT NULL DEFAULT 0 CHECK (is_approved IN (0, 1)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    approved_by TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) = updated_at_utc)),
    FOREIGN KEY (category_id, flow_kind)
        REFERENCES category_flow_kinds(category_id, flow_kind) ON DELETE RESTRICT
);

CREATE TRIGGER trg_rules_version_increment
BEFORE UPDATE ON rules
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;

CREATE INDEX ix_rules_active_priority ON rules(is_active, is_approved, priority, rule_id);

CREATE TABLE rule_executions (
    rule_execution_id TEXT NOT NULL PRIMARY KEY CHECK (length(rule_execution_id) = 36 AND rule_execution_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    rule_id TEXT NOT NULL REFERENCES rules(rule_id) ON DELETE RESTRICT,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    outcome TEXT NOT NULL CHECK (outcome IN ('matched', 'applied', 'skipped_manual', 'skipped_scope', 'failed')),
    before_json TEXT,
    after_json TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1)),
    audit_event_id TEXT REFERENCES audit_events(audit_event_id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    executed_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', executed_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', executed_at_utc) = executed_at_utc))
);
CREATE INDEX ix_rule_executions_rule ON rule_executions(rule_id, executed_at_utc DESC);
CREATE INDEX ix_rule_executions_transaction ON rule_executions(transaction_id, executed_at_utc DESC);

CREATE TABLE transaction_links (
    transaction_link_id TEXT NOT NULL PRIMARY KEY CHECK (length(transaction_link_id) = 36 AND transaction_link_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    source_transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    target_transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    link_kind TEXT NOT NULL CHECK (link_kind IN ('refund_of', 'transfer_pair', 'related')),
    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents > 0),
    source_allocation_id TEXT REFERENCES allocations(allocation_id) ON DELETE RESTRICT,
    target_allocation_id TEXT REFERENCES allocations(allocation_id) ON DELETE RESTRICT,
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'confirmed', 'rejected')),
    superseded_by_transaction_link_id TEXT REFERENCES transaction_links(transaction_link_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    superseded_by_audit_event_id TEXT REFERENCES audit_events(audit_event_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    CHECK (source_transaction_id <> target_transaction_id),
    CHECK (link_kind <> 'refund_of' OR source_allocation_id IS NOT NULL),
    CHECK ((superseded_by_transaction_link_id IS NULL AND superseded_by_audit_event_id IS NULL)
        OR (superseded_by_transaction_link_id IS NOT NULL AND superseded_by_audit_event_id IS NOT NULL)),
    CHECK (superseded_by_transaction_link_id IS NULL
        OR superseded_by_transaction_link_id <> transaction_link_id)
);

CREATE TRIGGER trg_transaction_links_version_increment
BEFORE UPDATE ON transaction_links
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;

-- Les liens refund ne quittent proposed que vers confirmed ou rejected.
-- Confirmed/rejected sont terminaux; un remplacement confirmé utilise une
-- nouvelle ligne proposée et conserve l'ancienne ligne confirmée supersédée.
CREATE TRIGGER trg_refund_links_status_transition
BEFORE UPDATE OF status ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status <> OLD.status
 AND NOT (OLD.status = 'proposed' AND NEW.status IN ('confirmed', 'rejected'))
BEGIN
    SELECT RAISE(ABORT, 'refund_link_status_transition_invalid');
END;

CREATE UNIQUE INDEX uq_transaction_links_active
    ON transaction_links(source_transaction_id, source_allocation_id, target_transaction_id, link_kind,
                         COALESCE(target_allocation_id, ''))
    WHERE status IN ('proposed', 'confirmed') AND superseded_by_transaction_link_id IS NULL;
CREATE INDEX ix_transaction_links_source ON transaction_links(source_transaction_id, status, link_kind);
CREATE INDEX ix_transaction_links_target ON transaction_links(target_transaction_id, status, link_kind);

-- Les parts exactes d'un remboursement proratisé sont figées à sa confirmation.
-- Leur total correspond au lien; les agrégats analytiques relisent ces mêmes centimes.
CREATE TABLE refund_link_allocation_parts (
    transaction_link_id TEXT NOT NULL CHECK (length(transaction_link_id) = 36 AND transaction_link_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]')
        REFERENCES transaction_links(transaction_link_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    allocation_id TEXT NOT NULL CHECK (length(allocation_id) = 36 AND allocation_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]')
        REFERENCES allocations(allocation_id) ON DELETE RESTRICT,
    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents > 0),
    PRIMARY KEY (transaction_link_id, allocation_id)
);
CREATE INDEX ix_refund_link_allocation_parts_allocation
    ON refund_link_allocation_parts(allocation_id, transaction_link_id);

CREATE TRIGGER trg_refund_links_confirmed_direction_insert
BEFORE INSERT ON transaction_links
WHEN NEW.link_kind = 'refund_of'
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM transactions s
        JOIN transactions t ON t.transaction_id = NEW.target_transaction_id
        JOIN allocations src ON src.allocation_id = NEW.source_allocation_id
        WHERE s.transaction_id = NEW.source_transaction_id
          AND s.amount_cents > 0 AND t.amount_cents < 0
          AND s.account_id = t.account_id
          AND src.transaction_id = s.transaction_id AND src.flow_kind = 'refund'
          AND src.amount_cents > 0 AND src.superseded_by_allocation_id IS NULL
          AND src.retracted_at_utc IS NULL
          AND (NEW.target_allocation_id IS NULL OR EXISTS (
              SELECT 1 FROM allocations a WHERE a.allocation_id = NEW.target_allocation_id
                AND a.transaction_id = t.transaction_id AND a.flow_kind = 'expense'
                AND a.amount_cents < 0 AND a.superseded_by_allocation_id IS NULL
                AND a.retracted_at_utc IS NULL
          ))
          AND EXISTS (SELECT 1 FROM allocations a WHERE a.transaction_id = t.transaction_id
                      AND a.flow_kind = 'expense' AND a.amount_cents < 0
                      AND a.superseded_by_allocation_id IS NULL AND a.retracted_at_utc IS NULL)
    ) THEN RAISE(ABORT, 'refund_link_direction_invalid') END;
END;

CREATE TRIGGER trg_refund_links_confirmed_direction_update
BEFORE UPDATE ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status IN ('proposed', 'confirmed')
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM transactions s
        JOIN transactions t ON t.transaction_id = NEW.target_transaction_id
        JOIN allocations src ON src.allocation_id = NEW.source_allocation_id
        WHERE s.transaction_id = NEW.source_transaction_id
          AND s.amount_cents > 0 AND t.amount_cents < 0
          AND s.account_id = t.account_id
          AND src.transaction_id = s.transaction_id AND src.flow_kind = 'refund'
          AND src.amount_cents > 0 AND src.superseded_by_allocation_id IS NULL
          AND src.retracted_at_utc IS NULL
          AND (NEW.target_allocation_id IS NULL OR EXISTS (
              SELECT 1 FROM allocations a WHERE a.allocation_id = NEW.target_allocation_id
                AND a.transaction_id = t.transaction_id AND a.flow_kind = 'expense'
                AND a.amount_cents < 0 AND a.superseded_by_allocation_id IS NULL
                AND a.retracted_at_utc IS NULL
          ))
          AND EXISTS (SELECT 1 FROM allocations a WHERE a.transaction_id = t.transaction_id
                      AND a.flow_kind = 'expense' AND a.amount_cents < 0
                      AND a.superseded_by_allocation_id IS NULL AND a.retracted_at_utc IS NULL)
    ) THEN RAISE(ABORT, 'refund_link_direction_invalid') END;
END;

CREATE TRIGGER trg_refund_links_source_cap_insert
BEFORE INSERT ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status = 'confirmed'
 AND EXISTS (SELECT 1 FROM allocations a WHERE a.allocation_id = NEW.source_allocation_id
             AND a.flow_kind = 'refund' AND a.superseded_by_allocation_id IS NULL
             AND a.retracted_at_utc IS NULL)
BEGIN
    SELECT CASE WHEN COALESCE((
        SELECT sum(amount_cents) FROM transaction_links
        WHERE source_allocation_id = NEW.source_allocation_id
          AND link_kind = 'refund_of' AND status = 'confirmed'
          AND superseded_by_transaction_link_id IS NULL
    ), 0) + NEW.amount_cents > COALESCE((
        SELECT amount_cents FROM allocations
        WHERE allocation_id = NEW.source_allocation_id AND flow_kind = 'refund'
          AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL
    ), 0
    ) THEN RAISE(ABORT, 'refund_link_source_cap_exceeded') END;
END;

CREATE TRIGGER trg_refund_links_source_cap_update
BEFORE UPDATE ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status = 'confirmed'
 AND EXISTS (SELECT 1 FROM allocations a WHERE a.allocation_id = NEW.source_allocation_id
             AND a.flow_kind = 'refund' AND a.superseded_by_allocation_id IS NULL
             AND a.retracted_at_utc IS NULL)
BEGIN
    SELECT CASE WHEN COALESCE((
        SELECT sum(amount_cents) FROM transaction_links
        WHERE source_allocation_id = NEW.source_allocation_id
          AND link_kind = 'refund_of' AND status = 'confirmed'
          AND transaction_link_id <> OLD.transaction_link_id
          AND superseded_by_transaction_link_id IS NULL
    ), 0) + NEW.amount_cents > COALESCE((
        SELECT amount_cents FROM allocations
        WHERE allocation_id = NEW.source_allocation_id AND flow_kind = 'refund'
          AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL
    ), 0
    ) THEN RAISE(ABORT, 'refund_link_source_cap_exceeded') END;
END;

CREATE TRIGGER trg_refund_links_target_cap_insert
BEFORE INSERT ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status = 'confirmed'
BEGIN
    SELECT CASE WHEN COALESCE((
        SELECT sum(amount_cents) FROM transaction_links
        WHERE target_transaction_id = NEW.target_transaction_id
          AND link_kind = 'refund_of' AND status = 'confirmed'
          AND superseded_by_transaction_link_id IS NULL
    ), 0) + NEW.amount_cents > COALESCE((
        SELECT -sum(amount_cents) FROM allocations
        WHERE transaction_id = NEW.target_transaction_id AND flow_kind = 'expense'
          AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL
    ), 0) THEN RAISE(ABORT, 'refund_link_target_cap_exceeded') END;
END;

CREATE TRIGGER trg_refund_links_target_allocation_cap_insert
BEFORE INSERT ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status = 'confirmed' AND NEW.target_allocation_id IS NOT NULL
BEGIN
    SELECT CASE WHEN COALESCE((
        SELECT sum(amount_cents) FROM transaction_links
        WHERE target_allocation_id = NEW.target_allocation_id
          AND link_kind = 'refund_of' AND status = 'confirmed'
          AND superseded_by_transaction_link_id IS NULL
    ), 0) + NEW.amount_cents > (
        SELECT -amount_cents FROM allocations WHERE allocation_id = NEW.target_allocation_id
          AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL
    ) THEN RAISE(ABORT, 'refund_link_target_allocation_cap_exceeded') END;
END;

CREATE TRIGGER trg_refund_links_target_cap_update
BEFORE UPDATE ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status = 'confirmed'
BEGIN
    SELECT CASE WHEN COALESCE((
        SELECT sum(amount_cents) FROM transaction_links
        WHERE target_transaction_id = NEW.target_transaction_id
          AND link_kind = 'refund_of' AND status = 'confirmed'
          AND transaction_link_id <> OLD.transaction_link_id
          AND superseded_by_transaction_link_id IS NULL
    ), 0) + NEW.amount_cents > COALESCE((
        SELECT -sum(amount_cents) FROM allocations
        WHERE transaction_id = NEW.target_transaction_id AND flow_kind = 'expense'
          AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL
    ), 0) THEN RAISE(ABORT, 'refund_link_target_cap_exceeded') END;
END;

CREATE TRIGGER trg_refund_links_target_allocation_cap_update
BEFORE UPDATE ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status = 'confirmed' AND NEW.target_allocation_id IS NOT NULL
BEGIN
    SELECT CASE WHEN COALESCE((
        SELECT sum(amount_cents) FROM transaction_links
        WHERE target_allocation_id = NEW.target_allocation_id
          AND link_kind = 'refund_of' AND status = 'confirmed'
          AND transaction_link_id <> OLD.transaction_link_id
          AND superseded_by_transaction_link_id IS NULL
    ), 0) + NEW.amount_cents > (
        SELECT -amount_cents FROM allocations WHERE allocation_id = NEW.target_allocation_id
          AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL
    ) THEN RAISE(ABORT, 'refund_link_target_allocation_cap_exceeded') END;
END;

CREATE TRIGGER trg_refund_link_allocation_parts_insert_guard
BEFORE INSERT ON refund_link_allocation_parts
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM transaction_links l
        JOIN allocations a ON a.allocation_id = NEW.allocation_id
        WHERE l.transaction_link_id = NEW.transaction_link_id
          AND l.link_kind = 'refund_of' AND l.status = 'proposed'
          AND l.target_allocation_id IS NULL
          AND l.superseded_by_transaction_link_id IS NULL
          AND a.transaction_id = l.target_transaction_id AND a.flow_kind = 'expense'
          AND a.amount_cents < 0 AND a.superseded_by_allocation_id IS NULL
          AND a.retracted_at_utc IS NULL
    ) THEN RAISE(ABORT, 'refund_link_part_target_invalid') END;
END;

CREATE TRIGGER trg_refund_link_allocation_parts_update_forbidden
BEFORE UPDATE ON refund_link_allocation_parts
BEGIN
    SELECT RAISE(ABORT, 'refund_link_part_immutable');
END;

CREATE TRIGGER trg_refund_link_allocation_parts_delete_forbidden
BEFORE DELETE ON refund_link_allocation_parts
BEGIN
    SELECT RAISE(ABORT, 'refund_link_part_delete_forbidden');
END;

-- Confirmation requires a complete, active-allocation attribution for prorated
-- links. Per-allocation caps combine direct targets and these materialized parts.
CREATE TRIGGER trg_refund_links_confirmed_attribution_insert
BEFORE INSERT ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status = 'confirmed'
BEGIN
    SELECT CASE WHEN NEW.target_allocation_id IS NULL AND COALESCE((
        SELECT sum(amount_cents) FROM refund_link_allocation_parts
        WHERE transaction_link_id = NEW.transaction_link_id
    ), 0) <> NEW.amount_cents
        THEN RAISE(ABORT, 'refund_link_attribution_total_mismatch') END;
    SELECT CASE WHEN NEW.target_allocation_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM refund_link_allocation_parts
        WHERE transaction_link_id = NEW.transaction_link_id
    ) THEN RAISE(ABORT, 'refund_link_attribution_target_mismatch') END;
END;

CREATE TRIGGER trg_refund_links_confirmed_attribution_update
BEFORE UPDATE ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status = 'confirmed'
BEGIN
    SELECT CASE WHEN NEW.target_allocation_id IS NULL AND COALESCE((
        SELECT sum(amount_cents) FROM refund_link_allocation_parts
        WHERE transaction_link_id = NEW.transaction_link_id
    ), 0) <> NEW.amount_cents
        THEN RAISE(ABORT, 'refund_link_attribution_total_mismatch') END;
    SELECT CASE WHEN NEW.target_allocation_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM refund_link_allocation_parts
        WHERE transaction_link_id = NEW.transaction_link_id
    ) THEN RAISE(ABORT, 'refund_link_attribution_target_mismatch') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM refund_link_allocation_parts p
        LEFT JOIN allocations a ON a.allocation_id = p.allocation_id
        WHERE p.transaction_link_id = NEW.transaction_link_id
          AND (a.allocation_id IS NULL OR a.transaction_id <> NEW.target_transaction_id
               OR a.flow_kind <> 'expense' OR a.amount_cents >= 0
               OR a.superseded_by_allocation_id IS NOT NULL OR a.retracted_at_utc IS NOT NULL)
    ) THEN RAISE(ABORT, 'refund_link_part_target_invalid') END;
END;

CREATE TRIGGER trg_refund_links_confirmed_allocation_caps_insert
BEFORE INSERT ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status = 'confirmed'
BEGIN
    SELECT CASE WHEN COALESCE((
        SELECT sum(amount_cents) FROM transaction_links
        WHERE target_transaction_id = NEW.target_transaction_id
          AND link_kind = 'refund_of' AND status = 'confirmed'
          AND superseded_by_transaction_link_id IS NULL
    ), 0) + NEW.amount_cents > COALESCE((
        SELECT -sum(amount_cents) FROM allocations
        WHERE transaction_id = NEW.target_transaction_id AND flow_kind = 'expense'
          AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL
    ), 0) THEN RAISE(ABORT, 'refund_link_target_cap_exceeded') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM allocations a
        WHERE a.transaction_id = NEW.target_transaction_id AND a.flow_kind = 'expense'
          AND a.amount_cents < 0 AND a.superseded_by_allocation_id IS NULL
          AND a.retracted_at_utc IS NULL
          AND COALESCE((
              SELECT sum(l.amount_cents) FROM transaction_links l
              WHERE l.target_allocation_id = a.allocation_id
                AND l.link_kind = 'refund_of' AND l.status = 'confirmed'
                AND l.superseded_by_transaction_link_id IS NULL
          ), 0)
          + COALESCE((
              SELECT sum(p.amount_cents) FROM refund_link_allocation_parts p
              JOIN transaction_links l ON l.transaction_link_id = p.transaction_link_id
              WHERE p.allocation_id = a.allocation_id
                AND l.link_kind = 'refund_of' AND l.status = 'confirmed'
                AND l.superseded_by_transaction_link_id IS NULL
          ), 0)
          + CASE WHEN NEW.target_allocation_id = a.allocation_id
                 THEN NEW.amount_cents ELSE 0 END
          + CASE WHEN NEW.target_allocation_id IS NULL
                 THEN COALESCE((
                     SELECT sum(p.amount_cents) FROM refund_link_allocation_parts p
                     WHERE p.transaction_link_id = NEW.transaction_link_id
                       AND p.allocation_id = a.allocation_id
                 ), 0) ELSE 0 END
          > -a.amount_cents
    ) THEN RAISE(ABORT, 'refund_link_target_allocation_cap_exceeded') END;
END;

CREATE TRIGGER trg_refund_links_confirmed_allocation_caps_update
BEFORE UPDATE ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status = 'confirmed'
BEGIN
    SELECT CASE WHEN NEW.superseded_by_transaction_link_id IS NULL AND COALESCE((
        SELECT sum(amount_cents) FROM transaction_links
        WHERE target_transaction_id = NEW.target_transaction_id
          AND link_kind = 'refund_of' AND status = 'confirmed'
          AND transaction_link_id <> OLD.transaction_link_id
          AND superseded_by_transaction_link_id IS NULL
    ), 0) + NEW.amount_cents > COALESCE((
        SELECT -sum(amount_cents) FROM allocations
        WHERE transaction_id = NEW.target_transaction_id AND flow_kind = 'expense'
          AND superseded_by_allocation_id IS NULL AND retracted_at_utc IS NULL
    ), 0) THEN RAISE(ABORT, 'refund_link_target_cap_exceeded') END;
    SELECT CASE WHEN NEW.superseded_by_transaction_link_id IS NULL AND EXISTS (
        SELECT 1 FROM allocations a
        WHERE a.transaction_id = NEW.target_transaction_id AND a.flow_kind = 'expense'
          AND a.amount_cents < 0 AND a.superseded_by_allocation_id IS NULL
          AND a.retracted_at_utc IS NULL
          AND COALESCE((
              SELECT sum(l.amount_cents) FROM transaction_links l
              WHERE l.target_allocation_id = a.allocation_id
                AND l.link_kind = 'refund_of' AND l.status = 'confirmed'
                AND l.transaction_link_id <> OLD.transaction_link_id
                AND l.superseded_by_transaction_link_id IS NULL
          ), 0)
          + COALESCE((
              SELECT sum(p.amount_cents) FROM refund_link_allocation_parts p
              JOIN transaction_links l ON l.transaction_link_id = p.transaction_link_id
              WHERE p.allocation_id = a.allocation_id
                AND l.link_kind = 'refund_of' AND l.status = 'confirmed'
                AND l.transaction_link_id <> OLD.transaction_link_id
                AND l.superseded_by_transaction_link_id IS NULL
          ), 0)
          + CASE WHEN NEW.target_allocation_id = a.allocation_id
                 THEN NEW.amount_cents ELSE 0 END
          + CASE WHEN NEW.target_allocation_id IS NULL
                 THEN COALESCE((
                     SELECT sum(p.amount_cents) FROM refund_link_allocation_parts p
                     WHERE p.transaction_link_id = NEW.transaction_link_id
                       AND p.allocation_id = a.allocation_id
                 ), 0) ELSE 0 END
          > -a.amount_cents
    ) THEN RAISE(ABORT, 'refund_link_target_allocation_cap_exceeded') END;
END;

-- Un lien confirmé conserve son contenu ; sa seule mutation est une supersession
-- auditée. La FK différée permet de pointer vers la nouvelle ligne insérée ensuite.
CREATE TRIGGER trg_confirmed_transaction_links_supersede_only
BEFORE UPDATE ON transaction_links
WHEN OLD.status = 'confirmed'
 AND NOT (
    NEW.transaction_link_id IS OLD.transaction_link_id
    AND NEW.source_transaction_id IS OLD.source_transaction_id
    AND NEW.target_transaction_id IS OLD.target_transaction_id
    AND NEW.source_allocation_id IS OLD.source_allocation_id
    AND NEW.target_allocation_id IS OLD.target_allocation_id
    AND NEW.link_kind IS OLD.link_kind
    AND NEW.amount_cents IS OLD.amount_cents
    AND NEW.status IS OLD.status
    AND NEW.created_at_utc IS OLD.created_at_utc
    AND NEW.version = OLD.version + 1
    AND OLD.superseded_by_transaction_link_id IS NULL
    AND NEW.superseded_by_transaction_link_id IS NOT NULL
    AND NEW.superseded_by_audit_event_id IS NOT NULL
 )
BEGIN
    SELECT RAISE(ABORT, 'confirmed_transaction_link_supersession_required');
END;

CREATE TRIGGER trg_transaction_link_supersession_replacement
BEFORE INSERT ON transaction_links
WHEN EXISTS (
    SELECT 1 FROM transaction_links old
    WHERE old.superseded_by_transaction_link_id = NEW.transaction_link_id
      AND (old.status <> 'confirmed' OR NEW.status NOT IN ('proposed', 'confirmed')
           OR old.source_transaction_id <> NEW.source_transaction_id
           OR old.source_allocation_id <> NEW.source_allocation_id
           OR old.link_kind <> NEW.link_kind)
)
BEGIN
    SELECT RAISE(ABORT, 'transaction_link_supersession_replacement_invalid');
END;

CREATE TRIGGER trg_transaction_link_supersession_replacement_confirmation
BEFORE UPDATE ON transaction_links
WHEN OLD.status = 'proposed' AND NEW.status = 'confirmed' AND EXISTS (
    SELECT 1 FROM transaction_links old
    WHERE old.superseded_by_transaction_link_id = NEW.transaction_link_id
      AND (old.status <> 'confirmed'
           OR old.source_transaction_id <> NEW.source_transaction_id
           OR old.source_allocation_id <> NEW.source_allocation_id
           OR old.link_kind <> NEW.link_kind)
)
BEGIN
    SELECT RAISE(ABORT, 'transaction_link_supersession_replacement_invalid');
END;

CREATE TRIGGER trg_refund_links_initial_status
BEFORE INSERT ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status <> 'proposed'
BEGIN
    SELECT RAISE(ABORT, 'refund_link_initial_status_invalid');
END;

CREATE TRIGGER trg_confirmed_transaction_links_no_delete
BEFORE DELETE ON transaction_links
WHEN OLD.status = 'confirmed'
BEGIN
    SELECT RAISE(ABORT, 'confirmed_transaction_link_immutable');
END;

CREATE TABLE audit_events (
    audit_event_id TEXT NOT NULL PRIMARY KEY CHECK (length(audit_event_id) = 36 AND audit_event_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    import_file_id TEXT REFERENCES import_files(import_file_id) ON DELETE RESTRICT,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    idempotency_key TEXT,
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc))
);
CREATE INDEX ix_audit_target ON audit_events(target_type, target_id, created_at_utc, audit_event_id);
CREATE INDEX ix_audit_import ON audit_events(import_file_id, created_at_utc);

CREATE TABLE app_settings (
    setting_key TEXT NOT NULL PRIMARY KEY,
    setting_value TEXT NOT NULL,
    is_secret INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) = updated_at_utc))
);

CREATE TRIGGER trg_app_settings_version_increment
BEFORE UPDATE ON app_settings
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;


CREATE TABLE llm_runs (
    llm_run_id TEXT NOT NULL PRIMARY KEY CHECK (length(llm_run_id) = 36 AND llm_run_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'partial', 'timeout', 'http_error', 'invalid_response', 'cancelled')),
    batch_size INTEGER NOT NULL CHECK (typeof(batch_size) = 'integer' AND batch_size >= 0),
    latency_ms INTEGER CHECK (latency_ms IS NULL OR (typeof(latency_ms) = 'integer' AND latency_ms >= 0)),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR (typeof(input_tokens) = 'integer' AND input_tokens >= 0)),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR (typeof(output_tokens) = 'integer' AND output_tokens >= 0)),
    recovered_line_count INTEGER NOT NULL DEFAULT 0 CHECK (typeof(recovered_line_count) = 'integer' AND recovered_line_count >= 0),
    redacted_error_code TEXT,
    started_at_utc TEXT NOT NULL CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', started_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', started_at_utc) = started_at_utc)),
    finished_at_utc TEXT CHECK (finished_at_utc IS NULL OR (strftime('%Y-%m-%dT%H:%M:%fZ', finished_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', finished_at_utc) = finished_at_utc))
);

CREATE TABLE budgets (
    budget_id TEXT NOT NULL PRIMARY KEY CHECK (length(budget_id) = 36 AND budget_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    category_id TEXT NOT NULL,
    flow_kind TEXT NOT NULL DEFAULT 'expense' CHECK (flow_kind IN ('expense', 'allocation_personal')),
    period_start TEXT NOT NULL CHECK (date(period_start) = period_start),
    period_end TEXT NOT NULL CHECK (date(period_end) = period_end),
    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents >= 0),
    label TEXT NOT NULL CHECK (length(trim(label)) > 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) = updated_at_utc)),
    FOREIGN KEY (category_id, flow_kind)
        REFERENCES category_flow_kinds(category_id, flow_kind) ON DELETE RESTRICT,
    CHECK (period_start <= period_end)
);

CREATE TRIGGER trg_budgets_version_increment
BEFORE UPDATE ON budgets
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;

CREATE INDEX ix_budgets_period ON budgets(period_start, period_end, is_active);

CREATE TABLE budget_accounts (
    budget_id TEXT NOT NULL REFERENCES budgets(budget_id) ON DELETE RESTRICT CHECK (length(budget_id) = 36 AND budget_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE RESTRICT CHECK (length(account_id) = 36 AND account_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    PRIMARY KEY (budget_id, account_id)
);
CREATE INDEX ix_budget_accounts_account ON budget_accounts(account_id, budget_id);

CREATE TABLE insights (
    insight_id TEXT NOT NULL PRIMARY KEY CHECK (length(insight_id) = 36 AND insight_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ignored', 'archived')),
    narrative TEXT NOT NULL,
    generated_by TEXT NOT NULL CHECK (generated_by IN ('deterministic', 'llm')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', updated_at_utc) = updated_at_utc))
);

CREATE TRIGGER trg_insights_version_increment
BEFORE UPDATE ON insights
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
END;

CREATE INDEX ix_insights_status_created ON insights(status, created_at_utc DESC);

-- Les faits sont unitaires : leur metric_definition_id est validé ici et chaque
-- transaction utilisée peut être reliée pour le drilldown vérifiable.
CREATE TABLE insight_facts (
    insight_fact_id TEXT NOT NULL PRIMARY KEY CHECK (length(insight_fact_id) = 36 AND insight_fact_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    insight_id TEXT NOT NULL REFERENCES insights(insight_id) ON DELETE RESTRICT,
    fact_ordinal INTEGER NOT NULL CHECK (typeof(fact_ordinal) = 'integer' AND fact_ordinal > 0),
    metric_definition_id TEXT NOT NULL CHECK (metric_definition_id IN (
        'bank_net_flow.v1', 'consumption_net.v1', 'month_completeness.v1',
        'monthly_delta.v1', 'operation_coverage.v1', 'operational_saving_capacity.v1',
        'operational_saving_rate.v1', 'personal_allowances.v1', 'regular_income.v1',
        'savings_net_transferred.v1', 'unclassified_amount_share.v1'
    )),
    period_start TEXT NOT NULL CHECK (date(period_start) = period_start),
    period_end TEXT NOT NULL CHECK (date(period_end) = period_end),
    value_kind TEXT NOT NULL CHECK (value_kind IN ('cents', 'basis_points', 'count', 'state')),
    raw_cents INTEGER CHECK (raw_cents IS NULL OR typeof(raw_cents) = 'integer'),
    raw_basis_points INTEGER CHECK (raw_basis_points IS NULL OR typeof(raw_basis_points) = 'integer'),
    raw_count INTEGER CHECK (raw_count IS NULL OR (typeof(raw_count) = 'integer' AND raw_count >= 0)),
    state_value TEXT,
    filters_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    UNIQUE (insight_id, fact_ordinal),
    CHECK (period_start <= period_end),
    CHECK (
        (value_kind = 'cents' AND raw_cents IS NOT NULL AND raw_basis_points IS NULL AND raw_count IS NULL AND state_value IS NULL)
        OR (value_kind = 'basis_points' AND raw_cents IS NULL AND raw_basis_points IS NOT NULL AND raw_count IS NULL AND state_value IS NULL)
        OR (value_kind = 'count' AND raw_cents IS NULL AND raw_basis_points IS NULL AND raw_count IS NOT NULL AND state_value IS NULL)
        OR (value_kind = 'state' AND raw_cents IS NULL AND raw_basis_points IS NULL AND raw_count IS NULL AND state_value IN ('complete', 'partial', 'unknown'))
    )
);
CREATE INDEX ix_insight_facts_metric_period ON insight_facts(metric_definition_id, period_start, period_end);

CREATE TABLE insight_fact_transactions (
    insight_fact_id TEXT NOT NULL REFERENCES insight_facts(insight_fact_id) ON DELETE RESTRICT CHECK (length(insight_fact_id) = 36 AND insight_fact_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT CHECK (length(transaction_id) = 36 AND transaction_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    contribution_cents INTEGER CHECK (contribution_cents IS NULL OR typeof(contribution_cents) = 'integer'),
    PRIMARY KEY (insight_fact_id, transaction_id)
);
CREATE INDEX ix_insight_fact_transactions_transaction ON insight_fact_transactions(transaction_id, insight_fact_id);

CREATE TABLE backup_manifests (
    backup_manifest_id TEXT NOT NULL PRIMARY KEY CHECK (length(backup_manifest_id) = 36 AND backup_manifest_id GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'),
    schema_version TEXT NOT NULL,
    archive_relpath TEXT NOT NULL,
    archive_sha256 TEXT NOT NULL CHECK (length(archive_sha256) = 64 AND archive_sha256 NOT GLOB '*[^0-9a-f]*'),
    file_size_bytes INTEGER NOT NULL CHECK (typeof(file_size_bytes) = 'integer' AND file_size_bytes >= 0),
    archive_permissions_octal TEXT NOT NULL DEFAULT '0600' CHECK (archive_permissions_octal = '0600'),
    includes_originals INTEGER NOT NULL DEFAULT 0 CHECK (includes_originals IN (0, 1)),
    includes_hmac_key INTEGER NOT NULL DEFAULT 1 CHECK (includes_hmac_key = 1),
    status TEXT NOT NULL CHECK (status IN ('created', 'verified', 'restored', 'failed')),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    verified_at_utc TEXT CHECK (verified_at_utc IS NULL OR (strftime('%Y-%m-%dT%H:%M:%fZ', verified_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', verified_at_utc) = verified_at_utc))
);
CREATE INDEX ix_backup_manifests_created ON backup_manifests(created_at_utc DESC);

-- Registre des mutations idempotentes HTTP. Les entrées expirent après 7 jours
-- et sont purgées au démarrage ; hors fenêtre, l'API ne promet pas le rejeu.
-- La réponse conservée n'inclut ni secret ni libellé brut non nécessaire.
CREATE TABLE idempotency_records (
    idempotency_key TEXT NOT NULL PRIMARY KEY,
    http_method TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    response_status INTEGER NOT NULL CHECK (response_status BETWEEN 200 AND 599),
    response_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK ((strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) IS NOT NULL AND strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc) = created_at_utc)),
    expires_at_utc TEXT GENERATED ALWAYS AS
        (strftime('%Y-%m-%dT%H:%M:%fZ', created_at_utc, '+7 days')) STORED
        CHECK (strftime('%Y-%m-%dT%H:%M:%fZ', expires_at_utc) = expires_at_utc)
);
CREATE INDEX ix_idempotency_expiry ON idempotency_records(expires_at_utc);

-- Les champs issus de la ligne bancaire sont en écriture unique. Un payee_id et
-- la version peuvent évoluer par action métier auditée ; la source ne le peut pas.
CREATE TRIGGER trg_transactions_source_immutable
BEFORE UPDATE ON transactions
WHEN NEW.account_id IS NOT OLD.account_id
  OR NEW.booked_on IS NOT OLD.booked_on
  OR NEW.amount_cents IS NOT OLD.amount_cents
  OR NEW.raw_label IS NOT OLD.raw_label
  OR NEW.identity_label IS NOT OLD.identity_label
  OR NEW.identity_key_sha256 IS NOT OLD.identity_key_sha256
  OR NEW.occurrence_rank IS NOT OLD.occurrence_rank
  OR NEW.imported_at_utc IS NOT OLD.imported_at_utc
BEGIN
    SELECT RAISE(ABORT, 'transaction_source_immutable');
END;
CREATE TRIGGER trg_transactions_no_delete
BEFORE DELETE ON transactions
BEGIN
    SELECT RAISE(ABORT, 'transaction_source_immutable');
END;

CREATE TRIGGER trg_committed_section_mapping_immutable
BEFORE UPDATE OF account_id, account_fingerprint ON import_account_sections
WHEN EXISTS (
    SELECT 1 FROM import_files
    WHERE import_files.import_file_id = OLD.import_file_id
      AND import_files.status = 'committed'
)
BEGIN
    SELECT RAISE(ABORT, 'committed_account_mapping_immutable');
END;

CREATE TRIGGER trg_source_rows_immutable
BEFORE UPDATE ON source_rows
BEGIN
    SELECT RAISE(ABORT, 'source_row_immutable');
END;
CREATE TRIGGER trg_source_rows_no_delete
BEFORE DELETE ON source_rows
BEGIN
    SELECT RAISE(ABORT, 'source_row_immutable');
END;

CREATE TRIGGER trg_audit_events_immutable_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_event_immutable');
END;
CREATE TRIGGER trg_audit_events_immutable_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_event_immutable');
END;

-- La vie d'un import est complète par fichier : aucune ligne ne peut être
-- publiée avant le passage atomique previewed -> committed.
CREATE TRIGGER trg_import_files_no_direct_commit
BEFORE INSERT ON import_files
WHEN NEW.status = 'committed'
BEGIN
    SELECT RAISE(ABORT, 'import_must_start_previewed');
END;

CREATE TRIGGER trg_import_files_status_transition
BEFORE UPDATE OF status ON import_files
WHEN NEW.status <> OLD.status
 AND NOT (OLD.status = 'previewed' AND NEW.status IN ('committed', 'rejected'))
BEGIN
    SELECT RAISE(ABORT, 'import_status_transition_invalid');
END;

CREATE TRIGGER trg_import_files_reject_requires_no_sources
BEFORE UPDATE OF status ON import_files
WHEN OLD.status = 'previewed' AND NEW.status = 'rejected'
 AND EXISTS (SELECT 1 FROM source_rows s WHERE s.import_file_id = OLD.import_file_id)
BEGIN
    SELECT RAISE(ABORT, 'rejected_import_cannot_have_source_rows');
END;

CREATE TRIGGER trg_import_files_noop_validate
BEFORE INSERT ON import_files
WHEN NEW.status = 'no_op'
 AND NOT EXISTS (
    SELECT 1 FROM import_files original
    WHERE original.import_file_id = NEW.duplicate_of_import_file_id
      AND original.status = 'committed'
      AND original.file_sha256 = NEW.file_sha256
 )
BEGIN
    SELECT RAISE(ABORT, 'no_op_must_reference_committed_same_hash');
END;

CREATE TRIGGER trg_import_files_committed_immutable
BEFORE UPDATE ON import_files
WHEN OLD.status = 'committed'
BEGIN
    SELECT RAISE(ABORT, 'committed_import_immutable');
END;

CREATE TRIGGER trg_import_files_no_delete
BEFORE DELETE ON import_files
WHEN OLD.status <> 'previewed'
BEGIN
    SELECT RAISE(ABORT, 'import_history_immutable');
END;

CREATE TRIGGER trg_import_files_commit_guard
BEFORE UPDATE OF status ON import_files
WHEN OLD.status = 'previewed' AND NEW.status = 'committed'
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM import_account_sections s WHERE s.import_file_id = NEW.import_file_id
    ) THEN RAISE(ABORT, 'committed_import_requires_section') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM import_account_sections s
        WHERE s.import_file_id = NEW.import_file_id
          AND (s.account_id IS NULL OR s.coverage_from IS NULL OR s.coverage_through IS NULL
               OR s.complete_through IS NOT CASE
                    WHEN NEW.generation_date_source = 'header_download' THEN
                        CASE WHEN s.coverage_through <= date(NEW.generation_date, '-1 day')
                             THEN s.coverage_through ELSE date(NEW.generation_date, '-1 day') END
                    ELSE CASE
                        WHEN (SELECT max(t.booked_on)
                              FROM source_rows r JOIN transactions t ON t.transaction_id = r.transaction_id
                              WHERE r.import_file_id = NEW.import_file_id
                                AND r.section_index = s.section_index) IS NULL THEN NULL
                        WHEN s.coverage_through <= date((SELECT max(t.booked_on)
                              FROM source_rows r JOIN transactions t ON t.transaction_id = r.transaction_id
                              WHERE r.import_file_id = NEW.import_file_id
                                AND r.section_index = s.section_index), '-1 day')
                        THEN s.coverage_through
                        ELSE date((SELECT max(t.booked_on)
                              FROM source_rows r JOIN transactions t ON t.transaction_id = r.transaction_id
                              WHERE r.import_file_id = NEW.import_file_id
                                AND r.section_index = s.section_index), '-1 day')
                    END
                END)
    ) THEN RAISE(ABORT, 'committed_import_section_unresolved') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM import_rejections r
        WHERE r.import_file_id = NEW.import_file_id
          AND (r.severity = 'error'
               OR (r.requires_resolution = 1 AND r.resolution_status <> 'resolved'))
    ) THEN RAISE(ABORT, 'committed_import_has_unresolved_rejection') END;
    SELECT CASE WHEN (
        SELECT count(*) FROM source_rows s WHERE s.import_file_id = NEW.import_file_id
    ) <> NEW.parsed_operation_count
    THEN RAISE(ABORT, 'committed_import_source_count_mismatch') END;
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM transactions t
        WHERE NOT EXISTS (SELECT 1 FROM source_rows s WHERE s.transaction_id = t.transaction_id)
    ) THEN RAISE(ABORT, 'transaction_without_source_row') END;
END;

CREATE TRIGGER trg_import_sections_insert_previewed_only
BEFORE INSERT ON import_account_sections
WHEN NOT EXISTS (
    SELECT 1 FROM import_files f WHERE f.import_file_id = NEW.import_file_id AND f.status = 'previewed'
)
BEGIN
    SELECT RAISE(ABORT, 'import_section_requires_previewed_file');
END;

CREATE TRIGGER trg_import_sections_committed_immutable_update
BEFORE UPDATE ON import_account_sections
WHEN EXISTS (
    SELECT 1 FROM import_files f
    WHERE f.import_file_id = OLD.import_file_id AND f.status = 'committed'
)
BEGIN
    SELECT RAISE(ABORT, 'committed_import_section_immutable');
END;

CREATE TRIGGER trg_import_sections_committed_immutable_delete
BEFORE DELETE ON import_account_sections
WHEN EXISTS (
    SELECT 1 FROM import_files f
    WHERE f.import_file_id = OLD.import_file_id AND f.status = 'committed'
)
BEGIN
    SELECT RAISE(ABORT, 'committed_import_section_immutable');
END;

CREATE TRIGGER trg_allocations_transaction_sign
BEFORE INSERT ON allocations
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM transactions t WHERE t.transaction_id = NEW.transaction_id
          AND ((t.amount_cents < 0 AND NEW.amount_cents >= 0)
            OR (t.amount_cents > 0 AND NEW.amount_cents <= 0))
    ) THEN RAISE(ABORT, 'allocation_sign_must_match_transaction') END;
END;

CREATE TRIGGER trg_allocations_total_cap
BEFORE INSERT ON allocations
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM transactions t WHERE t.transaction_id = NEW.transaction_id
          AND ((t.amount_cents < 0 AND
                COALESCE((SELECT sum(a.amount_cents) FROM allocations a
                          WHERE a.transaction_id = t.transaction_id
                            AND a.superseded_by_allocation_id IS NULL
                            AND a.retracted_at_utc IS NULL), 0) + NEW.amount_cents < t.amount_cents)
            OR (t.amount_cents > 0 AND
                COALESCE((SELECT sum(a.amount_cents) FROM allocations a
                          WHERE a.transaction_id = t.transaction_id
                            AND a.superseded_by_allocation_id IS NULL
                            AND a.retracted_at_utc IS NULL), 0) + NEW.amount_cents > t.amount_cents))
    ) THEN RAISE(ABORT, 'allocation_total_exceeds_transaction') END;
END;

CREATE TRIGGER trg_allocations_insert_active_only
BEFORE INSERT ON allocations
WHEN NEW.retracted_at_utc IS NOT NULL OR NEW.retracted_by_decision_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'allocation_retraction_requires_update');
END;

CREATE TRIGGER trg_allocations_category_direction_insert
BEFORE INSERT ON allocations
WHEN (NEW.category_id IN (
        'flow_savings_out', 'flow_loan_principal', 'flow_loan_payment_unknown_split',
        'flow_personal_a', 'flow_personal_b', 'flow_capital_improvement',
        'flow_cash_unallocated', 'flow_intermediary_unresolved'
      ) AND NEW.amount_cents >= 0)
  OR (NEW.category_id IN (
        'flow_savings_in', 'flow_loan_disbursement', 'income_salary_a', 'income_salary_b',
        'income_benefits', 'income_regular_other', 'income_extraordinary', 'income_refund'
      ) AND NEW.amount_cents <= 0)
BEGIN
    SELECT RAISE(ABORT, 'allocation_category_direction_invalid');
END;

CREATE TRIGGER trg_allocations_supersession_target_match
AFTER INSERT ON allocations
WHEN EXISTS (
    SELECT 1 FROM allocations previous
    WHERE previous.superseded_by_allocation_id = NEW.allocation_id
      AND previous.transaction_id <> NEW.transaction_id
)
BEGIN
    SELECT RAISE(ABORT, 'allocation_supersession_transaction_mismatch');
END;

CREATE TRIGGER trg_allocations_supersession_existing_target_match
BEFORE UPDATE ON allocations
WHEN NEW.superseded_by_allocation_id IS NOT NULL AND EXISTS (
    SELECT 1 FROM allocations replacement
    WHERE replacement.allocation_id = NEW.superseded_by_allocation_id
      AND replacement.transaction_id <> NEW.transaction_id
)
BEGIN
    SELECT RAISE(ABORT, 'allocation_supersession_transaction_mismatch');
END;

CREATE TRIGGER trg_allocations_retraction_decision_same_transaction
BEFORE UPDATE ON allocations
WHEN NEW.retracted_at_utc IS NOT NULL
 AND NEW.retracted_at_utc IS NOT OLD.retracted_at_utc
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM classification_decisions d
        WHERE d.decision_id = NEW.retracted_by_decision_id
          AND d.transaction_id = NEW.transaction_id
    ) THEN RAISE(ABORT, 'allocation_retraction_decision_transaction_mismatch') END;
END;

CREATE TRIGGER trg_allocations_referenced_links_immutable
BEFORE UPDATE ON allocations
WHEN OLD.superseded_by_allocation_id IS NULL AND OLD.retracted_at_utc IS NULL
 AND (NEW.superseded_by_allocation_id IS NOT NULL OR NEW.retracted_at_utc IS NOT NULL)
 AND (
    EXISTS (
        SELECT 1 FROM transaction_links l
        WHERE l.status = 'confirmed' AND l.superseded_by_transaction_link_id IS NULL
          AND (l.source_allocation_id = OLD.allocation_id OR l.target_allocation_id = OLD.allocation_id)
    )
    OR EXISTS (
        SELECT 1 FROM refund_link_allocation_parts p
        JOIN transaction_links l ON l.transaction_link_id = p.transaction_link_id
        WHERE p.allocation_id = OLD.allocation_id
          AND l.status = 'confirmed' AND l.superseded_by_transaction_link_id IS NULL
    )
 )
BEGIN
    SELECT RAISE(ABORT, 'allocation_referenced_by_confirmed_refund_link');
END;

CREATE TRIGGER trg_allocations_update_only_supersede
BEFORE UPDATE ON allocations
WHEN OLD.superseded_by_allocation_id IS NOT NULL
  OR OLD.retracted_at_utc IS NOT NULL
  OR NEW.version <> OLD.version + 1
  OR NEW.allocation_id IS NOT OLD.allocation_id
  OR NEW.transaction_id IS NOT OLD.transaction_id
  OR NEW.category_id IS NOT OLD.category_id
  OR NEW.flow_kind IS NOT OLD.flow_kind
  OR NEW.amount_cents IS NOT OLD.amount_cents
  OR NEW.origin IS NOT OLD.origin
  OR NEW.decision IS NOT OLD.decision
  OR NEW.beneficiary IS NOT OLD.beneficiary
  OR NEW.cadence IS NOT OLD.cadence
  OR NEW.flexibility IS NOT OLD.flexibility
  OR NEW.acquisition IS NOT OLD.acquisition
  OR NEW.created_at_utc IS NOT OLD.created_at_utc
  OR NOT (
      (NEW.superseded_by_allocation_id IS NOT NULL
       AND NEW.superseded_by_audit_event_id IS NOT NULL
       AND NEW.retracted_at_utc IS NULL
       AND NEW.retracted_by_decision_id IS NULL)
      OR
      (NEW.superseded_by_allocation_id IS NULL
       AND NEW.superseded_by_audit_event_id IS NULL
       AND NEW.retracted_at_utc IS NOT NULL
       AND NEW.retracted_by_decision_id IS NOT NULL)
  )
BEGIN
    SELECT RAISE(ABORT, 'allocation_update_requires_audited_supersession');
END;

CREATE TRIGGER trg_allocations_no_delete
BEFORE DELETE ON allocations
BEGIN
    SELECT RAISE(ABORT, 'allocation_delete_forbidden');
END;
