-- Open GitHub items without a work report belong to the backlog.
ALTER TABLE lanes DROP CONSTRAINT lanes_state_check;
ALTER TABLE lanes ADD CONSTRAINT lanes_state_check
    CHECK (state IN ('open','working','waiting_person','blocked','review','done','failed','unknown'));
UPDATE lanes SET state='open' WHERE source='github' AND state='unknown';
