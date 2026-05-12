# DB Schema Simplification Backlog

Last updated: 2026-05-12

Goal: reduce the PostgreSQL schema by removing unused legacy tables, without direct production deletes. Every removal must go through Alembic and must remove runtime code references first.

## Rules

- No direct `DROP TABLE` in production.
- Every legacy table removal must be guarded by row-count checks.
- Code references must be removed or redirected before schema objects are dropped.
- Downgrades should recreate the legacy table shape for rollback compatibility.
- Validation should include `rg`, `python3 -m py_compile`, SQL shape checks where relevant, and tests when local dependencies are installed.

## Current Progress

All approved simplification work is now grouped into a single Alembic migration:

`brain/platform/db/alembic/versions/0003_schema_simplification.py`

| Status | Scope | Code Status | Validation | Notes |
|---|---|---|---|---|
| Done | `cron_jobs` | `CronJob` model removed from `system.py`; model test updated | `py_compile` OK; no strict runtime refs found | Consolidated migration refuses drop if legacy tables are non-empty |
| Done | `run_log` | CLI run storage moved to `agent_runs` + `agent_run_artifacts` | `py_compile` OK; no runtime `run_log` SQL refs; DB columns checked | Payload replay now uses artifact `run_payload` |
| Done | `tasks` | Planning/meta/nightly logic moved to `agent_runs.input_message`; `Task` model deleted | `py_compile` OK; no runtime `tasks` SQL refs; replacement SQL checked with `EXPLAIN` | `context["tasks"]` remains as compatibility key, sourced from `agent_runs` |
| Done | Legacy learning tables | DB-backed learning models, repositories, API observatory, post-run learning writes, and promotion persistence removed | `py_compile` OK; remote DB verified empty/no views | Pure learning policy/budget utilities remain |
| Done | `agency_*` | Agency API, models, repository, scheduler handoff, runtime settings, mirroring hooks, and tests removed | `py_compile` OK; no runtime agency refs found | Product decision: agency flow is unused |
| Done | Habit tables | Habit compiler models, DB-backed learning module, orphan signature helper, and tests removed | `py_compile` OK; no runtime habit refs found | Migration removes circular FK before dropping tables |
| Done | Legacy quality tables | Legacy critic CLI, delegation tracker, Cortex delegation stats endpoint, nightly assess delegation source, model, and tests removed | `py_compile` OK; no runtime legacy quality refs found | Consolidated migration refuses drop if non-empty |
| Done | `reflections`, `operating_params` | Legacy system models, operating params repository, UoW property, and model test refs removed | `py_compile` OK; no runtime table refs found | Tables are empty and unused |
| Done | Prompt template tables | Prompt template models, storage module, prompt builder, nightly evolution pipeline, scheduler step, and tests removed | `py_compile` OK; no runtime prompt template refs found | Consolidated migration refuses drop if non-empty |
| Done | `brain_prompts` | BrainPrompt model, brain prompt API routes, generator pipeline, scheduler step, and model test refs removed | `py_compile` OK; no runtime brain prompt refs found | Consolidated migration refuses drop if non-empty |
| Done | Emotion system | Emotion API/model/repository, memory emotional embeddings, CLI/MCP/session hooks, dashboard, and pipeline emotion logic removed | `py_compile` OK; no runtime emotion refs found outside migration | Drops `emotional_snapshots` and emotion columns |
| Keep | `scheduler_jobs`, `scheduler_runs`, `scheduler_run_steps`, `scheduler_leases` | Keep for planned cron replacement | n/a | Scheduler is the structured replacement for `cron_jobs`; do not simplify yet |
| Pending | Apply migration | Runtime code is ready | Not applied to remote DB | Prod DB unchanged until Alembic runs |

## Later Candidates

| Priority | Tables | Current Hypothesis | Risk |
|---|---|---|---|
| 1 | TBD | Re-run schema scan after `0003_schema_simplification.py` is applied | Medium |

## Useful Commands

```bash
rg -n "FROM <table>|INSERT INTO <table>|UPDATE <table>|DELETE FROM <table>|__tablename__ = \"<table>\"" brain tests scripts docs
python3 -m py_compile <changed-files>
python3 -m pytest <target-tests>
```

Remote DB metadata checks should stay read-only unless running Alembic intentionally.
