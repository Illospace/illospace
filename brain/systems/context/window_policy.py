"""Run-scoped policy for model-context admission and compaction progress."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from brain.systems.context.budget import ModelContextBudget, resolve_model_context_budget
from brain.systems.context.compaction import CompactionReport, estimate_session_tokens
from brain.systems.context.errors import (
    ContextCompactionStalledError,
    ContextFloorExceedsBudgetError,
)
from brain.systems.context.semantic_compaction import (
    SemanticCompactor,
    plan_session_compaction,
)

DEFAULT_MIN_COMPACTION_MESSAGES = 4
DEFAULT_MAX_COMPACTION_NO_PROGRESS = 3


@dataclass(frozen=True)
class ContextAdmission:
    """The canonical minimum prompt measured for one run configuration."""

    budget: ModelContextBudget
    floor_tokens: int
    tool_count: int
    min_messages: int


@dataclass(frozen=True)
class ContextCompactionOutcome:
    """One policy-evaluated compaction result for the run loop to orchestrate."""

    messages: list[dict]
    report: CompactionReport | None
    estimated_tokens: int
    final_tokens: int
    warning_required: bool = False


@dataclass
class ContextWindowPolicy:
    """Own context boundaries and mutable compaction progress for one run."""

    budget: ModelContextBudget
    min_messages: int = DEFAULT_MIN_COMPACTION_MESSAGES
    max_consecutive_no_progress: int = DEFAULT_MAX_COMPACTION_NO_PROGRESS
    consecutive_no_progress: int = 0
    warning_emitted: bool = False

    @classmethod
    def resolve(
        cls,
        *,
        model: str,
        provider: str | None = None,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> "ContextWindowPolicy":
        """Build the run-scoped policy from one resolved model budget."""

        return cls(
            budget=resolve_model_context_budget(
                model=model,
                provider=provider,
                reasoning_effort=reasoning_effort,
                max_output_tokens=max_output_tokens,
                tools=tools,
            )
        )

    @property
    def threshold_tokens(self) -> int:
        return self.budget.auto_compact_threshold_tokens

    def admits(self, tokens: int) -> bool:
        """Equality is admitted: the threshold is the inclusive prompt ceiling."""

        return int(tokens) <= self.threshold_tokens

    def requires_compaction(self, tokens: int) -> bool:
        """Compaction starts only after crossing the inclusive prompt ceiling."""

        return int(tokens) > self.threshold_tokens

    def admit(
        self,
        messages: list[dict],
        *,
        system: Any = None,
        tools: list[dict] | None = None,
        session_id: str = "",
        phase: str = "admission",
    ) -> ContextAdmission:
        """Measure and admit the canonical minimum checkpointed prompt."""

        minimum_plan = plan_session_compaction(
            messages,
            token_limit=self.threshold_tokens,
            target_tokens=1,
            session_id=session_id,
            phase=phase,
            system=system,
            tools=tools,
            max_messages=self.min_messages,
            min_messages=self.min_messages,
            force=True,
        )
        admission = ContextAdmission(
            budget=self.budget,
            floor_tokens=minimum_plan.estimated_tokens,
            tool_count=len(tools or []),
            min_messages=self.min_messages,
        )
        if not self.admits(admission.floor_tokens):
            raise ContextFloorExceedsBudgetError(
                floor=admission.floor_tokens,
                ceiling=self.threshold_tokens,
                tools=admission.tool_count,
                min_messages=admission.min_messages,
            )
        return admission

    def compact(
        self,
        messages: list[dict],
        *,
        session_id: str,
        phase: str,
        system: Any = None,
        tools: list[dict] | None = None,
        max_messages: int,
        semantic_compactor: SemanticCompactor | None = None,
        force: bool = False,
        emergency: bool = False,
    ) -> ContextCompactionOutcome:
        """Plan compaction and own all no-progress/warning state transitions."""

        estimated_tokens = estimate_session_tokens(messages, system=system, tools=tools)
        if not force and not self.requires_compaction(estimated_tokens):
            self._reset_progress()
            return ContextCompactionOutcome(
                messages=messages,
                report=None,
                estimated_tokens=estimated_tokens,
                final_tokens=estimated_tokens,
            )

        plan = plan_session_compaction(
            messages,
            token_limit=self.threshold_tokens,
            target_tokens=(
                self.budget.emergency_target_tokens
                if emergency
                else self.budget.target_tokens
            ),
            session_id=session_id,
            phase=phase,
            system=system,
            tools=tools,
            max_messages=max_messages,
            min_messages=self.min_messages,
            force=force,
            emergency=emergency,
            semantic_compactor=semantic_compactor,
        )
        made_sufficient_progress = (
            plan.report.omitted_count > 0
            and self.admits(plan.estimated_tokens)
        )
        if made_sufficient_progress:
            self._reset_progress()
        else:
            self.consecutive_no_progress += 1

        warning_required = False
        if plan.report.omitted_count <= 0 and not self.warning_emitted:
            self.warning_emitted = True
            warning_required = True

        if (
            not made_sufficient_progress
            and self.consecutive_no_progress >= self.max_consecutive_no_progress
        ):
            raise ContextCompactionStalledError(
                estimated=plan.estimated_tokens,
                ceiling=self.threshold_tokens,
                tools=len(tools or []),
                attempts=self.consecutive_no_progress,
                phase=phase,
            )

        return ContextCompactionOutcome(
            messages=plan.messages if plan.report.omitted_count > 0 else messages,
            report=plan.report if plan.report.omitted_count > 0 else None,
            estimated_tokens=estimated_tokens,
            final_tokens=plan.estimated_tokens,
            warning_required=warning_required,
        )

    def _reset_progress(self) -> None:
        self.consecutive_no_progress = 0
        self.warning_emitted = False


__all__ = [
    "ContextAdmission",
    "ContextCompactionOutcome",
    "ContextWindowPolicy",
    "DEFAULT_MAX_COMPACTION_NO_PROGRESS",
    "DEFAULT_MIN_COMPACTION_MESSAGES",
]
