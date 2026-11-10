ALTER TABLE asks DROP CONSTRAINT asks_answered_via_check;
ALTER TABLE asks ADD CONSTRAINT asks_answered_via_check
    CHECK (answered_via IN ('person', 'rule', 'approved_draft'));
