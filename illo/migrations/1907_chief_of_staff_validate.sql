SET LOCAL lock_timeout = '1s';

ALTER TABLE board_posts VALIDATE CONSTRAINT board_posts_decision_provenance_check;
ALTER TABLE board_episodes VALIDATE CONSTRAINT board_episodes_reason_check;
ALTER TABLE board_coordinator_runs VALIDATE CONSTRAINT board_coordinator_runs_action_check;
ALTER TABLE board_escalations VALIDATE CONSTRAINT board_escalations_reason_check;
ALTER TABLE board_escalations VALIDATE CONSTRAINT board_escalations_text_check;
