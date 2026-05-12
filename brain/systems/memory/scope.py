#!/usr/bin/env python3
"""Classify memory scope as 'universal' or 'personal'.

Usage:
    python3 -m services.scope_classifier --reclassify-all [--dry-run]
"""
import argparse
import os
import re
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

# --- Heuristic patterns ---

# Personal signals: any match → personal
PERSONAL_PATTERNS = [
    # Specific people / roles with names
    r'\bcustomer\s+\w+\b',
    r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+(?:prefers|asked|said|corrected|reported|found|wants|needs)\b',
    # IP addresses
    r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b',
    # Tokens / secrets
    r'\b(?:ghp_|github_pat_|sk-|xox[bpas]-|Bearer\s)\S+',
    # URLs with specific domains (not generic docs)
    r'https?://(?!(?:docs\.|en\.wikipedia|stackoverflow))[a-zA-Z0-9.-]+\.[a-z]{2,}/\S*',
    # File paths outside the runtime-private state directory
    r'(?:/home/\w+|/var|/etc|/opt|C:\\)[^\s]+',
    # Specific repo references
    r'\b(?:illo|shopify-app|illoaiapp)\b',
    # Dates tied to specific events
    r'\b(?:on|last)\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}',
    r'\bon\s+\d{4}-\d{2}-\d{2}\b',
    # Email addresses
    r'\b[\w.+-]+@[\w-]+\.[\w.]+\b',
    # Emotional context about specific interactions
    r'\b(?:frustrated with|angry at|happy that|disappointed by)\s+[A-Z]',
]

# Universal signals
UNIVERSAL_PATTERNS = [
    r'\b(?:always|never|when\s+\w+\s+happens?|rule|principle|pattern|best\s+practice)\b',
    r'\b(?:technique|approach|strategy|architecture|methodology)\b',
    r'\b(?:error\s+pattern|debugging|root\s+cause|symptom)\b',
]

UNIVERSAL_TYPES = {'lesson', 'principle', 'skill', 'dream'}
PERSONAL_TYPES = {'session', 'daily_log'}


def classify_scope(content: str, memory_type: str = None) -> str:
    """Returns 'universal' or 'personal'."""
    # Type-based bias
    if memory_type in PERSONAL_TYPES:
        return 'personal'

    # Check for personal signals — any match → personal
    for pattern in PERSONAL_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return 'personal'

    # Universal type + no personal signals → universal
    if memory_type in UNIVERSAL_TYPES:
        return 'universal'

    # Check universal signal density
    universal_hits = sum(
        1 for p in UNIVERSAL_PATTERNS if re.search(p, content, re.IGNORECASE)
    )
    if universal_hits >= 2:
        return 'universal'

    # Default: personal (safer)
    return 'personal'


def reclassify_all(dry_run: bool = False) -> dict:
    """Reclassify all existing memories."""
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    stats = {'total': 0, 'universal': 0, 'personal': 0, 'changed': 0}
    with UnitOfWork() as uow:
        result = uow.session.execute(text(
            "SELECT id, content, memory_type, scope FROM memories WHERE NOT archived"
        ))
        rows = result.mappings().all()
        stats['total'] = len(rows)

        for row in rows:
            new_scope = classify_scope(row['content'], row.get('memory_type'))
            old_scope = row.get('scope') or 'personal'
            stats[new_scope] += 1

            if new_scope != old_scope:
                stats['changed'] += 1
                if not dry_run:
                    uow.session.execute(text(
                        "UPDATE memories SET scope = :scope WHERE id = :id"
                    ), {"scope": new_scope, "id": row['id']})
                print(f"  [{row['id']}] {old_scope} → {new_scope}: {row['content'][:80]}")

        if dry_run:
            uow.rollback()

    return stats


def main():
    parser = argparse.ArgumentParser(description='Reclassify memory scopes')
    parser.add_argument('--reclassify-all', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    if args.reclassify_all:
        stats = reclassify_all(dry_run=args.dry_run)
        mode = "DRY RUN" if args.dry_run else "APPLIED"
        print(f"\n[{mode}] {stats['total']} memories: "
              f"{stats['universal']} universal, {stats['personal']} personal, "
              f"{stats['changed']} changed")
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
