ALTER TABLE asks ALTER COLUMN owner_person_id DROP NOT NULL;
ALTER TABLE asks ADD COLUMN area text;
ALTER TABLE asks ADD COLUMN allow_rule boolean NOT NULL DEFAULT true;
ALTER TABLE asks ADD COLUMN permission boolean NOT NULL DEFAULT false;
ALTER TABLE asks ADD COLUMN awaiting_text boolean NOT NULL DEFAULT false;
ALTER TABLE asks ADD COLUMN card_digest text;
ALTER TABLE asks ADD COLUMN card_sent_at timestamptz;
ALTER TABLE drafts ADD COLUMN card_digest text;
ALTER TABLE drafts ADD COLUMN send_error boolean NOT NULL DEFAULT false;

CREATE VIEW ask_daily_stats AS
SELECT p.key AS person, (a.card_sent_at AT TIME ZONE p.timezone)::date AS day,
       count(*) AS cards_sent
FROM asks a JOIN people p ON p.id=a.owner_person_id
WHERE a.card_sent_at IS NOT NULL
GROUP BY p.key, (a.card_sent_at AT TIME ZONE p.timezone)::date;

CREATE VIEW ask_rule_stats AS
SELECT p.key AS person, count(*) AS answered_by_rule
FROM asks a JOIN people p ON p.id=a.owner_person_id
WHERE a.answered_via='rule' GROUP BY p.key;

ALTER TABLE asks ADD COLUMN card_state text;
ALTER TABLE asks ADD COLUMN card_delivery_state text;
ALTER TABLE drafts ADD COLUMN card_state text;
ALTER TABLE drafts ADD COLUMN card_send_error boolean;
