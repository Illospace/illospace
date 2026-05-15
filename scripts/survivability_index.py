#!/usr/bin/env python3
"""Measure the Capability Survivability Index for the repository."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain.systems.quality.survivability import (  # noqa: E402
    CapabilityResult,
    SurvivabilityReport,
    assess_survivability,
    changed_files_from_git,
    load_survivability_config,
)


DEFAULT_CONFIG = ROOT / "docs" / "survivability-capabilities.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--changed", nargs="*", default=None)
    parser.add_argument("--base", help="Git base ref to diff against, e.g. origin/main.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument(
        "--fail-under",
        type=_threshold_value,
        help="Exit nonzero if overall score is below this threshold. Accepts 0-1 or 0-100.",
    )
    parser.add_argument(
        "--fail-impacted-under",
        type=_threshold_value,
        help="Exit nonzero if impacted score is below this threshold. Accepts 0-1 or 0-100.",
    )
    parser.add_argument(
        "--fail-impacted-thresholds",
        action="store_true",
        help="Exit nonzero if any impacted capability is below its configured threshold.",
    )
    parser.add_argument(
        "--fail-on-unmapped",
        action="store_true",
        help="Exit nonzero if changed files are not mapped to a capability.",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    config = load_survivability_config(args.config.resolve())
    changed_files = _changed_files(repo_root, args)
    report = assess_survivability(config, repo_root, changed_files=changed_files)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(_format_report(report))

    failed = False
    if args.fail_under is not None and report.overall_score < args.fail_under:
        failed = True
    if (
        args.fail_impacted_under is not None
        and report.impacted_score is not None
        and report.impacted_score < args.fail_impacted_under
    ):
        failed = True
    if args.fail_impacted_thresholds and any(
        capability.status != "meets" for capability in report.impacted_capabilities
    ):
        failed = True
    if args.fail_on_unmapped and report.unmapped_changed_files:
        failed = True
    return 1 if failed else 0


def _changed_files(repo_root: Path, args: argparse.Namespace) -> list[str] | None:
    if args.changed is not None:
        return sorted(path for path in args.changed if path)
    if args.base:
        return changed_files_from_git(repo_root, args.base)
    return None


def _threshold_value(raw: str) -> float:
    value = float(raw)
    if value > 1:
        value = value / 100.0
    if not 0 <= value <= 1:
        raise argparse.ArgumentTypeError("threshold must be between 0 and 1, or 0 and 100")
    return value


def _format_report(report: SurvivabilityReport) -> str:
    lines = [
        "Capability Survivability Index",
        f"Overall score: {_percent(report.overall_score)}",
    ]
    if report.impacted_score is not None:
        lines.append(f"Impacted score: {_percent(report.impacted_score)}")
    else:
        lines.append("Impacted score: n/a (no impacted capabilities)")
    if report.unmapped_changed_files:
        lines.append("Unmapped changed files:")
        for file_path in report.unmapped_changed_files[:10]:
            lines.append(f"  - {file_path}")
        if len(report.unmapped_changed_files) > 10:
            lines.append(f"  - ... {len(report.unmapped_changed_files) - 10} more")

    lines.append("")
    lines.append("Capabilities:")
    for capability in sorted(report.capabilities, key=lambda item: (not item.impacted, item.score)):
        marker = "*" if capability.impacted else " "
        lines.append(
            f"{marker} {capability.capability_id}: {_percent(capability.score)} "
            f"({capability.status} threshold {_percent(capability.threshold)})"
        )
        summary = _category_summary(capability)
        if summary:
            lines.append(f"    {summary}")
        for missing in capability.missing_patterns[:5]:
            lines.append(f"    missing: {missing}")
        if len(capability.missing_patterns) > 5:
            lines.append(f"    missing: ... {len(capability.missing_patterns) - 5} more")
    return "\n".join(lines)


def _category_summary(capability: CapabilityResult) -> str:
    return "; ".join(
        f"{category.category} {category.present}/{category.total}"
        for category in capability.categories
    )


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
