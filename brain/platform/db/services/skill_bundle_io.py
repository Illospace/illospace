"""Skill bundle import/export service."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from brain.platform.db.models.skill import Skill
from brain.platform.db.models.skill_bundle import (
    SkillAsset,
    SkillBundle as SkillBundleModel,
    SkillBundleVersion,
    SkillInstallation,
)
from brain.platform.db.repositories.skill_bundles import SkillBundleRepository
from brain.platform.db.repositories.skills import SkillRepository
from brain.systems.skills.bundles import (
    ASSET_KINDS,
    MAX_INLINE_TEXT_BYTES,
    SKILL_FILENAME,
    SkillBundle as ParsedSkillBundle,
    SkillBundleAsset,
    compute_bundle_digest,
    load_skill_bundle,
    validate_skill_bundle_manifest_payload,
)


class SkillBundleIOService:
    """Install filesystem bundles into DB-backed runtime skill projections."""

    def __init__(
        self,
        skill_repo: SkillRepository,
        bundle_repo: SkillBundleRepository,
    ) -> None:
        self._skills = skill_repo
        self._bundles = bundle_repo

    def import_bundle(
        self,
        bundle_dir: str | Path,
        *,
        namespace: str = "local",
        org_id: str | None = None,
        user_id: str | None = None,
        installed_by_user_id: str | None = None,
        enabled_scope: str = "user",
        update_policy: str = "manual",
        review_status: str = "approved",
        trust_level: str | None = None,
        source_kind: str | None = None,
        auto_bump_conflicting_semver: bool = False,
    ) -> dict[str, Any]:
        """Import a portable bundle and return installed row identifiers."""
        parsed = load_skill_bundle(bundle_dir)
        bundle_payload = parsed.to_db_bundle_payload(namespace=namespace)
        if trust_level is not None:
            bundle_payload["trust_level"] = trust_level
        if source_kind is not None:
            bundle_payload["source_kind"] = source_kind

        bundle = self._bundles.get_or_create_bundle(**bundle_payload)
        version_payload = parsed.to_db_version_payload(asset_root=str(parsed.root))
        version_was_existing = False
        version = (
            self._bundles.get_version_by_digest(bundle.id, parsed.content_digest)
            if auto_bump_conflicting_semver
            else None
        )
        if version is not None:
            version_was_existing = True
        else:
            requested_semver = parsed.manifest.semver
            existing_semver = self._bundles.get_version(bundle.id, requested_semver)
            if existing_semver is not None:
                if existing_semver.content_digest == parsed.content_digest:
                    version = existing_semver
                    version_was_existing = True
                elif auto_bump_conflicting_semver:
                    version_payload = dict(version_payload)
                    version_payload["semver"] = self._next_semver(bundle.id, requested_semver)
                    version_payload["provenance"] = {
                        **dict(version_payload.get("provenance") or {}),
                        "declared_semver": requested_semver,
                        "auto_bumped_from_semver": requested_semver,
                        "auto_bumped_reason": "semver_digest_conflict",
                    }
            if version is None:
                version = self._bundles.create_version(
                    bundle,
                    **version_payload,
                    status=review_status,
                    created_by_user_id=installed_by_user_id,
                    validate_manifest=True,
                )
        self._add_missing_assets(version.id, parsed)

        skill = self._materialize_skill_projection(
            parsed,
            bundle_version_id=version.id,
            source_kind=bundle.source_kind,
            trust_level=bundle.trust_level,
        )
        self._skills._session.flush()
        installation = self._upsert_installation(
            version.id,
            bundle.id,
            skill_id=skill.id,
            digest=version.content_digest,
            org_id=org_id,
            user_id=user_id,
            installed_by_user_id=installed_by_user_id,
            enabled_scope=enabled_scope,
            update_policy=update_policy,
            review_status=review_status,
        )
        skill.skill_installation_id = installation.id

        return {
            "bundle": {
                "id": bundle.id,
                "namespace": bundle.namespace,
                "name": bundle.name,
            },
            "version": {
                "id": version.id,
                "semver": version.semver,
                "content_digest": version.content_digest,
                "existing": version_was_existing,
            },
            "skill": {
                "id": skill.id,
                "name": skill.name,
                "version": skill.version,
                "bundle_version_id": skill.bundle_version_id,
                "bundle_digest": skill.bundle_digest,
                "effective_digest": skill.effective_digest,
                "source_kind": skill.source_kind,
                "trust_level": skill.trust_level,
            },
            "installation": {
                "id": installation.id,
                "enabled_scope": installation.enabled_scope,
                "update_policy": installation.update_policy,
                "installed_digest": installation.installed_digest,
                "rollback_bundle_version_id": installation.rollback_bundle_version_id,
            },
            "assets": len(self._bundles.list_assets(version.id)),
        }

    def ensure_skill_bundle(
        self,
        skill_id: int,
        *,
        namespace: str = "local",
        org_id: str | None = None,
        user_id: str | None = None,
        installed_by_user_id: str | None = None,
        enabled_scope: str = "user",
        update_policy: str = "manual",
        review_status: str = "approved",
        trust_level: str = "private_local",
        source_kind: str = "local",
    ) -> Skill:
        """Convert a legacy DB skill into a local bundle-backed projection if needed."""
        skill = self._skills.get_or_raise(skill_id)
        if skill.bundle_version_id or skill.skill_installation_id:
            return skill

        with TemporaryDirectory(prefix="illo-skill-bundle-") as tmp:
            target = Path(tmp) / skill.name
            self.export_skill_bundle(
                skill.name,
                target,
                version=f"0.0.{skill.version or 1}",
                namespace=namespace,
                source=source_kind,
                visibility=trust_level,
            )
            result = self.import_bundle(
                target,
                namespace=namespace,
                org_id=org_id,
                user_id=user_id,
                installed_by_user_id=installed_by_user_id,
                enabled_scope=enabled_scope,
                update_policy=update_policy,
                review_status=review_status,
                trust_level=trust_level,
                source_kind=source_kind,
            )
        converted_id = result.get("skill", {}).get("id") or skill_id
        return self._skills.get_or_raise(converted_id)

    def upsert_skill_asset(
        self,
        skill_id: int,
        *,
        path: str,
        content: str,
        asset_kind: str | None = None,
        mime_type: str | None = None,
        loading_budget_tokens: int | None = None,
        namespace: str = "local",
        org_id: str | None = None,
        user_id: str | None = None,
        installed_by_user_id: str | None = None,
    ) -> SkillAsset:
        """Add or replace a text asset by publishing a new local bundle version."""
        safe_path = _writable_asset_path(path)
        resolved_kind = _asset_kind_for_path(safe_path, asset_kind)
        resolved_mime = mime_type or _guess_asset_mime_type(safe_path)
        data = content.encode("utf-8")
        if len(data) > MAX_INLINE_TEXT_BYTES:
            raise ValueError(f"Skill asset content exceeds {MAX_INLINE_TEXT_BYTES} bytes")

        skill = self.ensure_skill_bundle(
            skill_id,
            namespace=namespace,
            org_id=org_id,
            user_id=user_id,
            installed_by_user_id=installed_by_user_id,
        )
        installation, version, bundle, assets = self._load_package_rows(skill)
        if version is None or bundle is None:
            raise LookupError("Skill package rows were not created")

        existing = next((asset for asset in assets if asset.path == safe_path), None)
        digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
        if (
            existing is not None
            and existing.content_digest == digest
            and existing.asset_kind == resolved_kind
            and existing.mime_type == resolved_mime
            and existing.loading_budget_tokens == loading_budget_tokens
            and existing.content_text == content
        ):
            return existing

        payloads = self._asset_payloads_for_revision(assets)
        payloads[safe_path] = {
            "path": safe_path,
            "asset_kind": resolved_kind,
            "mime_type": resolved_mime,
            "size_bytes": len(data),
            "content_digest": digest,
            "storage_kind": "inline",
            "storage_uri": None,
            "content_text": content,
            "loading_budget_tokens": loading_budget_tokens,
            "metadata": {},
        }
        new_version = self._publish_asset_revision(
            skill,
            bundle=bundle,
            current_version=version,
            installation=installation,
            payloads=payloads,
        )
        created = next(
            asset for asset in self._bundles.list_assets(new_version.id) if asset.path == safe_path
        )
        return created

    def delete_skill_asset(
        self,
        skill_id: int,
        *,
        path: str,
        namespace: str = "local",
        org_id: str | None = None,
        user_id: str | None = None,
        installed_by_user_id: str | None = None,
    ) -> Skill:
        """Remove a package asset by publishing a new local bundle version."""
        safe_path = _writable_asset_path(path)
        skill = self.ensure_skill_bundle(
            skill_id,
            namespace=namespace,
            org_id=org_id,
            user_id=user_id,
            installed_by_user_id=installed_by_user_id,
        )
        installation, version, bundle, assets = self._load_package_rows(skill)
        if version is None or bundle is None:
            raise LookupError("Skill package rows were not created")
        if not any(asset.path == safe_path for asset in assets):
            raise LookupError(f"Skill asset not found: {safe_path}")

        payloads = self._asset_payloads_for_revision(assets)
        payloads.pop(safe_path, None)
        self._publish_asset_revision(
            skill,
            bundle=bundle,
            current_version=version,
            installation=installation,
            payloads=payloads,
        )
        return skill

    def export_skill_bundle(
        self,
        skill_name: str,
        target_dir: str | Path,
        *,
        version: str | None = None,
        namespace: str = "local",
        license: str = "UNLICENSED",
        source: str = "local",
        visibility: str = "private_local",
    ) -> ParsedSkillBundle:
        """Export an existing DB skill as a portable filesystem bundle."""
        skill = self._skills.get_by_name_or_raise(skill_name)
        root = Path(target_dir)
        root.mkdir(parents=True, exist_ok=True)

        semver = version or f"0.0.{skill.version or 1}"
        manifest = {
            "schema_version": 1,
            "name": skill.name,
            "display_name": skill.name.replace("-", " ").title(),
            "version": semver,
            "description": skill.description or "",
            "license": license,
            "source": source,
            "visibility": visibility,
            "routing": {
                "triggers": skill.triggers or [],
                "embedding_text": f"{skill.name}: {skill.description or ''}".strip(),
            },
            "runtime": {
                "default_model_tier": skill.model_tier,
                "default_thinking_tier": skill.thinking_tier,
            },
            "loading": {
                "summary": f"{SKILL_FILENAME}#summary",
                "procedure": f"{SKILL_FILENAME}#procedure",
                "pitfalls": f"{SKILL_FILENAME}#pitfalls",
                "examples": "examples/",
                "templates": "templates/",
                "schemas": "schemas/",
                "evals": "evals/",
                "references": "references/",
                "scripts": "scripts/",
            },
        }

        (root / "skill.toml").write_text(_to_toml(manifest), encoding="utf-8")
        (root / SKILL_FILENAME).write_text(skill.procedure, encoding="utf-8")
        return load_skill_bundle(root)

    def _load_package_rows(
        self,
        skill: Skill,
    ) -> tuple[SkillInstallation | None, SkillBundleVersion | None, SkillBundleModel | None, list[SkillAsset]]:
        session = self._skills._session
        installation = None
        if skill.skill_installation_id:
            installation = session.get(SkillInstallation, skill.skill_installation_id)
        if installation is None and skill.bundle_version_id and skill.id:
            from sqlalchemy import select

            stmt = (
                select(SkillInstallation)
                .where(
                    SkillInstallation.skill_id == skill.id,
                    (SkillInstallation.archived == False) | SkillInstallation.archived.is_(None),  # noqa: E712
                )
                .order_by(SkillInstallation.id.desc())
            )
            installation = session.scalars(stmt).first()

        version_id = skill.bundle_version_id or (installation.bundle_version_id if installation else None)
        version = session.get(SkillBundleVersion, version_id) if version_id else None
        bundle_id = version.bundle_id if version else (installation.bundle_id if installation else None)
        bundle = session.get(SkillBundleModel, bundle_id) if bundle_id else None
        assets = list(self._bundles.list_assets(version.id)) if version is not None else []
        return installation, version, bundle, assets

    def _asset_payloads_for_revision(self, assets: list[SkillAsset]) -> dict[str, dict[str, Any]]:
        payloads: dict[str, dict[str, Any]] = {}
        for asset in assets:
            if asset.path == SKILL_FILENAME:
                continue
            payloads[asset.path] = {
                "path": asset.path,
                "asset_kind": asset.asset_kind,
                "mime_type": asset.mime_type,
                "size_bytes": asset.size_bytes,
                "content_digest": asset.content_digest,
                "storage_kind": asset.storage_kind,
                "storage_uri": asset.storage_uri,
                "content_text": asset.content_text,
                "loading_budget_tokens": asset.loading_budget_tokens,
                "metadata": dict(asset.metadata_ or {}),
            }
        return payloads

    def _publish_asset_revision(
        self,
        skill: Skill,
        *,
        bundle: SkillBundleModel,
        current_version: SkillBundleVersion,
        installation: SkillInstallation | None,
        payloads: dict[str, dict[str, Any]],
    ) -> SkillBundleVersion:
        semver = self._next_semver(bundle.id, current_version.semver)
        manifest = self._manifest_for_revision(skill, bundle, current_version, semver, payloads)
        parsed_manifest = validate_skill_bundle_manifest_payload(manifest)
        procedure = skill.procedure or ""
        procedure_bytes = procedure.encode("utf-8")
        procedure_sha = hashlib.sha256(procedure_bytes).hexdigest()
        digest_assets = [
            SkillBundleAsset(
                path=payload["path"],
                kind=payload["asset_kind"],
                mime_type=payload["mime_type"],
                size=int(payload.get("size_bytes") or 0),
                sha256=str(payload["content_digest"]).replace("sha256:", "", 1),
                content_text=payload.get("content_text"),
            )
            for payload in sorted(payloads.values(), key=lambda item: item["path"])
        ]
        content_digest = f"sha256:{compute_bundle_digest(parsed_manifest, procedure_sha, digest_assets)}"

        version = self._bundles.create_version(
            bundle,
            semver=semver,
            content_digest=content_digest,
            manifest=dict(parsed_manifest.raw),
            asset_root=current_version.asset_root,
            routing_card=dict(manifest.get("routing") or {}),
            permissions=dict(current_version.permissions or {}),
            compatibility=dict(current_version.compatibility or {}),
            eval_summary=dict(current_version.eval_summary or {}),
            signature=current_version.signature,
            provenance={
                **dict(current_version.provenance or {}),
                "source": bundle.source_kind,
                "bundle_digest": content_digest,
            },
            created_by_user_id=installation.installed_by_user_id if installation else None,
            status=current_version.status or "approved",
            validate_manifest=True,
        )
        self._bundles.add_asset(
            version.id,
            path=SKILL_FILENAME,
            asset_kind="procedure",
            mime_type="text/markdown",
            size_bytes=len(procedure_bytes),
            content_digest=f"sha256:{procedure_sha}",
            storage_kind="inline",
            content_text=procedure,
        )
        for payload in sorted(payloads.values(), key=lambda item: item["path"]):
            self._bundles.add_asset(version.id, **payload)

        skill.bundle_version_id = version.id
        skill.bundle_digest = version.content_digest
        skill.effective_digest = version.content_digest
        skill.overlay_revision = None
        skill.version = (skill.version or 1) + 1
        skill.source_kind = bundle.source_kind
        skill.trust_level = bundle.trust_level

        if installation is not None:
            if installation.bundle_version_id != version.id:
                installation.rollback_bundle_version_id = installation.bundle_version_id
            installation.bundle_version_id = version.id
            installation.skill_id = skill.id
            installation.installed_digest = version.content_digest
            skill.skill_installation_id = installation.id

        self._skills._session.flush()
        return version

    def _manifest_for_revision(
        self,
        skill: Skill,
        bundle: SkillBundleModel,
        current_version: SkillBundleVersion,
        semver: str,
        payloads: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        manifest = dict(current_version.manifest or {})
        manifest["schema_version"] = int(manifest.get("schema_version") or 1)
        manifest["name"] = bundle.name or skill.name
        manifest["display_name"] = bundle.display_name or skill.name.replace("-", " ").title()
        manifest["version"] = semver
        manifest["description"] = bundle.description or skill.description or "Skill package"
        manifest["source"] = bundle.source_kind or skill.source_kind or "local"
        manifest["visibility"] = bundle.visibility or skill.trust_level or "private_local"

        routing = dict(manifest.get("routing") or {})
        routing["triggers"] = skill.triggers or routing.get("triggers") or []
        routing.setdefault("embedding_text", f"{skill.name}: {skill.description or ''}".strip())
        manifest["routing"] = routing

        runtime = dict(manifest.get("runtime") or {})
        runtime["default_model_tier"] = skill.model_tier
        runtime["default_thinking_tier"] = skill.thinking_tier
        manifest["runtime"] = runtime

        loading = dict(manifest.get("loading") or {})
        loading.setdefault("summary", f"{SKILL_FILENAME}#summary")
        loading["procedure"] = f"{SKILL_FILENAME}#procedure"
        for payload in payloads.values():
            root = payload["path"].split("/", 1)[0]
            if root in ASSET_KINDS:
                loading[root] = f"{root}/"
        manifest["loading"] = loading
        return manifest

    def _next_semver(self, bundle_id: int, current_semver: str | None) -> str:
        base = current_semver or "0.0.0"
        match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$", base)
        if match:
            major, minor, patch = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        else:
            major, minor, patch = 0, 0, 0
        for offset in range(1, 1000):
            candidate = f"{major}.{minor}.{patch + offset}"
            if self._bundles.get_version(bundle_id, candidate) is None:
                return candidate
        raise ValueError("Unable to allocate a new skill package version")

    def _add_missing_assets(self, version_id: int, parsed: ParsedSkillBundle) -> None:
        existing_paths = {
            asset.path for asset in self._bundles.list_assets(version_id)
        }
        if SKILL_FILENAME not in existing_paths:
            self._bundles.add_asset(
                version_id,
                path=SKILL_FILENAME,
                asset_kind="procedure",
                mime_type="text/markdown",
                size_bytes=len(parsed.skill_markdown.encode("utf-8")),
                content_digest=f"sha256:{parsed.skill_sha256}",
                storage_kind="inline",
                content_text=parsed.skill_markdown,
            )
            existing_paths.add(SKILL_FILENAME)

        for asset_payload in parsed.to_db_asset_payloads():
            if asset_payload["path"] in existing_paths:
                continue
            self._bundles.add_asset(version_id, **asset_payload)

    def _materialize_skill_projection(
        self,
        parsed: ParsedSkillBundle,
        *,
        bundle_version_id: int,
        source_kind: str,
        trust_level: str,
    ) -> Skill:
        skill = self._skills.get_by_name(parsed.manifest.name)
        if skill is None:
            skill = self._skills.create(
                name=parsed.manifest.name,
                description=parsed.manifest.description,
                procedure=parsed.skill_markdown,
                version=1,
                skill_type="skill",
                builtin=trust_level == "illo_core",
            )
        else:
            if skill.procedure != parsed.skill_markdown:
                skill.version = (skill.version or 1) + 1
            skill.description = parsed.manifest.description
            skill.procedure = parsed.skill_markdown
            skill.skill_type = "skill"

        skill.bundle_version_id = bundle_version_id
        skill.bundle_digest = parsed.content_digest
        skill.overlay_revision = None
        skill.effective_digest = parsed.content_digest
        skill.source_kind = source_kind
        skill.trust_level = trust_level
        skill.builtin = trust_level == "illo_core"
        if parsed.manifest.routing.triggers:
            skill.triggers = list(parsed.manifest.routing.triggers)
        for field_name in ("guardrails", "pitfalls", "refinements"):
            manifest_values = _manifest_list(parsed, field_name)
            if manifest_values is not None:
                setattr(skill, field_name, manifest_values)
        if parsed.manifest.runtime.default_model_tier:
            skill.model_tier = parsed.manifest.runtime.default_model_tier
        if parsed.manifest.runtime.default_thinking_tier:
            skill.thinking_tier = parsed.manifest.runtime.default_thinking_tier
        return skill

    def _upsert_installation(
        self,
        bundle_version_id: int,
        bundle_id: int,
        *,
        skill_id: int,
        digest: str,
        org_id: str | None,
        user_id: str | None,
        installed_by_user_id: str | None,
        enabled_scope: str,
        update_policy: str,
        review_status: str,
    ) -> SkillInstallation:
        existing = self._bundles.get_active_installation(
            bundle_id,
            org_id=org_id,
            user_id=user_id,
            enabled_scope=enabled_scope,
        )
        if existing is None:
            return self._bundles.create_installation(
                bundle_version_id,
                org_id=org_id,
                user_id=user_id,
                enabled_scope=enabled_scope,
                update_policy=update_policy,
                skill_id=skill_id,
                installed_by_user_id=installed_by_user_id,
                installed_digest=digest,
                review_status=review_status,
            )

        if existing.bundle_version_id != bundle_version_id:
            existing.rollback_bundle_version_id = existing.bundle_version_id
        existing.bundle_version_id = bundle_version_id
        existing.skill_id = skill_id
        existing.installed_digest = digest
        existing.update_policy = update_policy
        existing.review_status = review_status
        existing.installed_by_user_id = installed_by_user_id
        return existing


def _to_toml(data: dict[str, Any]) -> str:
    """Render the small manifest subset we emit for skill exports."""
    lines: list[str] = []
    tables: list[tuple[str, dict[str, Any]]] = []

    for key, value in data.items():
        if isinstance(value, dict):
            tables.append((key, value))
        else:
            lines.append(f"{key} = {_toml_value(value)}")

    for table_name, table in tables:
        lines.append("")
        lines.append(f"[{table_name}]")
        for key, value in table.items():
            lines.append(f"{key} = {_toml_value(value)}")

    return "\n".join(lines).rstrip() + "\n"


def _manifest_list(parsed: SkillBundle, field_name: str) -> list[Any] | None:
    value = parsed.manifest.raw.get(field_name)
    if value is None:
        knowledge = parsed.manifest.raw.get("knowledge")
        if isinstance(knowledge, dict):
            value = knowledge.get(field_name)
    if value is None:
        return None
    if isinstance(value, list):
        return list(value)
    return []


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if value is None:
        return '""'
    return json.dumps(str(value))


def _writable_asset_path(path: str) -> str:
    candidate = (path or "").strip()
    if not candidate:
        raise ValueError("Asset path is required")
    if "\\" in candidate:
        raise ValueError("Asset path must use POSIX separators")
    pure = Path(candidate)
    posix = candidate.replace("\\", "/")
    if posix == SKILL_FILENAME:
        raise ValueError("Edit the skill procedure instead of writing SKILL.md as an asset")
    if posix.startswith("/") or pure.is_absolute():
        raise ValueError("Asset path must be relative")
    parts = posix.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Asset path cannot contain traversal segments")
    root = parts[0]
    if root not in ASSET_KINDS:
        allowed = ", ".join(f"{name}/" for name in sorted(ASSET_KINDS))
        raise ValueError(f"Asset path must live under one of: {allowed}")
    return posix


def _asset_kind_for_path(path: str, asset_kind: str | None) -> str:
    root = path.split("/", 1)[0]
    expected = ASSET_KINDS[root]
    if asset_kind and asset_kind != expected:
        raise ValueError(f"Asset kind for {path} must be {expected!r}")
    return expected


def _guess_asset_mime_type(path: str) -> str:
    if path.endswith(".schema.json"):
        return "application/schema+json"
    suffix = Path(path).suffix.lower()
    overrides = {
        ".jsonl": "application/jsonl",
        ".md": "text/markdown",
        ".toml": "application/toml",
        ".yaml": "application/x-yaml",
        ".yml": "application/x-yaml",
        ".py": "text/x-python",
        ".sh": "text/x-shellscript",
    }
    if suffix in overrides:
        return overrides[suffix]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "text/plain"
