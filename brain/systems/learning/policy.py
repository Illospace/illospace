"""Deterministic tenant learning policy defaults.

This module is intentionally persistence-free. It defines the learning policy
contract that hosted and self-hosted deployments can expose before wiring any
new runtime behavior to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
import os
from typing import Mapping


class LearningDeploymentMode(StrEnum):
    HOSTED = "hosted"
    SELF_HOSTED = "self_hosted"


class CommunitySkillAutoUpdatePolicy(StrEnum):
    DISABLED = "disabled"
    SECURITY_PATCH_ONLY = "security_patch_only"
    PATCH_ONLY = "patch_only"
    MINOR_AND_PATCH = "minor_and_patch"


class PrivateDataRedactionMode(StrEnum):
    STRICT = "strict"
    STANDARD = "standard"
    LOCAL_ONLY = "local_only"
    DISABLED = "disabled"


HOSTED_ALLOWED_MODEL_TIERS = ("low", "medium")
SELF_HOSTED_ALLOWED_MODEL_TIERS = ("local", "low")
MODEL_TIER_ALIASES = {
    "cheap": "low",
    "small": "low",
    "standard": "medium",
    "balanced": "medium",
    "premium": "high",
    "expensive": "high",
}

SELF_HOSTED_MODE_VALUES = {
    "self_hosted",
    "self-hosted",
    "selfhosted",
    "local",
    "oss",
    "open_source",
    "open-source",
}
HOSTED_MODE_VALUES = {"hosted", "managed", "cloud", "saas"}


def _normalize_token(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _coerce_deployment_mode(value: LearningDeploymentMode | str | None) -> LearningDeploymentMode:
    if isinstance(value, LearningDeploymentMode):
        return value
    normalized = _normalize_token(value)
    if normalized in {_normalize_token(item) for item in SELF_HOSTED_MODE_VALUES}:
        return LearningDeploymentMode.SELF_HOSTED
    if normalized in {_normalize_token(item) for item in HOSTED_MODE_VALUES}:
        return LearningDeploymentMode.HOSTED
    return LearningDeploymentMode.HOSTED


def _coerce_auto_update_policy(
    value: CommunitySkillAutoUpdatePolicy | str | None,
    default: CommunitySkillAutoUpdatePolicy,
) -> CommunitySkillAutoUpdatePolicy:
    if isinstance(value, CommunitySkillAutoUpdatePolicy):
        return value
    normalized = _normalize_token(value)
    aliases = {
        "off": CommunitySkillAutoUpdatePolicy.DISABLED,
        "none": CommunitySkillAutoUpdatePolicy.DISABLED,
        "disabled": CommunitySkillAutoUpdatePolicy.DISABLED,
        "security": CommunitySkillAutoUpdatePolicy.SECURITY_PATCH_ONLY,
        "security_only": CommunitySkillAutoUpdatePolicy.SECURITY_PATCH_ONLY,
        "security_patch": CommunitySkillAutoUpdatePolicy.SECURITY_PATCH_ONLY,
        "security_patches": CommunitySkillAutoUpdatePolicy.SECURITY_PATCH_ONLY,
        "security_patch_only": CommunitySkillAutoUpdatePolicy.SECURITY_PATCH_ONLY,
        "patch": CommunitySkillAutoUpdatePolicy.PATCH_ONLY,
        "patches": CommunitySkillAutoUpdatePolicy.PATCH_ONLY,
        "patch_only": CommunitySkillAutoUpdatePolicy.PATCH_ONLY,
        "minor": CommunitySkillAutoUpdatePolicy.MINOR_AND_PATCH,
        "minor_and_patch": CommunitySkillAutoUpdatePolicy.MINOR_AND_PATCH,
        "minor_patch": CommunitySkillAutoUpdatePolicy.MINOR_AND_PATCH,
    }
    return aliases.get(normalized, default)


def _coerce_redaction_mode(
    value: PrivateDataRedactionMode | str | None,
    default: PrivateDataRedactionMode,
) -> PrivateDataRedactionMode:
    if isinstance(value, PrivateDataRedactionMode):
        return value
    normalized = _normalize_token(value)
    aliases = {
        "strict": PrivateDataRedactionMode.STRICT,
        "standard": PrivateDataRedactionMode.STANDARD,
        "local": PrivateDataRedactionMode.LOCAL_ONLY,
        "local_only": PrivateDataRedactionMode.LOCAL_ONLY,
        "off": PrivateDataRedactionMode.DISABLED,
        "none": PrivateDataRedactionMode.DISABLED,
        "disabled": PrivateDataRedactionMode.DISABLED,
    }
    return aliases.get(normalized, default)


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return _normalize_token(value) in {"1", "true", "yes", "on", "enabled"}


def _clamp_rate(value: object, default: float) -> float:
    if value is None or str(value).strip() == "":
        return default
    try:
        return max(0.0, min(1.0, float(str(value).strip())))
    except (TypeError, ValueError):
        return default


def _coerce_units(value: object, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _normalize_model_tiers(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        raw_items = value.split(",")
    else:
        try:
            raw_items = list(value)  # type: ignore[arg-type]
        except TypeError:
            raw_items = [value]

    tiers: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        tier = MODEL_TIER_ALIASES.get(_normalize_token(item), _normalize_token(item))
        if tier and tier not in seen:
            tiers.append(tier)
            seen.add(tier)
    return tuple(tiers) if tiers else default


def _first_env(env: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = env.get(name)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _feature_switch_env(
    env: Mapping[str, str],
    *,
    enabled_names: tuple[str, ...],
    disabled_names: tuple[str, ...] = (),
) -> bool | None:
    enabled = _first_env(env, *enabled_names)
    if enabled is not None:
        return _coerce_bool(enabled)
    disabled = _first_env(env, *disabled_names)
    if disabled is not None:
        return not _coerce_bool(disabled)
    return None


@dataclass(frozen=True)
class LearningPolicyOverride:
    """Optional tenant/org override payload.

    The override is deliberately plain data so callers can source it from env,
    config files, or a future DB row without changing the policy builder.
    """

    scope: str = "org"
    scope_id: str | None = None
    source: str | None = None
    enabled: bool | None = None
    after_run_sample_rate: float | None = None
    night_budget_units: int | None = None
    tenant_daily_budget_units: int | None = None
    active_context_policy_enabled: bool | None = None
    skill_quality_routing_enabled: bool | None = None
    after_run_learning_enabled: bool | None = None
    night_llm_adjudication_enabled: bool | None = None
    allowed_model_tiers: tuple[str, ...] | list[str] | str | None = None
    external_eval_export_allowed: bool | None = None
    community_skill_auto_update_policy: CommunitySkillAutoUpdatePolicy | str | None = None
    private_data_redaction_mode: PrivateDataRedactionMode | str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _normalize_token(self.scope) or "org")
        if self.enabled is not None:
            object.__setattr__(self, "enabled", _coerce_bool(self.enabled))
        if self.external_eval_export_allowed is not None:
            object.__setattr__(
                self,
                "external_eval_export_allowed",
                _coerce_bool(self.external_eval_export_allowed),
            )
        for name in (
            "active_context_policy_enabled",
            "skill_quality_routing_enabled",
            "after_run_learning_enabled",
            "night_llm_adjudication_enabled",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _coerce_bool(value))
        if self.after_run_sample_rate is not None:
            object.__setattr__(
                self,
                "after_run_sample_rate",
                _clamp_rate(self.after_run_sample_rate, 1.0),
            )
        if self.night_budget_units is not None:
            object.__setattr__(self, "night_budget_units", _coerce_units(self.night_budget_units, 0))
        if self.tenant_daily_budget_units is not None:
            object.__setattr__(
                self,
                "tenant_daily_budget_units",
                _coerce_units(self.tenant_daily_budget_units, 0),
            )
        if self.allowed_model_tiers is not None:
            object.__setattr__(
                self,
                "allowed_model_tiers",
                _normalize_model_tiers(self.allowed_model_tiers, ()),
            )
        if self.community_skill_auto_update_policy is not None:
            object.__setattr__(
                self,
                "community_skill_auto_update_policy",
                _coerce_auto_update_policy(
                    self.community_skill_auto_update_policy,
                    CommunitySkillAutoUpdatePolicy.SECURITY_PATCH_ONLY,
                ),
            )
        if self.private_data_redaction_mode is not None:
            object.__setattr__(
                self,
                "private_data_redaction_mode",
                _coerce_redaction_mode(
                    self.private_data_redaction_mode,
                    PrivateDataRedactionMode.STRICT,
                ),
            )

    def apply_to(self, policy: "TenantLearningPolicy") -> "TenantLearningPolicy":
        updates: dict[str, object] = {}
        if self.enabled is not None:
            updates["enabled"] = bool(self.enabled)
        if self.after_run_sample_rate is not None:
            updates["after_run_sample_rate"] = self.after_run_sample_rate
        if self.night_budget_units is not None:
            updates["night_budget_units"] = self.night_budget_units
        if self.tenant_daily_budget_units is not None:
            updates["tenant_daily_budget_units"] = self.tenant_daily_budget_units
        if self.active_context_policy_enabled is not None:
            updates["active_context_policy_enabled"] = self.active_context_policy_enabled
        if self.skill_quality_routing_enabled is not None:
            updates["skill_quality_routing_enabled"] = self.skill_quality_routing_enabled
        if self.after_run_learning_enabled is not None:
            updates["after_run_learning_enabled"] = self.after_run_learning_enabled
        if self.night_llm_adjudication_enabled is not None:
            updates["night_llm_adjudication_enabled"] = self.night_llm_adjudication_enabled
        if self.allowed_model_tiers is not None:
            updates["allowed_model_tiers"] = self.allowed_model_tiers
        if self.external_eval_export_allowed is not None:
            updates["external_eval_export_allowed"] = bool(self.external_eval_export_allowed)
        if self.community_skill_auto_update_policy is not None:
            updates["community_skill_auto_update_policy"] = self.community_skill_auto_update_policy
        if self.private_data_redaction_mode is not None:
            updates["private_data_redaction_mode"] = self.private_data_redaction_mode

        updates["applied_overrides"] = (*policy.applied_overrides, self)
        return replace(policy, **updates)

    def to_payload(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for name in (
            "enabled",
            "after_run_sample_rate",
            "night_budget_units",
            "tenant_daily_budget_units",
            "active_context_policy_enabled",
            "skill_quality_routing_enabled",
            "after_run_learning_enabled",
            "night_llm_adjudication_enabled",
            "external_eval_export_allowed",
        ):
            value = getattr(self, name)
            if value is not None:
                values[name] = value
        if self.allowed_model_tiers is not None:
            values["allowed_model_tiers"] = list(self.allowed_model_tiers)
        if self.community_skill_auto_update_policy is not None:
            values["community_skill_auto_update_policy"] = str(self.community_skill_auto_update_policy)
        if self.private_data_redaction_mode is not None:
            values["private_data_redaction_mode"] = str(self.private_data_redaction_mode)
        return {
            "scope": self.scope,
            "scope_id": self.scope_id,
            "source": self.source,
            "values": values,
        }


@dataclass(frozen=True)
class TenantLearningPolicy:
    enabled: bool = True
    deployment_mode: LearningDeploymentMode | str = LearningDeploymentMode.HOSTED
    after_run_sample_rate: float = 0.25
    night_budget_units: int = 100_000
    tenant_daily_budget_units: int = 250_000
    active_context_policy_enabled: bool = True
    skill_quality_routing_enabled: bool = True
    after_run_learning_enabled: bool = True
    night_llm_adjudication_enabled: bool = True
    allowed_model_tiers: tuple[str, ...] | list[str] | str = field(
        default_factory=lambda: HOSTED_ALLOWED_MODEL_TIERS
    )
    external_eval_export_allowed: bool = False
    community_skill_auto_update_policy: CommunitySkillAutoUpdatePolicy | str = (
        CommunitySkillAutoUpdatePolicy.SECURITY_PATCH_ONLY
    )
    private_data_redaction_mode: PrivateDataRedactionMode | str = PrivateDataRedactionMode.STRICT
    applied_overrides: tuple[LearningPolicyOverride, ...] = field(default_factory=tuple)
    risk_flags: tuple[str, ...] = field(default_factory=tuple, init=False)

    def __post_init__(self) -> None:
        deployment_mode = _coerce_deployment_mode(self.deployment_mode)
        object.__setattr__(self, "deployment_mode", deployment_mode)
        object.__setattr__(self, "enabled", _coerce_bool(self.enabled))
        object.__setattr__(
            self,
            "external_eval_export_allowed",
            _coerce_bool(self.external_eval_export_allowed),
        )
        object.__setattr__(
            self,
            "after_run_sample_rate",
            _clamp_rate(self.after_run_sample_rate, 0.25),
        )
        object.__setattr__(self, "night_budget_units", _coerce_units(self.night_budget_units, 0))
        object.__setattr__(
            self,
            "tenant_daily_budget_units",
            _coerce_units(self.tenant_daily_budget_units, 0),
        )
        for name in (
            "active_context_policy_enabled",
            "skill_quality_routing_enabled",
            "after_run_learning_enabled",
            "night_llm_adjudication_enabled",
        ):
            object.__setattr__(self, name, _coerce_bool(getattr(self, name)))
        default_tiers = (
            SELF_HOSTED_ALLOWED_MODEL_TIERS
            if deployment_mode == LearningDeploymentMode.SELF_HOSTED
            else HOSTED_ALLOWED_MODEL_TIERS
        )
        object.__setattr__(
            self,
            "allowed_model_tiers",
            _normalize_model_tiers(self.allowed_model_tiers, default_tiers),
        )
        object.__setattr__(
            self,
            "community_skill_auto_update_policy",
            _coerce_auto_update_policy(
                self.community_skill_auto_update_policy,
                CommunitySkillAutoUpdatePolicy.SECURITY_PATCH_ONLY,
            ),
        )
        object.__setattr__(
            self,
            "private_data_redaction_mode",
            _coerce_redaction_mode(
                self.private_data_redaction_mode,
                PrivateDataRedactionMode.STRICT,
            ),
        )
        object.__setattr__(self, "applied_overrides", tuple(self.applied_overrides or ()))
        if not self.enabled:
            for name in (
                "active_context_policy_enabled",
                "skill_quality_routing_enabled",
                "after_run_learning_enabled",
                "night_llm_adjudication_enabled",
            ):
                object.__setattr__(self, name, False)
        object.__setattr__(self, "risk_flags", self._derive_risk_flags())

    def _derive_risk_flags(self) -> tuple[str, ...]:
        flags: list[str] = []
        if self.external_eval_export_allowed and self.private_data_redaction_mode == PrivateDataRedactionMode.DISABLED:
            flags.append("external_eval_export_without_redaction")
        if self.external_eval_export_allowed and self.private_data_redaction_mode == PrivateDataRedactionMode.LOCAL_ONLY:
            flags.append("external_eval_export_needs_export_redaction_review")
        if self.community_skill_auto_update_policy == CommunitySkillAutoUpdatePolicy.MINOR_AND_PATCH:
            flags.append("community_skill_auto_update_allows_minor_versions")
        if "high" in self.allowed_model_tiers:
            flags.append("high_intelligence_learning_model_tier_enabled")
        return tuple(flags)

    def to_payload(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "deployment_mode": str(self.deployment_mode),
            "after_run_sample_rate": self.after_run_sample_rate,
            "night_budget_units": self.night_budget_units,
            "tenant_daily_budget_units": self.tenant_daily_budget_units,
            "active_context_policy_enabled": self.active_context_policy_enabled,
            "skill_quality_routing_enabled": self.skill_quality_routing_enabled,
            "after_run_learning_enabled": self.after_run_learning_enabled,
            "night_llm_adjudication_enabled": self.night_llm_adjudication_enabled,
            "allowed_model_tiers": list(self.allowed_model_tiers),
            "external_eval_export_allowed": self.external_eval_export_allowed,
            "community_skill_auto_update_policy": str(self.community_skill_auto_update_policy),
            "private_data_redaction_mode": str(self.private_data_redaction_mode),
            "risk_flags": list(self.risk_flags),
            "applied_overrides": [override.to_payload() for override in self.applied_overrides],
        }


def _defaults_for_deployment(deployment_mode: LearningDeploymentMode) -> TenantLearningPolicy:
    if deployment_mode == LearningDeploymentMode.SELF_HOSTED:
        return TenantLearningPolicy(
            enabled=True,
            deployment_mode=LearningDeploymentMode.SELF_HOSTED,
            after_run_sample_rate=1.0,
            night_budget_units=100_000,
            tenant_daily_budget_units=250_000,
            active_context_policy_enabled=True,
            skill_quality_routing_enabled=True,
            after_run_learning_enabled=True,
            night_llm_adjudication_enabled=True,
            allowed_model_tiers=SELF_HOSTED_ALLOWED_MODEL_TIERS,
            external_eval_export_allowed=False,
            community_skill_auto_update_policy=CommunitySkillAutoUpdatePolicy.PATCH_ONLY,
            private_data_redaction_mode=PrivateDataRedactionMode.LOCAL_ONLY,
        )
    return TenantLearningPolicy(
        enabled=True,
        deployment_mode=LearningDeploymentMode.HOSTED,
        after_run_sample_rate=0.25,
        night_budget_units=100_000,
        tenant_daily_budget_units=250_000,
        active_context_policy_enabled=True,
        skill_quality_routing_enabled=True,
        after_run_learning_enabled=True,
        night_llm_adjudication_enabled=True,
        allowed_model_tiers=HOSTED_ALLOWED_MODEL_TIERS,
        external_eval_export_allowed=False,
        community_skill_auto_update_policy=CommunitySkillAutoUpdatePolicy.SECURITY_PATCH_ONLY,
        private_data_redaction_mode=PrivateDataRedactionMode.STRICT,
    )


def _apply_env_overrides(policy: TenantLearningPolicy, env: Mapping[str, str]) -> TenantLearningPolicy:
    enabled = _first_env(env, "LEARNING_POLICY_ENABLED", "LEARNING_ENABLED")
    after_run_sample_rate = _first_env(
        env,
        "LEARNING_POLICY_AFTER_RUN_SAMPLE_RATE",
        "LEARNING_BUDGET_AFTER_RUN_SAMPLE_RATE",
    )
    night_budget_units = _first_env(
        env,
        "LEARNING_POLICY_NIGHT_BUDGET_UNITS",
        "LEARNING_BUDGET_NIGHT_TOKENS",
    )
    tenant_daily_budget_units = _first_env(
        env,
        "LEARNING_POLICY_TENANT_DAILY_BUDGET_UNITS",
        "LEARNING_BUDGET_TENANT_DAILY_TOKENS",
    )
    external_eval_export_allowed = _first_env(
        env,
        "LEARNING_POLICY_EXTERNAL_EVAL_EXPORT_ALLOWED",
    )
    override = LearningPolicyOverride(
        scope="deployment",
        source="env",
        enabled=_coerce_bool(enabled) if enabled is not None else None,
        after_run_sample_rate=(
            _clamp_rate(after_run_sample_rate, policy.after_run_sample_rate)
            if after_run_sample_rate is not None
            else None
        ),
        night_budget_units=(
            _coerce_units(night_budget_units, policy.night_budget_units)
            if night_budget_units is not None
            else None
        ),
        tenant_daily_budget_units=(
            _coerce_units(tenant_daily_budget_units, policy.tenant_daily_budget_units)
            if tenant_daily_budget_units is not None
            else None
        ),
        active_context_policy_enabled=_feature_switch_env(
            env,
            enabled_names=(
                "LEARNING_POLICY_ACTIVE_CONTEXT_POLICY_ENABLED",
                "LEARNING_ACTIVE_CONTEXT_POLICY_ENABLED",
            ),
            disabled_names=(
                "LEARNING_POLICY_ACTIVE_CONTEXT_POLICY_DISABLED",
                "LEARNING_ACTIVE_CONTEXT_POLICY_DISABLED",
            ),
        ),
        skill_quality_routing_enabled=_feature_switch_env(
            env,
            enabled_names=(
                "LEARNING_POLICY_SKILL_QUALITY_ROUTING_ENABLED",
                "LEARNING_SKILL_QUALITY_ROUTING_ENABLED",
            ),
            disabled_names=(
                "LEARNING_POLICY_SKILL_QUALITY_ROUTING_DISABLED",
                "LEARNING_SKILL_QUALITY_ROUTING_DISABLED",
            ),
        ),
        after_run_learning_enabled=_feature_switch_env(
            env,
            enabled_names=(
                "LEARNING_POLICY_AFTER_RUN_LEARNING_ENABLED",
                "LEARNING_AFTER_RUN_LEARNING_ENABLED",
                "AFTER_RUN_LEARNING_ENABLED",
            ),
            disabled_names=(
                "LEARNING_POLICY_AFTER_RUN_LEARNING_DISABLED",
                "LEARNING_AFTER_RUN_LEARNING_DISABLED",
                "AFTER_RUN_LEARNING_DISABLED",
            ),
        ),
        night_llm_adjudication_enabled=_feature_switch_env(
            env,
            enabled_names=(
                "LEARNING_POLICY_NIGHT_LLM_ADJUDICATION_ENABLED",
                "LEARNING_NIGHT_LLM_ADJUDICATION_ENABLED",
            ),
            disabled_names=(
                "LEARNING_POLICY_NIGHT_LLM_ADJUDICATION_DISABLED",
                "LEARNING_NIGHT_LLM_ADJUDICATION_DISABLED",
            ),
        ),
        allowed_model_tiers=_first_env(env, "LEARNING_POLICY_ALLOWED_MODEL_TIERS"),
        external_eval_export_allowed=(
            _coerce_bool(external_eval_export_allowed)
            if external_eval_export_allowed is not None
            else None
        ),
        community_skill_auto_update_policy=_first_env(
            env,
            "LEARNING_POLICY_COMMUNITY_SKILL_AUTO_UPDATE",
        ),
        private_data_redaction_mode=_first_env(env, "LEARNING_POLICY_PRIVATE_DATA_REDACTION"),
    )
    return override.apply_to(policy) if override.to_payload()["values"] else policy


def build_learning_policy(
    *,
    deployment_mode: LearningDeploymentMode | str | None = None,
    env: Mapping[str, str] | None = None,
    org_override: LearningPolicyOverride | None = None,
    tenant_override: LearningPolicyOverride | None = None,
) -> TenantLearningPolicy:
    """Build a deterministic tenant learning policy.

    The builder is pure for a given input mapping. Use
    ``build_learning_policy_from_env`` at runtime when os.environ should be
    consulted.
    """

    env = env or {}
    raw_deployment_mode = (
        deployment_mode
        or _first_env(
            env,
            "LEARNING_POLICY_DEPLOYMENT_MODE",
            "LEARNING_BUDGET_DEPLOYMENT_MODE",
            "ILLO_DEPLOYMENT_MODE",
            "DEPLOYMENT_MODE",
        )
    )
    policy = _defaults_for_deployment(_coerce_deployment_mode(raw_deployment_mode))
    policy = _apply_env_overrides(policy, env)
    if org_override is not None:
        policy = org_override.apply_to(policy)
    if tenant_override is not None:
        policy = tenant_override.apply_to(policy)
    return policy


def build_learning_policy_from_env(
    env: Mapping[str, str] | None = None,
    *,
    org_override: LearningPolicyOverride | None = None,
    tenant_override: LearningPolicyOverride | None = None,
) -> TenantLearningPolicy:
    """Build the policy from process environment values."""

    return build_learning_policy(
        env=os.environ if env is None else env,
        org_override=org_override,
        tenant_override=tenant_override,
    )
