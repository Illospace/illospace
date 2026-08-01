from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_routing_marketplace_snapshot_exposes_historical_fallback_reason():
    from brain.systems.routing.marketplace import get_routing_marketplace_snapshot

    health_row = SimpleNamespace(
        provider="openai",
        model="gpt-5.6-sol",
        window_start=datetime.now(timezone.utc),
        window_end=datetime.now(timezone.utc),
        p50_latency_ms=1200,
        p95_latency_ms=1800,
        error_rate=0.1,
        auth_fail_rate=0.0,
        rate_limit_rate=0.0,
        sample_count=14,
        source="test",
    )
    decision_row = SimpleNamespace(
        run_id=1,
        task_family="develop",
        lane="worker",
        decision_mode="active",
        selected_provider="openai",
        selected_model="gpt-5.6-sol",
        applied=True,
        fallback_used=True,
        created_at=datetime.now(timezone.utc),
        inputs={
            "route_summary": {
                "fallback_reason": "canary_evidence_not_strong",
                "legacy": {"score": 0.51},
                "selected": {"score": 0.49},
                "candidate_count": 2,
                "eligible_candidate_count": 1,
            }
        },
        constraints={
            "fallback_reason": "canary_evidence_not_strong",
            "route_summary": {
                "fallback_reason": "canary_evidence_not_strong",
                "legacy": {"score": 0.51},
                "selected": {"score": 0.49},
                "candidate_count": 2,
                "eligible_candidate_count": 1,
            },
        },
    )

    def _result(rows):
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows
        return result

    session = MagicMock()
    session.execute.side_effect = [_result([health_row]), _result([decision_row])]

    snapshot = await get_routing_marketplace_snapshot(
        session,
        user_id="user-1",
        org_id="org-1",
        provider="openai",
    )

    assert snapshot["healthy"] is True
    assert (
        snapshot["latest_decisions"][0]["fallback_reason"]
        == "canary_evidence_not_strong"
    )
    assert snapshot["latest_decisions"][0]["selected_over_legacy_delta"] == -0.02
