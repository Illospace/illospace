"""Cheap source-freshness checks for repo/project memory candidates.

The evaluator is intentionally deterministic and local-only.  It inspects the
metadata attached to a memory candidate and, when possible, verifies only the
referenced file path, digest, or git commit.  It never walks a repository or
calls a model/network service.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

FreshnessStatus = Literal["fresh", "possibly_stale", "stale", "unknown"]

_SOURCE_FIELD_NAMES = (
    "id",
    "memory_id",
    "source_ref",
    "source_digest",
    "source_kind",
    "subject_ref",
    "subject_type",
    "observed_at",
    "valid_from",
    "valid_until",
    "staleness_score",
)
_NESTED_METADATA_KEYS = ("metadata", "metadata_", "truth_state", "source_metadata")
_SHA256_RE = re.compile(r"(?:sha256[:=])?([a-fA-F0-9]{64})")
_COMMIT_RE = re.compile(
    r"(?:commit|git_commit|git|rev|revision|sha)[:=@/\s]+([a-fA-F0-9]{7,40})"
)
_HEX_COMMIT_RE = re.compile(r"^[a-fA-F0-9]{7,40}$")
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


@dataclass(frozen=True)
class SourceFreshnessResult:
    """Advisory freshness status for one memory candidate."""

    status: FreshnessStatus
    reasons: tuple[str, ...] = ()
    confidence: float | None = None
    score: float | None = None
    checked_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "confidence": self.confidence,
            "score": self.score,
            "checked_refs": list(self.checked_refs),
            "metadata": {key: _jsonable(value) for key, value in self.metadata.items()},
        }


@dataclass(frozen=True)
class _Signal:
    status: FreshnessStatus
    reason: str
    confidence: float
    score: float
    checked_ref: str | None = None


def evaluate_source_freshness(
    candidate: Any,
    *,
    repo_root: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
    reference_digests: Mapping[str, str] | None = None,
    stale_observation_days: int = 90,
    possibly_stale_observation_days: int = 30,
    staleness_stale_threshold: float = 0.85,
    staleness_possible_threshold: float = 0.50,
    max_digest_bytes: int = 5_000_000,
) -> SourceFreshnessResult:
    """Evaluate one memory/candidate against its attached source metadata.

    Args:
        candidate: Memory ORM object, Pydantic model, namespace, or dict-like
            retrieval candidate.
        repo_root: Optional git/worktree root used to resolve relative paths and
            commit refs.  If omitted, relative paths resolve against ``cwd`` and
            git commit checks are skipped.
        now: Optional clock override for deterministic tests.
        reference_digests: Optional caller-supplied digest map.  Keys can be the
            candidate's ``subject_ref`` or ``source_ref``; values are expected
            sha256 digests with or without the ``sha256:`` prefix.
        stale_observation_days: Observations older than this become
            ``possibly_stale`` unless stronger source evidence exists.
        possibly_stale_observation_days: Lower age threshold for weak
            ``possibly_stale`` evidence.
        staleness_stale_threshold: Explicit ``staleness_score`` at or above this
            threshold is treated as stale.
        staleness_possible_threshold: Explicit ``staleness_score`` at or above
            this threshold is treated as possibly stale.
        max_digest_bytes: Safety cap for hashing referenced files.

    Returns:
        SourceFreshnessResult with status, reasons, optional confidence/score,
        checked refs, and normalized metadata.
    """
    clock = _coerce_datetime(now) or datetime.now(timezone.utc)
    repo = _resolve_repo_root(repo_root)
    data = _candidate_metadata(candidate)
    signals: list[_Signal] = []
    checked_refs: list[str] = []

    valid_until = _coerce_datetime(data.get("valid_until"))
    if valid_until is not None:
        checked_refs.append("valid_until")
        if valid_until < clock:
            signals.append(_Signal("stale", "valid_until_expired", 1.0, 0.0, "valid_until"))
        else:
            signals.append(_Signal("fresh", "valid_until_active", 0.45, 0.75, "valid_until"))

    valid_from = _coerce_datetime(data.get("valid_from"))
    if valid_from is not None:
        checked_refs.append("valid_from")
        if valid_from > clock:
            signals.append(_Signal("unknown", "valid_from_in_future", 0.35, 0.45, "valid_from"))
        else:
            signals.append(_Signal("fresh", "valid_from_reached", 0.25, 0.60, "valid_from"))

    staleness_score = _coerce_float(data.get("staleness_score"))
    if staleness_score is not None:
        staleness_score = _clamp(staleness_score)
        checked_refs.append("staleness_score")
        if staleness_score >= staleness_stale_threshold:
            signals.append(
                _Signal("stale", "staleness_score_high", 0.80, 1.0 - staleness_score, "staleness_score")
            )
        elif staleness_score >= staleness_possible_threshold:
            signals.append(
                _Signal(
                    "possibly_stale",
                    "staleness_score_elevated",
                    0.65,
                    1.0 - staleness_score,
                    "staleness_score",
                )
            )
        else:
            signals.append(
                _Signal("fresh", "staleness_score_low", 0.35, 1.0 - staleness_score, "staleness_score")
            )

    observed_at = _coerce_datetime(data.get("observed_at"))
    if observed_at is not None:
        checked_refs.append("observed_at")
        if observed_at > clock:
            signals.append(_Signal("unknown", "observed_at_in_future", 0.35, 0.50, "observed_at"))
        else:
            age_days = max(0.0, (clock - observed_at).total_seconds() / 86_400)
            if age_days >= stale_observation_days:
                signals.append(
                    _Signal(
                        "possibly_stale",
                        "observed_at_older_than_stale_window",
                        0.55,
                        0.35,
                        "observed_at",
                    )
                )
            elif age_days >= possibly_stale_observation_days:
                signals.append(
                    _Signal(
                        "possibly_stale",
                        "observed_at_older_than_fresh_window",
                        0.40,
                        0.50,
                        "observed_at",
                    )
                )
            else:
                signals.append(_Signal("fresh", "observed_at_recent", 0.35, 0.70, "observed_at"))

    subject_ref = _clean_text(data.get("subject_ref"))
    source_ref = _clean_text(data.get("source_ref"))
    source_digest = _clean_text(data.get("source_digest"))

    digest_signal = _evaluate_reference_digest(
        source_digest=source_digest,
        subject_ref=subject_ref,
        source_ref=source_ref,
        reference_digests=reference_digests,
    )
    if digest_signal is not None:
        signals.append(digest_signal)
        if digest_signal.checked_ref:
            checked_refs.append(digest_signal.checked_ref)

    path_signal = _evaluate_subject_path(
        subject_ref=subject_ref,
        source_digest=source_digest,
        repo_root=repo,
        max_digest_bytes=max_digest_bytes,
    )
    if path_signal is not None:
        signals.append(path_signal)
        if path_signal.checked_ref:
            checked_refs.append(path_signal.checked_ref)

    commit_signals = _evaluate_git_source(
        source_ref=source_ref,
        source_kind=_clean_text(data.get("source_kind")),
        subject_ref=subject_ref,
        repo_root=repo,
    )
    for signal in commit_signals:
        signals.append(signal)
        if signal.checked_ref:
            checked_refs.append(signal.checked_ref)

    if not signals:
        missing_fields = [
            name
            for name in (
                "source_ref",
                "source_digest",
                "subject_ref",
                "observed_at",
                "valid_from",
                "valid_until",
                "staleness_score",
            )
            if data.get(name) in (None, "")
        ]
        reason = "no_source_metadata" if len(missing_fields) == 7 else "source_metadata_uncheckable"
        return SourceFreshnessResult(
            status="unknown",
            reasons=(reason,),
            confidence=0.0,
            score=None,
            checked_refs=(),
            metadata=_normalized_metadata(data),
        )

    return _combine_signals(
        signals,
        checked_refs=tuple(_dedupe_preserving_order(checked_refs)),
        metadata=_normalized_metadata(data),
    )


def evaluate_source_freshness_batch(
    candidates: Iterable[Any],
    *,
    top_k: int = 5,
    **kwargs: Any,
) -> list[SourceFreshnessResult]:
    """Evaluate at most the first ``top_k`` candidates in order.

    This keeps the hot path bounded by candidate count and explicit references;
    callers that need wider coverage should opt into a larger ``top_k``.
    """
    if top_k <= 0:
        return []

    results: list[SourceFreshnessResult] = []
    for index, candidate in enumerate(candidates):
        if index >= top_k:
            break
        results.append(evaluate_source_freshness(candidate, **kwargs))
    return results


def annotate_source_freshness(
    candidates: Sequence[Any],
    *,
    top_k: int = 5,
    key: str = "source_freshness",
    **kwargs: Any,
) -> list[Any]:
    """Return candidates with advisory source freshness metadata attached.

    Dict candidates are copied and annotated.  Non-dict candidates are returned
    unchanged after the first ``top_k`` results have been evaluated; this helper
    is intentionally advisory and does not filter or reorder retrieval results.
    """
    evaluations = evaluate_source_freshness_batch(candidates[:top_k], top_k=top_k, **kwargs)
    annotated: list[Any] = []
    for index, candidate in enumerate(candidates):
        if index < len(evaluations) and isinstance(candidate, Mapping):
            item = dict(candidate)
            item[key] = evaluations[index].to_dict()
            annotated.append(item)
        else:
            annotated.append(candidate)
    return annotated


def _candidate_metadata(candidate: Any) -> dict[str, Any]:
    if isinstance(candidate, Mapping):
        data: dict[str, Any] = dict(candidate)
    else:
        data = {}
        model_dump = getattr(candidate, "model_dump", None)
        if callable(model_dump):
            try:
                dumped = model_dump()
                if isinstance(dumped, Mapping):
                    data.update(dumped)
            except TypeError:
                pass
        for field_name in _SOURCE_FIELD_NAMES:
            if field_name in data:
                continue
            try:
                value = getattr(candidate, field_name)
            except Exception:
                continue
            if not callable(value):
                data[field_name] = value

    for nested_key in _NESTED_METADATA_KEYS:
        nested = data.get(nested_key)
        if isinstance(nested, Mapping):
            for field_name in _SOURCE_FIELD_NAMES:
                if field_name not in data and field_name in nested:
                    data[field_name] = nested[field_name]

    return data


def _normalized_metadata(data: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for name in _SOURCE_FIELD_NAMES:
        value = data.get(name)
        if value not in (None, ""):
            metadata[name] = value
    return metadata


def _combine_signals(
    signals: Sequence[_Signal],
    *,
    checked_refs: tuple[str, ...],
    metadata: dict[str, Any],
) -> SourceFreshnessResult:
    stale = [signal for signal in signals if signal.status == "stale" and signal.confidence >= 0.70]
    if stale:
        selected_status: FreshnessStatus = "stale"
        confidence = max(signal.confidence for signal in stale)
        score = min(signal.score for signal in stale)
    else:
        strong_fresh = [
            signal for signal in signals if signal.status == "fresh" and signal.confidence >= 0.70
        ]
        possible = [signal for signal in signals if signal.status == "possibly_stale"]
        if strong_fresh and (
            not possible or max(signal.confidence for signal in strong_fresh) >= max(signal.confidence for signal in possible)
        ):
            selected_status = "fresh"
            confidence = max(signal.confidence for signal in strong_fresh)
            score = max(signal.score for signal in strong_fresh)
        elif possible:
            selected_status = "possibly_stale"
            confidence = max(signal.confidence for signal in possible)
            score = min(signal.score for signal in possible)
        else:
            fresh = [signal for signal in signals if signal.status == "fresh"]
            if fresh:
                selected_status = "fresh"
                confidence = max(signal.confidence for signal in fresh)
                score = max(signal.score for signal in fresh)
            else:
                unknown = [signal for signal in signals if signal.status == "unknown"]
                selected_status = "unknown"
                confidence = max((signal.confidence for signal in unknown), default=0.0)
                score = max((signal.score for signal in unknown), default=0.5)

    reasons = tuple(_dedupe_preserving_order(signal.reason for signal in signals))
    return SourceFreshnessResult(
        status=selected_status,
        reasons=reasons,
        confidence=round(_clamp(confidence), 3),
        score=round(_clamp(score), 3),
        checked_refs=checked_refs,
        metadata=metadata,
    )


def _evaluate_reference_digest(
    *,
    source_digest: str | None,
    subject_ref: str | None,
    source_ref: str | None,
    reference_digests: Mapping[str, str] | None,
) -> _Signal | None:
    expected = _normalize_sha256(source_digest)
    if expected is None or not reference_digests:
        return None

    for key in (subject_ref, source_ref):
        if not key:
            continue
        actual = _normalize_sha256(reference_digests.get(key))
        if actual is None:
            continue
        if actual == expected:
            return _Signal("fresh", "source_digest_matches_reference", 0.95, 0.95, key)
        return _Signal("stale", "source_digest_mismatch", 0.95, 0.05, key)
    return None


def _evaluate_subject_path(
    *,
    subject_ref: str | None,
    source_digest: str | None,
    repo_root: Path | None,
    max_digest_bytes: int,
) -> _Signal | None:
    if not subject_ref:
        return None

    resolved = _resolve_subject_path(subject_ref, repo_root=repo_root)
    if resolved is None:
        return _Signal("unknown", "subject_ref_not_a_local_path", 0.20, 0.50, subject_ref)

    if not resolved.exists():
        return _Signal("stale", "subject_path_missing", 0.85, 0.10, str(resolved))
    if not resolved.is_file():
        return _Signal("unknown", "subject_path_not_file", 0.25, 0.50, str(resolved))

    expected = _normalize_sha256(source_digest)
    if expected is None:
        return _Signal("fresh", "subject_path_exists", 0.35, 0.60, str(resolved))

    actual, reason = _file_sha256(resolved, max_digest_bytes=max_digest_bytes)
    if actual is None:
        return _Signal("unknown", reason or "subject_digest_unavailable", 0.30, 0.50, str(resolved))
    if actual == expected:
        return _Signal("fresh", "source_digest_matches_subject_path", 0.95, 0.95, str(resolved))
    return _Signal("stale", "source_digest_mismatch", 0.95, 0.05, str(resolved))


def _evaluate_git_source(
    *,
    source_ref: str | None,
    source_kind: str | None,
    subject_ref: str | None,
    repo_root: Path | None,
) -> list[_Signal]:
    commit_ref = _extract_commit_ref(source_ref, source_kind=source_kind)
    if not commit_ref:
        return []
    if repo_root is None:
        return [_Signal("unknown", "repo_root_missing_for_commit_check", 0.20, 0.50, commit_ref)]
    if not _git_commit_exists(repo_root, commit_ref):
        return [_Signal("stale", "source_commit_missing", 0.80, 0.10, commit_ref)]

    head = _git_head(repo_root)
    if head and _git_rev_parse(repo_root, commit_ref) == head:
        return [_Signal("fresh", "source_commit_is_head", 0.80, 0.90, commit_ref)]

    path = _resolve_subject_path(subject_ref, repo_root=repo_root) if subject_ref else None
    rel_path = _repo_relative_path(path, repo_root) if path is not None else None
    if rel_path:
        changed = _git_path_changed_since(repo_root, commit_ref, rel_path)
        if changed is True:
            return [_Signal("stale", "subject_path_changed_since_source_commit", 0.90, 0.10, rel_path)]
        if changed is False:
            return [_Signal("fresh", "subject_path_unchanged_since_source_commit", 0.75, 0.85, rel_path)]
        return [_Signal("unknown", "subject_path_commit_diff_unavailable", 0.30, 0.50, rel_path)]

    return [_Signal("possibly_stale", "source_commit_not_head", 0.60, 0.45, commit_ref)]


def _resolve_subject_path(subject_ref: str | None, *, repo_root: Path | None) -> Path | None:
    text = _clean_text(subject_ref)
    if not text:
        return None
    if text.startswith("file://"):
        text = text[len("file://") :]
    elif _URL_RE.match(text):
        return None

    for prefix in ("file:", "path:", "repo_file:", "workspace:"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if text.startswith(("repo:", "project:", "commit:", "git:", "sha256:")):
        return None

    path = Path(text)
    base = repo_root or Path.cwd()
    try:
        resolved = path.expanduser().resolve(strict=False) if path.is_absolute() else (base / path).resolve(strict=False)
    except OSError:
        return None

    if repo_root is not None and not _is_relative_to(resolved, repo_root):
        return None
    return resolved


def _resolve_repo_root(repo_root: str | os.PathLike[str] | None) -> Path | None:
    if repo_root is None:
        return None
    try:
        return Path(repo_root).expanduser().resolve(strict=False)
    except OSError:
        return None


def _repo_relative_path(path: Path | None, repo_root: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve(strict=False).relative_to(repo_root).as_posix()
    except ValueError:
        return None


def _file_sha256(path: Path, *, max_digest_bytes: int) -> tuple[str | None, str | None]:
    try:
        stat = path.stat()
    except OSError:
        return None, "subject_stat_failed"
    if stat.st_size > max_digest_bytes:
        return None, "subject_file_too_large_for_digest"

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 128), b""):
                digest.update(chunk)
    except OSError:
        return None, "subject_digest_read_failed"
    return digest.hexdigest(), None


def _extract_commit_ref(source_ref: str | None, *, source_kind: str | None) -> str | None:
    text = _clean_text(source_ref)
    if not text:
        return None
    match = _COMMIT_RE.search(text)
    if match:
        return match.group(1)
    if source_kind and "git" in source_kind.lower() and _HEX_COMMIT_RE.fullmatch(text):
        return text
    return None


def _git_commit_exists(repo_root: Path, commit_ref: str) -> bool:
    return _run_git(repo_root, ["cat-file", "-e", f"{commit_ref}^{{commit}}"]).returncode == 0


def _git_head(repo_root: Path) -> str | None:
    result = _run_git(repo_root, ["rev-parse", "--verify", "HEAD"])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_rev_parse(repo_root: Path, ref: str) -> str | None:
    result = _run_git(repo_root, ["rev-parse", "--verify", ref])
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_path_changed_since(repo_root: Path, commit_ref: str, rel_path: str) -> bool | None:
    result = _run_git(repo_root, ["diff", "--quiet", f"{commit_ref}..HEAD", "--", rel_path])
    if result.returncode == 0:
        return False
    if result.returncode == 1:
        return True
    return None


def _run_git(repo_root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args=list(args), returncode=127, stdout="", stderr="")


def _normalize_sha256(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    match = _SHA256_RE.search(text)
    return match.group(1).lower() if match else None


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _dedupe_preserving_order(values: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value
