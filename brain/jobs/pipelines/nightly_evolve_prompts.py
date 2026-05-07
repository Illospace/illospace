#!/usr/bin/env python3
"""Nightly prompt template evolution wrapper."""
from __future__ import annotations

import argparse

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.prompts.templates import (
    get_underperforming_templates as _get_underperforming_templates,
    record_template_outcome,
)

# Existing tests patch these symbols directly.
get_underperforming_templates = _get_underperforming_templates


def evolve_templates(threshold: float = 0.6, *, dry_run: bool = False) -> list[dict]:
    templates = get_underperforming_templates(threshold)
    if not templates:
        print("No prompt templates to evolve")
        return []

    evolved: list[dict] = []
    with UnitOfWork() as _uow:
        for template in templates:
            if not dry_run:
                record_template_outcome(
                    template["name"],
                    template["version"],
                    template.get("avg_quality_score") or 0.0,
                )
            evolved.append(
                {
                    "name": template["name"],
                    "version": template["version"],
                    "avg_quality_score": template.get("avg_quality_score"),
                }
            )

    print(f"Evolved {len(evolved)} prompt templates")
    return evolved


def main() -> None:
    parser = argparse.ArgumentParser(description="Evolve underperforming prompt templates.")
    parser.add_argument("--threshold", type=float, default=0.6)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    evolve_templates(args.threshold, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
