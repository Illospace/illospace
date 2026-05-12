#!/usr/bin/env python3
"""Nightly dream phase — creative recombination of memories.

Crosses today's high-salience memories with random old ones from different
domains to find surprising connections, counterfactuals, and novel ideas.
Stores outputs as type='dream' memories.

Usage:
    python3 -m brain.jobs.pipelines.nightly_dream [--date 2026-03-04] [--dry-run]
"""
import argparse
import json
import os
import sys
from datetime import date

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))
import brain.kernel.config as config
from brain.platform.db.repositories.unit_of_work import UnitOfWork

PROJECT_ROOT = str(config.BRAIN_DIR)
WORKSPACE = str(config.WORKSPACE_ROOT)
LOG_DIR = str(config.BRAIN_LOG_DIR)


def gather_today_memories(target_date: date, limit: int = 10, org_id: str | None = None) -> list[dict]:
    """Get today's highest-salience memories."""
    _org_filter = "AND org_id = :org_id" if org_id else ""
    _org_params = {"org_id": org_id} if org_id else {}
    with UnitOfWork() as uow:
        result = uow.session.execute(text(f"""
            SELECT id, content, memory_type, salience, tags
            FROM memories
            WHERE created_at::date = :target_date AND NOT archived {_org_filter}
            ORDER BY salience DESC
            LIMIT :lim
        """), {"target_date": target_date, **_org_params, "lim": limit})
        return [dict(r) for r in result.mappings().all()]


def gather_random_old_memories(target_date: date, limit: int = 10, org_id: str | None = None) -> list[dict]:
    """Get random old memories from diverse types."""
    _org_filter = "AND org_id = :org_id" if org_id else ""
    _org_params = {"org_id": org_id} if org_id else {}
    with UnitOfWork() as uow:
        result = uow.session.execute(text(f"""
            SELECT id, content, memory_type, salience, tags,
                   created_at::date as created_date
            FROM memories
            WHERE created_at::date < :target_date - INTERVAL '7 days'
              AND NOT archived
              AND salience >= 4 {_org_filter}
            ORDER BY RANDOM()
            LIMIT :lim
        """), {"target_date": target_date, **_org_params, "lim": limit})
        return [dict(r) for r in result.mappings().all()]


def build_dream_prompt(today_mems: list[dict], old_mems: list[dict],
                       target_date: date) -> str:
    """Build a concise prompt for creative recombination."""
    today_summary = "\n".join(
        f"- [{m['memory_type']}] (salience {m['salience']}): {m['content'][:150]}"
        for m in today_mems
    )
    old_summary = "\n".join(
        f"- [{m['memory_type']}] ({m.get('created_date', '?')}): {m['content'][:150]}"
        for m in old_mems
    )

    return f"""You are Illo's dream module — a creative recombination system.

Date: {target_date}

## Today's key memories:
{today_summary}

## Random older memories from different periods:
{old_summary}

## Task
Find 2-3 surprising connections. For each, provide:
1. A brief insight (1-2 sentences)
2. Why it matters

Also suggest 1 counterfactual: "What if I'd approached X differently?"

Respond as JSON:
```json
{{
  "connections": [
    {{"insight": "...", "why_it_matters": "...", "memories_linked": [id1, id2]}}
  ],
  "counterfactual": {{"scenario": "...", "potential_outcome": "..."}}
}}
```

Keep total output under 500 tokens. Be creative but grounded."""


def call_llm(prompt: str) -> dict | None:
    """Call the configured runtime provider."""
    from brain.systems.runs.direct_agent import call_llm as _call_llm
    return _call_llm(prompt, thinking="low")


def store_dream_memories(dream_output: dict, target_date: date, dry_run: bool) -> int:
    """Store dream connections as memories with type='dream'."""
    stored = 0
    connections = dream_output.get("connections", [])
    counterfactual = dream_output.get("counterfactual")

    items = []
    for conn in connections:
        items.append(f"Connection: {conn.get('insight', '')} — {conn.get('why_it_matters', '')}")
    if counterfactual:
        items.append(f"Counterfactual: {counterfactual.get('scenario', '')} → {counterfactual.get('potential_outcome', '')}")

    if dry_run:
        for item in items:
            print(f"  🔍 [DRY RUN] Would store dream: {item[:80]}")
        return len(items)

    from brain.app.cli.memory import add_memory

    for item in items:
        try:
            result = add_memory(
                content=item,
                memory_type="dream",
                salience=3.0,
                tags=[f"dream-{target_date.isoformat()}"],
                source="nightly-dream",
                decay_eligible=True,
            )
            if not result.get("rejected"):
                stored += 1
                print(f"  💭 Stored dream memory #{result.get('id', '?')}: {item[:60]}")
            else:
                print(f"  ⚠️ Dream rejected: {result.get('reason', '?')}")
        except Exception as e:
            print(f"  ❌ Error storing dream: {e}")

    return stored


def main():
    parser = argparse.ArgumentParser(description="Nightly dream — creative recombination")
    parser.add_argument("--date", help="Target date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    dry_run = args.dry_run

    print(f"{'='*60}")
    print(f"NIGHTLY DREAM — {target_date} {'[DRY RUN]' if dry_run else ''}")
    print(f"{'='*60}")

    today_mems = gather_today_memories(target_date)
    old_mems = gather_random_old_memories(target_date)

    if not today_mems:
        print("No memories from today to dream about. Skipping.")
        return

    print(f"Dreaming with {len(today_mems)} today + {len(old_mems)} old memories...")

    prompt = build_dream_prompt(today_mems, old_mems, target_date)

    if dry_run:
        print(f"\n[DRY RUN] Would send {len(prompt)} char prompt to the configured LLM")
        print(f"[DRY RUN] Would store ~3-4 dream memories")
        return

    dream_output = call_llm(prompt)
    if not dream_output:
        print("❌ LLM call failed — no dream output")
        # Save prompt for debugging
        os.makedirs(LOG_DIR, exist_ok=True)
        prompt_path = os.path.join(LOG_DIR, f"dream-prompt-{target_date}.md")
        with open(prompt_path, "w") as f:
            f.write(prompt)
        print(f"Prompt saved to {prompt_path}")
        return

    # Save raw output
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"dream-output-{target_date}.json"), "w") as f:
        json.dump(dream_output, f, indent=2, default=str)

    stored = store_dream_memories(dream_output, target_date, dry_run)
    print(f"\n💭 Dream complete. Stored {stored} dream memories.")


if __name__ == "__main__":
    main()
