-- Existing tokens belong to session clients. New tokens default to collectors.
UPDATE tokens SET scopes = ARRAY['agent']::text[];
ALTER TABLE tokens ALTER COLUMN scopes SET DEFAULT ARRAY['collector']::text[];
ALTER TABLE tokens ADD CONSTRAINT tokens_scope
    CHECK (scopes = ARRAY['collector']::text[] OR scopes = ARRAY['agent']::text[]);
