"""Enums are StrEnum — they must equal their string values for DB compat."""
from brain.platform.db.enums import (
    ContractStatus,
    ContractType,
    RunStatus,
    Maturity,
    MemoryScope,
    MemoryTier,
    MemoryType,
    Outcome,
    ScoutLane,
    ScoutStatus,
    SettlementState,
    SkillLevel,
    TargetStatus,
    ThinkingTier,
    UserRole,
    VaultAction,
    Visibility,
)

def test_strenum_values():
    assert Maturity.EMERGING == "emerging"
    assert ThinkingTier.HIGH == "high"
    assert Outcome.SUCCESS == "success"
    assert MemoryType.LESSON == "lesson"
    assert Visibility.PRIVATE == "private"
    assert SkillLevel.COGNITIVE == "cognitive"
    assert MemoryTier.EPISODIC == "episodic"
    assert MemoryScope.PERSONAL == "personal"
    assert RunStatus.QUEUED == "queued"
    assert SettlementState.CLAIMED == "claimed"
    assert ContractStatus.CREATED == "created"
    assert ContractType.FREEFORM == "freeform"
    assert TargetStatus.UNSPECIFIED == "unspecified"
    assert ScoutStatus.APPLIED == "applied"
    assert ScoutLane.REPLY_ONLY == "reply_only"
    assert VaultAction.READ == "read"
    assert UserRole.OWNER == "owner"

def test_strenum_is_str():
    assert isinstance(Maturity.EMERGING, str)
    assert f"{Maturity.EMERGING}" == "emerging"

def test_all_maturity_values():
    assert set(Maturity) == {"emerging", "developing", "proficient", "expert"}

def test_run_enum_values_match_db_strings():
    assert {status.value for status in RunStatus} == {
        "pending",
        "queued",
        "running",
        "pending_approval",
        "completed",
        "failed",
        "canceled",
        "cancelled",
        "timeout",
    }
    assert {state.value for state in SettlementState} == {
        "queued",
        "claimed",
        "scout",
        "running",
        "repairing",
        "awaiting_budget_approval",
        "completed",
        "failed",
        "canceled",
        "expired",
        "superseded",
        "admitted",
        "executing",
        "awaiting_verification",
        "settled_success",
        "settled_failure",
        "cancelled",
    }
    assert {status.value for status in ContractStatus} == {
        "created",
        "in_progress",
        "verification_pending",
        "satisfied",
        "failed",
    }
    assert {contract_type.value for contract_type in ContractType} == {
        "answer",
        "freeform",
        "read_only_audit",
        "skill_catalog_audit",
        "existing_pr_review",
        "code_change",
        "created_pr",
        "deployment",
        "browser_preview",
        "cycle_create",
        "cycle_update",
        "domain_update",
        "workspace_app",
        "pr",
        "pr_review",
        "issue",
        "commit",
        "file",
        "document",
    }
    assert {status.value for status in TargetStatus} == {
        "unspecified",
        "validated",
        "invalid",
        "valid",
        "resolved",
        "blocked",
        "must_verify",
        "high_risk",
    }
    assert {status.value for status in ScoutStatus} == {
        "applied",
        "observed",
    }
    assert {lane.value for lane in ScoutLane} == {
        "reply_only",
        "await_user",
        "context_reply",
        "full_pipeline",
    }
