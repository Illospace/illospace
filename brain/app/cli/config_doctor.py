"""Production configuration doctor for deploy/restart scout checks."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from brain.systems.learning.budget import BudgetLane, LearningBudgetPolicy
from brain.systems.learning.policy import build_learning_policy


_FALSE_VALUES = {"0", "false", "no", "off", "disabled", ""}
_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_BOOL_VALUES = _FALSE_VALUES | _TRUE_VALUES
_ENV_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL)[A-Z0-9_]*)\s*=\s*(?P<value>.*)\s*$"
)
_SECRET_CONFIG_PATH_RE = re.compile(
    r"(^|/)(?:\.env(?:\.|$)|env\.(?:conf|sh|env)$|[^/]*\.(?:env|conf)$)"
)
_EXAMPLE_CONFIG_PATH_RE = re.compile(r"(^|/)[^/]*(?:example|sample|template)[^/]*$")
_ENV_FILES = (
    ".env",
    "brain/.env",
    "core/.env",
)
_LEARNING_INT_ENV = (
    "LEARNING_BUDGET_HOT_PATH_TOKENS",
    "LEARNING_BUDGET_AFTER_RUN_TOKENS",
    "LEARNING_BUDGET_NIGHT_TOKENS",
    "LEARNING_BUDGET_TENANT_DAILY_TOKENS",
    "LEARNING_BUDGET_HOT_PATH_MAX_ELAPSED_MS",
    "LEARNING_BUDGET_NIGHT_MIN_PRIORITY",
    "LEARNING_POLICY_NIGHT_BUDGET_UNITS",
    "LEARNING_POLICY_TENANT_DAILY_BUDGET_UNITS",
    "EMBEDDING_DIM",
)
_LEARNING_RATE_ENV = (
    "LEARNING_BUDGET_AFTER_RUN_SAMPLE_RATE",
    "LEARNING_POLICY_AFTER_RUN_SAMPLE_RATE",
)
_LEARNING_BOOL_ENV = (
    "LEARNING_BUDGET_ENABLED",
    "LEARNING_BUDGET_ALLOW_REMOTE",
    "LEARNING_BUDGET_ALLOW_HOT_PATH_GENERATION",
    "LEARNING_POLICY_ENABLED",
    "LEARNING_ENABLED",
    "LEARNING_POLICY_EXTERNAL_EVAL_EXPORT_ALLOWED",
)
_LOCAL_MODEL_ENV = (
    "LLM_MODEL",
    "LLM_MODEL_PATH",
    "LOCAL_LLM_MODEL",
    "OLLAMA_MODEL",
    "OLLAMA_URL",
    "GPU_LLM_FALLBACK_MODEL",
    "LLM_FALLBACK_MODEL",
)


@dataclass(frozen=True)
class DoctorFinding:
    """A config doctor finding that never includes secret values."""

    code: str
    message: str
    severity: str = "error"


@dataclass(frozen=True)
class DoctorStatus:
    """Structured status for one checked surface."""

    code: str
    status: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class DoctorReport:
    findings: tuple[DoctorFinding, ...]
    statuses: tuple[DoctorStatus, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings) and not any(
            status.status == "error" for status in self.statuses
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "statuses": [status.to_payload() for status in self.statuses],
            "findings": [
                {
                    "code": finding.code,
                    "severity": finding.severity,
                    "message": finding.message,
                }
                for finding in self.findings
            ],
        }


def _strip_env_value(value: str) -> str:
    value = value.strip()
    if value and value[0] in {"'", '"'} and value[-1:] == value[0]:
        return value[1:-1]
    return value


def _load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return env
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key and re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            env[key] = _strip_env_value(value)
    return env


def _effective_env(root: Path, base_env: Mapping[str, str]) -> dict[str, str]:
    loaded: dict[str, str] = {}
    for relative in _ENV_FILES:
        loaded.update(_load_env_file(root / relative))
    loaded.update(base_env)
    return loaded


def _env_flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name)
    if value is None:
        return default
    return value.strip().lower() not in _FALSE_VALUES


def _is_disabled(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name)
    if value is None:
        return False
    return value.strip().lower() in _FALSE_VALUES


def _has_database_config(env: Mapping[str, str]) -> bool:
    if any(env.get(name) for name in ("DATABASE_URL", "DB_URL", "BRAIN_DB_URL")):
        return True
    explicit_parts = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
    return all(env.get(name) for name in explicit_parts)


def _looks_like_learning_repo(root: Path) -> bool:
    return (root / "brain" / "systems" / "learning").is_dir()


def _first_env(env: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = env.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _env_int_is_valid(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return True
    try:
        return int(str(raw).strip()) >= 0
    except (TypeError, ValueError):
        return False


def _env_rate_is_valid(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return True
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return False
    return 0.0 <= value <= 1.0


def _env_bool_is_valid(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() in _BOOL_VALUES


def _status_finding(status: DoctorStatus) -> DoctorFinding | None:
    if status.status not in {"warning", "error"}:
        return None
    return DoctorFinding(
        code=status.code,
        message=status.message,
        severity=status.status,
    )


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _scheduler_status(root: Path) -> DoctorStatus:
    required_files = (
        "brain/app/scheduler/daemon.py",
        "brain/app/cli/scheduler.py",
        "brain/app/scheduler/catalog.py",
        "brain/app/scheduler/programs.py",
    )
    missing = [relative for relative in required_files if not (root / relative).exists()]
    service_path = root / "ops" / "illo-scheduler.service"
    service_text = _read_text(service_path)
    catalog_text = _read_text(root / "brain" / "app" / "scheduler" / "catalog.py")
    missing_catalog_bits = [
        bit
        for bit in ("nightly_sleep", "scheduler_split_steps", "brain.systems.learning.night_budget")
        if bit not in catalog_text
    ]
    service_ready = (
        service_path.exists()
        and "brain.app.cli.scheduler daemon" in service_text
        and "ILLO_ENV=production" in service_text
    )

    if missing or missing_catalog_bits:
        return DoctorStatus(
            code="learning-scheduler-missing",
            status="error",
            message=(
                "Learning/night mode needs the scheduler daemon, CLI, and nightly catalog. "
                "One or more required pieces are missing."
            ),
            details={
                "missing_files": missing,
                "missing_catalog_entries": missing_catalog_bits,
                "service_ready": service_ready,
            },
        )
    if not service_ready:
        return DoctorStatus(
            code="learning-scheduler-service",
            status="warning",
            message=(
                "Scheduler code is present, but the self-hosted systemd service is not fully configured. "
                "Night runs can still be started manually with the scheduler CLI."
            ),
            details={"service_path": "ops/illo-scheduler.service", "service_ready": service_ready},
        )

    return DoctorStatus(
        code="learning-scheduler",
        status="ok",
        message="Scheduler daemon, self-hosted service, and nightly catalog are available.",
        details={
            "service_path": "ops/illo-scheduler.service",
            "night_job": "nightly_sleep",
            "after_run_lane": "learning queue policy",
        },
    )


def _learning_budget_status(env: Mapping[str, str]) -> DoctorStatus:
    invalid_int = [name for name in _LEARNING_INT_ENV if not _env_int_is_valid(env, name)]
    invalid_rates = [name for name in _LEARNING_RATE_ENV if not _env_rate_is_valid(env, name)]
    invalid_bools = [name for name in _LEARNING_BOOL_ENV if not _env_bool_is_valid(env, name)]
    if invalid_int or invalid_rates or invalid_bools:
        parts = []
        if invalid_int:
            parts.append(f"non-negative integer expected: {', '.join(invalid_int)}")
        if invalid_rates:
            parts.append(f"0.0-1.0 expected: {', '.join(invalid_rates)}")
        if invalid_bools:
            parts.append(f"boolean expected: {', '.join(invalid_bools)}")
        return DoctorStatus(
            code="invalid-learning-budget",
            status="error",
            message="Learning budget settings are invalid; " + "; ".join(parts) + ".",
            details={
                "invalid_integer_settings": invalid_int,
                "invalid_rate_settings": invalid_rates,
                "invalid_boolean_settings": invalid_bools,
            },
        )

    budget = LearningBudgetPolicy.from_env(env)
    policy = build_learning_policy(env=env)
    hot_path = budget.limit_for(BudgetLane.HOT_PATH)
    after_run = budget.limit_for(BudgetLane.AFTER_RUN)
    night = budget.limit_for(BudgetLane.NIGHT)
    tenant_daily = budget.limit_for(BudgetLane.TENANT_DAILY)

    warnings: list[str] = []
    if not budget.enabled or not policy.enabled:
        warnings.append("learning is disabled")
    if budget.allow_hot_path_generation:
        warnings.append("hot path generation is enabled")
    if hot_path > 0 and budget.hot_path_max_elapsed_ms <= 0:
        warnings.append("hot path latency budget is zero")
    if after_run <= 0 or policy.after_run_sample_rate <= 0:
        warnings.append("after-run learning is disabled")
    if night <= 0 or policy.night_budget_units <= 0:
        warnings.append("night learning has no budget")
    if tenant_daily <= 0 or policy.tenant_daily_budget_units <= 0:
        warnings.append("tenant daily learning budget is zero")
    if tenant_daily and (after_run > tenant_daily or night > tenant_daily):
        warnings.append("lane budget is larger than the tenant daily cap")

    details = {
        "budget": budget.to_payload(),
        "policy": policy.to_payload(),
        "warnings": warnings,
    }
    if warnings:
        return DoctorStatus(
            code="learning-budget-warning",
            status="warning",
            message="Learning budgets are readable, but review: " + "; ".join(warnings) + ".",
            details=details,
        )

    return DoctorStatus(
        code="learning-budget",
        status="ok",
        message="Learning budget settings are valid for hot path, after-run, night, and daily caps.",
        details=details,
    )


def _privacy_status(env: Mapping[str, str]) -> DoctorStatus:
    policy = build_learning_policy(env=env)
    payload = policy.to_payload()
    risk_flags = list(policy.risk_flags)
    redaction_mode = str(policy.private_data_redaction_mode)
    export_allowed = bool(policy.external_eval_export_allowed)

    if "external_eval_export_without_redaction" in risk_flags:
        return DoctorStatus(
            code="learning-privacy-unsafe",
            status="error",
            message=(
                "External learning/eval export is allowed while private-data redaction is disabled. "
                "Enable redaction or keep external export off."
            ),
            details={"policy": payload, "risk_flags": risk_flags},
        )
    if export_allowed or redaction_mode == "disabled" or risk_flags:
        return DoctorStatus(
            code="learning-privacy-review",
            status="warning",
            message=(
                "Learning privacy settings need review before sharing eval exports outside this install."
            ),
            details={"policy": payload, "risk_flags": risk_flags},
        )

    return DoctorStatus(
        code="learning-privacy",
        status="ok",
        message="Learning export is off by default and redaction is safe for this deployment mode.",
        details={"policy": payload},
    )


def _learning_model_status(env: Mapping[str, str]) -> DoctorStatus:
    policy = build_learning_policy(env=env)
    allowed_classes = tuple(str(model_class) for model_class in policy.allowed_model_classes)
    has_local_or_economy = bool({"local", "economy"} & set(allowed_classes))
    has_local_model_setting = any(_first_env(env, name) for name in _LOCAL_MODEL_ENV)
    deployment_mode = str(policy.deployment_mode)

    details = {
        "deployment_mode": deployment_mode,
        "allowed_model_classes": list(allowed_classes),
        "has_local_model_setting": has_local_model_setting,
        "local_model_env_names": list(_LOCAL_MODEL_ENV),
    }
    if not has_local_or_economy:
        return DoctorStatus(
            code="learning-model-class-review",
            status="warning",
            message=(
                "Learning has no local or economy model class available; background learning may cost more than expected."
            ),
            details=details,
        )
    if deployment_mode == "self_hosted" and "local" in allowed_classes and not has_local_model_setting:
        return DoctorStatus(
            code="learning-local-model-missing",
            status="warning",
            message=(
                "Self-hosted learning allows the local model class, but no local model setting was found. "
                "Configure a local model or rely on the economy class."
            ),
            details=details,
        )

    return DoctorStatus(
        code="learning-model-class",
        status="ok",
        message="Learning can use a local or economy model class.",
        details=details,
    )


def _embedding_status(root: Path, env: Mapping[str, str]) -> DoctorStatus:
    backend = str(_first_env(env, "EMBEDDING_BACKEND") or "api").strip().lower()
    if backend not in {"api", "cpu", "gpu"}:
        return DoctorStatus(
            code="learning-embedding-backend",
            status="error",
            message="Embedding backend must be api, cpu, or gpu.",
            details={"backend": backend},
        )

    raw_dim = _first_env(env, "EMBEDDING_DIM")
    if raw_dim is not None and not _env_int_is_valid(env, "EMBEDDING_DIM"):
        return DoctorStatus(
            code="learning-embedding-dim",
            status="error",
            message="EMBEDDING_DIM must be a non-negative integer.",
            details={"backend": backend},
        )

    service_available = (root / "ops" / "illo-embed.service").exists() or (
        root / "ops" / "gpu_server" / "illo-gpu-server.service"
    ).exists()
    details: dict[str, Any] = {
        "backend": backend,
        "service_available": service_available,
        "degraded_fallback": False,
    }

    if backend == "api":
        api_key_configured = bool(_first_env(env, "EMBEDDING_API_KEY", "GEMINI_API_KEY"))
        details["api_key_configured"] = api_key_configured
        details["provider"] = _first_env(env, "EMBEDDING_API_PROVIDER") or "gemini"
        if not api_key_configured:
            details["degraded_fallback"] = True
            return DoctorStatus(
                code="learning-embedding-degraded",
                status="warning",
                message=(
                    "Embedding backend is set to API, but no embedding API key is configured. "
                    "Memory encoding will be degraded until you configure an API key or switch to cpu/gpu."
                ),
                details=details,
            )
        return DoctorStatus(
            code="learning-embedding",
            status="ok",
            message="Embedding API backend is configured.",
            details=details,
        )

    if backend == "gpu":
        model_configured = bool(_first_env(env, "EMBEDDING_MODEL", "EMBEDDING_MODEL_PATH"))
        details["model_configured"] = model_configured
        details["gpu_server_url"] = _first_env(env, "GPU_SERVER_URL") or "http://127.0.0.1:9800"
        if not service_available or not model_configured:
            missing = []
            if not service_available:
                missing.append("embedding service file")
            if not model_configured:
                missing.append("embedding model")
            return DoctorStatus(
                code="learning-embedding-gpu-review",
                status="warning",
                message=(
                    "GPU embedding is selected, but review the missing setup: "
                    + ", ".join(missing)
                    + "."
                ),
                details=details,
            )
        return DoctorStatus(
            code="learning-embedding",
            status="ok",
            message="Local GPU embedding service is configured.",
            details=details,
        )

    details["cpu_model"] = _first_env(env, "EMBEDDING_CPU_MODEL") or "all-MiniLM-L6-v2"
    details["degraded_fallback"] = True
    return DoctorStatus(
        code="learning-embedding",
        status="ok",
        message="CPU embedding fallback is configured; it is private but slower/lower-capacity than GPU or API.",
        details=details,
    )


def _learning_statuses(root: Path, env: Mapping[str, str]) -> tuple[DoctorStatus, ...]:
    return (
        _scheduler_status(root),
        _learning_budget_status(env),
        _privacy_status(env),
        _learning_model_status(env),
        _embedding_status(root, env),
    )


def _is_placeholder_secret(value: str) -> bool:
    stripped = _strip_env_value(value).strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    if lowered.startswith("$") or lowered.startswith("${"):
        return True
    return any(
        marker in lowered
        for marker in (
            "...",
            "changeme",
            "change-me",
            "example",
            "placeholder",
            "dummy",
            "fake",
            "test-token",
            "your_",
            "your-",
        )
    )


def _tracked_files(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _scan_tracked_secret_config(root: Path) -> list[DoctorFinding]:
    findings: list[DoctorFinding] = []
    for relative in _tracked_files(root):
        if _EXAMPLE_CONFIG_PATH_RE.search(relative):
            continue
        if not _SECRET_CONFIG_PATH_RE.search(relative):
            continue
        path = root / relative
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(lines, start=1):
            match = _ENV_ASSIGNMENT_RE.match(line)
            if not match:
                continue
            key = match.group("key")
            value = match.group("value")
            if _is_placeholder_secret(value):
                continue
            findings.append(
                DoctorFinding(
                    code="tracked-secret-like-config",
                    message=(
                        f"{relative}:{line_no} contains tracked secret-like key "
                        f"{key}; move it to an untracked env/secret store and rotate it"
                    ),
                )
            )
    return findings


def run_checks(
    *,
    root: Path,
    env: Mapping[str, str] | None = None,
    production: bool = False,
    scan_tracked_secrets: bool = True,
    include_learning_checks: bool | None = None,
) -> DoctorReport:
    """Run production-safety config checks without exposing secret values."""

    root = root.resolve()
    effective = _effective_env(root, os.environ if env is None else env)
    is_production = production or effective.get("ILLO_ENV", "development") == "production"
    findings: list[DoctorFinding] = []
    statuses: list[DoctorStatus] = []

    if is_production:
        if _env_flag(effective, "ILLO_ALLOW_LOCAL_CODEX_AUTH_FALLBACK", default=False):
            findings.append(
                DoctorFinding(
                    code="unsafe-dev-auth-fallback",
                    message="ILLO_ALLOW_LOCAL_CODEX_AUTH_FALLBACK must be disabled in production",
                )
            )
        if _env_flag(effective, "AUTH_DEV_FALLBACK_ENABLED", default=False):
            findings.append(
                DoctorFinding(
                    code="unsafe-dev-auth-fallback",
                    message="AUTH_DEV_FALLBACK_ENABLED must be disabled in production",
                )
            )
        if not (effective.get("SECRET_KEY") or effective.get("FLASK_SECRET_KEY")):
            findings.append(
                DoctorFinding(
                    code="missing-secret-key",
                    message="production requires SECRET_KEY or FLASK_SECRET_KEY",
                )
            )
        if not effective.get("VAULT_MASTER_KEY"):
            findings.append(
                DoctorFinding(
                    code="missing-vault-master-key",
                    message="production requires VAULT_MASTER_KEY for encrypted Vault storage",
                )
            )
        if not _has_database_config(effective):
            findings.append(
                DoctorFinding(
                    code="missing-db-url",
                    message="production requires DATABASE_URL/DB_URL or explicit DB_* connection settings",
                )
            )
        disabled_migration_flags = [
            name
            for name in (
                "ILLO_VALIDATE_MIGRATIONS",
                "VALIDATE_MIGRATIONS",
            )
            if _is_disabled(effective, name)
        ]
        skipped_migration_flags = [
            name
            for name in (
                "ILLO_SKIP_MIGRATION_VALIDATION",
                "ILLO_DISABLE_MIGRATION_VALIDATION",
                "DISABLE_MIGRATION_VALIDATION",
                "SKIP_MIGRATION_VALIDATION",
            )
            if _env_flag(effective, name, default=False)
        ]
        if disabled_migration_flags or skipped_migration_flags:
            names = ", ".join(disabled_migration_flags + skipped_migration_flags)
            findings.append(
                DoctorFinding(
                    code="disabled-migration-validation",
                    message=f"migration validation must stay enabled in production ({names})",
                )
            )

    if scan_tracked_secrets:
        findings.extend(_scan_tracked_secret_config(root))

    if include_learning_checks is None:
        include_learning_checks = _looks_like_learning_repo(root)
    if include_learning_checks:
        statuses.extend(_learning_statuses(root, effective))
        findings.extend(
            finding
            for status in statuses
            if (finding := _status_finding(status)) is not None
        )

    return DoctorReport(tuple(findings), tuple(statuses))


def _format_report(report: DoctorReport) -> str:
    lines: list[str]
    if report.ok and not report.findings:
        lines = ["Config doctor passed."]
    elif report.ok:
        lines = ["Config doctor passed with warnings:"]
    else:
        lines = ["Config doctor found unsafe configuration:"]
    if report.statuses:
        lines.append("Statuses:")
        for status in report.statuses:
            lines.append(f"- [{status.status}] {status.code}: {status.message}")
    if report.findings:
        lines.append("Findings:")
    for finding in report.findings:
        lines.append(f"- [{finding.severity}] {finding.code}: {finding.message}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m brain.app.cli.config_doctor",
        description="Validate production config before deploy/restart.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root to inspect (default: current directory).",
    )
    parser.add_argument(
        "--production",
        action="store_true",
        help="Force production checks even when ILLO_ENV is not set to production.",
    )
    parser.add_argument(
        "--no-tracked-secret-scan",
        action="store_true",
        help="Skip scanning tracked config files for secret-like assignments.",
    )
    parser.add_argument(
        "--no-learning-checks",
        action="store_true",
        help="Skip learning/night-mode config checks.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable status objects.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_checks(
        root=args.root,
        production=args.production,
        scan_tracked_secrets=not args.no_tracked_secret_scan,
        include_learning_checks=False if args.no_learning_checks else None,
    )
    output = json.dumps(report.to_payload(), indent=2, sort_keys=True) if args.json else _format_report(report)
    print(output, file=sys.stdout if report.ok else sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
