#!/usr/bin/env python3
"""
Illo Brain — Manual Daily Entry Generator

Convenience wrapper to generate blog + journal entries for a given date.
The nightly_sleep.sh cron handles this automatically, but this script
allows manual runs for backfills or re-generation.

Usage:
    python3 scripts/write_daily_entries.py                    # today
    python3 scripts/write_daily_entries.py --date 2026-03-25  # specific date
    python3 scripts/write_daily_entries.py --blog-only        # just blog
    python3 scripts/write_daily_entries.py --journal-only     # just journal
    python3 scripts/write_daily_entries.py --range 2026-03-20 2026-03-25  # backfill range
"""

import argparse
import os
import subprocess
import sys
from datetime import date, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
BLOG_DIR = os.path.join(PROJECT_ROOT, "content", "blog")
JOURNAL_DIR = os.path.join(PROJECT_ROOT, "journal")


def generate_blog(target_date: date) -> bool:
    """Generate blog entry by calling generate_blog.py."""
    out_path = os.path.join(BLOG_DIR, f"{target_date.isoformat()}.md")
    if os.path.exists(out_path):
        print(f"  📝 Blog already exists: {out_path}")
        return True

    print(f"  📝 Generating blog for {target_date}...")
    result = subprocess.run(
        [sys.executable, os.path.join(BLOG_DIR, "generate_blog.py"),
         "--date", target_date.isoformat()],
        cwd=PROJECT_ROOT,
        capture_output=True, text=True, timeout=300
    )
    if result.returncode == 0:
        print(f"  ✅ Blog generated")
        return True
    else:
        print(f"  ❌ Blog generation failed: {result.stderr[:200]}")
        return False


def generate_journal(target_date: date) -> bool:
    """Generate journal entry by calling nightly_reflect."""
    out_path = os.path.join(JOURNAL_DIR, f"{target_date.isoformat()}.md")
    if os.path.exists(out_path):
        print(f"  📓 Journal already exists: {out_path}")
        return True

    print(f"  📓 Generating journal for {target_date}...")
    result = subprocess.run(
        [sys.executable, "-m", "brain.jobs.pipelines.nightly_reflect",
         "--date", target_date.isoformat()],
        cwd=PROJECT_ROOT,
        capture_output=True, text=True, timeout=300
    )
    if result.returncode == 0:
        print(f"  ✅ Journal generated")
        return True
    else:
        print(f"  ❌ Journal generation failed: {result.stderr[:200]}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Generate daily blog + journal entries")
    parser.add_argument("--date", type=str, default=None,
                        help="Target date YYYY-MM-DD (default: today)")
    parser.add_argument("--range", nargs=2, metavar=("START", "END"),
                        help="Generate for date range: START END (inclusive)")
    parser.add_argument("--blog-only", action="store_true",
                        help="Only generate blog entry")
    parser.add_argument("--journal-only", action="store_true",
                        help="Only generate journal entry")
    parser.add_argument("--force", action="store_true",
                        help="Regenerate even if entry exists")
    args = parser.parse_args()

    if args.range:
        start = date.fromisoformat(args.range[0])
        end = date.fromisoformat(args.range[1])
        dates = []
        current = start
        while current <= end:
            dates.append(current)
            current += timedelta(days=1)
    elif args.date:
        dates = [date.fromisoformat(args.date)]
    else:
        dates = [date.today()]

    print(f"🗓️  Processing {len(dates)} date(s): {dates[0]} → {dates[-1]}")

    for target_date in dates:
        print(f"\n--- {target_date.isoformat()} ---")

        if not args.journal_only:
            if args.force:
                path = os.path.join(BLOG_DIR, f"{target_date.isoformat()}.md")
                if os.path.exists(path):
                    os.remove(path)
            generate_blog(target_date)

        if not args.blog_only:
            if args.force:
                path = os.path.join(JOURNAL_DIR, f"{target_date.isoformat()}.md")
                if os.path.exists(path):
                    os.remove(path)
            generate_journal(target_date)

    print("\n✅ Done!")


if __name__ == "__main__":
    main()
