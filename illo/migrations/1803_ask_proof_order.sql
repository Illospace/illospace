SET LOCAL lock_timeout = '1s';

-- jsonb does not keep the order of keys. This column keeps the order that the requester gave.
ALTER TABLE asks ADD COLUMN IF NOT EXISTS proof_order jsonb;
