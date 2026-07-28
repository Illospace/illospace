"""Shared deploy-tracker record field ownership.

Mechanical deploy state is derived from GitHub ancestry.  Tracker records own
only the fix identity and the non-derivable human verification overlay.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


DEPLOY_EVIDENCE_FIELDS = frozenset(
    {
        "fix_pr",
        "fix_merge_sha",
        "verified",
        "verified_at",
    }
)

# These fields belonged to the retired persisted deploy-state model.  They are
# hidden at read boundaries until the re-runnable backfill removes them.  The
# one exception is ``deploy_state=prod_pending``: that is a closure-workflow
# marker, not a claim about branch ancestry.
RETIRED_DEPLOY_FIELDS = frozenset(
    {
        "deploy_state",
        "deployed_at",
        "fix_merged_at",
        "promotion_recommended_at",
    }
)

DEPLOY_FIELDS_HIDDEN_FROM_RECORD_PROSE = (
    DEPLOY_EVIDENCE_FIELDS | RETIRED_DEPLOY_FIELDS
)


def deploy_ticket_object_keys() -> frozenset[str]:
    """Object-type keys that hold deploy-tracker tickets."""
    raw = os.environ.get("ILLO_DEPLOY_TICKET_OBJECT_KEYS", "").strip()
    keys = frozenset(key.strip() for key in raw.split(",") if key.strip())
    return keys or frozenset({"ticket", "github_ticket"})


def without_retired_deploy_fields(
    data: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return externally safe tracker data without retired stored state."""
    normalized = dict(data or {})
    safe = {
        key: value
        for key, value in normalized.items()
        if key not in RETIRED_DEPLOY_FIELDS
    }
    if str(normalized.get("deploy_state") or "").strip() == "prod_pending":
        safe["deploy_state"] = "prod_pending"
    return safe


def record_data_for_serialization(
    object_key: str | None,
    data: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Apply deploy-field ownership at every external record boundary."""
    normalized = dict(data or {})
    if object_key in deploy_ticket_object_keys():
        return without_retired_deploy_fields(normalized)
    return normalized
