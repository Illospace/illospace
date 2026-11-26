SET LOCAL lock_timeout = '1s';

ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_profile_check;
ALTER TABLE runs ADD CONSTRAINT runs_profile_check CHECK (profile IN (
    'person_dm', 'team_channel', 'tainted', 'routine',
    'agent_request', 'board_coordinator', 'comms_review'
)) NOT VALID;
