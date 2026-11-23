ALTER TABLE lanes ADD COLUMN is_step boolean NOT NULL DEFAULT false;
ALTER TABLE lanes ADD COLUMN parent_ref text;
