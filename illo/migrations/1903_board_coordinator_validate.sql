SET LOCAL lock_timeout = '1s';
ALTER TABLE runs VALIDATE CONSTRAINT runs_profile_check;
ALTER TABLE board_threads VALIDATE CONSTRAINT board_threads_group_key_check;
ALTER TABLE asks VALIDATE CONSTRAINT asks_board_answer_via_check;
