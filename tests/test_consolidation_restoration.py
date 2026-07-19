"""Regression coverage for the nightly consolidation restoration."""

from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import text

from tests.conftest import requires_db


@requires_db
async def test_reconstructive_consolidation_phase_is_persisted(
    db_session,
    unit_of_work_for_session,
    monkeypatch,
):
    """The public consolidation entrypoint accepts its meaningful phase name."""
    from brain.jobs.pipelines import consolidate

    monkeypatch.setattr(consolidate, "UnitOfWork", unit_of_work_for_session)

    result = await consolidate.phase_consolidation(date(2040, 1, 2))

    phase = await db_session.scalar(
        text("SELECT phase FROM consolidation_runs WHERE id = :run_id"),
        {"run_id": result["run_id"]},
    )
    max_length = await db_session.scalar(
        text(
            """
            SELECT character_maximum_length
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'consolidation_runs'
              AND column_name = 'phase'
            """
        )
    )

    assert result["phase"] == "reconstructive_consolidation"
    assert phase == "reconstructive_consolidation"
    assert max_length == 64


def test_scheduler_runtime_state_stays_under_private_home(tmp_path):
    """Scheduler-owned files follow ILLO_PRIVATE_HOME, never the source tree."""
    private_home = tmp_path / "private"
    script = r'''
import asyncio
import builtins
from io import StringIO
import json
from pathlib import Path

from brain.kernel import config
from brain.jobs.pipelines import curiosity
from brain.app.cli import meta_learn, token_metrics

private_home = Path(config.PRIVATE_HOME).resolve()
write_paths = []
real_open = builtins.open


class Sink(StringIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def guarded_open(file, mode="r", *args, **kwargs):
    path = Path(file).resolve()
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        write_paths.append(path)
        if not path.is_relative_to(private_home):
            return Sink()
    return real_open(file, mode, *args, **kwargs)


async def fake_report(days=7):
    return {
        "overall": {
            "avg_tokens_total": 0,
            "avg_tokens_input": 0,
            "avg_tokens_output": 0,
            "avg_cost": 0,
            "total_cost": 0,
            "total_runs": 0,
        },
        "token_tracking_coverage_pct": 0,
        "cache_efficiency": {"avg_cache_hit_pct": 0},
        "by_model": [],
    }


async def fake_daily_breakdown(days=7):
    return []


builtins.open = guarded_open
token_metrics.report = fake_report
token_metrics.daily_breakdown = fake_daily_breakdown
try:
    curiosity.save_state({"last_reads": {}, "total_readings": 0, "last_run": None})
    meta_learn._save_meta_state({"validation_criteria": {}})
    asyncio.run(token_metrics.baseline_snapshot())
finally:
    builtins.open = real_open

configured_paths = [
    curiosity.STATE_FILE,
    curiosity.READINGS_DIR,
    curiosity.BRIEFS_DIR,
    curiosity.LOGS_DIR,
    Path(meta_learn.META_STATE_PATH),
]
print(json.dumps({
    "private_home": str(private_home),
    "configured_paths": [str(Path(path).resolve()) for path in configured_paths],
    "write_paths": [str(path) for path in write_paths],
}))
'''
    env = os.environ.copy()
    env["ILLO_PRIVATE_HOME"] = str(private_home)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    private_root = Path(payload["private_home"])
    configured_paths = [Path(path) for path in payload["configured_paths"]]
    write_paths = [Path(path) for path in payload["write_paths"]]

    assert len(write_paths) == 3
    assert all(path.is_relative_to(private_root) for path in configured_paths)
    assert all(path.is_relative_to(private_root) for path in write_paths)
    assert all(path.is_file() for path in write_paths)
