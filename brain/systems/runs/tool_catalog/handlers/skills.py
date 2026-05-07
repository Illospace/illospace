"""Skills orchestration tool handlers."""

from __future__ import annotations

from brain.systems.runs.tool_catalog.handlers.common import *

def _handle_create_skill(
    name: str,
    description: str,
    procedure: str,
    user_requested: bool = False,
    model_tier: str = "medium",
    thinking_tier: str = "medium",
    triggers: list | None = None,
    guardrails: list | None = None,
    pitfalls: list | None = None,
    refinements: list | None = None,
    assets: list | None = None,
    create_as_package: bool = False,
) -> dict:
    """Create a new skill via the live gate (no CLI ceremony required)."""
    from brain.systems.skills.gate import enforce_live_gate

    model_tier = _MODEL_TIER_ALIASES.get(model_tier, model_tier)
    if model_tier not in _MODEL_TIERS:
        return {
            "created": False,
            "error": f"Invalid model_tier: {model_tier}",
            "hint": f"Use one of: {', '.join(sorted(_MODEL_TIERS))}",
        }
    if thinking_tier not in _REASONING_EFFORTS:
        return {
            "created": False,
            "error": f"Invalid thinking_tier: {thinking_tier}",
            "hint": f"Use one of: {', '.join(sorted(_REASONING_EFFORTS))}",
        }

    passed, violations, provisional = enforce_live_gate(
        name, description, procedure, user_requested=user_requested,
    )

    if not passed:
        return {
            "created": False,
            "error": "Skill gate blocked creation",
            "violations": violations,
            "hint": "Fix the violations and try again. Names must be lowercase-hyphenated, "
                    "procedures need concrete steps (numbered or bulleted).",
        }

    try:
        from brain.systems.memory.embeddings import embed_document, vec_to_pg

        source_kind = "private_local" if user_requested else "agent_draft"
        trust_level = "private_local" if user_requested else "agent_draft"

        # Embed before DB work (most expensive part, no need to hold conn)
        emb_text = f"{name}: {description}"
        embedding = embed_document(emb_text)

        # Atomic upsert — INSERT ... ON CONFLICT avoids race conditions
        from sqlalchemy import text as sa_text
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.platform.db.services.skill_bundle_io import SkillBundleIOService

        asset_specs = assets or []
        with UnitOfWork() as uow:
            row = uow.session.execute(sa_text("""
                INSERT INTO skills
                    (name, description, procedure, level, model_tier, thinking_tier,
                     provisional, auto_emerged, embedding,
                     skill_type, source_kind, trust_level,
                     triggers, guardrails, pitfalls, refinements)
                VALUES (:name, :desc, :proc, 'cognitive', :model_tier, :thinking_tier,
                        :provisional, FALSE, CAST(:embedding AS vector),
                        'skill', :source_kind, :trust_level,
                        CAST(:triggers AS jsonb), CAST(:guardrails AS jsonb),
                        CAST(:pitfalls AS jsonb), CAST(:refinements AS jsonb))
                ON CONFLICT (name) DO NOTHING
                RETURNING id
            """), {
                "name": name, "desc": description, "proc": procedure,
                "model_tier": model_tier, "thinking_tier": thinking_tier,
                "provisional": provisional, "embedding": vec_to_pg(embedding),
                "source_kind": source_kind, "trust_level": trust_level,
                "triggers": json.dumps(triggers or []),
                "guardrails": json.dumps(guardrails or []),
                "pitfalls": json.dumps(pitfalls or []),
                "refinements": json.dumps(refinements or []),
            }).mappings().first()
            if row is None:
                return {
                    "created": False,
                    "error": f"Skill '{name}' already exists",
                    "hint": "Choose a different name or update the existing skill.",
                }
            skill_id = row["id"]
            if create_as_package or asset_specs:
                service = SkillBundleIOService(uow.skills, uow.skill_bundles)
                service.ensure_skill_bundle(
                    skill_id,
                    namespace="local",
                    user_id=getattr(_agent_context, "user_id", None),
                    org_id=getattr(_agent_context, "org_id", None),
                    installed_by_user_id=getattr(_agent_context, "user_id", None),
                    trust_level=trust_level,
                    source_kind=source_kind,
                )
                for asset in asset_specs:
                    if not isinstance(asset, dict):
                        raise ValueError("Each skill asset must be an object")
                    service.upsert_skill_asset(
                        skill_id,
                        path=str(asset.get("path") or ""),
                        content=str(asset.get("content") or ""),
                        asset_kind=asset.get("asset_kind"),
                        mime_type=asset.get("mime_type"),
                        loading_budget_tokens=asset.get("loading_budget_tokens"),
                        namespace="local",
                        user_id=getattr(_agent_context, "user_id", None),
                        org_id=getattr(_agent_context, "org_id", None),
                        installed_by_user_id=getattr(_agent_context, "user_id", None),
                    )

        status = "provisional (flagged for review)" if provisional else "active"
        logger.info(
            "Created skill '%s' (id=%s, %s, user_requested=%s)",
            name, skill_id, status, user_requested,
        )

        return {
            "created": True,
            "skill_id": skill_id,
            "name": name,
            "provisional": provisional,
            "status": status,
            "model_tier": model_tier,
            "thinking_tier": thinking_tier,
            "source_kind": source_kind,
            "trust_level": trust_level,
            "package_created": bool(create_as_package or asset_specs),
            "asset_count": len(asset_specs),
        }

    except Exception as e:
        logger.error("Failed to create skill '%s': %s", name, e)
        return {"created": False, "error": str(e)}


def _handle_manage_skill_asset(
    skill_name: str,
    path: str,
    action: str = "upsert",
    content: str = "",
    asset_kind: str | None = None,
    mime_type: str | None = None,
    loading_budget_tokens: int | None = None,
) -> dict:
    """Add, update, or delete a package asset for an installed skill."""
    action = (action or "upsert").strip().lower()
    if action not in {"upsert", "delete"}:
        return {"ok": False, "error": "action must be 'upsert' or 'delete'"}

    try:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.platform.db.services.skill_bundle_io import SkillBundleIOService

        with UnitOfWork() as uow:
            skill = uow.skills.get_by_name(skill_name)
            if skill is None:
                return {"ok": False, "error": f"Skill '{skill_name}' not found"}
            service = SkillBundleIOService(uow.skills, uow.skill_bundles)
            if action == "delete":
                updated = service.delete_skill_asset(
                    skill.id,
                    path=path,
                    user_id=getattr(_agent_context, "user_id", None),
                    org_id=getattr(_agent_context, "org_id", None),
                    installed_by_user_id=getattr(_agent_context, "user_id", None),
                )
                return {
                    "ok": True,
                    "action": "delete",
                    "skill_id": updated.id,
                    "name": updated.name,
                    "path": path,
                    "bundle_version_id": updated.bundle_version_id,
                    "effective_digest": updated.effective_digest,
                }
            asset = service.upsert_skill_asset(
                skill.id,
                path=path,
                content=content,
                asset_kind=asset_kind,
                mime_type=mime_type,
                loading_budget_tokens=loading_budget_tokens,
                user_id=getattr(_agent_context, "user_id", None),
                org_id=getattr(_agent_context, "org_id", None),
                installed_by_user_id=getattr(_agent_context, "user_id", None),
            )
            return {
                "ok": True,
                "action": "upsert",
                "skill_id": skill.id,
                "name": skill.name,
                "path": asset.path,
                "asset_kind": asset.asset_kind,
                "mime_type": asset.mime_type,
                "size_bytes": asset.size_bytes,
                "bundle_version_id": asset.bundle_version_id,
            }
    except Exception as e:
        logger.error("Failed to manage skill asset '%s:%s': %s", skill_name, path, e)
        return {"ok": False, "error": str(e)}

__all__ = [name for name in globals() if not name.startswith("__")]
