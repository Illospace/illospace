"""Deterministic advisory eval runner for portable skill bundles.

The runner is intentionally DB-free and execution-free. Bundle eval assets can
describe expected routing, instruction rendering, heuristic output checks, or a
named verifier shell, but this module never calls an LLM and never executes
scripts. Failures become result payloads suitable for evidence storage.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from brain.systems.skills.bundles import SkillBundle, SkillBundleAsset, SkillBundleAssetType

try:  # pragma: no cover - exercised when optional dependency is absent.
    import yaml
except Exception:  # pragma: no cover
    yaml = None


SKILL_BUNDLE_EVAL_SCHEMA_VERSION = 1
MAX_EVAL_CASES = 100
MAX_EVIDENCE_TEXT_CHARS = 64_000
MAX_REGEX_CHARS = 500

EvalVerifier = Callable[["SkillBundleEvalCase", "SkillBundleEvalRunContext"], Any]

_SUPPORTED_SUFFIXES = frozenset({".json", ".jsonl", ".toml", ".yaml", ".yml", ".txt", ".md"})
_EVAL_TYPE_ALIASES = {
    "routing": "routing",
    "routing-only": "routing",
    "routing_only": "routing",
    "route": "routing",
    "instruction": "instruction_render",
    "instruction/render": "instruction_render",
    "instruction-render": "instruction_render",
    "instruction_render": "instruction_render",
    "render": "instruction_render",
    "expected-output": "expected_output",
    "expected_output": "expected_output",
    "output": "expected_output",
    "heuristic": "expected_output",
    "verifier": "verifier",
    "verifier-backed": "verifier",
    "verifier_backed": "verifier",
}
_BUILTIN_VERIFIERS = frozenset(
    {
        "contains_tokens",
        "instruction_contains",
        "output_contains",
        "routing_match",
    }
)
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "auth",
        "authorization",
        "context_pack",
        "email",
        "memory",
        "memories",
        "org_id",
        "private_context",
        "secret",
        "tenant",
        "tenant_context",
        "tenant_data",
        "token",
        "user_context",
        "user_data",
        "user_id",
    }
)
_SCRIPT_KEYS = frozenset({"cmd", "command", "exec", "execute", "script", "shell"})
_NETWORK_KEYS = frozenset({"network", "url", "urls", "webhook"})
_FILESYSTEM_KEYS = frozenset(
    {
        "external_file",
        "external_files",
        "file_path",
        "file_paths",
        "filesystem",
        "read_files",
        "write_files",
    }
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


@dataclass(frozen=True, slots=True)
class SkillBundleEvalSafetyPolicy:
    """Hosted-safe defaults for deterministic bundle eval execution."""

    allow_private_eval_evidence: bool = False
    allow_network: bool = False
    allow_filesystem: bool = False
    allow_scripts: bool = False
    allowed_verifiers: tuple[str, ...] = ()
    max_cases: int = MAX_EVAL_CASES


@dataclass(frozen=True, slots=True)
class SkillBundleEvalRunContext:
    """In-memory observations supplied by a caller that is running evals."""

    outputs: Mapping[str, Any] = field(default_factory=dict)
    rendered_instructions: Mapping[str, str] = field(default_factory=dict)
    verifier_registry: Mapping[str, EvalVerifier] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SkillBundleEvalParseDiagnostic:
    """Parse or normalization issue found in an eval asset."""

    source_path: str
    message: str
    severity: str = "error"
    line: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "message": self.message,
            "severity": self.severity,
            "line": self.line,
        }


@dataclass(frozen=True, slots=True)
class SkillBundleEvalCase:
    """One normalized advisory eval case parsed from bundle assets."""

    case_id: str
    eval_type: str
    source_path: str
    raw: Mapping[str, Any]
    input: Mapping[str, Any] = field(default_factory=dict)
    expected: Any = field(default_factory=dict)
    private: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return _sha256_json(
            {
                "case_id": self.case_id,
                "eval_type": self.eval_type,
                "source_path": self.source_path,
                "raw": _json_safe(self.raw),
            }
        )

    def to_payload(
        self,
        *,
        safety_policy: SkillBundleEvalSafetyPolicy | None = None,
    ) -> dict[str, Any]:
        policy = safety_policy or SkillBundleEvalSafetyPolicy()
        allow_private = self.private and policy.allow_private_eval_evidence
        return {
            "case_id": self.case_id,
            "digest": f"sha256:{self.digest}",
            "eval_type": self.eval_type,
            "source_path": self.source_path,
            "private": self.private,
            "input": _redact_payload(self.input, allow_private=allow_private),
            "expected": _redact_payload(self.expected, allow_private=allow_private),
            "metadata": _redact_payload(self.metadata, allow_private=allow_private),
        }


@dataclass(frozen=True, slots=True)
class SkillBundleEvalPlan:
    """Parsed eval cases plus non-fatal diagnostics."""

    cases: tuple[SkillBundleEvalCase, ...]
    diagnostics: tuple[SkillBundleEvalParseDiagnostic, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SKILL_BUNDLE_EVAL_SCHEMA_VERSION,
            "cases": [case.to_payload() for case in self.cases],
            "diagnostics": [diagnostic.to_payload() for diagnostic in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class SkillBundleEvalResult:
    """Advisory result for one eval case or parse diagnostic."""

    case_id: str
    eval_type: str
    source_path: str
    passed: bool
    outcome_label: str
    verifier_status: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    blocked: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "eval_type": self.eval_type,
            "source_path": self.source_path,
            "passed": self.passed,
            "outcome_label": self.outcome_label,
            "verifier_status": self.verifier_status,
            "blocked": self.blocked,
            "errors": list(self.errors),
            "evidence": _json_safe(self.evidence),
        }

    def to_skill_run_evidence_payload(
        self,
        bundle: SkillBundle,
        *,
        namespace: str | None = None,
        org_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Return a payload compatible with SkillRunEvidenceRepository insertion."""
        notes = {
            "schema_version": SKILL_BUNDLE_EVAL_SCHEMA_VERSION,
            "case_id": self.case_id,
            "source_path": self.source_path,
            "errors": list(self.errors),
            "evidence": _truncate_evidence(self.evidence),
        }
        return {
            "skill_name": bundle.manifest.name,
            "skill_effective_digest": bundle.content_digest,
            "skill_id": None,
            "bundle_namespace": namespace,
            "bundle_name": bundle.manifest.name,
            "bundle_version": bundle.manifest.semver,
            "bundle_digest": bundle.content_digest,
            "run_id": None,
            "trace_id": _trace_id(self.case_id, self.source_path),
            "task_class": f"bundle_eval:{self.eval_type}",
            "outcome_label": self.outcome_label,
            "verifier_status": self.verifier_status,
            "user_feedback": None,
            "token_bucket": "none",
            "total_tokens": 0,
            "cost_bucket": "free",
            "cost_usd": 0.0,
            "runtime_bucket": "instant",
            "runtime_ms": 0,
            "tool_risk_class": "none",
            "action_risk_class": "advisory_eval",
            "evidence_source": "skill_bundle_eval",
            "notes": json.dumps(notes, sort_keys=True, separators=(",", ":")),
            "org_id": org_id,
            "user_id": user_id,
        }


@dataclass(frozen=True, slots=True)
class SkillBundleEvalSuiteResult:
    """Deterministic advisory eval suite result for one bundle."""

    bundle_name: str
    bundle_version: str
    bundle_digest: str
    results: tuple[SkillBundleEvalResult, ...]
    diagnostics: tuple[SkillBundleEvalParseDiagnostic, ...] = ()

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def summary(self) -> dict[str, int]:
        passed_count = sum(1 for result in self.results if result.passed)
        blocked_count = sum(1 for result in self.results if result.blocked)
        return {
            "total": len(self.results),
            "passed": passed_count,
            "failed": len(self.results) - passed_count,
            "blocked": blocked_count,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SKILL_BUNDLE_EVAL_SCHEMA_VERSION,
            "advisory_only": True,
            "bundle": {
                "name": self.bundle_name,
                "version": self.bundle_version,
                "digest": self.bundle_digest,
            },
            "passed": self.passed,
            "summary": self.summary,
            "diagnostics": [diagnostic.to_payload() for diagnostic in self.diagnostics],
            "results": [result.to_payload() for result in self.results],
        }

    def to_skill_run_evidence_payloads(
        self,
        bundle: SkillBundle,
        *,
        namespace: str | None = None,
        org_id: str | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            result.to_skill_run_evidence_payload(
                bundle,
                namespace=namespace,
                org_id=org_id,
                user_id=user_id,
            )
            for result in self.results
        ]


def parse_skill_bundle_eval_assets(bundle: SkillBundle) -> SkillBundleEvalPlan:
    """Parse supported ``evals/`` assets without raising on bad eval content."""
    cases: list[SkillBundleEvalCase] = []
    diagnostics: list[SkillBundleEvalParseDiagnostic] = []

    for asset in _eval_assets(bundle):
        suffix = PurePosixPath(asset.path).suffix.lower()
        if suffix not in _SUPPORTED_SUFFIXES:
            diagnostics.append(
                SkillBundleEvalParseDiagnostic(
                    asset.path,
                    f"unsupported eval asset suffix: {suffix or '<none>'}",
                    severity="warning",
                )
            )
            continue
        if asset.content_text is None:
            diagnostics.append(
                SkillBundleEvalParseDiagnostic(
                    asset.path,
                    "eval asset is not inline UTF-8 text and was skipped",
                    severity="warning",
                )
            )
            continue

        parsed_cases, parsed_diagnostics = _parse_eval_asset(asset)
        cases.extend(parsed_cases)
        diagnostics.extend(parsed_diagnostics)

    return SkillBundleEvalPlan(
        cases=tuple(cases),
        diagnostics=tuple(diagnostics),
    )


def run_skill_bundle_evals(
    bundle: SkillBundle,
    *,
    outputs: Mapping[str, Any] | None = None,
    rendered_instructions: Mapping[str, str] | None = None,
    verifier_registry: Mapping[str, EvalVerifier] | None = None,
    safety_policy: SkillBundleEvalSafetyPolicy | None = None,
) -> SkillBundleEvalSuiteResult:
    """Run bundle-provided eval cases in memory and return advisory results."""
    policy = safety_policy or SkillBundleEvalSafetyPolicy()
    context = SkillBundleEvalRunContext(
        outputs=outputs or {},
        rendered_instructions=rendered_instructions or {},
        verifier_registry=verifier_registry or {},
    )
    plan = parse_skill_bundle_eval_assets(bundle)
    results: list[SkillBundleEvalResult] = [
        _diagnostic_result(diagnostic)
        for diagnostic in plan.diagnostics
        if diagnostic.severity == "error"
    ]

    cases = plan.cases[: max(0, policy.max_cases)]
    if len(plan.cases) > len(cases):
        results.append(
            SkillBundleEvalResult(
                case_id="eval-case-limit",
                eval_type="parse",
                source_path="evals/",
                passed=False,
                outcome_label="blocked",
                verifier_status="blocked",
                blocked=True,
                errors=(f"eval case limit exceeded: {len(plan.cases)} > {policy.max_cases}",),
                evidence={"max_cases": policy.max_cases, "case_count": len(plan.cases)},
            )
        )

    for case in cases:
        results.append(_run_eval_case(bundle, case, context, policy))

    return SkillBundleEvalSuiteResult(
        bundle_name=bundle.manifest.name,
        bundle_version=bundle.manifest.semver,
        bundle_digest=bundle.content_digest,
        results=tuple(results),
        diagnostics=plan.diagnostics,
    )


def _eval_assets(bundle: SkillBundle) -> list[SkillBundleAsset]:
    return [
        asset
        for asset in sorted(bundle.assets, key=lambda item: item.path)
        if asset.kind == SkillBundleAssetType.EVAL.value or asset.path.startswith("evals/")
    ]


def _parse_eval_asset(
    asset: SkillBundleAsset,
) -> tuple[list[SkillBundleEvalCase], list[SkillBundleEvalParseDiagnostic]]:
    suffix = PurePosixPath(asset.path).suffix.lower()
    text = asset.content_text or ""
    try:
        if suffix == ".json":
            payload = json.loads(text)
        elif suffix == ".jsonl":
            return _parse_jsonl_eval_asset(asset, text)
        elif suffix == ".toml":
            payload = tomllib.loads(text)
        elif suffix in {".yaml", ".yml"}:
            if yaml is None:
                return [], [
                    SkillBundleEvalParseDiagnostic(
                        asset.path,
                        "YAML eval assets require PyYAML",
                    )
                ]
            payload = yaml.safe_load(text) or {}
        else:
            payload = _text_eval_payload(asset, text)
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        return [], [SkillBundleEvalParseDiagnostic(asset.path, f"invalid eval asset: {exc}")]
    except Exception as exc:
        return [], [
            SkillBundleEvalParseDiagnostic(
                asset.path,
                f"invalid eval asset: {type(exc).__name__}: {exc}",
            )
        ]

    return _cases_from_payload(payload, asset.path)


def _parse_jsonl_eval_asset(
    asset: SkillBundleAsset,
    text: str,
) -> tuple[list[SkillBundleEvalCase], list[SkillBundleEvalParseDiagnostic]]:
    cases: list[SkillBundleEvalCase] = []
    diagnostics: list[SkillBundleEvalParseDiagnostic] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            diagnostics.append(
                SkillBundleEvalParseDiagnostic(
                    asset.path,
                    f"invalid JSONL eval case: {exc}",
                    line=line_number,
                )
            )
            continue
        parsed, parsed_diagnostics = _cases_from_payload(
            payload,
            asset.path,
            base_index=line_number - 1,
        )
        cases.extend(parsed)
        diagnostics.extend(parsed_diagnostics)
    return cases, diagnostics


def _cases_from_payload(
    payload: Any,
    source_path: str,
    *,
    base_index: int = 0,
) -> tuple[list[SkillBundleEvalCase], list[SkillBundleEvalParseDiagnostic]]:
    diagnostics: list[SkillBundleEvalParseDiagnostic] = []
    raw_cases: list[Any]
    if isinstance(payload, Mapping):
        if isinstance(payload.get("cases"), Sequence) and not isinstance(
            payload.get("cases"), (str, bytes)
        ):
            raw_cases = list(payload["cases"])
        elif isinstance(payload.get("evals"), Sequence) and not isinstance(
            payload.get("evals"), (str, bytes)
        ):
            raw_cases = list(payload["evals"])
        else:
            raw_cases = [payload]
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        raw_cases = list(payload)
    else:
        raw_cases = [_text_eval_payload_from_value(source_path, payload)]

    cases: list[SkillBundleEvalCase] = []
    for offset, raw_case in enumerate(raw_cases):
        index = base_index + offset
        if not isinstance(raw_case, Mapping):
            raw_case = _text_eval_payload_from_value(source_path, raw_case)
        normalized = _normalize_eval_case(raw_case, source_path, index)
        if isinstance(normalized, SkillBundleEvalParseDiagnostic):
            diagnostics.append(normalized)
        else:
            cases.append(normalized)
    return cases, diagnostics


def _normalize_eval_case(
    raw_case: Mapping[str, Any],
    source_path: str,
    index: int,
) -> SkillBundleEvalCase | SkillBundleEvalParseDiagnostic:
    raw = _json_safe(raw_case)
    if not isinstance(raw, Mapping):
        return SkillBundleEvalParseDiagnostic(source_path, "eval case must be a mapping")

    case_id = _clean_text(raw.get("id") or raw.get("case_id") or raw.get("name"))
    if not case_id:
        case_id = f"{source_path}#{index}"
    eval_type = _normalize_eval_type(
        raw.get("type") or raw.get("eval_type") or raw.get("kind") or _infer_eval_type(raw)
    )
    if eval_type is None:
        return SkillBundleEvalParseDiagnostic(
            source_path,
            f"unsupported eval type for case {case_id!r}",
        )

    input_payload = _mapping_or_empty(raw.get("input"))
    if "prompt" in raw and "prompt" not in input_payload:
        input_payload = {**input_payload, "prompt": raw.get("prompt")}

    expected = raw.get("expected", raw.get("expected_output", {}))
    private = bool(
        raw.get("private")
        or raw.get("private_eval")
        or raw.get("visibility") == "private"
    )
    metadata = _mapping_or_empty(raw.get("metadata"))

    return SkillBundleEvalCase(
        case_id=str(case_id),
        eval_type=eval_type,
        source_path=source_path,
        raw=raw,
        input=input_payload,
        expected=expected,
        private=private,
        metadata=metadata,
    )


def _run_eval_case(
    bundle: SkillBundle,
    case: SkillBundleEvalCase,
    context: SkillBundleEvalRunContext,
    policy: SkillBundleEvalSafetyPolicy,
) -> SkillBundleEvalResult:
    violations = _safety_violations(case, policy)
    if violations:
        return _blocked_result(case, violations, policy)

    try:
        if case.eval_type == "routing":
            return _run_routing_eval(bundle, case, policy)
        if case.eval_type == "instruction_render":
            return _run_instruction_render_eval(bundle, case, context, policy)
        if case.eval_type == "expected_output":
            return _run_expected_output_eval(case, context, policy)
        if case.eval_type == "verifier":
            return _run_verifier_eval(bundle, case, context, policy)
    except Exception as exc:
        return _failure_result(
            case,
            [f"{type(exc).__name__}: {exc}"],
            observed={"error": f"{type(exc).__name__}: {exc}"},
            policy=policy,
        )

    return _failure_result(case, [f"unsupported eval type: {case.eval_type}"], policy=policy)


def _run_routing_eval(
    bundle: SkillBundle,
    case: SkillBundleEvalCase,
    policy: SkillBundleEvalSafetyPolicy,
) -> SkillBundleEvalResult:
    routing = bundle.manifest.routing
    trigger_texts = [_trigger_text(trigger) for trigger in routing.triggers]
    prompt = _clean_text(case.input.get("prompt")) or _clean_text(case.raw.get("prompt")) or ""
    matched_triggers = _matched_triggers(prompt, trigger_texts, routing.keywords)
    observed = {
        "skill_name": bundle.manifest.name,
        "bundle_name": bundle.manifest.name,
        "description": bundle.manifest.description,
        "triggers": trigger_texts,
        "keywords": list(routing.keywords),
        "embedding_text": routing.embedding_text,
        "should_route": bool(matched_triggers) if prompt else None,
        "matched_triggers": matched_triggers,
    }

    expected = _mapping_or_empty(case.expected)
    errors = _routing_assertion_errors(expected, observed)
    if not expected:
        errors.append("routing eval has no deterministic assertions")
    return _result_from_errors(case, errors, observed=observed, policy=policy)


def _run_instruction_render_eval(
    bundle: SkillBundle,
    case: SkillBundleEvalCase,
    context: SkillBundleEvalRunContext,
    policy: SkillBundleEvalSafetyPolicy,
) -> SkillBundleEvalResult:
    rendered = context.rendered_instructions.get(case.case_id)
    if rendered is None:
        rendered = _clean_text(case.raw.get("rendered") or case.raw.get("rendered_instruction"))
    text = rendered if rendered is not None else bundle.skill_markdown

    asset_paths = _string_list(
        case.raw.get("asset_paths")
        or case.raw.get("assets")
        or case.raw.get("template_path")
        or case.raw.get("asset_path")
    )
    asset_errors: list[str] = []
    if asset_paths:
        assets = {asset.path: asset for asset in bundle.assets}
        chunks = [text]
        for asset_path in asset_paths:
            asset = assets.get(asset_path)
            if asset is None:
                asset_errors.append(f"bundle asset not found: {asset_path}")
                continue
            if asset.content_text is None:
                asset_errors.append(f"bundle asset is not inline text: {asset_path}")
                continue
            chunks.append(asset.content_text)
        text = "\n".join(chunks)

    variables = _mapping_or_empty(case.raw.get("variables") or case.input.get("variables"))
    if variables:
        text = _render_brace_variables(text, variables)

    errors = asset_errors + _text_assertion_errors(case.expected, text)
    if not _has_text_assertions(case.expected):
        errors.append("instruction/render eval has no deterministic assertions")
    observed = {"rendered_text": _preview(text), "asset_paths": asset_paths}
    return _result_from_errors(case, errors, observed=observed, policy=policy)


def _run_expected_output_eval(
    case: SkillBundleEvalCase,
    context: SkillBundleEvalRunContext,
    policy: SkillBundleEvalSafetyPolicy,
) -> SkillBundleEvalResult:
    observed = context.outputs.get(case.case_id)
    if observed is None:
        observed = case.raw.get("observed_output", case.raw.get("actual", case.raw.get("output")))
    if observed is None:
        return SkillBundleEvalResult(
            case_id=case.case_id,
            eval_type=case.eval_type,
            source_path=case.source_path,
            passed=False,
            outcome_label="blocked",
            verifier_status="skipped",
            blocked=True,
            errors=("expected-output eval has no observed output",),
            evidence=_case_evidence(case, {"observed_output": None}, policy),
        )

    errors = _expected_output_assertion_errors(case.expected, observed)
    if not _has_expected_output_assertions(case.expected):
        errors.append("expected-output eval has no deterministic assertions")
    return _result_from_errors(
        case,
        errors,
        observed={"observed_output": observed},
        policy=policy,
    )


def _run_verifier_eval(
    bundle: SkillBundle,
    case: SkillBundleEvalCase,
    context: SkillBundleEvalRunContext,
    policy: SkillBundleEvalSafetyPolicy,
) -> SkillBundleEvalResult:
    verifier = case.raw.get("verifier") or case.raw.get("verifier_name") or case.raw.get("check")
    verifier_spec = verifier if isinstance(verifier, Mapping) else {"name": verifier}
    verifier_name = _clean_text(verifier_spec.get("name") or verifier_spec.get("id"))
    if not verifier_name:
        return _blocked_result(case, ["verifier eval has no named verifier"], policy)
    if policy.allowed_verifiers and verifier_name not in policy.allowed_verifiers:
        return _blocked_result(
            case,
            [f"verifier is not allowed by policy: {verifier_name}"],
            policy,
        )

    if verifier_name in _BUILTIN_VERIFIERS:
        return _run_builtin_verifier(bundle, case, verifier_name, verifier_spec, context, policy)

    registered = context.verifier_registry.get(verifier_name)
    if registered is None:
        return _blocked_result(case, [f"no registered verifier: {verifier_name}"], policy)

    try:
        outcome = registered(case, context)
    except Exception as exc:
        return _failure_result(
            case,
            [f"verifier {verifier_name} raised {type(exc).__name__}: {exc}"],
            observed={"verifier": verifier_name},
            policy=policy,
        )

    passed, observed, errors, status = _coerce_verifier_outcome(verifier_name, outcome)
    return SkillBundleEvalResult(
        case_id=case.case_id,
        eval_type=case.eval_type,
        source_path=case.source_path,
        passed=passed,
        outcome_label="success" if passed else "failure",
        verifier_status=status or ("passed" if passed else "failed"),
        errors=tuple(errors),
        evidence=_case_evidence(case, observed, policy),
    )


def _run_builtin_verifier(
    bundle: SkillBundle,
    case: SkillBundleEvalCase,
    verifier_name: str,
    verifier_spec: Mapping[str, Any],
    context: SkillBundleEvalRunContext,
    policy: SkillBundleEvalSafetyPolicy,
) -> SkillBundleEvalResult:
    args = _mapping_or_empty(verifier_spec.get("args"))
    if verifier_name in {"contains_tokens", "instruction_contains"}:
        text = bundle.skill_markdown
        if case.case_id in context.rendered_instructions:
            text = context.rendered_instructions[case.case_id]
        expected = args or case.expected
        errors = _text_assertion_errors(expected, text)
        return _result_from_errors(
            case,
            errors,
            observed={"verifier": verifier_name, "text": _preview(text)},
            policy=policy,
        )
    if verifier_name == "output_contains":
        observed_output = context.outputs.get(case.case_id, case.raw.get("output"))
        errors = _expected_output_assertion_errors(args or case.expected, observed_output)
        return _result_from_errors(
            case,
            errors,
            observed={"verifier": verifier_name, "observed_output": observed_output},
            policy=policy,
        )
    if verifier_name == "routing_match":
        return _run_routing_eval(bundle, case, policy)
    return _blocked_result(case, [f"unsupported built-in verifier: {verifier_name}"], policy)


def _routing_assertion_errors(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    route_to = expected.get("route_to", expected.get("skill_name", expected.get("bundle_name")))
    if route_to is not None:
        allowed = {item.lower() for item in _string_list(route_to)}
        if str(observed["skill_name"]).lower() not in allowed:
            errors.append(
                f"route_to expected {sorted(allowed)!r}, "
                f"observed {observed['skill_name']!r}"
            )

    if "should_route" in expected and observed.get("should_route") is not None:
        if bool(expected["should_route"]) != bool(observed["should_route"]):
            errors.append(
                f"should_route expected {bool(expected['should_route'])!r}, "
                f"observed {observed['should_route']!r}"
            )
    elif "should_route" in expected:
        errors.append("should_route assertion requires an input prompt")

    errors.extend(
        _contains_errors(
            expected.get("triggers_include"),
            "\n".join(observed.get("triggers") or []),
            label="triggers",
        )
    )
    errors.extend(
        _contains_errors(
            expected.get("keywords_include"),
            "\n".join(observed.get("keywords") or []),
            label="keywords",
        )
    )
    combined = "\n".join(
        str(value or "")
        for value in (
            observed.get("skill_name"),
            observed.get("description"),
            "\n".join(observed.get("triggers") or []),
            "\n".join(observed.get("keywords") or []),
            observed.get("embedding_text"),
        )
    )
    errors.extend(_text_assertion_errors(expected, combined, allow_empty=True))
    return errors


def _text_assertion_errors(expected: Any, text: Any, *, allow_empty: bool = False) -> list[str]:
    if expected is None:
        return []
    text_value = "" if text is None else str(text)
    errors: list[str] = []
    if isinstance(expected, str):
        expected = {"must_include": [expected]}
    if not isinstance(expected, Mapping):
        return [f"expected assertions must be a mapping or string, got {type(expected).__name__}"]

    if not expected and not allow_empty:
        return ["no text assertions provided"]
    case_sensitive = bool(expected.get("case_sensitive", False))
    errors.extend(
        _contains_errors(
            expected.get("must_include", expected.get("contains")),
            text_value,
            label="text",
            case_sensitive=case_sensitive,
        )
    )
    errors.extend(
        _excludes_errors(
            expected.get("must_not_include", expected.get("not_contains")),
            text_value,
            label="text",
            case_sensitive=case_sensitive,
        )
    )
    errors.extend(_section_errors(expected.get("required_sections"), text_value))
    errors.extend(_regex_errors(expected.get("matches", expected.get("regex")), text_value))

    if "equals" in expected and text_value != str(expected["equals"]):
        errors.append("text did not equal expected value")
    if "starts_with" in expected and not text_value.startswith(str(expected["starts_with"])):
        errors.append(f"text did not start with {expected['starts_with']!r}")
    if "ends_with" in expected and not text_value.endswith(str(expected["ends_with"])):
        errors.append(f"text did not end with {expected['ends_with']!r}")
    if "min_length" in expected and len(text_value) < int(expected["min_length"]):
        errors.append(f"text length below min_length {expected['min_length']}")
    if "max_length" in expected and len(text_value) > int(expected["max_length"]):
        errors.append(f"text length above max_length {expected['max_length']}")
    return errors


def _expected_output_assertion_errors(expected: Any, observed: Any) -> list[str]:
    if expected is None:
        return []
    if isinstance(expected, str):
        return _contains_errors([expected], str(observed or ""), label="observed_output")
    if not isinstance(expected, Mapping):
        return [
            "expected-output assertions must be a mapping or string, "
            f"got {type(expected).__name__}"
        ]

    errors = _text_assertion_errors(expected, observed, allow_empty=True)
    if "json_contains" in expected:
        observed_payload, parse_error = _json_payload(observed)
        if parse_error:
            errors.append(parse_error)
        elif not _contains_subset(expected["json_contains"], observed_payload):
            errors.append("observed JSON did not contain expected subset")
    return errors


def _contains_errors(
    expected: Any,
    text: str,
    *,
    label: str,
    case_sensitive: bool = False,
) -> list[str]:
    terms = _string_list(expected)
    if not terms:
        return []
    haystack = text if case_sensitive else text.lower()
    errors: list[str] = []
    for term in terms:
        needle = term if case_sensitive else term.lower()
        if needle not in haystack:
            errors.append(f"{label} missing expected text: {term!r}")
    return errors


def _excludes_errors(
    expected: Any,
    text: str,
    *,
    label: str,
    case_sensitive: bool = False,
) -> list[str]:
    terms = _string_list(expected)
    if not terms:
        return []
    haystack = text if case_sensitive else text.lower()
    errors: list[str] = []
    for term in terms:
        needle = term if case_sensitive else term.lower()
        if needle in haystack:
            errors.append(f"{label} contained forbidden text: {term!r}")
    return errors


def _section_errors(expected: Any, text: str) -> list[str]:
    sections = _string_list(expected)
    if not sections:
        return []
    lower_lines = [line.strip().lower().lstrip("#").strip() for line in text.splitlines()]
    return [
        f"missing required section: {section!r}"
        for section in sections
        if section.lower().strip().lstrip("#").strip() not in lower_lines
    ]


def _regex_errors(expected: Any, text: str) -> list[str]:
    patterns = _string_list(expected)
    errors: list[str] = []
    for pattern in patterns:
        if len(pattern) > MAX_REGEX_CHARS:
            errors.append(f"regex assertion too long: {len(pattern)} > {MAX_REGEX_CHARS}")
            continue
        try:
            if re.search(pattern, text) is None:
                errors.append(f"text did not match regex: {pattern!r}")
        except re.error as exc:
            errors.append(f"invalid regex {pattern!r}: {exc}")
    return errors


def _result_from_errors(
    case: SkillBundleEvalCase,
    errors: Sequence[str],
    *,
    observed: Mapping[str, Any],
    policy: SkillBundleEvalSafetyPolicy,
) -> SkillBundleEvalResult:
    passed = not errors
    return SkillBundleEvalResult(
        case_id=case.case_id,
        eval_type=case.eval_type,
        source_path=case.source_path,
        passed=passed,
        outcome_label="success" if passed else "failure",
        verifier_status="passed" if passed else "failed",
        errors=tuple(errors),
        evidence=_case_evidence(case, observed, policy),
    )


def _failure_result(
    case: SkillBundleEvalCase,
    errors: Sequence[str],
    *,
    observed: Mapping[str, Any] | None = None,
    policy: SkillBundleEvalSafetyPolicy,
) -> SkillBundleEvalResult:
    return SkillBundleEvalResult(
        case_id=case.case_id,
        eval_type=case.eval_type,
        source_path=case.source_path,
        passed=False,
        outcome_label="failure",
        verifier_status="failed",
        errors=tuple(errors),
        evidence=_case_evidence(case, observed or {}, policy),
    )


def _blocked_result(
    case: SkillBundleEvalCase,
    errors: Sequence[str],
    policy: SkillBundleEvalSafetyPolicy,
) -> SkillBundleEvalResult:
    return SkillBundleEvalResult(
        case_id=case.case_id,
        eval_type=case.eval_type,
        source_path=case.source_path,
        passed=False,
        outcome_label="blocked",
        verifier_status="blocked",
        blocked=True,
        errors=tuple(errors),
        evidence=_case_evidence(case, {"blocked": True}, policy),
    )


def _diagnostic_result(diagnostic: SkillBundleEvalParseDiagnostic) -> SkillBundleEvalResult:
    digest = hashlib.sha256(
        f"{diagnostic.source_path}:{diagnostic.line}:{diagnostic.message}".encode("utf-8")
    ).hexdigest()[:12]
    return SkillBundleEvalResult(
        case_id=f"parse-error:{digest}",
        eval_type="parse",
        source_path=diagnostic.source_path,
        passed=False,
        outcome_label="failure",
        verifier_status="failed",
        errors=(diagnostic.message,),
        evidence={"diagnostic": diagnostic.to_payload()},
    )


def _case_evidence(
    case: SkillBundleEvalCase,
    observed: Mapping[str, Any],
    policy: SkillBundleEvalSafetyPolicy,
) -> dict[str, Any]:
    allow_private = case.private and policy.allow_private_eval_evidence
    return {
        "case": case.to_payload(safety_policy=policy),
        "observed": _redact_payload(observed, allow_private=allow_private),
    }


def _safety_violations(
    case: SkillBundleEvalCase,
    policy: SkillBundleEvalSafetyPolicy,
) -> list[str]:
    keys = _deep_keys(case.raw)
    violations: list[str] = []
    if keys & _SCRIPT_KEYS and not policy.allow_scripts:
        violations.append("arbitrary script or shell execution is disabled")
    if keys & _NETWORK_KEYS and not policy.allow_network:
        violations.append("network access is disabled for hosted bundle evals")
    if keys & _FILESYSTEM_KEYS and not policy.allow_filesystem:
        violations.append("filesystem access outside bundle assets is disabled")
    return list(dict.fromkeys(violations))


def _deep_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        keys = {str(key).lower() for key in value}
        for child in value.values():
            keys.update(_deep_keys(child))
        return keys
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        keys: set[str] = set()
        for child in value:
            keys.update(_deep_keys(child))
        return keys
    return set()


def _coerce_verifier_outcome(
    verifier_name: str,
    outcome: Any,
) -> tuple[bool, Mapping[str, Any], list[str], str | None]:
    if isinstance(outcome, bool):
        errors = [] if outcome else ["verifier returned false"]
        return outcome, {"verifier": verifier_name, "outcome": outcome}, errors, None
    if not isinstance(outcome, Mapping):
        return (
            False,
            {"verifier": verifier_name, "outcome": repr(outcome)},
            ["verifier returned an unsupported outcome"],
            None,
        )

    passed = bool(outcome.get("passed", outcome.get("ok", False)))
    errors = [str(error) for error in _string_list(outcome.get("errors"))]
    if not passed and not errors:
        errors = [f"verifier {verifier_name} did not pass"]
    if errors:
        passed = False
    observed = _mapping_or_empty(outcome.get("observed"))
    observed = {"verifier": verifier_name, **observed}
    status = _clean_text(outcome.get("verifier_status") or outcome.get("status"))
    return passed, observed, errors, status


def _json_payload(value: Any) -> tuple[Any, str | None]:
    if isinstance(value, str):
        try:
            return json.loads(value), None
        except json.JSONDecodeError as exc:
            return None, f"observed output is not valid JSON: {exc}"
    return value, None


def _contains_subset(expected: Any, observed: Any) -> bool:
    if isinstance(expected, Mapping):
        if not isinstance(observed, Mapping):
            return False
        for key, expected_value in expected.items():
            if key not in observed or not _contains_subset(expected_value, observed[key]):
                return False
        return True
    if isinstance(expected, Sequence) and not isinstance(expected, (str, bytes)):
        if not isinstance(observed, Sequence) or isinstance(observed, (str, bytes)):
            return False
        return all(
            any(_contains_subset(item, candidate) for candidate in observed)
            for item in expected
        )
    return expected == observed


def _matched_triggers(prompt: str, triggers: Sequence[str], keywords: Sequence[str]) -> list[str]:
    if not prompt:
        return []
    prompt_lower = prompt.lower()
    prompt_words = set(re.findall(r"[a-z0-9]+", prompt_lower))
    matches: list[str] = []
    for trigger in triggers:
        trigger_lower = trigger.lower().strip()
        if not trigger_lower:
            continue
        trigger_words = re.findall(r"[a-z0-9]+", trigger_lower)
        if trigger_lower in prompt_lower or (
            trigger_words and all(word in prompt_words for word in trigger_words)
        ):
            matches.append(trigger)
    for keyword in keywords:
        keyword_lower = str(keyword).lower().strip()
        if keyword_lower and (keyword_lower in prompt_lower or keyword_lower in prompt_words):
            matches.append(str(keyword))
    return list(dict.fromkeys(matches))


def _trigger_text(trigger: Any) -> str:
    if isinstance(trigger, Mapping):
        for key in ("pattern", "text", "trigger", "name"):
            value = _clean_text(trigger.get(key))
            if value:
                return value
        return json.dumps(_json_safe(trigger), sort_keys=True, separators=(",", ":"))
    return str(trigger)


def _text_eval_payload(asset: SkillBundleAsset, text: str) -> dict[str, Any]:
    return _text_eval_payload_from_value(asset.path, text)


def _text_eval_payload_from_value(source_path: str, value: Any) -> dict[str, Any]:
    text = "" if value is None else str(value)
    lines = [
        line.strip().lstrip("-*").strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    return {
        "id": PurePosixPath(source_path).stem or source_path,
        "type": "instruction_render",
        "expected": {"must_include": lines[:20] or [text.strip()] if text.strip() else []},
        "metadata": {"source_format": "text"},
    }


def _infer_eval_type(raw: Mapping[str, Any]) -> str | None:
    if "verifier" in raw or "verifier_name" in raw or "check" in raw:
        return "verifier"
    if "expected_output" in raw or "observed_output" in raw or "output" in raw:
        return "expected_output"
    if "route_to" in raw or "should_route" in raw:
        return "routing"
    expected = raw.get("expected")
    if isinstance(expected, Mapping):
        if any(
            key in expected
            for key in ("route_to", "should_route", "triggers_include", "keywords_include")
        ):
            return "routing"
        if (
            any(
                key in expected
                for key in ("json_contains", "equals", "starts_with", "ends_with")
            )
            and "output" in raw
        ):
            return "expected_output"
    return "instruction_render"


def _normalize_eval_type(value: Any) -> str | None:
    if value is None:
        return None
    return _EVAL_TYPE_ALIASES.get(str(value).strip().lower())


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"))]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _redact_payload(value: Any, *, allow_private: bool) -> Any:
    if allow_private:
        return _json_safe(value)
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if key_text.lower() in _SENSITIVE_KEYS:
                redacted[key_text] = "[redacted]"
            else:
                redacted[key_text] = _redact_payload(child, allow_private=False)
        return redacted
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_redact_payload(item, allow_private=False) for item in value]
    if isinstance(value, str):
        return _EMAIL_RE.sub("[redacted-email]", value)
    return _json_safe(value)


def _render_brace_variables(text: str, variables: Mapping[str, Any]) -> str:
    rendered = text
    for key, value in sorted(variables.items(), key=lambda item: str(item[0])):
        safe_key = re.escape(str(key))
        rendered = re.sub(r"{{\s*" + safe_key + r"\s*}}", str(value), rendered)
    return rendered


def _has_text_assertions(expected: Any) -> bool:
    if isinstance(expected, str):
        return bool(expected)
    if not isinstance(expected, Mapping):
        return False
    return any(
        key in expected
        for key in (
            "contains",
            "ends_with",
            "equals",
            "matches",
            "max_length",
            "min_length",
            "must_include",
            "must_not_include",
            "not_contains",
            "regex",
            "required_sections",
            "starts_with",
        )
    )


def _has_expected_output_assertions(expected: Any) -> bool:
    if _has_text_assertions(expected):
        return True
    return isinstance(expected, Mapping) and "json_contains" in expected


def _preview(value: Any) -> str:
    text = "" if value is None else str(value)
    if len(text) <= 600:
        return text
    return text[:600] + "...[truncated]"


def _truncate_evidence(value: Any) -> Any:
    safe = _json_safe(value)
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"))
    if len(encoded) <= MAX_EVIDENCE_TEXT_CHARS:
        return safe
    return {"truncated": True, "sha256": _sha256_text(encoded), "preview": encoded[:1000]}


def _trace_id(case_id: str, source_path: str) -> str:
    digest = hashlib.sha256(f"{source_path}:{case_id}".encode("utf-8")).hexdigest()[:20]
    return f"skill-bundle-eval:{digest}"


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
