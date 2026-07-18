"""Product-oriented backend eval scenarios.

These scenarios are deliberately provider-neutral. The default suite uses a
mocked backend so normal CI can measure product contracts without live tokens.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ScenarioKind = Literal["golden", "chaos"]


@dataclass(frozen=True)
class EvalScenario:
    """A deterministic product scenario for mocked or live eval runners."""

    scenario_id: str
    kind: ScenarioKind
    user_prompt: str
    expected: dict[str, Any]
    fault: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "kind": self.kind,
            "user_prompt": self.user_prompt,
            "expected": dict(self.expected),
            "fault": self.fault,
            "metadata": dict(self.metadata),
        }


def list_default_scenarios() -> list[EvalScenario]:
    """Return the no-live-provider eval suite used by CI and routing gates."""
    return [
        EvalScenario(
            scenario_id="direct_reply",
            kind="golden",
            user_prompt="Say hello and answer briefly.",
            expected={"lane": "reply_only", "requires_run": False, "must_include": ["hello"]},
        ),
        EvalScenario(
            scenario_id="clarification",
            kind="golden",
            user_prompt="Do the thing for the client.",
            expected={"lane": "await_user", "requires_run": False, "must_include": ["clarify"]},
        ),
        EvalScenario(
            scenario_id="memory_recall",
            kind="golden",
            user_prompt="Use the team's launch notes before drafting the answer.",
            expected={"uses_memory": True, "memory_visibility": "org", "requires_run": True},
        ),
        EvalScenario(
            scenario_id="recurring_report",
            kind="golden",
            user_prompt="Every Monday, summarize open customer risks.",
            expected={"creates_scheduler_contract": True, "requires_run": True},
        ),
        EvalScenario(
            scenario_id="code_doc_update",
            kind="golden",
            user_prompt="Update the backend docs and include the exact files changed.",
            expected={"requires_artifact_evidence": True, "requires_run": True},
        ),
        EvalScenario(
            scenario_id="correction_truth_update",
            kind="golden",
            user_prompt="Actually, we use Provider B now, not Provider A.",
            expected={"truth_update": True, "requires_review_or_evidence": True},
        ),
        EvalScenario(
            scenario_id="slack_casual_dm_voice",
            kind="golden",
            user_prompt="nice, it worked",
            expected={
                "response_mode": "brief_text",
                "tone": "relaxed",
                "humour": "natural_optional",
                "forced_joke": False,
            },
            metadata={"rubric": "human_voice", "surface": "slack_dm"},
        ),
        EvalScenario(
            scenario_id="slack_social_ack_reaction",
            kind="golden",
            user_prompt="thank you!",
            expected={
                "response_tool": "react_to_slack_message",
                "text_required": False,
                "max_reactions": 1,
            },
            metadata={"rubric": "chat_native_response", "surface": "slack"},
        ),
        EvalScenario(
            scenario_id="slack_team_coordination_voice",
            kind="golden",
            user_prompt="Can you send the launch owner a short status update?",
            expected={
                "response_tool": "post_slack_reply",
                "text_required": True,
                "tone": "warm_clear",
            },
            metadata={"rubric": "human_voice", "surface": "slack_channel"},
        ),
        EvalScenario(
            scenario_id="slack_incident_voice",
            kind="golden",
            user_prompt="Production is down and checkout is failing.",
            expected={
                "response_tool": "post_slack_reply",
                "text_required": True,
                "tone": "calm_restrained",
                "humour": "none",
            },
            metadata={"rubric": "context_sensitive_tone", "surface": "slack_channel"},
        ),
        EvalScenario(
            scenario_id="slack_personal_failure_voice",
            kind="golden",
            user_prompt="I broke the deploy. This is my fault.",
            expected={
                "response_tool": "post_slack_reply",
                "text_required": True,
                "tone": "kind_direct",
                "humour": "none",
            },
            metadata={"rubric": "context_sensitive_tone", "surface": "slack_dm"},
        ),
        EvalScenario(
            scenario_id="slack_question_needs_text",
            kind="golden",
            user_prompt="Did the migration finish, and did it pass?",
            expected={
                "response_tool": "post_slack_reply",
                "text_required": True,
                "reaction_is_sufficient": False,
            },
            metadata={"rubric": "chat_native_response", "surface": "slack"},
        ),
        EvalScenario(
            scenario_id="provider_timeout",
            kind="chaos",
            user_prompt="Classify this request while the provider times out.",
            fault="provider_timeout",
            expected={"degraded": True, "fallback": "full_run"},
        ),
        EvalScenario(
            scenario_id="embedding_unavailable",
            kind="chaos",
            user_prompt="Recall related memories while embeddings are unavailable.",
            fault="embedding_unavailable",
            expected={"degraded": True, "fallback": "lexical_or_empty_recall"},
        ),
        EvalScenario(
            scenario_id="scheduler_restart",
            kind="chaos",
            user_prompt="Resume the recurring report after scheduler restart.",
            fault="scheduler_restart",
            expected={"recovers": True, "lease_safe": True},
        ),
        EvalScenario(
            scenario_id="worker_crash_lease_expiry",
            kind="chaos",
            user_prompt="Recover a run after the worker crashes.",
            fault="worker_crash_lease_expiry",
            expected={"recovers": True, "settlement": "expired_or_requeued"},
        ),
    ]
