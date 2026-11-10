# Search ranking

Search ranks visible text by match strength and age. It then adds a small bonus for each source rank.
The same rules apply to items, lanes, inbound Slack events, GitHub, and personal search providers.
A source cannot push a strong recent match down with many weak matches.

1. Search keeps the full tokens in the text. It also adds each part of an email or identifier.
   For example, `mira.orbit@example.com` matches the full address, `mira`, `orbit`, `example`, and `com`.
   `output_image_unreadable` and `ABC-123` also match their parts.
   The indexed vector and query use the same token rules.
2. Search uses the `simple` and `english` text configurations.
   The English configuration lets words such as `price` and `pricing` match.
   Plain queries accept any word as a fallback. Quotes, `OR`, and exclusions keep their search meaning.
3. Search removes `today`, `yesterday`, and `this week` from plain queries.
   These words do not cause a useful match to fail. They do not set a date filter.
   Quoted text and explicit search operators keep these words.
   A query with only these time words returns no results.
4. The lexical score uses PostgreSQL `ts_rank` with normalization 32: `rank / (rank + 1)`.
   Title weight is 1.0, summary weight is 0.75, and body weight is 0.5.
   A strict match meets the query in either text configuration. It gets a multiplier of 2.
   An any-word fallback gets a multiplier of 1.
5. The recency factor is `0.2 + 0.8 * 0.5 ** (age_days / 21)`.
   `age_days` counts complete days and cannot be negative. All sources share one clock per search.
   The variable part has a 21-day half-life. The factor stays at or above 0.2.
   Future dates get a factor of 1. Missing dates get a factor of 0.2.
   No workspace search config exists. `HALF_LIFE_DAYS` in `illo/sources/search.py` sets the constant.
6. Each source supplies up to 50 candidates, even for a shorter result page.
   Ledger sources select their candidates by `lexical * recency * match_multiplier`, before the result limit.
   Search considers fallback candidates even when strict candidates fill the page.
   The final score adds reciprocal-rank fusion: `sum(1 / (60 + source_rank))`.
   A repeated source reference keeps its strongest match evidence and its privacy flags.
   It also keeps the rank bonus from each source that found it.

The final `score` sets the result order. `match_tier` explains the strict or fallback multiplier.
The `knowledge.search` response gives the lexical score, recency factor, and rank bonus in `scores`.
The `scores.rrf` field contains only the rank bonus. It no longer acts as the full score.
The existing result fields stay available. Semantic search remains off.
The brain tool keeps its compact text format and the 12,000-character limit.

Live sources use the same lexical weights on their returned title and snippet.
A provider can return less text than the full source document. Its lexical score can then be lower.
Search keeps the provider's match tier because the provider can search fields absent from its snippet.
Missing source dates do not get a fresh timestamp.

Old decisions still match. Age changes their order; it never excludes them.
SQL checks visibility before it scores or limits items and lanes.
A recent private match stays hidden from another person and the team principal.
Personal providers keep their existing grant checks. A tainted run has no search tool.

## Migrations

`0402_search_ranking.sql` adds immutable token and query functions and a GIN expression index for items.
It does not rewrite the items table or replace its old generated `tsv` column.
`0403_search_event_parts.sql` adds the matching GIN expression index for inbound Slack events.
Lanes build their vector from their title, summary, and last 20 events during search.

Each index build holds a `SHARE` lock until its migration commits. Reads continue. Writes wait for the build.
The files use separate transactions so the event scan does not extend the items lock.
Each file has a one-second lock timeout and a ten-second statement timeout.
A timeout rolls back that file. The migration runner can retry it without a partial index or receipt.
The runner records each completed file and skips it on later runs.
A large event table can need a separate plan for an index build. These migrations never wait without a timeout.

## Evaluation

`tests/search/test_ranking.py` uses only invented data and a fixed clock.
It has 300 old archive items, 20 old lanes, 20 old GitHub issues, and one recent short channel message.
It also has an old named decision and a recent private match.
The test checks product pricing, time words, email parts, identifiers, source fairness, and response scores.
Safety tests also check that a perfect recent private match stays hidden.
