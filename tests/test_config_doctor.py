"""Tests for production config doctor and ops hygiene."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from brain.app.cli import config_doctor


ROOT = Path(__file__).resolve().parents[1]


def _codes(report: config_doctor.DoctorReport) -> set[str]:
    return {finding.code for finding in report.findings}


def _statuses(report: config_doctor.DoctorReport) -> dict[str, config_doctor.DoctorStatus]:
    return {status.code: status for status in report.statuses}


def _safe_prod_env() -> dict[str, str]:
    return {
        "ILLO_ENV": "production",
        "SECRET_KEY": "set-in-prod-secret-store",
        "VAULT_MASTER_KEY": "set-in-prod-secret-store",
        "DATABASE_URL": "postgresql://illo:password@db/illo_memory",
        "ILLO_ALLOW_LOCAL_CODEX_AUTH_FALLBACK": "0",
        "ILLO_VALIDATE_MIGRATIONS": "1",
    }


def test_production_rejects_unsafe_auth_fallback(tmp_path):
    env = _safe_prod_env() | {"ILLO_ALLOW_LOCAL_CODEX_AUTH_FALLBACK": "1"}

    report = config_doctor.run_checks(
        root=tmp_path,
        env=env,
        production=True,
        scan_tracked_secrets=False,
    )

    assert "unsafe-dev-auth-fallback" in _codes(report)
    assert not report.ok


def test_production_rejects_missing_secret_key(tmp_path):
    env = _safe_prod_env()
    env.pop("SECRET_KEY")

    report = config_doctor.run_checks(
        root=tmp_path,
        env=env,
        production=True,
        scan_tracked_secrets=False,
    )

    assert "missing-secret-key" in _codes(report)


def test_production_rejects_missing_vault_master_key(tmp_path):
    env = _safe_prod_env()
    env.pop("VAULT_MASTER_KEY")

    report = config_doctor.run_checks(
        root=tmp_path,
        env=env,
        production=True,
        scan_tracked_secrets=False,
    )

    assert "missing-vault-master-key" in _codes(report)


def test_production_rejects_missing_database_url_or_config(tmp_path):
    env = _safe_prod_env()
    env.pop("DATABASE_URL")

    report = config_doctor.run_checks(
        root=tmp_path,
        env=env,
        production=True,
        scan_tracked_secrets=False,
    )

    assert "missing-db-url" in _codes(report)


def test_production_rejects_disabled_migration_validation(tmp_path):
    env = _safe_prod_env() | {"ILLO_VALIDATE_MIGRATIONS": "false"}

    report = config_doctor.run_checks(
        root=tmp_path,
        env=env,
        production=True,
        scan_tracked_secrets=False,
    )

    assert "disabled-migration-validation" in _codes(report)


def test_valid_production_env_passes_without_tracked_secret_scan(tmp_path):
    report = config_doctor.run_checks(
        root=tmp_path,
        env=_safe_prod_env(),
        production=True,
        scan_tracked_secrets=False,
    )

    assert report.ok
    assert report.findings == ()


def test_tracked_secret_like_config_is_flagged_without_value(tmp_path):
    secret_file = tmp_path / "deploy" / "production.env"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("FLASK_SECRET_KEY=super-secret-value\n", encoding="utf-8")

    with patch("brain.app.cli.config_doctor._tracked_files", return_value=["deploy/production.env"]):
        report = config_doctor.run_checks(
            root=tmp_path,
            env=_safe_prod_env(),
            production=True,
            scan_tracked_secrets=True,
        )

    messages = "\n".join(finding.message for finding in report.findings)
    assert "tracked-secret-like-config" in _codes(report)
    assert "FLASK_SECRET_KEY" in messages
    assert "super-secret-value" not in messages


def test_cli_returns_nonzero_for_unsafe_production_config(capsys):
    env = _safe_prod_env() | {"ILLO_SKIP_MIGRATION_VALIDATION": "1"}

    with patch("brain.app.cli.config_doctor.os.environ", env), patch(
        "brain.app.cli.config_doctor._tracked_files",
        return_value=[],
    ):
        exit_code = config_doctor.main(["--production", "--root", str(ROOT)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "disabled-migration-validation" in captured.err


def test_config_doctor_uses_git_ls_files_for_tracked_secret_scan():
    with patch("brain.app.cli.config_doctor.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="deploy/production.env\n", returncode=0)

        assert config_doctor._tracked_files(ROOT) == ["deploy/production.env"]

    mock_run.assert_called_once()


def test_systemd_units_use_home_specifier_and_production_env():
    for relative in (
        "ops/illo-api.service",
        "ops/cortex-worker.service",
        "ops/illo-scheduler.service",
        "ops/illo-embed.service",
        "ops/gpu_server/illo-gpu-server.service",
    ):
        content = (ROOT / relative).read_text()
        assert "Environment=ILLO_ENV=production" in content
        assert "Environment=ILLO_VALIDATE_MIGRATIONS=1" in content
        assert "EnvironmentFile=-%h/.config/illo-brain/production.env" in content

    scheduler = (ROOT / "ops" / "illo-scheduler.service").read_text()
    assert "WorkingDirectory=%h/illo-brain" in scheduler
    assert "ExecStart=%h/illo-brain/venv/bin/python3" in scheduler

    gpu_server = (ROOT / "ops" / "gpu_server" / "illo-gpu-server.service").read_text()
    assert "/opt/" + "illo-brain" not in gpu_server
    assert "/home/" + "illo" not in gpu_server
    assert "User=" + "illo" not in gpu_server


def _safe_self_hosted_learning_env() -> dict[str, str]:
    return _safe_prod_env() | {
        "ILLO_DEPLOYMENT_MODE": "self-hosted",
        "LEARNING_POLICY_ALLOWED_MODEL_TIERS": "local,low",
        "EMBEDDING_BACKEND": "cpu",
        "LLM_MODEL": "qwen3.5:4b",
    }


def test_learning_doctor_reports_clear_self_hosted_status_objects():
    report = config_doctor.run_checks(
        root=ROOT,
        env=_safe_self_hosted_learning_env(),
        production=True,
        scan_tracked_secrets=False,
        include_learning_checks=True,
    )

    statuses = _statuses(report)
    assert report.ok
    assert report.findings == ()
    assert statuses["learning-scheduler"].status == "ok"
    assert statuses["learning-budget"].status == "ok"
    assert statuses["learning-privacy"].status == "ok"
    assert statuses["learning-model-tier"].status == "ok"
    assert statuses["learning-embedding"].details["backend"] == "cpu"


def test_learning_doctor_warns_when_embedding_api_has_no_key():
    report = config_doctor.run_checks(
        root=ROOT,
        env=_safe_self_hosted_learning_env() | {
            "EMBEDDING_BACKEND": "api",
            "EMBEDDING_API_KEY": "",
            "GEMINI_API_KEY": "",
        },
        production=True,
        scan_tracked_secrets=False,
        include_learning_checks=True,
    )

    assert report.ok
    assert "learning-embedding-degraded" in _codes(report)
    assert _statuses(report)["learning-embedding-degraded"].status == "warning"


def test_learning_doctor_rejects_invalid_budget_values():
    report = config_doctor.run_checks(
        root=ROOT,
        env=_safe_self_hosted_learning_env() | {
            "LEARNING_BUDGET_NIGHT_TOKENS": "many",
            "LEARNING_BUDGET_AFTER_RUN_SAMPLE_RATE": "2",
        },
        production=True,
        scan_tracked_secrets=False,
        include_learning_checks=True,
    )

    assert not report.ok
    assert "invalid-learning-budget" in _codes(report)
    status = _statuses(report)["invalid-learning-budget"]
    assert "LEARNING_BUDGET_NIGHT_TOKENS" in status.details["invalid_integer_settings"]
    assert "LEARNING_BUDGET_AFTER_RUN_SAMPLE_RATE" in status.details["invalid_rate_settings"]


def test_config_doctor_json_output_includes_learning_status_objects(capsys):
    with patch("brain.app.cli.config_doctor.os.environ", _safe_self_hosted_learning_env()), patch(
        "brain.app.cli.config_doctor._tracked_files",
        return_value=[],
    ):
        exit_code = config_doctor.main([
            "--production",
            "--root",
            str(ROOT),
            "--no-tracked-secret-scan",
            "--json",
        ])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ok"] is True
    assert {status["code"] for status in payload["statuses"]} >= {
        "learning-scheduler",
        "learning-budget",
        "learning-privacy",
        "learning-model-tier",
        "learning-embedding",
    }
