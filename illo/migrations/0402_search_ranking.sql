-- Fail fast behind a busy writer. The runner can retry this whole transaction.
SET LOCAL lock_timeout = '1s';
SET LOCAL statement_timeout = '10s';

-- Keep each full token. Add the words in emails and dotted or joined identifiers.
CREATE OR REPLACE FUNCTION illo_search_tokens(config regconfig, document text)
RETURNS tsvector LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT to_tsvector(config,coalesce(document,'')) ||
      to_tsvector(config,coalesce((
        SELECT string_agg(regexp_replace(m[1],'[._@+-]',' ','g'),' ')
        FROM regexp_matches(coalesce(document,''),
          '([[:alnum:]]+([._@+-][[:alnum:]]+)+)','g') AS m
      ),''))
$$;

CREATE OR REPLACE FUNCTION illo_search_vector(title text, summary text, body text)
RETURNS tsvector LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT setweight(illo_search_tokens('simple',title)||illo_search_tokens('english',title),'A') ||
      setweight(illo_search_tokens('simple',summary)||illo_search_tokens('english',summary),'B') ||
      setweight(illo_search_tokens('simple',body)||illo_search_tokens('english',body),'C')
$$;

CREATE OR REPLACE FUNCTION illo_search_query(config regconfig, query text)
RETURNS tsquery LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT websearch_to_tsquery(config,query) ||
      CASE WHEN query ~ '(^|[[:space:]])-[^[:space:]]|"|(^|[[:space:]])(OR|or)([[:space:]]|$)'
        THEN ''::tsquery
        ELSE websearch_to_tsquery(config,regexp_replace(query,'[._@+-]',' ','g')) END
$$;

-- An expression index avoids a table rewrite and leaves the old vector intact.
-- CREATE INDEX holds SHARE until commit. Reads continue; writes wait briefly.
CREATE INDEX IF NOT EXISTS items_search_parts_idx ON items USING gin (
    illo_search_vector(title,summary,body)
);
