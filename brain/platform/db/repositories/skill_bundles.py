"""Repository helpers for portable skill bundle state."""
from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import func, or_, select

from brain.platform.db.models.skill import Skill
from brain.platform.db.models.skill_bundle import (
    SkillAsset,
    SkillBundle,
    SkillBundleVersion,
    SkillInstallation,
    SkillOverlay,
)
from brain.platform.db.repositories.base import BaseRepository
from brain.systems.skills.bundles import (
    SkillBundleAssetType,
    SkillBundleInstallStatus,
    SkillBundleReviewStatus,
    SkillBundleSourceKind,
    SkillBundleTrustLevel,
    SkillBundleUpdatePolicy,
    coerce_skill_bundle_enum_value,
    validate_skill_bundle_manifest_payload,
)


class SkillBundleVersionConflict(ValueError):
    """Raised when a bundle version identity is not immutable."""


class SkillInstallationConflict(ValueError):
    """Raised when an active installation already exists for a scope."""


class SkillOverlayConflict(ValueError):
    """Raised when an overlay revision already exists."""


def _not_archived(model):
    return or_(model.archived == False, model.archived.is_(None))  # noqa: E712


class SkillBundleRepository(BaseRepository[SkillBundle]):
    """Domain queries for bundle, version, installation, and overlay rows."""

    model = SkillBundle

    # ------------------------------------------------------------------
    # Bundle identity
    # ------------------------------------------------------------------

    def get_bundle(self, namespace: str, name: str) -> SkillBundle | None:
        stmt = select(SkillBundle).where(
            SkillBundle.namespace == namespace,
            SkillBundle.name == name,
        )
        return self._session.scalars(stmt).first()

    async def a_get_bundle(self, namespace: str, name: str) -> SkillBundle | None:
        stmt = select(SkillBundle).where(
            SkillBundle.namespace == namespace,
            SkillBundle.name == name,
        )
        return (await self._session.scalars(stmt)).first()

    def get_or_create_bundle(
        self,
        namespace: str,
        name: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        owner_org_id: str | None = None,
        owner_user_id: str | None = None,
        visibility: str = "private_local",
        source_kind: str = "local",
        trust_level: str = "private_local",
    ) -> SkillBundle:
        source_kind = coerce_skill_bundle_enum_value(
            source_kind,
            SkillBundleSourceKind,
            "source_kind",
        )
        trust_level = coerce_skill_bundle_enum_value(
            trust_level,
            SkillBundleTrustLevel,
            "trust_level",
        )
        bundle = self.get_bundle(namespace, name)
        if bundle is not None:
            return bundle

        bundle = SkillBundle(
            namespace=namespace,
            name=name,
            display_name=display_name,
            description=description,
            owner_org_id=owner_org_id,
            owner_user_id=owner_user_id,
            visibility=visibility,
            source_kind=source_kind,
            trust_level=trust_level,
        )
        self._session.add(bundle)
        self._session.flush()
        return bundle

    async def a_get_or_create_bundle(
        self,
        namespace: str,
        name: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        owner_org_id: str | None = None,
        owner_user_id: str | None = None,
        visibility: str = "private_local",
        source_kind: str = "local",
        trust_level: str = "private_local",
    ) -> SkillBundle:
        source_kind = coerce_skill_bundle_enum_value(
            source_kind,
            SkillBundleSourceKind,
            "source_kind",
        )
        trust_level = coerce_skill_bundle_enum_value(
            trust_level,
            SkillBundleTrustLevel,
            "trust_level",
        )
        bundle = await self.a_get_bundle(namespace, name)
        if bundle is not None:
            return bundle

        bundle = SkillBundle(
            namespace=namespace,
            name=name,
            display_name=display_name,
            description=description,
            owner_org_id=owner_org_id,
            owner_user_id=owner_user_id,
            visibility=visibility,
            source_kind=source_kind,
            trust_level=trust_level,
        )
        self._session.add(bundle)
        await self._session.flush()
        return bundle

    # ------------------------------------------------------------------
    # Immutable versions
    # ------------------------------------------------------------------

    def get_version(
        self,
        bundle_id: int,
        semver: str,
    ) -> SkillBundleVersion | None:
        stmt = select(SkillBundleVersion).where(
            SkillBundleVersion.bundle_id == bundle_id,
            SkillBundleVersion.semver == semver,
        )
        return self._session.scalars(stmt).first()

    async def a_get_version(
        self,
        bundle_id: int,
        semver: str,
    ) -> SkillBundleVersion | None:
        stmt = select(SkillBundleVersion).where(
            SkillBundleVersion.bundle_id == bundle_id,
            SkillBundleVersion.semver == semver,
        )
        return (await self._session.scalars(stmt)).first()

    def create_version(
        self,
        bundle: SkillBundle | int,
        *,
        semver: str,
        content_digest: str,
        manifest: dict[str, Any] | None = None,
        asset_root: str | None = None,
        routing_card: dict[str, Any] | None = None,
        permissions: dict[str, Any] | None = None,
        compatibility: dict[str, Any] | None = None,
        eval_summary: dict[str, Any] | None = None,
        signature: str | None = None,
        provenance: dict[str, Any] | None = None,
        created_by_user_id: str | None = None,
        status: str = "draft",
        validate_manifest: bool = False,
    ) -> SkillBundleVersion:
        bundle_id = self._bundle_id(bundle)
        status = coerce_skill_bundle_enum_value(
            status,
            SkillBundleReviewStatus,
            "status",
        )
        if validate_manifest and manifest is not None:
            manifest = dict(validate_skill_bundle_manifest_payload(manifest).raw)

        existing_semver = self.get_version(bundle_id, semver)
        if existing_semver is not None:
            if existing_semver.content_digest != content_digest:
                raise SkillBundleVersionConflict(
                    "SkillBundleVersion semver already exists with a different digest"
                )
            return existing_semver

        existing_digest = self._get_version_by_digest(bundle_id, content_digest)
        if existing_digest is not None:
            raise SkillBundleVersionConflict(
                "SkillBundleVersion digest already exists with a different semver"
            )

        version = SkillBundleVersion(
            bundle_id=bundle_id,
            semver=semver,
            content_digest=content_digest,
            manifest=manifest or {},
            asset_root=asset_root,
            routing_card=routing_card or {},
            permissions=permissions or {},
            compatibility=compatibility or {},
            eval_summary=eval_summary or {},
            signature=signature,
            provenance=provenance or {},
            created_by_user_id=created_by_user_id,
            status=status,
        )
        self._session.add(version)
        self._session.flush()
        return version

    async def a_create_version(
        self,
        bundle: SkillBundle | int,
        *,
        semver: str,
        content_digest: str,
        manifest: dict[str, Any] | None = None,
        asset_root: str | None = None,
        routing_card: dict[str, Any] | None = None,
        permissions: dict[str, Any] | None = None,
        compatibility: dict[str, Any] | None = None,
        eval_summary: dict[str, Any] | None = None,
        signature: str | None = None,
        provenance: dict[str, Any] | None = None,
        created_by_user_id: str | None = None,
        status: str = "draft",
        validate_manifest: bool = False,
    ) -> SkillBundleVersion:
        bundle_id = await self._a_bundle_id(bundle)
        status = coerce_skill_bundle_enum_value(
            status,
            SkillBundleReviewStatus,
            "status",
        )
        if validate_manifest and manifest is not None:
            manifest = dict(validate_skill_bundle_manifest_payload(manifest).raw)

        existing_semver = await self.a_get_version(bundle_id, semver)
        if existing_semver is not None:
            if existing_semver.content_digest != content_digest:
                raise SkillBundleVersionConflict(
                    "SkillBundleVersion semver already exists with a different digest"
                )
            return existing_semver

        existing_digest = await self._a_get_version_by_digest(bundle_id, content_digest)
        if existing_digest is not None:
            raise SkillBundleVersionConflict(
                "SkillBundleVersion digest already exists with a different semver"
            )

        version = SkillBundleVersion(
            bundle_id=bundle_id,
            semver=semver,
            content_digest=content_digest,
            manifest=manifest or {},
            asset_root=asset_root,
            routing_card=routing_card or {},
            permissions=permissions or {},
            compatibility=compatibility or {},
            eval_summary=eval_summary or {},
            signature=signature,
            provenance=provenance or {},
            created_by_user_id=created_by_user_id,
            status=status,
        )
        self._session.add(version)
        await self._session.flush()
        return version

    def _get_version_by_digest(
        self,
        bundle_id: int,
        content_digest: str,
    ) -> SkillBundleVersion | None:
        stmt = select(SkillBundleVersion).where(
            SkillBundleVersion.bundle_id == bundle_id,
            SkillBundleVersion.content_digest == content_digest,
        )
        return self._session.scalars(stmt).first()

    async def _a_get_version_by_digest(
        self,
        bundle_id: int,
        content_digest: str,
    ) -> SkillBundleVersion | None:
        stmt = select(SkillBundleVersion).where(
            SkillBundleVersion.bundle_id == bundle_id,
            SkillBundleVersion.content_digest == content_digest,
        )
        return (await self._session.scalars(stmt)).first()

    def get_version_by_digest(
        self,
        bundle: SkillBundle | int,
        content_digest: str,
    ) -> SkillBundleVersion | None:
        """Return an existing immutable version by content digest."""
        return self._get_version_by_digest(self._bundle_id(bundle), content_digest)

    async def a_get_version_by_digest(
        self,
        bundle: SkillBundle | int,
        content_digest: str,
    ) -> SkillBundleVersion | None:
        """Return an existing immutable version by content digest."""
        return await self._a_get_version_by_digest(await self._a_bundle_id(bundle), content_digest)

    # ------------------------------------------------------------------
    # Assets
    # ------------------------------------------------------------------

    def add_asset(
        self,
        bundle_version: SkillBundleVersion | int,
        *,
        path: str,
        content_digest: str,
        asset_kind: str = "reference",
        mime_type: str = "text/plain",
        size_bytes: int | None = None,
        storage_kind: str = "inline",
        storage_uri: str | None = None,
        content_text: str | None = None,
        loading_budget_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SkillAsset:
        version_id = self._version_id(bundle_version)
        asset_kind = coerce_skill_bundle_enum_value(
            asset_kind,
            SkillBundleAssetType,
            "asset_kind",
        )
        asset = SkillAsset(
            bundle_version_id=version_id,
            path=path,
            asset_kind=asset_kind,
            mime_type=mime_type,
            size_bytes=size_bytes,
            content_digest=content_digest,
            storage_kind=storage_kind,
            storage_uri=storage_uri,
            content_text=content_text,
            loading_budget_tokens=loading_budget_tokens,
            metadata_=metadata or {},
        )
        self._session.add(asset)
        self._session.flush()
        return asset

    async def a_add_asset(
        self,
        bundle_version: SkillBundleVersion | int,
        *,
        path: str,
        content_digest: str,
        asset_kind: str = "reference",
        mime_type: str = "text/plain",
        size_bytes: int | None = None,
        storage_kind: str = "inline",
        storage_uri: str | None = None,
        content_text: str | None = None,
        loading_budget_tokens: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SkillAsset:
        version_id = await self._a_version_id(bundle_version)
        asset_kind = coerce_skill_bundle_enum_value(
            asset_kind,
            SkillBundleAssetType,
            "asset_kind",
        )
        asset = SkillAsset(
            bundle_version_id=version_id,
            path=path,
            asset_kind=asset_kind,
            mime_type=mime_type,
            size_bytes=size_bytes,
            content_digest=content_digest,
            storage_kind=storage_kind,
            storage_uri=storage_uri,
            content_text=content_text,
            loading_budget_tokens=loading_budget_tokens,
            metadata_=metadata or {},
        )
        self._session.add(asset)
        await self._session.flush()
        return asset

    def list_assets(self, bundle_version: SkillBundleVersion | int) -> Sequence[SkillAsset]:
        version_id = self._version_id(bundle_version)
        stmt = (
            select(SkillAsset)
            .where(SkillAsset.bundle_version_id == version_id)
            .order_by(SkillAsset.path)
        )
        return self._session.scalars(stmt).all()

    async def a_list_assets(self, bundle_version: SkillBundleVersion | int) -> Sequence[SkillAsset]:
        version_id = await self._a_version_id(bundle_version)
        stmt = (
            select(SkillAsset)
            .where(SkillAsset.bundle_version_id == version_id)
            .order_by(SkillAsset.path)
        )
        return (await self._session.scalars(stmt)).all()

    # ------------------------------------------------------------------
    # Installations
    # ------------------------------------------------------------------

    def get_installation(self, installation_id: int) -> SkillInstallation | None:
        return self._session.get(SkillInstallation, installation_id)

    async def a_get_installation(self, installation_id: int) -> SkillInstallation | None:
        return await self._session.get(SkillInstallation, installation_id)

    def create_installation(
        self,
        bundle_version: SkillBundleVersion | int,
        *,
        org_id: str | None = None,
        user_id: str | None = None,
        enabled_scope: str = "user",
        update_policy: str = "manual",
        permission_grants: list[dict[str, Any]] | None = None,
        skill_id: int | None = None,
        installed_by_user_id: str | None = None,
        installed_digest: str | None = None,
        review_status: str = "approved",
        disabled_sections: list[str] | None = None,
        loading_budgets: dict[str, Any] | None = None,
        rollback_bundle_version_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SkillInstallation:
        version = self._load_version(bundle_version)
        update_policy = coerce_skill_bundle_enum_value(
            update_policy,
            SkillBundleUpdatePolicy,
            "update_policy",
        )
        review_status = coerce_skill_bundle_enum_value(
            review_status,
            SkillBundleReviewStatus,
            "review_status",
        )
        digest = installed_digest or version.content_digest
        if digest != version.content_digest:
            raise ValueError("installed_digest must match the pinned bundle version digest")

        existing = self.get_active_installation(
            version.bundle_id,
            org_id=org_id,
            user_id=user_id,
            enabled_scope=enabled_scope,
        )
        if existing is not None:
            raise SkillInstallationConflict(
                "Active SkillInstallation already exists for this bundle scope"
            )

        installation = SkillInstallation(
            bundle_id=version.bundle_id,
            bundle_version_id=version.id,
            skill_id=skill_id,
            org_id=org_id,
            user_id=user_id,
            installed_by_user_id=installed_by_user_id,
            enabled=True,
            enabled_scope=enabled_scope,
            pinned=True,
            update_policy=update_policy,
            installed_digest=digest,
            review_status=review_status,
            permission_grants=permission_grants or [],
            disabled_sections=disabled_sections or [],
            loading_budgets=loading_budgets or {},
            rollback_bundle_version_id=rollback_bundle_version_id,
            metadata_=metadata or {},
        )
        self._session.add(installation)
        self._session.flush()
        return installation

    async def a_create_installation(
        self,
        bundle_version: SkillBundleVersion | int,
        *,
        org_id: str | None = None,
        user_id: str | None = None,
        enabled_scope: str = "user",
        update_policy: str = "manual",
        permission_grants: list[dict[str, Any]] | None = None,
        skill_id: int | None = None,
        installed_by_user_id: str | None = None,
        installed_digest: str | None = None,
        review_status: str = "approved",
        disabled_sections: list[str] | None = None,
        loading_budgets: dict[str, Any] | None = None,
        rollback_bundle_version_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SkillInstallation:
        version = await self._a_load_version(bundle_version)
        update_policy = coerce_skill_bundle_enum_value(
            update_policy,
            SkillBundleUpdatePolicy,
            "update_policy",
        )
        review_status = coerce_skill_bundle_enum_value(
            review_status,
            SkillBundleReviewStatus,
            "review_status",
        )
        digest = installed_digest or version.content_digest
        if digest != version.content_digest:
            raise ValueError("installed_digest must match the pinned bundle version digest")

        existing = await self.a_get_active_installation(
            version.bundle_id,
            org_id=org_id,
            user_id=user_id,
            enabled_scope=enabled_scope,
        )
        if existing is not None:
            raise SkillInstallationConflict(
                "Active SkillInstallation already exists for this bundle scope"
            )

        installation = SkillInstallation(
            bundle_id=version.bundle_id,
            bundle_version_id=version.id,
            skill_id=skill_id,
            org_id=org_id,
            user_id=user_id,
            installed_by_user_id=installed_by_user_id,
            enabled=True,
            enabled_scope=enabled_scope,
            pinned=True,
            update_policy=update_policy,
            installed_digest=digest,
            review_status=review_status,
            permission_grants=permission_grants or [],
            disabled_sections=disabled_sections or [],
            loading_budgets=loading_budgets or {},
            rollback_bundle_version_id=rollback_bundle_version_id,
            metadata_=metadata or {},
        )
        self._session.add(installation)
        await self._session.flush()
        return installation

    def get_active_installation(
        self,
        bundle_id: int,
        *,
        org_id: str | None = None,
        user_id: str | None = None,
        enabled_scope: str = "user",
    ) -> SkillInstallation | None:
        org_filter = (
            SkillInstallation.org_id.is_(None)
            if org_id is None
            else SkillInstallation.org_id == org_id
        )
        user_filter = (
            SkillInstallation.user_id.is_(None)
            if user_id is None
            else SkillInstallation.user_id == user_id
        )
        stmt = select(SkillInstallation).where(
            SkillInstallation.bundle_id == bundle_id,
            org_filter,
            user_filter,
            SkillInstallation.enabled_scope == enabled_scope,
            _not_archived(SkillInstallation),
        )
        return self._session.scalars(stmt).first()

    async def a_get_active_installation(
        self,
        bundle_id: int,
        *,
        org_id: str | None = None,
        user_id: str | None = None,
        enabled_scope: str = "user",
    ) -> SkillInstallation | None:
        org_filter = (
            SkillInstallation.org_id.is_(None)
            if org_id is None
            else SkillInstallation.org_id == org_id
        )
        user_filter = (
            SkillInstallation.user_id.is_(None)
            if user_id is None
            else SkillInstallation.user_id == user_id
        )
        stmt = select(SkillInstallation).where(
            SkillInstallation.bundle_id == bundle_id,
            org_filter,
            user_filter,
            SkillInstallation.enabled_scope == enabled_scope,
            _not_archived(SkillInstallation),
        )
        return (await self._session.scalars(stmt)).first()

    # ------------------------------------------------------------------
    # Overlays
    # ------------------------------------------------------------------

    def add_overlay_revision(
        self,
        installation: SkillInstallation | int,
        *,
        patch: dict[str, Any],
        overlay_revision: int | None = None,
        status: str = "draft",
        overlay_digest: str | None = None,
        effective_digest: str | None = None,
        author_user_id: str | None = None,
        reason: str | None = None,
        promoted_bundle_version_id: int | None = None,
    ) -> SkillOverlay:
        installation_obj = self._load_installation(installation)
        status = coerce_skill_bundle_enum_value(
            status,
            SkillBundleInstallStatus,
            "status",
        )
        revision = overlay_revision or self._next_overlay_revision(installation_obj.id)

        existing = self._get_overlay_revision(installation_obj.id, revision)
        if existing is not None:
            raise SkillOverlayConflict(
                "SkillOverlay revision already exists for this installation"
            )

        overlay = SkillOverlay(
            installation_id=installation_obj.id,
            base_bundle_version_id=installation_obj.bundle_version_id,
            overlay_revision=revision,
            status=status,
            patch=patch,
            overlay_digest=overlay_digest,
            effective_digest=effective_digest,
            author_user_id=author_user_id,
            reason=reason,
            promoted_bundle_version_id=promoted_bundle_version_id,
        )
        self._session.add(overlay)
        self._session.flush()
        return overlay

    def get_active_overlay(
        self,
        installation: SkillInstallation | int,
    ) -> SkillOverlay | None:
        installation_id = self._installation_id(installation)
        stmt = (
            select(SkillOverlay)
            .where(
                SkillOverlay.installation_id == installation_id,
                SkillOverlay.status == "active",
            )
            .order_by(SkillOverlay.overlay_revision.desc(), SkillOverlay.id.desc())
        )
        return self._session.scalars(stmt).first()

    # ------------------------------------------------------------------
    # Runtime projection metadata
    # ------------------------------------------------------------------

    def get_runtime_projection_metadata(
        self,
        *,
        skill_id: int | None = None,
        installation_id: int | None = None,
    ) -> dict[str, Any] | None:
        if (skill_id is None) == (installation_id is None):
            raise ValueError("Provide exactly one of skill_id or installation_id")

        installation = (
            self.get_installation(installation_id)
            if installation_id is not None
            else self._get_installation_by_skill_id(skill_id)
        )
        if installation is None:
            return None

        bundle = self._session.get(SkillBundle, installation.bundle_id)
        version = self._session.get(SkillBundleVersion, installation.bundle_version_id)
        if bundle is None or version is None:
            raise LookupError("Installed bundle projection is missing bundle/version rows")

        skill = (
            self._session.get(Skill, installation.skill_id)
            if installation.skill_id is not None
            else None
        )
        active_overlay = self.get_active_overlay(installation.id)
        effective_digest = (
            active_overlay.effective_digest
            if active_overlay and active_overlay.effective_digest
            else installation.installed_digest
        )

        return {
            "installation_id": installation.id,
            "skill_id": installation.skill_id,
            "bundle_id": bundle.id,
            "namespace": bundle.namespace,
            "name": bundle.name,
            "bundle_version_id": version.id,
            "semver": version.semver,
            "bundle_digest": version.content_digest,
            "installed_digest": installation.installed_digest,
            "effective_digest": effective_digest,
            "overlay_revision": (
                active_overlay.overlay_revision if active_overlay is not None else None
            ),
            "update_policy": installation.update_policy,
            "pinned": installation.pinned,
            "enabled": installation.enabled,
            "enabled_scope": installation.enabled_scope,
            "org_id": installation.org_id,
            "user_id": installation.user_id,
            "runtime_skill": {
                "id": skill.id,
                "bundle_version_id": skill.bundle_version_id,
                "bundle_digest": skill.bundle_digest,
                "overlay_revision": skill.overlay_revision,
                "effective_digest": skill.effective_digest,
                "source_kind": skill.source_kind,
                "trust_level": skill.trust_level,
            }
            if skill is not None
            else None,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _bundle_id(self, bundle: SkillBundle | int) -> int:
        if isinstance(bundle, SkillBundle):
            if bundle.id is None:
                self._session.flush()
            return bundle.id
        return bundle

    def _version_id(self, bundle_version: SkillBundleVersion | int) -> int:
        if isinstance(bundle_version, SkillBundleVersion):
            if bundle_version.id is None:
                self._session.flush()
            return bundle_version.id
        return bundle_version

    def _installation_id(self, installation: SkillInstallation | int) -> int:
        if isinstance(installation, SkillInstallation):
            if installation.id is None:
                self._session.flush()
            return installation.id
        return installation

    def _load_version(self, bundle_version: SkillBundleVersion | int) -> SkillBundleVersion:
        if isinstance(bundle_version, SkillBundleVersion):
            if bundle_version.id is None:
                self._session.flush()
            return bundle_version
        version = self._session.get(SkillBundleVersion, bundle_version)
        if version is None:
            raise LookupError(f"SkillBundleVersion {bundle_version} not found")
        return version

    async def _a_bundle_id(self, bundle: SkillBundle | int) -> int:
        if isinstance(bundle, SkillBundle):
            if bundle.id is None:
                await self._session.flush()
            return bundle.id
        return bundle

    async def _a_version_id(self, bundle_version: SkillBundleVersion | int) -> int:
        if isinstance(bundle_version, SkillBundleVersion):
            if bundle_version.id is None:
                await self._session.flush()
            return bundle_version.id
        return bundle_version

    async def _a_load_version(self, bundle_version: SkillBundleVersion | int) -> SkillBundleVersion:
        if isinstance(bundle_version, SkillBundleVersion):
            if bundle_version.id is None:
                await self._session.flush()
            return bundle_version
        version = await self._session.get(SkillBundleVersion, bundle_version)
        if version is None:
            raise LookupError(f"SkillBundleVersion {bundle_version} not found")
        return version

    def _load_installation(
        self,
        installation: SkillInstallation | int,
    ) -> SkillInstallation:
        if isinstance(installation, SkillInstallation):
            if installation.id is None:
                self._session.flush()
            return installation
        installation_obj = self.get_installation(installation)
        if installation_obj is None:
            raise LookupError(f"SkillInstallation {installation} not found")
        return installation_obj

    def _get_overlay_revision(
        self,
        installation_id: int,
        overlay_revision: int,
    ) -> SkillOverlay | None:
        stmt = select(SkillOverlay).where(
            SkillOverlay.installation_id == installation_id,
            SkillOverlay.overlay_revision == overlay_revision,
        )
        return self._session.scalars(stmt).first()

    def _next_overlay_revision(self, installation_id: int) -> int:
        stmt = select(func.max(SkillOverlay.overlay_revision)).where(
            SkillOverlay.installation_id == installation_id
        )
        current = self._session.scalar(stmt)
        return (current or 0) + 1

    def _get_installation_by_skill_id(
        self,
        skill_id: int | None,
    ) -> SkillInstallation | None:
        if skill_id is None:
            return None

        stmt = (
            select(SkillInstallation)
            .where(
                SkillInstallation.skill_id == skill_id,
                _not_archived(SkillInstallation),
            )
            .order_by(SkillInstallation.id.desc())
        )
        installation = self._session.scalars(stmt).first()
        if installation is not None:
            return installation

        skill = self._session.get(Skill, skill_id)
        if skill is None or skill.skill_installation_id is None:
            return None
        return self.get_installation(skill.skill_installation_id)
