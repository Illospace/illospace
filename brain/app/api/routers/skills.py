"""Skills router — list, CRUD, teach, versions, sparklines, import/export."""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import can_manage_skills
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.db_utils import run_db
from brain.platform.db.models.skill import Skill
from brain.platform.db.models.skill_bundle import SkillAsset, SkillBundle, SkillBundleVersion, SkillInstallation
from brain.platform.db.repositories.skill_bundles import SkillBundleRepository
from brain.platform.db.repositories.skills import SkillRepository
from brain.platform.db.schemas.skills import (
    SkillAssetContentRead,
    SkillAssetRead,
    SkillCreate,
    SkillEnhancedRead,
    SkillExport,
    SkillPackageRead,
    SkillProgressiveLoadingRead,
    SkillRead,
    SkillUpdate,
)
from brain.platform.db.services.skill_bundle_io import SkillBundleIOService

router = APIRouter(
    prefix="/api/skills",
    tags=["skills"],
    dependencies=[Depends(rate_limit)],
)

SkillPackageRows = tuple[
    SkillInstallation | None,
    SkillBundleVersion | None,
    SkillBundle | None,
    list[SkillAsset],
]


# ── Request bodies ──────────────────────────────────────────────

class GuardrailBody(BaseModel):
    text: str
    severity: str = "warning"


class ProcedureStepBody(BaseModel):
    text: str
    position: str = "end"  # "end" or "start"


class FlagExecutionBody(BaseModel):
    execution_id: int
    correction: str = ""


class TriggerBody(BaseModel):
    direction: str  # "for" or "not_for"
    pattern: str


class SkillAssetWriteBody(BaseModel):
    path: str | None = None
    content: str = ""
    asset_kind: str | None = None
    mime_type: str | None = None
    loading_budget_tokens: int | None = None


def _require_skills_manage(user: dict[str, Any]) -> None:
    if not can_manage_skills(user):
        raise HTTPException(status_code=403, detail="Permission denied")


async def _ensure_builtin_skill_catalog() -> None:
    from brain.systems.skills.builtin import ensure_builtin_skills_cached

    await ensure_builtin_skills_cached()


def _needs_attention(skill: Skill) -> bool:
    use_count = int(skill.use_count or 0)
    failure_count = int(skill.failure_count or 0)
    confidence = float(skill.confidence or 0)
    return (
        (confidence < 0.55 and use_count >= 3)
        or failure_count > 2
        or not (skill.procedure or "").strip()
        or (use_count >= 5 and len(skill.guardrails or []) == 0)
    )


def _asset_read(asset: SkillAsset) -> SkillAssetRead:
    return SkillAssetRead(
        id=asset.id,
        path=asset.path,
        asset_kind=asset.asset_kind,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        content_digest=asset.content_digest,
        storage_kind=asset.storage_kind,
        storage_uri=asset.storage_uri,
        loading_budget_tokens=asset.loading_budget_tokens,
        has_inline_content=asset.content_text is not None,
    )


def _safe_asset_path(path: str) -> str:
    candidate = (path or "").strip()
    if not candidate:
        raise HTTPException(status_code=422, detail="Asset path is required")
    if "\\" in candidate:
        raise HTTPException(status_code=422, detail="Asset path must use POSIX separators")
    pure = PurePosixPath(candidate)
    if pure.is_absolute() or ".." in pure.parts:
        raise HTTPException(status_code=422, detail="Asset path cannot be absolute or contain traversal")
    return str(pure)


def _truncate(value: str | None, max_chars: int) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    if max_chars <= 0 or len(value) <= max_chars:
        return value, False
    return value[:max_chars], True


def _asset_counts(assets: list[SkillAsset]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for asset in assets:
        counts[asset.asset_kind] = counts.get(asset.asset_kind, 0) + 1
    return counts


def _load_installation_for_skill(db: Session, skill: Skill) -> SkillInstallation | None:
    if skill.skill_installation_id:
        installation = db.get(SkillInstallation, skill.skill_installation_id)
        if installation is not None:
            return installation
    if not skill.bundle_version_id:
        return None
    if not skill.id:
        return None
    stmt = (
        select(SkillInstallation)
        .where(
            SkillInstallation.skill_id == skill.id,
            (SkillInstallation.archived == False) | SkillInstallation.archived.is_(None),  # noqa: E712
        )
        .order_by(SkillInstallation.id.desc())
    )
    return db.scalars(stmt).first()


def _load_package_rows(
    db: Session,
    skill: Skill,
) -> SkillPackageRows:
    installation = _load_installation_for_skill(db, skill)
    version_id = skill.bundle_version_id or (installation.bundle_version_id if installation else None)
    version = db.get(SkillBundleVersion, version_id) if version_id else None
    bundle_id = version.bundle_id if version else (installation.bundle_id if installation else None)
    bundle = db.get(SkillBundle, bundle_id) if bundle_id else None
    assets = []
    if version is not None:
        assets = list(SkillBundleRepository(db).list_assets(version.id))
    return installation, version, bundle, assets


def _package_for_skill(
    db: Session,
    skill: Skill,
    *,
    include_manifest: bool = False,
    package_rows: SkillPackageRows | None = None,
) -> SkillPackageRead:
    installation, version, bundle, assets = package_rows or _load_package_rows(db, skill)
    if version is None or bundle is None:
        return SkillPackageRead(
            package_kind="legacy_db",
            is_bundle_backed=False,
            package_name=skill.name,
            display_name=skill.name.replace("-", " ").replace("_", " ").title(),
            description=skill.description,
            source_kind=skill.source_kind or "legacy_db",
            trust_level=skill.trust_level or "private_local",
            bundle_version_id=skill.bundle_version_id,
            bundle_digest=skill.bundle_digest,
            effective_digest=skill.effective_digest or skill.bundle_digest,
            overlay_revision=skill.overlay_revision,
        )

    return SkillPackageRead(
        package_kind="bundle",
        is_bundle_backed=True,
        namespace=bundle.namespace,
        package_name=bundle.name,
        display_name=bundle.display_name or skill.name.replace("-", " ").replace("_", " ").title(),
        description=bundle.description or skill.description,
        source_kind=bundle.source_kind or skill.source_kind or "local",
        trust_level=bundle.trust_level or skill.trust_level or "private_local",
        visibility=bundle.visibility,
        bundle_id=bundle.id,
        bundle_version_id=version.id,
        semver=version.semver,
        bundle_digest=version.content_digest,
        effective_digest=skill.effective_digest or version.content_digest,
        overlay_revision=skill.overlay_revision,
        installation_id=installation.id if installation else skill.skill_installation_id,
        enabled=installation.enabled if installation else None,
        enabled_scope=installation.enabled_scope if installation else None,
        pinned=installation.pinned if installation else None,
        update_policy=installation.update_policy if installation else None,
        review_status=installation.review_status if installation else version.status,
        rollback_bundle_version_id=installation.rollback_bundle_version_id if installation else None,
        asset_count=len(assets),
        asset_counts=_asset_counts(assets),
        assets=[_asset_read(asset) for asset in assets],
        permissions=version.permissions or {},
        routing_card=version.routing_card or {},
        compatibility=version.compatibility or {},
        eval_summary=version.eval_summary or {},
        manifest=(version.manifest or {}) if include_manifest else {},
    )


def _progressive_loading_for_skill(
    db: Session,
    skill: Skill,
    *,
    package_rows: SkillPackageRows | None = None,
) -> SkillProgressiveLoadingRead:
    _, version, _, assets = package_rows or _load_package_rows(db, skill)
    available_sections = ["card", "summary", "metadata"]
    if skill.procedure:
        available_sections.insert(2, "procedure")
    for section_name, value in (
        ("pitfalls", skill.pitfalls),
        ("triggers", skill.triggers),
        ("guardrails", skill.guardrails),
        ("graduated_steps", skill.graduated_steps),
    ):
        if value:
            available_sections.append(section_name)
    available_assets = [asset.path for asset in assets]
    return SkillProgressiveLoadingRead(
        available_sections=available_sections,
        available_assets=available_assets,
        load_tools={
            "catalog": {
                "tool": "brain_skills",
                "arguments": {"task": "..."},
                "description": "Level 0: low-token catalog card used for routing.",
            },
            "card": {
                "tool": "skill_view",
                "arguments": {"name": skill.name, "section": "card"},
                "description": "Level 0: just the skill name and description.",
            },
            "summary": {
                "tool": "skill_view",
                "arguments": {"name": skill.name, "section": "summary"},
                "description": "Level 1: compact skill context before loading the full procedure.",
            },
            "sections": {
                "tool": "skill_view",
                "arguments": {"name": skill.name, "section": "procedure"},
                "description": "Level 2: load one named skill section on demand.",
            },
            "assets": {
                "tool": "skill_asset",
                "arguments": {"name": skill.name, "path": available_assets[0] if available_assets else "references/..."},
                "description": "Level 2: load one package file only when needed.",
                "available": version is not None and bool(available_assets),
            },
        },
    )


def _enhanced_skill(db: Session, skill: Skill) -> SkillEnhancedRead:
    package_rows = _load_package_rows(db, skill)
    return SkillEnhancedRead(
        skill=SkillRead.model_validate(skill),
        package=_package_for_skill(db, skill, package_rows=package_rows),
        progressive_loading=_progressive_loading_for_skill(db, skill, package_rows=package_rows),
        needs_attention=_needs_attention(skill),
        editable=True,
        convert_to_bundle_available=not bool(skill.bundle_version_id or skill.skill_installation_id),
    )


# ── Fixed-path routes (MUST come before /{skill_id} / /{skill_name}) ──


@router.get("/", response_model=list[SkillRead])
async def list_skills(
    include_executions: bool = True,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    await _ensure_builtin_skill_catalog()

    def _list(sync_db: Session):
        repo = SkillRepository(sync_db)
        if not include_executions:
            return repo.list_active_for_dashboard()
        return repo.list_active_with_executions()

    return await run_db(db, _list)


@router.post("/new", response_model=SkillRead)
async def create_skill(
    body: SkillCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _create(sync_db: Session):
        _require_skills_manage(user)
        repo = SkillRepository(sync_db)
        existing = repo.get_by_name(body.name)
        if existing:
            raise HTTPException(status_code=409, detail=f"Skill '{body.name}' already exists")
        skill = repo.create(
            **body.model_dump(exclude_none=True),
        )
        sync_db.flush()
        return skill

    return await run_db(db, _create)


@router.get("/export", response_model=list[SkillExport])
async def export_skills(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    await _ensure_builtin_skill_catalog()

    def _export(sync_db: Session):
        repo = SkillRepository(sync_db)
        return repo.list_active()

    return await run_db(db, _export)


@router.post("/import", response_model=list[SkillRead])
async def import_skills(
    body: list[SkillExport],
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _import(sync_db: Session):
        _require_skills_manage(user)
        repo = SkillRepository(sync_db)
        imported = []
        for item in body:
            existing = repo.get_by_name(item.name)
            if existing:
                continue
            skill = repo.create(**item.model_dump(exclude_none=True))
            imported.append(skill)
        sync_db.flush()
        return imported

    return await run_db(db, _import)


@router.get("/needing-attention", response_model=list[SkillRead])
async def needing_attention(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    await _ensure_builtin_skill_catalog()

    def _list(sync_db: Session):
        repo = SkillRepository(sync_db)
        return repo.needing_attention()

    return await run_db(db, _list)


@router.get("/enhanced", response_model=list[SkillEnhancedRead])
async def list_enhanced_skills(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    await _ensure_builtin_skill_catalog()

    def _list(sync_db: Session):
        repo = SkillRepository(sync_db)
        return [_enhanced_skill(sync_db, skill) for skill in repo.list_active()]

    return await run_db(db, _list)


# ── Path-parameter routes (AFTER fixed paths) ──


@router.patch("/{skill_id}", response_model=SkillRead)
async def update_skill_tiers(
    skill_id: int,
    body: SkillUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _update(sync_db: Session):
        _require_skills_manage(user)
        repo = SkillRepository(sync_db)
        try:
            updates = body.model_dump(exclude_unset=True)
            if not updates:
                raise HTTPException(status_code=422, detail="No fields to update")
            skill = repo.update_full(skill_id, **updates)
            return skill
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    return await run_db(db, _update)


@router.post("/{skill_id}/archive", response_model=SkillRead)
async def archive_skill(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _archive(sync_db: Session):
        _require_skills_manage(user)
        repo = SkillRepository(sync_db)
        try:
            return repo.archive(skill_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")

    return await run_db(db, _archive)


@router.delete("/{skill_id}", response_model=SkillRead)
async def delete_skill(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _delete(sync_db: Session):
        _require_skills_manage(user)
        repo = SkillRepository(sync_db)
        try:
            return repo.archive(skill_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")

    return await run_db(db, _delete)


@router.put("/{skill_id}/edit", response_model=SkillRead)
async def edit_skill(
    skill_id: int,
    body: SkillUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _edit(sync_db: Session):
        _require_skills_manage(user)
        repo = SkillRepository(sync_db)
        try:
            updates = body.model_dump(exclude_unset=True)
            if not updates:
                raise HTTPException(status_code=422, detail="No fields to update")
            skill = repo.update_full(skill_id, **updates)
            return skill
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    return await run_db(db, _edit)


@router.get("/{skill_id}/package", response_model=SkillPackageRead)
async def get_skill_package(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _get(sync_db: Session):
        repo = SkillRepository(sync_db)
        try:
            skill = repo.get_or_raise(skill_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")
        return _package_for_skill(sync_db, skill, include_manifest=True)

    return await run_db(db, _get)


@router.get("/{skill_id}/assets", response_model=list[SkillAssetRead])
async def list_skill_assets(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _list(sync_db: Session):
        repo = SkillRepository(sync_db)
        try:
            skill = repo.get_or_raise(skill_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")
        _, version, _, assets = _load_package_rows(sync_db, skill)
        if version is None:
            return []
        return [_asset_read(asset) for asset in assets]

    return await run_db(db, _list)


@router.post("/{skill_id}/assets", response_model=SkillAssetContentRead)
async def upsert_skill_asset(
    skill_id: int,
    body: SkillAssetWriteBody,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _upsert(sync_db: Session):
        _require_skills_manage(user)
        if not body.path:
            raise HTTPException(status_code=422, detail="Asset path is required")
        service = SkillBundleIOService(SkillRepository(sync_db), SkillBundleRepository(sync_db))
        try:
            asset = service.upsert_skill_asset(
                skill_id,
                path=body.path,
                content=body.content,
                asset_kind=body.asset_kind,
                mime_type=body.mime_type,
                loading_budget_tokens=body.loading_budget_tokens,
                org_id=user.get("org_id"),
                user_id=user.get("id"),
                installed_by_user_id=user.get("id"),
            )
            sync_db.flush()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc) or "Skill not found")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return SkillAssetContentRead(
            **_asset_read(asset).model_dump(),
            content=asset.content_text,
            truncated=False,
        )

    return await run_db(db, _upsert)


@router.put("/{skill_id}/assets/{asset_path:path}", response_model=SkillAssetContentRead)
async def replace_skill_asset(
    skill_id: int,
    asset_path: str,
    body: SkillAssetWriteBody,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _replace(sync_db: Session):
        _require_skills_manage(user)
        service = SkillBundleIOService(SkillRepository(sync_db), SkillBundleRepository(sync_db))
        try:
            asset = service.upsert_skill_asset(
                skill_id,
                path=asset_path,
                content=body.content,
                asset_kind=body.asset_kind,
                mime_type=body.mime_type,
                loading_budget_tokens=body.loading_budget_tokens,
                org_id=user.get("org_id"),
                user_id=user.get("id"),
                installed_by_user_id=user.get("id"),
            )
            sync_db.flush()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc) or "Skill not found")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return SkillAssetContentRead(
            **_asset_read(asset).model_dump(),
            content=asset.content_text,
            truncated=False,
        )

    return await run_db(db, _replace)


@router.get("/{skill_id}/assets/{asset_path:path}", response_model=SkillAssetContentRead)
async def get_skill_asset(
    skill_id: int,
    asset_path: str,
    max_chars: int = 12000,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _get(sync_db: Session):
        repo = SkillRepository(sync_db)
        safe_path = _safe_asset_path(asset_path)
        try:
            skill = repo.get_or_raise(skill_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")
        _, version, _, assets = _load_package_rows(sync_db, skill)
        if version is None:
            raise HTTPException(status_code=404, detail="Skill is not backed by a bundle")
        asset = next((item for item in assets if item.path == safe_path), None)
        if asset is None:
            raise HTTPException(status_code=404, detail="Skill asset not found")
        content, truncated = _truncate(asset.content_text, max_chars)
        return SkillAssetContentRead(
            **_asset_read(asset).model_dump(),
            content=content,
            truncated=truncated,
        )

    return await run_db(db, _get)


@router.delete("/{skill_id}/assets/{asset_path:path}", response_model=SkillPackageRead)
async def delete_skill_asset(
    skill_id: int,
    asset_path: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _delete(sync_db: Session):
        _require_skills_manage(user)
        service = SkillBundleIOService(SkillRepository(sync_db), SkillBundleRepository(sync_db))
        try:
            skill = service.delete_skill_asset(
                skill_id,
                path=asset_path,
                org_id=user.get("org_id"),
                user_id=user.get("id"),
                installed_by_user_id=user.get("id"),
            )
            sync_db.flush()
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc) or "Skill asset not found")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return _package_for_skill(sync_db, skill, include_manifest=True)

    return await run_db(db, _delete)


@router.post("/{skill_id}/convert-to-bundle", response_model=SkillRead)
async def convert_skill_to_bundle(
    skill_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _convert(sync_db: Session):
        _require_skills_manage(user)
        skill_repo = SkillRepository(sync_db)
        service = SkillBundleIOService(skill_repo, SkillBundleRepository(sync_db))
        try:
            skill = service.ensure_skill_bundle(
                skill_id,
                org_id=user.get("org_id"),
                user_id=user.get("id"),
                installed_by_user_id=user.get("id"),
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")
        sync_db.flush()
        return skill

    return await run_db(db, _convert)


@router.get("/{skill_name}/versions")
async def get_versions(
    skill_name: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _get(sync_db: Session):
        repo = SkillRepository(sync_db)
        try:
            versions = repo.get_versions(skill_name)
            return [
                {
                    "id": v.id,
                    "version": v.version,
                    "procedure": v.procedure,
                    "changed_by": v.changed_by,
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                }
                for v in versions
            ]
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")

    return await run_db(db, _get)


@router.post("/{skill_name}/versions/{version}/restore", response_model=SkillRead)
async def restore_version(
    skill_name: str,
    version: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _restore(sync_db: Session):
        _require_skills_manage(user)
        repo = SkillRepository(sync_db)
        try:
            skill = repo.get_by_name_or_raise(skill_name)
            versions = repo.get_versions(skill_name)
            target = next((v for v in versions if v.version == version), None)
            if not target:
                raise HTTPException(status_code=404, detail=f"Version {version} not found")
            skill = repo.update_full(skill.id, procedure=target.procedure)
            return skill
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")

    return await run_db(db, _restore)


@router.get("/{skill_name}/executions")
async def get_executions(
    skill_name: str,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _get(sync_db: Session):
        repo = SkillRepository(sync_db)
        try:
            execs = repo.get_sparkline(skill_name, limit=limit)
            return [
                {
                    "id": e.id,
                    "task_description": e.task_description,
                    "outcome": e.outcome,
                    "duration_sec": e.duration_sec,
                    "started_at": e.started_at.isoformat() if e.started_at else None,
                    "rework_rounds": e.rework_rounds,
                    "flagged": e.flagged,
                }
                for e in execs
            ]
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")

    return await run_db(db, _get)


@router.get("/{skill_name}/sparkline")
async def get_sparkline(
    skill_name: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _get(sync_db: Session):
        repo = SkillRepository(sync_db)
        try:
            execs = repo.get_sparkline(skill_name, limit=30)
            return [
                {"outcome": e.outcome, "started_at": e.started_at.isoformat() if e.started_at else None}
                for e in reversed(execs)
            ]
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")

    return await run_db(db, _get)


@router.post("/{skill_name}/guardrail", response_model=SkillRead)
async def add_guardrail(
    skill_name: str,
    body: GuardrailBody,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _add(sync_db: Session):
        _require_skills_manage(user)
        repo = SkillRepository(sync_db)
        try:
            return repo.add_guardrail(skill_name, body.text, body.severity)
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")

    return await run_db(db, _add)


@router.post("/{skill_name}/procedure-step", response_model=SkillRead)
async def add_procedure_step(
    skill_name: str,
    body: ProcedureStepBody,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _add(sync_db: Session):
        _require_skills_manage(user)
        repo = SkillRepository(sync_db)
        try:
            skill = repo.get_by_name_or_raise(skill_name)
            proc = skill.procedure or ""
            step_line = f"\n- {body.text}" if proc else f"- {body.text}"
            if body.position == "start":
                new_proc = step_line.lstrip("\n") + ("\n" + proc if proc else "")
            else:
                new_proc = proc + step_line
            skill = repo.update_full(skill.id, procedure=new_proc)
            return skill
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")

    return await run_db(db, _add)


@router.post("/{skill_name}/flag-execution")
async def flag_execution(
    skill_name: str,
    body: FlagExecutionBody,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _flag(sync_db: Session):
        from brain.platform.db.models.skill import SkillExecution

        repo = SkillRepository(sync_db)
        try:
            repo.get_by_name_or_raise(skill_name)
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")

        execution = sync_db.get(SkillExecution, body.execution_id)
        if not execution:
            raise HTTPException(status_code=404, detail="Execution not found")
        execution.flagged = True
        if body.correction:
            execution.operator_feedback = body.correction
        sync_db.flush()
        return {"ok": True, "execution_id": execution.id}

    return await run_db(db, _flag)


@router.post("/{skill_name}/trigger", response_model=SkillRead)
async def add_trigger(
    skill_name: str,
    body: TriggerBody,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _add(sync_db: Session):
        _require_skills_manage(user)
        repo = SkillRepository(sync_db)
        try:
            return repo.add_trigger(skill_name, body.direction, body.pattern)
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")

    return await run_db(db, _add)


@router.delete("/{skill_name}/trigger/{index}", response_model=SkillRead)
async def remove_trigger(
    skill_name: str,
    index: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _remove(sync_db: Session):
        _require_skills_manage(user)
        repo = SkillRepository(sync_db)
        try:
            return repo.remove_trigger(skill_name, index)
        except LookupError:
            raise HTTPException(status_code=404, detail="Skill not found")
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    return await run_db(db, _remove)
