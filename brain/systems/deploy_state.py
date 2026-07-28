"""Read-time deploy state derived from GitHub commit ancestry."""

from __future__ import annotations

import asyncio
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

from brain.systems.deploy_state_github import (
    AncestryObservation,
    ancestry_failure,
    observe_ancestry,
)


class DeployState(StrEnum):
    """A determinate observation of a fix merge commit."""

    UNMERGED = "unmerged"
    STAGING = "staging"
    DEPLOYED = "deployed"


@dataclass(frozen=True, slots=True)
class DeployStateObservation:
    """The branch-containment facts behind one derived state."""

    state: DeployState | None
    in_staging: bool | None
    in_main: bool | None
    comparisons: tuple[AncestryObservation, ...] = ()

    @property
    def display_state(self) -> str:
        return render_deploy_state(self.state)

    @property
    def failures(self) -> tuple[AncestryObservation, ...]:
        return tuple(item for item in self.comparisons if item.failed)

    def failure_payloads(self) -> list[dict[str, str | int]]:
        payloads: list[dict[str, str | int]] = []
        for failure in self.failures:
            payload: dict[str, str | int] = {
                "branch": failure.branch,
                "category": str(failure.error_category),
            }
            if failure.status_code is not None:
                payload["status_code"] = failure.status_code
            if failure.status is not None:
                payload["status"] = failure.status
            payloads.append(payload)
        return payloads

    def branch_ancestry_result(self, branch: str) -> str:
        """Render the latest comparison for one branch without re-checking it."""

        comparison = next(
            (
                item
                for item in reversed(self.comparisons)
                if item.branch == str(branch or "").strip()
            ),
            None,
        )
        if comparison is None:
            return "unknown"
        status = str(
            comparison.status
            or comparison.error_category
            or "unknown"
        )
        if comparison.is_ancestor is True:
            return f"{status} (contained)"
        if comparison.is_ancestor is False:
            return f"{status} (not contained)"
        return f"{status} (indeterminate)"


_Key = TypeVar("_Key", bound=Hashable)


class DeployStateBatch(dict[_Key, DeployState | None], Generic[_Key]):
    """State mapping with request-scoped observations for unique fix refs."""

    def __init__(
        self,
        states: Mapping[_Key, DeployState | None],
        *,
        observations_by_key: Mapping[_Key, DeployStateObservation],
        observations_by_ref: Mapping[tuple[str, str], DeployStateObservation],
    ) -> None:
        super().__init__(states)
        self.observations_by_key = dict(observations_by_key)
        self.observations_by_ref = dict(observations_by_ref)

    @property
    def unavailable_refs(
        self,
    ) -> dict[tuple[str, str], DeployStateObservation]:
        return {
            ref: observation
            for ref, observation in self.observations_by_ref.items()
            if observation.state is None and observation.failures
        }


def render_deploy_state(state: DeployState | None) -> str:
    """Render indeterminate GitHub reads honestly."""
    return state.value if state is not None else "unknown"


def _state_from_ancestry(
    *,
    in_staging: bool | None,
    in_main: bool | None,
) -> DeployState | None:
    if in_main is True:
        return DeployState.DEPLOYED
    if in_main is None:
        return None
    if in_staging is True:
        return DeployState.STAGING
    if in_staging is False:
        return DeployState.UNMERGED
    return None


def _ordered_tokens(tokens: Sequence[str | None]) -> tuple[str | None, ...]:
    ordered: list[str | None] = []
    for token in tokens or (None,):
        if token not in ordered:
            ordered.append(token)
    return tuple(ordered) or (None,)


async def _observe_branch(
    repo: str,
    sha: str,
    branch: str,
    *,
    token: str | None,
    comparison_semaphore: asyncio.Semaphore | None,
) -> AncestryObservation:
    try:
        if comparison_semaphore is None:
            return await observe_ancestry(repo, sha, branch, token=token)
        async with comparison_semaphore:
            return await observe_ancestry(repo, sha, branch, token=token)
    except Exception as exc:
        # Tests and alternate adapters may fail outside the GitHub helper's
        # degrade-open boundary. Keep that failure scoped to this fix ref.
        return ancestry_failure(branch, exc)


async def observe_deploy_state(
    repo: str,
    sha: str,
    *,
    tokens: Sequence[str | None],
    _comparison_semaphore: asyncio.Semaphore | None = None,
) -> DeployStateObservation:
    """Read branch ancestry, retrying ordered identities when indeterminate."""
    clean_repo = str(repo or "").strip()
    clean_sha = str(sha or "").strip()
    if not clean_repo or not clean_sha:
        return DeployStateObservation(
            state=None,
            in_staging=None,
            in_main=None,
            comparisons=(
                AncestryObservation(
                    branch="reference",
                    is_ancestor=None,
                    error_category="invalid_reference",
                ),
            ),
        )

    last = DeployStateObservation(state=None, in_staging=None, in_main=None)
    comparisons: list[AncestryObservation] = []
    for token in _ordered_tokens(tokens):
        staging, main = await asyncio.gather(
            _observe_branch(
                clean_repo,
                clean_sha,
                "staging",
                token=token,
                comparison_semaphore=_comparison_semaphore,
            ),
            _observe_branch(
                clean_repo,
                clean_sha,
                "main",
                token=token,
                comparison_semaphore=_comparison_semaphore,
            ),
        )
        comparisons.extend((staging, main))
        last = DeployStateObservation(
            state=_state_from_ancestry(
                in_staging=staging.is_ancestor,
                in_main=main.is_ancestor,
            ),
            in_staging=staging.is_ancestor,
            in_main=main.is_ancestor,
            comparisons=tuple(comparisons),
        )
        if last.state is not None:
            return last
    return last


async def derive_deploy_state(
    repo: str,
    sha: str,
    *,
    tokens: Sequence[str | None],
) -> DeployState | None:
    """Derive a fix's state from ancestry alone.

    ``None`` means GitHub could not establish enough ancestry facts. Callers
    must render that as ``"unknown"`` rather than infer a healthy state.
    """
    return (await observe_deploy_state(repo, sha, tokens=tokens)).state


async def derive_deploy_states(
    refs: Mapping[_Key, tuple[str, str]] | Iterable[tuple[_Key, str, str]],
    *,
    tokens: Sequence[str | None] | Mapping[str, Sequence[str | None]],
    concurrency: int = 8,
) -> DeployStateBatch[_Key]:
    """Derive each unique fix once while bounding actual GitHub compares."""
    items = (
        list(refs.items())
        if isinstance(refs, Mapping)
        else [(key, (repo, sha)) for key, repo, sha in refs]
    )
    normalized_items = [
        (
            key,
            (
                str(ref[0] or "").strip(),
                str(ref[1] or "").strip(),
            ),
        )
        for key, ref in items
    ]
    unique_refs = tuple(dict.fromkeys(ref for _key, ref in normalized_items))
    comparison_semaphore = asyncio.Semaphore(max(1, int(concurrency)))

    async def derive_unique(
        ref: tuple[str, str],
    ) -> tuple[tuple[str, str], DeployStateObservation]:
        repo, sha = ref
        try:
            repo_tokens = (
                tokens.get(repo, (None,))
                if isinstance(tokens, Mapping)
                else tokens
            )
            observation = await observe_deploy_state(
                repo,
                sha,
                tokens=repo_tokens,
                _comparison_semaphore=comparison_semaphore,
            )
        except Exception as exc:
            observation = DeployStateObservation(
                state=None,
                in_staging=None,
                in_main=None,
                comparisons=(
                    ancestry_failure("derive", exc),
                ),
            )
        return ref, observation

    observed_pairs = await asyncio.gather(
        *(derive_unique(ref) for ref in unique_refs)
    )
    observations_by_ref = dict(observed_pairs)
    observations_by_key = {
        key: observations_by_ref[ref]
        for key, ref in normalized_items
    }
    return DeployStateBatch(
        {
            key: observation.state
            for key, observation in observations_by_key.items()
        },
        observations_by_key=observations_by_key,
        observations_by_ref=observations_by_ref,
    )
