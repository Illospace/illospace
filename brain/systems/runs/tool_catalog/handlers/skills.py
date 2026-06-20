"""Skills orchestration tool handlers."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from brain.systems.runs.tool_catalog.handlers.common import *


_SKILL_UPDATE_FIELDS = (
    "name",
    "description",
    "procedure",
    "thinking_tier",
    "pitfalls",
    "refinements",
    "triggers",
    "guardrails",
)

_MAX_CREATE_MANY_SKILLS = 50


def _skill_error(message: str, *, hint: str | None = None, action: str | None = None) -> str:
    payload: dict[str, Any] = {"ok": False, "error": message}
    if action:
        payload["action"] = action
    if hint:
        payload["hint"] = hint
    return json.dumps(payload, default=str)


def _skill_payload(skill: Any, *, include_procedure: bool = True) -> dict[str, Any]:
    payload = {
        "id": getattr(skill, "id", None),
        "name": getattr(skill, "name", None),
        "description": getattr(skill, "description", None),
        "version": getattr(skill, "version", None),
        "skill_type": getattr(skill, "skill_type", None),
        "maturity": getattr(skill, "maturity", None),
        "confidence": getattr(skill, "confidence", None),
        "use_count": getattr(skill, "use_count", None),
        "success_count": getattr(skill, "success_count", None),
        "failure_count": getattr(skill, "failure_count", None),
        "partial_count": getattr(skill, "partial_count", None),
        "triggers": getattr(skill, "triggers", None) or [],
        "guardrails": getattr(skill, "guardrails", None) or [],
        "pitfalls": getattr(skill, "pitfalls", None) or [],
        "refinements": getattr(skill, "refinements", None) or [],
        "thinking_tier": getattr(skill, "thinking_tier", None),
        "builtin": bool(getattr(skill, "builtin", False)),
        "archived": bool(getattr(skill, "archived", False)),
        "skill_installation_id": getattr(skill, "skill_installation_id", None),
        "bundle_version_id": getattr(skill, "bundle_version_id", None),
        "bundle_digest": getattr(skill, "bundle_digest", None),
        "effective_digest": getattr(skill, "effective_digest", None),
        "source_kind": getattr(skill, "source_kind", None),
        "trust_level": getattr(skill, "trust_level", None),
    }
    if include_procedure:
        payload["procedure"] = getattr(skill, "procedure", None)
    return payload


def _asset_payload(asset: Any, *, include_content: bool = False, max_chars: int = 12000) -> dict[str, Any]:
    payload = {
        "id": getattr(asset, "id", None),
        "path": getattr(asset, "path", None),
        "asset_kind": getattr(asset, "asset_kind", None),
        "mime_type": getattr(asset, "mime_type", None),
        "size_bytes": getattr(asset, "size_bytes", None),
        "content_digest": getattr(asset, "content_digest", None),
        "storage_kind": getattr(asset, "storage_kind", None),
        "storage_uri": getattr(asset, "storage_uri", None),
        "loading_budget_tokens": getattr(asset, "loading_budget_tokens", None),
        "bundle_version_id": getattr(asset, "bundle_version_id", None),
    }
    if include_content:
        content = getattr(asset, "content_text", None)
        if content is not None and max_chars >= 0 and len(content) > max_chars:
            payload["content"] = content[:max_chars]
            payload["truncated"] = True
        else:
            payload["content"] = content
            payload["truncated"] = False
    return payload


def _coerce_limit(limit: int | None, *, default: int = 50, maximum: int = 200) -> int:
    try:
        value = int(limit if limit is not None else default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def _safe_skill_asset_path(path: str | None) -> str:
    candidate = str(path or "").strip()
    if not candidate:
        raise ValueError("Asset path is required")
    if "\\" in candidate:
        raise ValueError("Asset path must use POSIX separators")
    pure = PurePosixPath(candidate)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError("Asset path cannot be absolute or contain traversal")
    return str(pure)


def _resolve_skill(repo: Any, *, skill_id: int | None = None, skill_name: str | None = None) -> Any:
    if skill_id is not None:
        return repo.get_or_raise(int(skill_id))
    name = str(skill_name or "").strip()
    if name:
        return repo.get_by_name_or_raise(name)
    raise ValueError("skill_id or skill_name is required")


async def _async_resolve_skill(repo: Any, *, skill_id: int | None = None, skill_name: str | None = None) -> Any:
    if skill_id is not None:
        return await repo.a_get_or_raise(int(skill_id))
    name = str(skill_name or "").strip()
    if name:
        return await repo.a_get_by_name_or_raise(name)
    raise ValueError("skill_id or skill_name is required")


def _validate_skill_runtime(fields: dict[str, Any]) -> dict[str, Any]:
    if "thinking_tier" in fields and fields["thinking_tier"] not in _REASONING_EFFORTS:
        raise ValueError(
            f"Invalid thinking_tier: {fields['thinking_tier']}. "
            f"Use one of: {', '.join(sorted(_REASONING_EFFORTS))}"
        )
    return fields


async def _handle_create_skill(
    name: str,
    description: str,
    procedure: str,
    user_requested: bool = False,
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
        from brain.systems.runtime_settings.memory import async_get_embedding_runtime_config

        source_kind = "private_local" if user_requested else "agent_draft"
        trust_level = "private_local" if user_requested else "agent_draft"

        # Atomic upsert — INSERT ... ON CONFLICT avoids race conditions
        from sqlalchemy import text as sa_text
        from brain.platform.db.repositories.skill_bundles import SkillBundleRepository
        from brain.platform.db.repositories.skills import SkillRepository
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.platform.db.services.skill_bundle_io import AsyncSkillBundleIOService

        asset_specs = assets or []
        async with UnitOfWork() as uow:
            emb_text = f"{name}: {description}"
            runtime_config = await async_get_embedding_runtime_config(uow.session, include_secret=True)
            embedding = embed_document(emb_text, runtime_config=runtime_config)

            row = (await uow.session.execute(sa_text("""
                INSERT INTO skills
                    (name, description, procedure, level, thinking_tier,
                     provisional, auto_emerged, embedding,
                     skill_type, source_kind, trust_level,
                     triggers, guardrails, pitfalls, refinements)
                VALUES (:name, :desc, :proc, 'cognitive', :thinking_tier,
                        :provisional, FALSE, CAST(:embedding AS vector),
                        'skill', :source_kind, :trust_level,
                        CAST(:triggers AS jsonb), CAST(:guardrails AS jsonb),
                        CAST(:pitfalls AS jsonb), CAST(:refinements AS jsonb))
                ON CONFLICT (name) DO NOTHING
                RETURNING id
            """), {
                "name": name, "desc": description, "proc": procedure,
                "thinking_tier": thinking_tier,
                "provisional": provisional, "embedding": vec_to_pg(embedding),
                "source_kind": source_kind, "trust_level": trust_level,
                "triggers": json.dumps(triggers or []),
                "guardrails": json.dumps(guardrails or []),
                "pitfalls": json.dumps(pitfalls or []),
                "refinements": json.dumps(refinements or []),
            })).mappings().first()
            if row is None:
                return {
                    "created": False,
                    "error": f"Skill '{name}' already exists",
                    "hint": "Choose a different name or update the existing skill.",
                }
            skill_id = row["id"]
            if create_as_package or asset_specs:
                service = AsyncSkillBundleIOService(
                    SkillRepository(uow.session),
                    SkillBundleRepository(uow.session),
                )
                await service.ensure_skill_bundle(
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
                    await service.upsert_skill_asset(
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
            "thinking_tier": thinking_tier,
            "source_kind": source_kind,
            "trust_level": trust_level,
            "package_created": bool(create_as_package or asset_specs),
            "asset_count": len(asset_specs),
        }

    except Exception as e:
        logger.error("Failed to create skill '%s': %s", name, e)
        return {"created": False, "error": str(e)}


async def _handle_create_many_skills(
    skill_specs: list | None,
    *,
    thinking_tier: str = "medium",
    create_as_package: bool = False,
    user_requested: bool = True,
) -> dict:
    """Create several skills in one model-visible tool call."""
    if not isinstance(skill_specs, list) or not skill_specs:
        return {
            "created": False,
            "error": "create_many requires a non-empty skills array",
            "hint": "Pass skills=[{name, description, procedure, ...}, ...].",
        }
    if len(skill_specs) > _MAX_CREATE_MANY_SKILLS:
        return {
            "created": False,
            "error": f"create_many supports at most {_MAX_CREATE_MANY_SKILLS} skills per call",
            "count": len(skill_specs),
        }

    results: list[dict[str, Any]] = []
    for index, spec in enumerate(skill_specs):
        if not isinstance(spec, dict):
            results.append({
                "index": index,
                "ok": False,
                "created": False,
                "error": "Skill spec must be an object",
            })
            continue

        skill_name = str(spec.get("name") or "").strip()
        procedure = spec.get("procedure")
        if not skill_name:
            results.append({
                "index": index,
                "ok": False,
                "created": False,
                "error": "Skill spec requires: name",
            })
            continue
        if procedure is None:
            results.append({
                "index": index,
                "name": skill_name,
                "ok": False,
                "created": False,
                "error": "Skill spec requires: procedure",
            })
            continue

        skill_assets = spec.get("assets")
        if skill_assets is not None and not isinstance(skill_assets, list):
            results.append({
                "index": index,
                "name": skill_name,
                "ok": False,
                "created": False,
                "error": "Skill spec assets must be an array",
            })
            continue

        result = await _handle_create_skill(
            name=skill_name,
            description=str(spec.get("description") or ""),
            procedure=str(procedure),
            user_requested=bool(spec.get("user_requested", user_requested)),
            thinking_tier=str(spec.get("thinking_tier") or thinking_tier or "medium"),
            triggers=spec.get("triggers"),
            guardrails=spec.get("guardrails"),
            pitfalls=spec.get("pitfalls"),
            refinements=spec.get("refinements"),
            assets=skill_assets,
            create_as_package=bool(spec.get("create_as_package", create_as_package)),
        )
        results.append({
            "index": index,
            "ok": bool(result.get("created")),
            **result,
        })

    created_count = sum(1 for item in results if item.get("created"))
    failed_count = len(results) - created_count
    return {
        "created": failed_count == 0,
        "count": len(results),
        "created_count": created_count,
        "failed_count": failed_count,
        "results": results,
    }


async def _handle_manage_skill_asset(
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
        from brain.platform.db.repositories.skill_bundles import SkillBundleRepository
        from brain.platform.db.repositories.skills import SkillRepository
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.platform.db.services.skill_bundle_io import AsyncSkillBundleIOService

        async with UnitOfWork() as uow:
            skills = SkillRepository(uow.session)
            bundles = SkillBundleRepository(uow.session)
            skill = await skills.a_get_by_name(skill_name)
            if skill is None:
                return {"ok": False, "error": f"Skill '{skill_name}' not found"}
            service = AsyncSkillBundleIOService(skills, bundles)
            if action == "delete":
                updated = await service.delete_skill_asset(
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
            asset = await service.upsert_skill_asset(
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


async def _handle_manage_skill(
    action: str,
    operation: str | None = None,
    skill_id: int | None = None,
    skill_name: str | None = None,
    name: str | None = None,
    description: str | None = None,
    procedure: str | None = None,
    thinking_tier: str | None = None,
    triggers: list | None = None,
    guardrails: list | None = None,
    pitfalls: list | None = None,
    refinements: list | None = None,
    assets: list | None = None,
    skills: list | None = None,
    create_as_package: bool = False,
    user_requested: bool = True,
    path: str | None = None,
    content: str = "",
    asset_kind: str | None = None,
    mime_type: str | None = None,
    loading_budget_tokens: int | None = None,
    limit: int = 50,
    max_chars: int = 12000,
    include_archived: bool = False,
) -> str:
    """Umbrella tool for durable skill CRUD and bundle asset edits."""
    normalized = str(action or "").strip().lower()
    if normalized == "help":
        return _manage_tool_guide("manage_skill", operation)
    if normalized == "schema":
        return _manage_tool_guide("manage_skill", operation)
    if not normalized:
        return _skill_error(
            "manage_skill requires: action",
            hint="Use action='help' to inspect available operations.",
        )

    if normalized == "create":
        if not name:
            return _skill_error("create requires: name", action=normalized)
        if procedure is None:
            return _skill_error("create requires: procedure", action=normalized)
        result = await _handle_create_skill(
            name=name,
            description=description or "",
            procedure=procedure,
            user_requested=bool(user_requested),
            thinking_tier=thinking_tier or "medium",
            triggers=triggers,
            guardrails=guardrails,
            pitfalls=pitfalls,
            refinements=refinements,
            assets=assets,
            create_as_package=create_as_package,
        )
        return json.dumps({"ok": bool(result.get("created")), "action": normalized, **result}, default=str)

    if normalized == "create_many":
        result = await _handle_create_many_skills(
            skills,
            thinking_tier=thinking_tier or "medium",
            create_as_package=create_as_package,
            user_requested=bool(user_requested),
        )
        return json.dumps({"ok": bool(result.get("created")), "action": normalized, **result}, default=str)

    try:
        from brain.platform.db.repositories.skill_bundles import SkillBundleRepository
        from brain.platform.db.repositories.skills import SkillRepository
        from brain.platform.db.repositories.unit_of_work import UnitOfWork
        from brain.platform.db.services.skill_bundle_io import AsyncSkillBundleIOService

        async with UnitOfWork() as uow:
            skills = SkillRepository(uow.session)
            bundles = SkillBundleRepository(uow.session)
            if normalized == "list":
                list_limit = _coerce_limit(limit)
                rows = await skills.a_list_all(limit=list_limit) if include_archived else await skills.a_list_active()
                skills = [
                    _skill_payload(skill, include_procedure=False)
                    for skill in list(rows)[:list_limit]
                ]
                return json.dumps(
                    {
                        "ok": True,
                        "action": normalized,
                        "skills": skills,
                        "count": len(skills),
                        "include_archived": include_archived,
                    },
                    default=str,
                )

            if normalized == "get":
                skill = await _async_resolve_skill(skills, skill_id=skill_id, skill_name=skill_name)
                return json.dumps(
                    {"ok": True, "action": normalized, "skill": _skill_payload(skill)},
                    default=str,
                )

            if normalized in {"update", "edit"}:
                skill = await _async_resolve_skill(skills, skill_id=skill_id, skill_name=skill_name)
                raw_fields = {
                    "name": name,
                    "description": description,
                    "procedure": procedure,
                    "thinking_tier": thinking_tier,
                    "pitfalls": pitfalls,
                    "refinements": refinements,
                    "triggers": triggers,
                    "guardrails": guardrails,
                }
                updates = {
                    key: value
                    for key, value in raw_fields.items()
                    if key in _SKILL_UPDATE_FIELDS and value is not None
                }
                if not updates:
                    return _skill_error(
                        "update requires at least one changed field",
                        action=normalized,
                    )
                _validate_skill_runtime(updates)
                updated = await skills.a_update_full(skill.id, **updates)
                return json.dumps(
                    {"ok": True, "action": normalized, "skill": _skill_payload(updated)},
                    default=str,
                )

            if normalized in {"archive", "delete"}:
                skill = await _async_resolve_skill(skills, skill_id=skill_id, skill_name=skill_name)
                archived = await skills.a_archive(skill.id)
                return json.dumps(
                    {
                        "ok": True,
                        "action": normalized,
                        "effect": "archived",
                        "skill": _skill_payload(archived, include_procedure=False),
                    },
                    default=str,
                )

            if normalized == "convert_to_bundle":
                skill = await _async_resolve_skill(skills, skill_id=skill_id, skill_name=skill_name)
                service = AsyncSkillBundleIOService(skills, bundles)
                converted = await service.ensure_skill_bundle(
                    skill.id,
                    namespace="local",
                    user_id=getattr(_agent_context, "user_id", None),
                    org_id=getattr(_agent_context, "org_id", None),
                    installed_by_user_id=getattr(_agent_context, "user_id", None),
                )
                return json.dumps(
                    {"ok": True, "action": normalized, "skill": _skill_payload(converted)},
                    default=str,
                )

            if normalized == "list_assets":
                skill = await _async_resolve_skill(skills, skill_id=skill_id, skill_name=skill_name)
                service = AsyncSkillBundleIOService(skills, bundles)
                _, version, _, asset_rows = await service._load_package_rows(skill)
                asset_limit = _coerce_limit(limit)
                assets_payload = [_asset_payload(asset) for asset in asset_rows[:asset_limit]]
                return json.dumps(
                    {
                        "ok": True,
                        "action": normalized,
                        "skill": _skill_payload(skill, include_procedure=False),
                        "bundle_version_id": getattr(version, "id", None),
                        "assets": assets_payload,
                        "count": len(assets_payload),
                    },
                    default=str,
                )

            if normalized == "get_asset":
                skill = await _async_resolve_skill(skills, skill_id=skill_id, skill_name=skill_name)
                safe_path = _safe_skill_asset_path(path)
                service = AsyncSkillBundleIOService(skills, bundles)
                _, version, _, asset_rows = await service._load_package_rows(skill)
                if version is None:
                    return _skill_error("Skill is not backed by a bundle", action=normalized)
                asset = next((item for item in asset_rows if item.path == safe_path), None)
                if asset is None:
                    return _skill_error(f"Skill asset not found: {safe_path}", action=normalized)
                return json.dumps(
                    {
                        "ok": True,
                        "action": normalized,
                        "skill_id": skill.id,
                        "name": skill.name,
                        "asset": _asset_payload(asset, include_content=True, max_chars=max_chars),
                    },
                    default=str,
                )

            if normalized == "upsert_asset":
                skill = await _async_resolve_skill(skills, skill_id=skill_id, skill_name=skill_name)
                safe_path = _safe_skill_asset_path(path)
                service = AsyncSkillBundleIOService(skills, bundles)
                asset = await service.upsert_skill_asset(
                    skill.id,
                    path=safe_path,
                    content=content,
                    asset_kind=asset_kind,
                    mime_type=mime_type,
                    loading_budget_tokens=loading_budget_tokens,
                    namespace="local",
                    user_id=getattr(_agent_context, "user_id", None),
                    org_id=getattr(_agent_context, "org_id", None),
                    installed_by_user_id=getattr(_agent_context, "user_id", None),
                )
                return json.dumps(
                    {
                        "ok": True,
                        "action": normalized,
                        "skill_id": skill.id,
                        "name": skill.name,
                        "asset": _asset_payload(asset),
                    },
                    default=str,
                )

            if normalized == "delete_asset":
                skill = await _async_resolve_skill(skills, skill_id=skill_id, skill_name=skill_name)
                safe_path = _safe_skill_asset_path(path)
                service = AsyncSkillBundleIOService(skills, bundles)
                updated = await service.delete_skill_asset(
                    skill.id,
                    path=safe_path,
                    namespace="local",
                    user_id=getattr(_agent_context, "user_id", None),
                    org_id=getattr(_agent_context, "org_id", None),
                    installed_by_user_id=getattr(_agent_context, "user_id", None),
                )
                return json.dumps(
                    {
                        "ok": True,
                        "action": normalized,
                        "skill": _skill_payload(updated, include_procedure=False),
                        "path": safe_path,
                    },
                    default=str,
                )

            return _skill_error(
                f"Unknown manage_skill action: {action}",
                hint="Use action='help' to inspect available operations.",
            )
    except LookupError as e:
        return _skill_error(str(e) or "Skill not found", action=normalized)
    except ValueError as e:
        return _skill_error(str(e), action=normalized)
    except Exception as e:
        logger.exception("manage_skill failed: %s", e)
        return _skill_error(str(e), action=normalized)

__all__ = [name for name in globals() if not name.startswith("__")]
