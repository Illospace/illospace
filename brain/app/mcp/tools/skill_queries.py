"""Database query helpers for MCP skill planning."""
from __future__ import annotations

from typing import Any

from sqlalchemy import text


async def fetch_skill_count(uow: Any, *, session_execute: Any) -> int:
    result = await session_execute(uow.session, text(
        "SELECT COUNT(*) as cnt FROM skills WHERE NOT archived"
    ))
    return int(result.mappings().one()["cnt"])


async def fetch_skill_card(uow: Any, name: str, *, session_execute: Any) -> Any:
    result = await session_execute(uow.session, text("""
        SELECT id, name, description, version, maturity, confidence, use_count,
               success_count::float / GREATEST(use_count, 1) as success_rate,
               model_tier, thinking_tier, pitfalls, triggers,
               bundle_version_id, bundle_digest, overlay_revision,
               effective_digest, source_kind, trust_level,
               1.0 as skill_match,
               NULL as centroid_match,
               centroid_count
        FROM skills
        WHERE NOT archived
          AND skill_type != 'meta'
          AND name = :name
        LIMIT 1
    """), {"name": name})
    return result.mappings().first()


async def fetch_matching_skills(
    uow: Any,
    *,
    emb_str: str | None,
    skill_count: int,
    small_skillset_threshold: int,
    session_execute: Any,
) -> list[Any]:
    if emb_str is None:
        limit_clause = "" if skill_count <= small_skillset_threshold else "LIMIT 15"
        result = await session_execute(uow.session, text(f"""
            SELECT id, name, description, version, maturity, confidence, use_count,
                   success_count::float / GREATEST(use_count, 1) as success_rate,
                   model_tier, thinking_tier, pitfalls, triggers,
                   bundle_version_id, bundle_digest, overlay_revision,
                   effective_digest, source_kind, trust_level,
                   0.0 as skill_match,
                   NULL as centroid_match,
                   centroid_count
            FROM skills
            WHERE NOT archived AND skill_type != 'meta'
            ORDER BY use_count DESC, confidence DESC, name ASC
            {limit_clause}
        """))
        return result.mappings().all()

    if skill_count <= small_skillset_threshold:
        result = await session_execute(uow.session, text("""
            SELECT id, name, description, version, maturity, confidence, use_count,
                   success_count::float / GREATEST(use_count, 1) as success_rate,
                   model_tier, thinking_tier, pitfalls, triggers,
                   bundle_version_id, bundle_digest, overlay_revision,
                   effective_digest, source_kind, trust_level,
                   1 - (embedding <=> CAST(:emb1 AS vector)) as skill_match,
                   CASE WHEN task_centroid IS NOT NULL
                        THEN 1 - (task_centroid <=> CAST(:emb2 AS vector)) ELSE NULL
                   END as centroid_match,
                   centroid_count
            FROM skills
            WHERE NOT archived AND skill_type != 'meta'
            ORDER BY
                CASE WHEN centroid_count >= 3
                     THEN (1 - (task_centroid <=> CAST(:emb3 AS vector))) * 0.7 + (1 - (embedding <=> CAST(:emb4 AS vector))) * 0.3
                     ELSE 1 - (embedding <=> CAST(:emb5 AS vector))
                END DESC
        """), {"emb1": emb_str, "emb2": emb_str, "emb3": emb_str, "emb4": emb_str, "emb5": emb_str})
        return result.mappings().all()

    result = await session_execute(uow.session, text("""
        SELECT id, name, description, version, maturity, confidence, use_count,
               success_count::float / GREATEST(use_count, 1) as success_rate,
               model_tier, thinking_tier, pitfalls, triggers,
               bundle_version_id, bundle_digest, overlay_revision,
               effective_digest, source_kind, trust_level,
               CASE WHEN embedding IS NOT NULL
                    THEN 1 - (embedding <=> CAST(:emb1 AS vector))
                    ELSE 0.35
               END as skill_match,
               CASE WHEN task_centroid IS NOT NULL
                    THEN 1 - (task_centroid <=> CAST(:emb2 AS vector)) ELSE NULL
               END as centroid_match,
               centroid_count
        FROM skills
        WHERE NOT archived AND skill_type != 'meta' AND (embedding IS NOT NULL OR builtin IS TRUE)
        ORDER BY
            CASE WHEN embedding IS NULL
                 THEN 0.35
                 WHEN centroid_count >= 3
                 THEN (1 - (task_centroid <=> CAST(:emb3 AS vector))) * 0.7 + (1 - (embedding <=> CAST(:emb4 AS vector))) * 0.3
                 ELSE 1 - (embedding <=> CAST(:emb5 AS vector))
            END DESC
        LIMIT 15
    """), {"emb1": emb_str, "emb2": emb_str, "emb3": emb_str, "emb4": emb_str, "emb5": emb_str})
    return result.mappings().all()


async def fetch_asset_paths_by_version(
    uow: Any,
    matching_skills: list[Any],
    *,
    maybe_await: Any,
) -> dict[int, list[dict[str, Any]]]:
    asset_paths_by_version: dict[int, list[dict[str, Any]]] = {}
    bundle_version_ids = {
        int(skill["bundle_version_id"])
        for skill in matching_skills
        if skill.get("bundle_version_id")
    }
    for version_id in bundle_version_ids:
        try:
            assets = await maybe_await(uow.skill_bundles.list_assets(version_id))
        except Exception:
            continue
        asset_paths_by_version[version_id] = [
            {
                "path": asset.path,
                "kind": asset.asset_kind,
                "mime_type": asset.mime_type,
            }
            for asset in assets
        ]
    return asset_paths_by_version
