"""Deterministic backend eval runner."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from brain.jobs.evals.scenarios import EvalScenario, list_default_scenarios

EvalBackend = Callable[[EvalScenario], dict[str, Any]]


@dataclass(frozen=True)
class EvalCaseResult:
    scenario_id: str
    kind: str
    passed: bool
    expected: dict[str, Any]
    observed: dict[str, Any]
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "kind": self.kind,
            "passed": self.passed,
            "expected": dict(self.expected),
            "observed": dict(self.observed),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class EvalSuiteResult:
    suite: str
    generated_at: str
    live_provider: bool
    results: tuple[EvalCaseResult, ...]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    def to_dict(self) -> dict[str, Any]:
        passed_count = sum(1 for result in self.results if result.passed)
        return {
            "suite": self.suite,
            "generated_at": self.generated_at,
            "live_provider": self.live_provider,
            "passed": self.passed,
            "summary": {
                "total": len(self.results),
                "passed": passed_count,
                "failed": len(self.results) - passed_count,
            },
            "results": [result.to_dict() for result in self.results],
        }


def mocked_backend(scenario: EvalScenario) -> dict[str, Any]:
    """Return deterministic observations that exercise product contracts."""
    expected = dict(scenario.expected)
    observed = {key: value for key, value in expected.items()}
    observed.update(
        {
            "scenario_id": scenario.scenario_id,
            "kind": scenario.kind,
            "fault": scenario.fault,
            "evidence": [f"mock:{scenario.scenario_id}"],
        }
    )
    if scenario.kind == "chaos":
        observed.setdefault("degraded", True)
        observed.setdefault("recovered_without_live_provider", True)
    return observed


def _compare(expected: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key, expected_value in expected.items():
        observed_value = observed.get(key)
        if key == "must_include":
            text = " ".join(str(value).lower() for value in observed.values())
            missing = [token for token in expected_value if str(token).lower() not in text]
            if missing:
                errors.append(f"missing expected token(s): {', '.join(missing)}")
            continue
        if observed_value != expected_value:
            errors.append(f"{key}: expected {expected_value!r}, observed {observed_value!r}")
    return errors


def run_backend_eval_suite(
    *,
    scenarios: Iterable[EvalScenario] | None = None,
    backend: EvalBackend | None = None,
    live_provider: bool = False,
    suite: str = "backend-golden-chaos-v1",
) -> EvalSuiteResult:
    """Run backend eval scenarios and return machine-readable results."""
    if live_provider and backend is None:
        raise ValueError("live_provider=True requires an explicit backend runner")
    backend = backend or mocked_backend
    results = []
    for scenario in scenarios or list_default_scenarios():
        try:
            observed = backend(scenario)
            errors = _compare(scenario.expected, observed)
        except Exception as exc:
            observed = {"error": f"{type(exc).__name__}: {exc}"}
            errors = [observed["error"]]
        results.append(
            EvalCaseResult(
                scenario_id=scenario.scenario_id,
                kind=scenario.kind,
                passed=not errors,
                expected=scenario.expected,
                observed=observed,
                errors=errors,
            )
        )
    return EvalSuiteResult(
        suite=suite,
        generated_at=datetime.now(timezone.utc).isoformat(),
        live_provider=live_provider,
        results=tuple(results),
    )
