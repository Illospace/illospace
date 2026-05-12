"""Domain enums — replaces magic strings throughout the codebase.

Every stringly-typed DB column gets a StrEnum here. Values match
existing DB strings exactly, so zero migration is needed.
"""
from enum import StrEnum


class Maturity(StrEnum):
    EMERGING = "emerging"
    DEVELOPING = "developing"
    PROFICIENT = "proficient"
    EXPERT = "expert"


class ModelTier(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    LOCAL = "local"


class ThinkingTier(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class Outcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"


class MemoryType(StrEnum):
    LESSON = "lesson"
    PATTERN = "pattern"
    DECISION = "decision"
    FACT = "fact"
    PREFERENCE = "preference"
    PROCEDURE = "procedure"
    EPISODE = "episode"
    INSIGHT = "insight"


class Visibility(StrEnum):
    PRIVATE = "private"
    TEAM = "team"
    ORG = "org"


class SkillLevel(StrEnum):
    COGNITIVE = "cognitive"
    PROCEDURAL = "procedural"
    META = "meta"


class MemoryTier(StrEnum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    CONCEPTUAL = "conceptual"


class MemoryScope(StrEnum):
    PERSONAL = "personal"
    PROJECT = "project"
    GLOBAL = "global"


class RunStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class SettlementState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    SCOUT = "scout"
    RUNNING = "running"
    REPAIRING = "repairing"
    AWAITING_BUDGET_APPROVAL = "awaiting_budget_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    ADMITTED = "admitted"
    EXECUTING = "executing"
    AWAITING_VERIFICATION = "awaiting_verification"
    SETTLED_SUCCESS = "settled_success"
    SETTLED_FAILURE = "settled_failure"
    CANCELLED = "cancelled"


class ContractStatus(StrEnum):
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    VERIFICATION_PENDING = "verification_pending"
    SATISFIED = "satisfied"
    FAILED = "failed"


class ContractType(StrEnum):
    ANSWER = "answer"
    FREEFORM = "freeform"
    READ_ONLY_AUDIT = "read_only_audit"
    SKILL_CATALOG_AUDIT = "skill_catalog_audit"
    EXISTING_PR_REVIEW = "existing_pr_review"
    CODE_CHANGE = "code_change"
    CREATED_PR = "created_pr"
    DEPLOYMENT = "deployment"
    BROWSER_PREVIEW = "browser_preview"
    CYCLE_CREATE = "cycle_create"
    CYCLE_UPDATE = "cycle_update"
    DOMAIN_UPDATE = "domain_update"
    WORKSPACE_APP = "workspace_app"
    PR = "pr"
    PR_REVIEW = "pr_review"
    ISSUE = "issue"
    COMMIT = "commit"
    FILE = "file"
    DOCUMENT = "document"


class TargetStatus(StrEnum):
    UNSPECIFIED = "unspecified"
    VALIDATED = "validated"
    INVALID = "invalid"
    VALID = "valid"
    RESOLVED = "resolved"
    BLOCKED = "blocked"
    MUST_VERIFY = "must_verify"
    HIGH_RISK = "high_risk"


class ScoutStatus(StrEnum):
    APPLIED = "applied"
    OBSERVED = "observed"


class ScoutLane(StrEnum):
    REPLY_ONLY = "reply_only"
    AWAIT_USER = "await_user"
    CONTEXT_REPLY = "context_reply"
    FULL_PIPELINE = "full_pipeline"


class VaultAction(StrEnum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    SHARE = "share"
    REVOKE = "revoke"


class UserRole(StrEnum):
    OWNER = "owner"
    MEMBER = "member"


class HarvestType(StrEnum):
    FACT = "fact"
    DECISION = "decision"
    PREFERENCE = "preference"
    COMMITMENT = "commitment"
    PROCEDURE = "procedure"
    CORRECTION = "correction"
    LESSON = "lesson"
    OUTCOME = "outcome"
    UNRESOLVED = "unresolved"
    RAW_EPISODE = "raw_episode"


class PoolName(StrEnum):
    EXPLOIT = "exploit"
    EXPLORE = "explore"
    NARRATIVE = "narrative"
