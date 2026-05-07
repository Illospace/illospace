"""
Post-merge hooks — run after successful PR merges to illo-brain.

Usage:
    from brain.systems.cortex.post_merge_hooks import run_post_merge_hooks
    run_post_merge_hooks(changed_files=["dashboard/cortex_api.py", "services/foo.py"])

CLI:
    python3 -m brain.systems.cortex.post_merge_hooks --files frontend/src/routes/+layout.svelte
"""
from __future__ import annotations
import argparse
import sys


DASHBOARD_PATHS = ("dashboard/",)


def has_dashboard_changes(files: list[str]) -> bool:
    """Check if any changed files are dashboard-related."""
    return any(f.startswith(p) for f in files for p in DASHBOARD_PATHS)


def run_post_merge_hooks(changed_files: list[str]) -> None:
    """Run all post-merge hooks based on changed files."""
    if has_dashboard_changes(changed_files):
        print("📦 Dashboard files changed — restarting illo-dashboard...")
        from brain.systems.cortex.restarter import restart_dashboard
        restart_dashboard()
    else:
        print("ℹ️  No dashboard files changed — no restart needed.")


def main():
    parser = argparse.ArgumentParser(description="Run post-merge hooks")
    parser.add_argument("--files", nargs="+", required=True, help="List of changed files")
    args = parser.parse_args()
    run_post_merge_hooks(args.files)


if __name__ == "__main__":
    main()
