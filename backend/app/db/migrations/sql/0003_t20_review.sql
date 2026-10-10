-- T20: extraire et rétablir des décisions de façon versionnée et idempotente.
ALTER TABLE classification_decisions
    ADD COLUMN transaction_version INTEGER
    CHECK (transaction_version IS NULL OR
           (typeof(transaction_version) = 'integer' AND transaction_version > 0));

ALTER TABLE rule_executions
    ADD COLUMN batch_id TEXT
    CHECK (batch_id IS NULL OR
        (length(batch_id) = 36 AND batch_id GLOB
         '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]-4[0-9a-f][0-9a-f][0-9a-f]-[89ab][0-9a-f][0-9a-f][0-9a-f]-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]'));
ALTER TABLE rule_executions
    ADD COLUMN rule_version INTEGER
    CHECK (rule_version IS NULL OR (typeof(rule_version) = 'integer' AND rule_version > 0));
ALTER TABLE rule_executions
    ADD COLUMN batch_scope_sha256 TEXT
    CHECK (batch_scope_sha256 IS NULL OR
           (length(batch_scope_sha256) = 64 AND
            batch_scope_sha256 NOT GLOB '*[^0-9a-f]*'));
ALTER TABLE rule_executions
    ADD COLUMN classification_decision_id TEXT
    REFERENCES classification_decisions(decision_id) ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE rule_executions
    ADD COLUMN undone_by_decision_id TEXT
    REFERENCES classification_decisions(decision_id) ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED;

-- MIGRATION_BACKFILL_BOUNDARY
CREATE UNIQUE INDEX ux_rule_executions_batch_transaction
    ON rule_executions(batch_id, transaction_id, dry_run)
    WHERE batch_id IS NOT NULL;
CREATE UNIQUE INDEX ux_rule_executions_classification_decision
    ON rule_executions(classification_decision_id)
    WHERE classification_decision_id IS NOT NULL;
CREATE UNIQUE INDEX ux_rule_executions_undo_decision
    ON rule_executions(undone_by_decision_id)
    WHERE undone_by_decision_id IS NOT NULL;
CREATE UNIQUE INDEX ux_classification_decisions_undo_target
    ON classification_decisions(undone_by_decision_id)
    WHERE undone_by_decision_id IS NOT NULL;
CREATE INDEX ix_rule_executions_batch
    ON rule_executions(batch_id, outcome, transaction_id);

CREATE TRIGGER trg_rule_executions_insert_guard
BEFORE INSERT ON rule_executions
WHEN NEW.batch_id IS NULL OR NEW.rule_version IS NULL OR
     NEW.batch_scope_sha256 IS NULL OR
     (NEW.outcome = 'applied' AND NEW.dry_run = 0 AND
      (NEW.classification_decision_id IS NULL OR NOT EXISTS (
          SELECT 1 FROM classification_decisions d
          WHERE d.decision_id = NEW.classification_decision_id
            AND d.transaction_id = NEW.transaction_id
            AND d.action IN ('confirm', 'edit', 'split')
      )))
BEGIN
    SELECT RAISE(ABORT, 'rule_execution_versioned_trace_required');
END;

CREATE TRIGGER trg_rule_executions_batch_consistency
BEFORE INSERT ON rule_executions
WHEN EXISTS (
    SELECT 1 FROM rule_executions prior
    WHERE prior.batch_id = NEW.batch_id
      AND (prior.rule_id IS NOT NEW.rule_id OR
           prior.rule_version IS NOT NEW.rule_version OR
           prior.batch_scope_sha256 IS NOT NEW.batch_scope_sha256)
)
BEGIN
    SELECT RAISE(ABORT, 'rule_execution_batch_metadata_conflict');
END;

CREATE TRIGGER trg_rule_executions_update_guard
BEFORE UPDATE ON rule_executions
WHEN OLD.undone_by_decision_id IS NOT NULL OR
     NEW.undone_by_decision_id IS NULL OR
     NEW.rule_execution_id IS NOT OLD.rule_execution_id OR
     NEW.rule_id IS NOT OLD.rule_id OR
     NEW.transaction_id IS NOT OLD.transaction_id OR
     NEW.outcome IS NOT OLD.outcome OR
     NEW.before_json IS NOT OLD.before_json OR
     NEW.after_json IS NOT OLD.after_json OR
     NEW.dry_run IS NOT OLD.dry_run OR
     NEW.audit_event_id IS NOT OLD.audit_event_id OR
     NEW.executed_at_utc IS NOT OLD.executed_at_utc OR
     NEW.batch_id IS NOT OLD.batch_id OR
     NEW.rule_version IS NOT OLD.rule_version OR
     NEW.batch_scope_sha256 IS NOT OLD.batch_scope_sha256 OR
     NEW.classification_decision_id IS NOT OLD.classification_decision_id OR
     NOT EXISTS (
         SELECT 1 FROM classification_decisions d
         WHERE d.decision_id = NEW.undone_by_decision_id
           AND d.transaction_id = NEW.transaction_id
           AND d.action = 'undo'
     )
BEGIN
    SELECT RAISE(ABORT, 'rule_execution_immutable');
END;

CREATE TRIGGER trg_rule_executions_delete_forbidden
BEFORE DELETE ON rule_executions
BEGIN
    SELECT RAISE(ABORT, 'rule_execution_immutable');
END;

CREATE TRIGGER trg_classification_decisions_undo_guard
BEFORE UPDATE ON classification_decisions
WHEN OLD.undone_by_decision_id IS NOT NULL OR
     NEW.undone_by_decision_id IS NULL OR
     NEW.decision_id IS NOT OLD.decision_id OR
     NEW.transaction_id IS NOT OLD.transaction_id OR
     NEW.suggestion_id IS NOT OLD.suggestion_id OR
     NEW.action IS NOT OLD.action OR
     NEW.actor IS NOT OLD.actor OR
     NEW.before_json IS NOT OLD.before_json OR
     NEW.after_json IS NOT OLD.after_json OR
     NEW.transaction_version IS NOT OLD.transaction_version OR
     NEW.created_at_utc IS NOT OLD.created_at_utc OR
     NOT EXISTS (
         SELECT 1 FROM classification_decisions d
         WHERE d.decision_id = NEW.undone_by_decision_id
           AND d.transaction_id = NEW.transaction_id
           AND d.action = 'undo'
     )
BEGIN
    SELECT RAISE(ABORT, 'classification_decision_undo_link_invalid');
END;

CREATE TRIGGER trg_classification_decisions_delete_forbidden
BEFORE DELETE ON classification_decisions
BEGIN
    SELECT RAISE(ABORT, 'classification_decision_immutable');
END;
