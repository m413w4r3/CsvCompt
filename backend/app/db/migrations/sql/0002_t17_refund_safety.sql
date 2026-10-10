-- T17 delta from frozen 0001_initial to the schema reference (0001 + 0002).

-- Foreign keys are disabled by env.py only inside a guarded migration transaction.

DROP TRIGGER "trg_allocations_referenced_links_immutable";

DROP TRIGGER "trg_refund_links_confirmed_direction_insert";

DROP TRIGGER "trg_refund_links_confirmed_direction_update";

DROP TRIGGER "trg_transaction_link_supersession_replacement";

DROP TRIGGER "trg_classification_decisions_version_increment";

DROP INDEX "ix_classification_decisions_transaction";

PRAGMA legacy_alter_table = ON;

ALTER TABLE classification_decisions RENAME TO classification_decisions_0001;

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

INSERT INTO classification_decisions ("decision_id", "transaction_id", "suggestion_id", "action", "actor", "before_json", "after_json", "undone_by_decision_id", "version", "created_at_utc") SELECT "decision_id", "transaction_id", "suggestion_id", "action", "actor", "before_json", "after_json", "undone_by_decision_id", "version", "created_at_utc" FROM classification_decisions_0001;

DROP TABLE classification_decisions_0001;

PRAGMA legacy_alter_table = OFF;

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

-- MIGRATION_BACKFILL_BOUNDARY

CREATE INDEX ix_classification_decisions_transaction ON classification_decisions(transaction_id, created_at_utc);

CREATE TRIGGER trg_classification_decisions_version_increment
BEFORE UPDATE ON classification_decisions
WHEN NEW.version <> OLD.version + 1
BEGIN
    SELECT RAISE(ABORT, 'version_increment_required');
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

CREATE TRIGGER trg_refund_link_allocation_parts_delete_forbidden
BEFORE DELETE ON refund_link_allocation_parts
BEGIN
    SELECT RAISE(ABORT, 'refund_link_part_delete_forbidden');
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

CREATE TRIGGER trg_refund_links_status_transition
BEFORE UPDATE OF status ON transaction_links
WHEN NEW.link_kind = 'refund_of' AND NEW.status <> OLD.status
 AND NOT (OLD.status = 'proposed' AND NEW.status IN ('confirmed', 'rejected'))
BEGIN
    SELECT RAISE(ABORT, 'refund_link_status_transition_invalid');
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
