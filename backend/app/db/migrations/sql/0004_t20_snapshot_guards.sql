-- T20 correctif revue : empreintes immuables et liens d'annulation stricts.
ALTER TABLE classification_decisions
    ADD COLUMN before_json_sha256 TEXT
    CHECK (before_json_sha256 IS NULL OR
           (length(before_json_sha256) = 64 AND
            before_json_sha256 NOT GLOB '*[^0-9a-f]*'));
ALTER TABLE classification_decisions
    ADD COLUMN after_json_sha256 TEXT
    CHECK (after_json_sha256 IS NULL OR
           (length(after_json_sha256) = 64 AND
            after_json_sha256 NOT GLOB '*[^0-9a-f]*'));

DROP TRIGGER trg_rule_executions_update_guard;
DROP TRIGGER trg_classification_decisions_undo_guard;

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
     OLD.outcome IS NOT 'applied' OR
     OLD.dry_run IS NOT 0 OR
     OLD.classification_decision_id IS NULL OR
     NOT EXISTS (
         SELECT 1 FROM classification_decisions source
         WHERE source.decision_id = OLD.classification_decision_id
           AND source.transaction_id = OLD.transaction_id
           AND source.action <> 'undo'
           AND (source.undone_by_decision_id IS NULL OR
                source.undone_by_decision_id = NEW.undone_by_decision_id)
     ) OR
     NOT EXISTS (
         SELECT 1 FROM classification_decisions undo
         WHERE undo.decision_id = NEW.undone_by_decision_id
           AND undo.transaction_id = NEW.transaction_id
           AND undo.action = 'undo'
           AND undo.undone_by_decision_id IS NULL
     )
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
     NEW.before_json_sha256 IS NOT OLD.before_json_sha256 OR
     NEW.after_json_sha256 IS NOT OLD.after_json_sha256 OR
     NEW.transaction_version IS NOT OLD.transaction_version OR
     NEW.created_at_utc IS NOT OLD.created_at_utc OR
     OLD.action = 'undo' OR
     NOT EXISTS (
         SELECT 1 FROM classification_decisions undo
         WHERE undo.decision_id = NEW.undone_by_decision_id
           AND undo.transaction_id = NEW.transaction_id
           AND undo.action = 'undo'
           AND undo.decision_id <> OLD.decision_id
           AND undo.undone_by_decision_id IS NULL
     )
BEGIN
    SELECT RAISE(ABORT, 'classification_decision_undo_link_invalid');
END;
