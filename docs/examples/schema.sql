-- Schéma de référence SQLite — Comptes familiaux v1 (T02 / G0)
-- Exécuter sur chaque connexion SQLite : les PRAGMA sont locaux à la connexion.
PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;

CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY,
    alias TEXT NOT NULL CHECK (length(trim(alias)) > 0),
    account_role TEXT NOT NULL CHECK (account_role IN (
        'current', 'savings', 'pea', 'personal', 'cash', 'other'
    )),
    bank_fingerprint TEXT UNIQUE,
    fingerprint_key_version INTEGER CHECK (fingerprint_key_version IS NULL OR fingerprint_key_version > 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z'),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(updated_at_utc, -1) = 'Z'),
    CHECK ((bank_fingerprint IS NULL AND fingerprint_key_version IS NULL)
        OR (length(bank_fingerprint) = 64 AND fingerprint_key_version IS NOT NULL))
);

CREATE TABLE import_files (
    import_file_id TEXT PRIMARY KEY,
    file_sha256 TEXT NOT NULL CHECK (
        length(file_sha256) = 64 AND file_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    archive_relpath TEXT NOT NULL UNIQUE CHECK (
        archive_relpath = 'data/originals/' || import_file_id || '.bin'
    ),
    archive_storage_mode TEXT NOT NULL DEFAULT 'raw' CHECK (archive_storage_mode = 'raw'),
    archive_permissions_octal TEXT NOT NULL DEFAULT '0600'
        CHECK (archive_permissions_octal = '0600'),
    file_size_bytes INTEGER NOT NULL CHECK (typeof(file_size_bytes) = 'integer' AND file_size_bytes >= 0),
    detected_encoding TEXT CHECK (detected_encoding IS NULL OR detected_encoding IN ('cp1252', 'utf-8', 'utf-8-sig')),
    status TEXT NOT NULL CHECK (status IN ('previewed', 'committed', 'rejected', 'no_op')),
    duplicate_of_import_file_id TEXT REFERENCES import_files(import_file_id) ON DELETE RESTRICT,
    failure_code TEXT,
    failure_summary TEXT,
    started_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(started_at_utc, -1) = 'Z'),
    finished_at_utc TEXT CHECK (finished_at_utc IS NULL OR substr(finished_at_utc, -1) = 'Z'),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    CHECK ((status = 'no_op' AND duplicate_of_import_file_id IS NOT NULL)
        OR (status <> 'no_op' AND duplicate_of_import_file_id IS NULL)),
    CHECK (duplicate_of_import_file_id IS NULL OR duplicate_of_import_file_id <> import_file_id)
);

-- Une empreinte ne peut être committée qu'une fois. Les tentatives répétées sont
-- conservées comme status=no_op, rattachées au commit initial, sans source_rows.
CREATE UNIQUE INDEX uq_import_files_committed_sha256
    ON import_files(file_sha256) WHERE status = 'committed';
CREATE INDEX ix_import_files_started_at ON import_files(started_at_utc DESC, import_file_id DESC);

CREATE TABLE import_account_sections (
    import_file_id TEXT NOT NULL REFERENCES import_files(import_file_id) ON DELETE RESTRICT,
    section_index INTEGER NOT NULL CHECK (section_index > 0),
    account_id TEXT REFERENCES accounts(account_id) ON DELETE RESTRICT,
    account_fingerprint TEXT CHECK (account_fingerprint IS NULL OR length(account_fingerprint) = 64),
    coverage_from TEXT CHECK (coverage_from IS NULL OR date(coverage_from) = coverage_from),
    coverage_through TEXT CHECK (coverage_through IS NULL OR date(coverage_through) = coverage_through),
    balance_as_of TEXT CHECK (balance_as_of IS NULL OR date(balance_as_of) = balance_as_of),
    header_balance_cents INTEGER CHECK (header_balance_cents IS NULL OR typeof(header_balance_cents) = 'integer'),
    reconciled_balance_cents INTEGER CHECK (reconciled_balance_cents IS NULL OR typeof(reconciled_balance_cents) = 'integer'),
    balance_check_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (balance_check_status IN ('unknown', 'match', 'mismatch', 'not_comparable')),
    section_operation_count INTEGER CHECK (section_operation_count IS NULL OR
        (typeof(section_operation_count) = 'integer' AND section_operation_count >= 0)),
    PRIMARY KEY (import_file_id, section_index),
    CHECK (coverage_from IS NULL OR coverage_through IS NULL OR coverage_from <= coverage_through),
    CHECK ((balance_check_status IN ('match', 'mismatch') AND header_balance_cents IS NOT NULL
            AND reconciled_balance_cents IS NOT NULL)
        OR balance_check_status IN ('unknown', 'not_comparable'))
);
CREATE INDEX ix_import_sections_account_coverage
    ON import_account_sections(account_id, coverage_from, coverage_through);

CREATE TABLE import_rejections (
    import_rejection_id TEXT PRIMARY KEY,
    import_file_id TEXT NOT NULL REFERENCES import_files(import_file_id) ON DELETE RESTRICT,
    section_index INTEGER,
    logical_row_idx INTEGER CHECK (logical_row_idx IS NULL OR
        (typeof(logical_row_idx) = 'integer' AND logical_row_idx > 0)),
    field_name TEXT,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('warning', 'error')),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z'),
    FOREIGN KEY (import_file_id, section_index)
        REFERENCES import_account_sections(import_file_id, section_index) ON DELETE RESTRICT
);
CREATE INDEX ix_import_rejections_file
    ON import_rejections(import_file_id, severity, section_index, logical_row_idx, import_rejection_id);

CREATE TABLE payees (
    payee_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    normalized_name TEXT NOT NULL CHECK (length(trim(normalized_name)) > 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z'),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(updated_at_utc, -1) = 'Z'),
    UNIQUE (normalized_name)
);

CREATE TABLE payee_aliases (
    payee_alias_id TEXT PRIMARY KEY,
    payee_id TEXT NOT NULL REFERENCES payees(payee_id) ON DELETE RESTRICT,
    alias_text TEXT NOT NULL CHECK (length(trim(alias_text)) > 0),
    normalized_alias TEXT NOT NULL CHECK (length(trim(normalized_alias)) > 0),
    match_kind TEXT NOT NULL CHECK (match_kind IN ('exact', 'approved_pattern')),
    approved_by TEXT,
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z'),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    UNIQUE (normalized_alias)
);
CREATE INDEX ix_payee_aliases_payee ON payee_aliases(payee_id);

CREATE TABLE categories (
    category_id TEXT PRIMARY KEY,
    parent_category_id TEXT REFERENCES categories(category_id) ON DELETE RESTRICT,
    label TEXT NOT NULL CHECK (length(trim(label)) > 0),
    taxonomy_version INTEGER NOT NULL CHECK (taxonomy_version > 0),
    sort_order INTEGER NOT NULL DEFAULT 0 CHECK (typeof(sort_order) = 'integer'),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z'),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(updated_at_utc, -1) = 'Z'),
    CHECK (parent_category_id IS NULL OR parent_category_id <> category_id)
);
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
    transaction_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE RESTRICT,
    booked_on TEXT NOT NULL CHECK (date(booked_on) = booked_on),
    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer'),
    raw_label TEXT NOT NULL,
    identity_label TEXT NOT NULL CHECK (length(identity_label) > 0),
    identity_key_sha256 TEXT NOT NULL CHECK (
        length(identity_key_sha256) = 64 AND identity_key_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    occurrence_rank INTEGER NOT NULL CHECK (typeof(occurrence_rank) = 'integer' AND occurrence_rank >= 1),
    payee_id TEXT REFERENCES payees(payee_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    imported_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(imported_at_utc, -1) = 'Z'),
    UNIQUE (account_id, booked_on, amount_cents, identity_label, occurrence_rank)
);
CREATE INDEX ix_transactions_account_booked
    ON transactions(account_id, booked_on DESC, transaction_id DESC);
CREATE INDEX ix_transactions_payee_booked ON transactions(payee_id, booked_on DESC);

CREATE TABLE source_rows (
    source_row_id TEXT PRIMARY KEY,
    import_file_id TEXT NOT NULL,
    section_index INTEGER NOT NULL,
    logical_row_idx INTEGER NOT NULL CHECK (typeof(logical_row_idx) = 'integer' AND logical_row_idx > 0),
    physical_line_start INTEGER CHECK (physical_line_start IS NULL OR physical_line_start > 0),
    physical_line_end INTEGER CHECK (physical_line_end IS NULL OR physical_line_end >= physical_line_start),
    row_sha256 TEXT NOT NULL CHECK (length(row_sha256) = 64 AND row_sha256 NOT GLOB '*[^0-9a-f]*'),
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z'),
    FOREIGN KEY (import_file_id, section_index)
        REFERENCES import_account_sections(import_file_id, section_index) ON DELETE RESTRICT,
    UNIQUE (import_file_id, section_index, logical_row_idx)
);
CREATE INDEX ix_source_rows_transaction ON source_rows(transaction_id, import_file_id);

CREATE TABLE import_reconciliation_candidates (
    reconciliation_candidate_id TEXT PRIMARY KEY,
    source_row_id TEXT NOT NULL REFERENCES source_rows(source_row_id) ON DELETE RESTRICT,
    candidate_transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    reason_code TEXT NOT NULL CHECK (reason_code IN (
        'case_difference', 'accent_difference', 'punctuation_difference', 'near_label',
        'coverage_boundary_ambiguous', 'other'
    )),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'reviewed_distinct', 'resolved_by_explicit_action')),
    reviewed_by TEXT,
    reviewed_at_utc TEXT CHECK (reviewed_at_utc IS NULL OR substr(reviewed_at_utc, -1) = 'Z'),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z'),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    CHECK (status = 'open' OR (reviewed_by IS NOT NULL AND reviewed_at_utc IS NOT NULL)),
    CHECK (source_row_id IS NOT NULL AND candidate_transaction_id IS NOT NULL),
    UNIQUE (source_row_id, candidate_transaction_id)
);
CREATE INDEX ix_reconciliation_candidates_review
    ON import_reconciliation_candidates(status, created_at_utc, reconciliation_candidate_id);

CREATE TABLE allocations (
    allocation_id TEXT PRIMARY KEY,
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
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z'),
    FOREIGN KEY (category_id, flow_kind)
        REFERENCES category_flow_kinds(category_id, flow_kind) ON DELETE RESTRICT,
    CHECK (
        (flow_kind IN ('expense', 'allocation_personal', 'loan_repayment', 'capital_event',
                       'cash_withdrawal_pending', 'intermediary_pending') AND amount_cents < 0)
        OR (flow_kind IN ('income_recurring', 'income_other', 'loan_disbursement', 'refund') AND amount_cents > 0)
        OR flow_kind IN ('transfer_savings', 'transfer_owned', 'reconciliation')
    )
);
CREATE INDEX ix_allocations_transaction ON allocations(transaction_id, decision);
CREATE INDEX ix_allocations_review ON allocations(decision, origin, transaction_id);

CREATE TABLE classification_suggestions (
    suggestion_id TEXT PRIMARY KEY,
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
        CHECK (substr(created_at_utc, -1) = 'Z'),
    FOREIGN KEY (category_id, proposed_flow_kind)
        REFERENCES category_flow_kinds(category_id, flow_kind) ON DELETE RESTRICT,
    FOREIGN KEY (llm_run_id) REFERENCES llm_runs(llm_run_id) ON DELETE RESTRICT
);
CREATE INDEX ix_suggestions_review ON classification_suggestions(status, method, created_at_utc, suggestion_id);
CREATE INDEX ix_suggestions_transaction ON classification_suggestions(transaction_id, status);

CREATE TABLE classification_decisions (
    decision_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    suggestion_id TEXT REFERENCES classification_suggestions(suggestion_id) ON DELETE RESTRICT,
    action TEXT NOT NULL CHECK (action IN ('confirm', 'edit', 'split', 'reject', 'undo')),
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    before_json TEXT,
    after_json TEXT,
    undone_by_decision_id TEXT REFERENCES classification_decisions(decision_id) ON DELETE RESTRICT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z')
);
CREATE INDEX ix_classification_decisions_transaction ON classification_decisions(transaction_id, created_at_utc);

CREATE TABLE rules (
    rule_id TEXT PRIMARY KEY,
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
        CHECK (substr(created_at_utc, -1) = 'Z'),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(updated_at_utc, -1) = 'Z'),
    FOREIGN KEY (category_id, flow_kind)
        REFERENCES category_flow_kinds(category_id, flow_kind) ON DELETE RESTRICT
);
CREATE INDEX ix_rules_active_priority ON rules(is_active, is_approved, priority, rule_id);

CREATE TABLE rule_executions (
    rule_execution_id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL REFERENCES rules(rule_id) ON DELETE RESTRICT,
    transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    outcome TEXT NOT NULL CHECK (outcome IN ('matched', 'applied', 'skipped_manual', 'skipped_scope', 'failed')),
    before_json TEXT,
    after_json TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1)),
    audit_event_id TEXT REFERENCES audit_events(audit_event_id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    executed_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(executed_at_utc, -1) = 'Z')
);
CREATE INDEX ix_rule_executions_rule ON rule_executions(rule_id, executed_at_utc DESC);
CREATE INDEX ix_rule_executions_transaction ON rule_executions(transaction_id, executed_at_utc DESC);

CREATE TABLE transaction_links (
    transaction_link_id TEXT PRIMARY KEY,
    source_transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    target_transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id) ON DELETE RESTRICT,
    link_kind TEXT NOT NULL CHECK (link_kind IN ('refund_of', 'transfer_pair', 'related')),
    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents > 0),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'confirmed', 'rejected')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z'),
    CHECK (source_transaction_id <> target_transaction_id),
    UNIQUE (source_transaction_id, target_transaction_id, link_kind)
);
CREATE INDEX ix_transaction_links_source ON transaction_links(source_transaction_id, status, link_kind);
CREATE INDEX ix_transaction_links_target ON transaction_links(target_transaction_id, status, link_kind);

CREATE TABLE audit_events (
    audit_event_id TEXT PRIMARY KEY,
    import_file_id TEXT REFERENCES import_files(import_file_id) ON DELETE RESTRICT,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    idempotency_key TEXT,
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z')
);
CREATE INDEX ix_audit_target ON audit_events(target_type, target_id, created_at_utc, audit_event_id);
CREATE INDEX ix_audit_import ON audit_events(import_file_id, created_at_utc);

CREATE TABLE app_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    is_secret INTEGER NOT NULL DEFAULT 0 CHECK (is_secret IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(updated_at_utc, -1) = 'Z')
);

CREATE TABLE llm_runs (
    llm_run_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'partial', 'timeout', 'http_error', 'invalid_response', 'cancelled')),
    batch_size INTEGER NOT NULL CHECK (typeof(batch_size) = 'integer' AND batch_size >= 0),
    latency_ms INTEGER CHECK (latency_ms IS NULL OR (typeof(latency_ms) = 'integer' AND latency_ms >= 0)),
    input_tokens INTEGER CHECK (input_tokens IS NULL OR (typeof(input_tokens) = 'integer' AND input_tokens >= 0)),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR (typeof(output_tokens) = 'integer' AND output_tokens >= 0)),
    recovered_line_count INTEGER NOT NULL DEFAULT 0 CHECK (typeof(recovered_line_count) = 'integer' AND recovered_line_count >= 0),
    redacted_error_code TEXT,
    started_at_utc TEXT NOT NULL CHECK (substr(started_at_utc, -1) = 'Z'),
    finished_at_utc TEXT CHECK (finished_at_utc IS NULL OR substr(finished_at_utc, -1) = 'Z')
);

CREATE TABLE budgets (
    budget_id TEXT PRIMARY KEY,
    category_id TEXT NOT NULL,
    flow_kind TEXT NOT NULL DEFAULT 'expense' CHECK (flow_kind IN ('expense', 'allocation_personal')),
    period_start TEXT NOT NULL CHECK (date(period_start) = period_start),
    period_end TEXT NOT NULL CHECK (date(period_end) = period_end),
    amount_cents INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents >= 0),
    label TEXT NOT NULL CHECK (length(trim(label)) > 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z'),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(updated_at_utc, -1) = 'Z'),
    FOREIGN KEY (category_id, flow_kind)
        REFERENCES category_flow_kinds(category_id, flow_kind) ON DELETE RESTRICT,
    CHECK (period_start <= period_end)
);
CREATE INDEX ix_budgets_period ON budgets(period_start, period_end, is_active);

CREATE TABLE budget_accounts (
    budget_id TEXT NOT NULL REFERENCES budgets(budget_id) ON DELETE RESTRICT,
    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE RESTRICT,
    PRIMARY KEY (budget_id, account_id)
);
CREATE INDEX ix_budget_accounts_account ON budget_accounts(account_id, budget_id);

CREATE TABLE insights (
    insight_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ignored', 'archived')),
    metric_definition_id TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    narrative TEXT NOT NULL,
    generated_by TEXT NOT NULL CHECK (generated_by IN ('deterministic', 'llm')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z'),
    updated_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(updated_at_utc, -1) = 'Z')
);
CREATE INDEX ix_insights_status_created ON insights(status, created_at_utc DESC);

CREATE TABLE backup_manifests (
    backup_manifest_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    archive_relpath TEXT NOT NULL,
    archive_sha256 TEXT NOT NULL CHECK (length(archive_sha256) = 64 AND archive_sha256 NOT GLOB '*[^0-9a-f]*'),
    file_size_bytes INTEGER NOT NULL CHECK (typeof(file_size_bytes) = 'integer' AND file_size_bytes >= 0),
    includes_originals INTEGER NOT NULL DEFAULT 0 CHECK (includes_originals IN (0, 1)),
    status TEXT NOT NULL CHECK (status IN ('created', 'verified', 'restored', 'failed')),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z'),
    verified_at_utc TEXT CHECK (verified_at_utc IS NULL OR substr(verified_at_utc, -1) = 'Z')
);
CREATE INDEX ix_backup_manifests_created ON backup_manifests(created_at_utc DESC);

-- Registre durable des mutations idempotentes HTTP. Le corps conservé est la
-- réponse contractuelle, sans secret ni libellé brut ajouté aux logs.
CREATE TABLE idempotency_records (
    idempotency_key TEXT PRIMARY KEY,
    http_method TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
    response_status INTEGER NOT NULL CHECK (response_status BETWEEN 200 AND 599),
    response_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at_utc, -1) = 'Z')
);

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
