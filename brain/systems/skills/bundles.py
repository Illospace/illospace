"""Portable filesystem skill bundle parsing.

A bundle is a directory with ``skill.toml`` and ``SKILL.md`` plus optional
assets under known asset roots. This module stays intentionally DB-free: the
dataclasses expose stable payload helpers, but importing remains a later service
concern.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import tomllib

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


MANIFEST_FILENAME = "skill.toml"
SKILL_FILENAME = "SKILL.md"
MAX_INLINE_TEXT_BYTES = 256 * 1024

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh"})
_LEGACY_RUNTIME_PROVIDER_KEYS = frozenset(
    {
        "default_provider",
        "default_model",
        "default_reasoning_effort",
        "service_tier",
        "auth_mode",
    }
)
_TEXT_SUFFIXES = frozenset(
    {
        ".css",
        ".csv",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".jsonl",
        ".mjs",
        ".md",
        ".py",
        ".rb",
        ".rst",
        ".sh",
        ".sql",
        ".ts",
        ".toml",
        ".tsv",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_TEXT_MIME_TYPES = frozenset(
    {
        "application/json",
        "application/jsonl",
        "application/schema+json",
        "application/toml",
        "application/x-yaml",
    }
)
_MIME_OVERRIDES = {
    ".jsonl": "application/jsonl",
    ".md": "text/markdown",
    ".toml": "application/toml",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
}


class SkillBundleSourceKind(str, Enum):
    """Origin of a runtime skill projection or portable bundle."""

    LEGACY_DB = "legacy_db"
    LOCAL = "local"
    PRIVATE_LOCAL = "private_local"
    AGENT_DRAFT = "agent_draft"
    ILLO_CORE = "illo-core"
    ILLO_CORE_UNDERSCORE = "illo_core"
    MARKETPLACE = "marketplace"
    SELF_HOSTED = "self_hosted"


class SkillBundleTrustLevel(str, Enum):
    """Permission-review trust tier assigned to a skill bundle."""

    AGENT_DRAFT = "agent_draft"
    PRIVATE_LOCAL = "private_local"
    PUBLIC = "public"
    ILLO_CORE = "illo_core"
    MARKETPLACE = "marketplace"
    SELF_HOSTED = "self_hosted"


class SkillBundleInstallStatus(str, Enum):
    """Lifecycle status for installed bundle projections and overlays."""

    DRAFT = "draft"
    PENDING = "pending"
    ACTIVE = "active"
    INSTALLED = "installed"
    ENABLED = "enabled"
    DISABLED = "disabled"
    ARCHIVED = "archived"
    FAILED = "failed"


class SkillBundleAssetType(str, Enum):
    """Known asset roles inside a portable bundle."""

    PROCEDURE = "procedure"
    EXAMPLE = "example"
    TEMPLATE = "template"
    SCHEMA = "schema"
    EVAL = "eval"
    REFERENCE = "reference"
    SCRIPT = "script"


class SkillBundleUpdatePolicy(str, Enum):
    """How an installation tracks newer bundle versions."""

    MANUAL = "manual"
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"
    AUTOMATIC = "automatic"
    PINNED = "pinned"


class SkillBundleReviewStatus(str, Enum):
    """Hosted review state for bundle versions and installations."""

    DRAFT = "draft"
    PENDING = "pending"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"


ASSET_KINDS: Mapping[str, str] = {
    "examples": SkillBundleAssetType.EXAMPLE.value,
    "templates": SkillBundleAssetType.TEMPLATE.value,
    "schemas": SkillBundleAssetType.SCHEMA.value,
    "evals": SkillBundleAssetType.EVAL.value,
    "references": SkillBundleAssetType.REFERENCE.value,
    "scripts": SkillBundleAssetType.SCRIPT.value,
}

_ALLOWED_MANIFEST_PATH_ROOTS = frozenset({SKILL_FILENAME, *ASSET_KINDS})


class SkillBundleError(ValueError):
    """Raised when a filesystem bundle is malformed or unsafe."""


class _ManifestModel(BaseModel):
    """Shared Pydantic behavior for skill bundle manifest specs."""

    model_config = ConfigDict(extra="allow", use_enum_values=True)


class SkillBundleRoutingSpec(_ManifestModel):
    """Typed routing hints carried by ``skill.toml``."""

    triggers: list[str | dict[str, Any]] = Field(default_factory=list)
    embedding_text: str | None = None
    keywords: list[str] = Field(default_factory=list)

    @field_validator("embedding_text", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        return _optional_string(value)

    @field_validator("keywords", mode="before")
    @classmethod
    def _keywords_list(cls, value: Any) -> Any:
        if value is None:
            return []
        return value


class SkillBundleRuntimeSpec(_ManifestModel):
    """Default runtime preferences carried by ``skill.toml``."""

    model_config = ConfigDict(extra="ignore", use_enum_values=True)

    default_thinking_tier: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_reasoning_alias(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            payload = dict(value)
            if "default_thinking_tier" not in payload and payload.get("default_reasoning_effort"):
                payload["default_thinking_tier"] = payload.get("default_reasoning_effort")
            return payload
        return value

    @field_validator(
        "default_thinking_tier",
        mode="before",
    )
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        return _optional_string(value)

    @field_validator("default_thinking_tier")
    @classmethod
    def _valid_thinking_tier(cls, value: str | None) -> str | None:
        if value is not None and value not in _REASONING_EFFORTS:
            raise ValueError(
                f"default_thinking_tier must be one of {sorted(_REASONING_EFFORTS)}"
            )
        return value


class SkillBundlePermissionGrantSpec(_ManifestModel):
    """One permission request in a hosted-reviewable bundle manifest."""

    kind: str
    name: str | None = None
    scope: str | None = None
    reason: str | None = None
    required: bool = True

    @field_validator("kind")
    @classmethod
    def _required_kind(cls, value: str) -> str:
        return _required_text_value(value, "kind")

    @field_validator("name", "scope", "reason", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        return _optional_string(value)


class SkillBundlePermissionSpec(_ManifestModel):
    """Typed permission review hints carried by ``skill.toml``."""

    toolsets: list[str] = Field(default_factory=list)
    tools: list[str | SkillBundlePermissionGrantSpec] = Field(default_factory=list)
    resources: list[str | SkillBundlePermissionGrantSpec] = Field(default_factory=list)
    network: list[str | SkillBundlePermissionGrantSpec] = Field(default_factory=list)
    filesystem: list[str | SkillBundlePermissionGrantSpec] = Field(default_factory=list)
    requires_review: bool = False

    @field_validator("toolsets", "tools", "resources", "network", "filesystem", mode="before")
    @classmethod
    def _list_or_default(cls, value: Any) -> Any:
        if value is None:
            return []
        return value


class SkillBundleManifest(_ManifestModel):
    """Validated bundle manifest fields plus the canonical raw manifest."""

    schema_version: int
    name: str
    version: str
    description: str
    display_name: str | None = None
    license: str | None = None
    source: SkillBundleSourceKind | None = None
    visibility: SkillBundleTrustLevel | None = None
    routing: SkillBundleRoutingSpec = Field(default_factory=SkillBundleRoutingSpec)
    runtime: SkillBundleRuntimeSpec = Field(default_factory=SkillBundleRuntimeSpec)
    permissions: SkillBundlePermissionSpec = Field(default_factory=SkillBundlePermissionSpec)
    raw: Mapping[str, Any] = Field(default_factory=dict, exclude=True)

    @property
    def semver(self) -> str:
        """Human package version used by the bundle registry."""
        return self.version

    @field_validator("schema_version", mode="before")
    @classmethod
    def _positive_schema_version(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("schema_version must be an integer")
        if value < 1:
            raise ValueError("schema_version must be positive")
        return value

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        value = _required_text_value(value, "name", max_length=80)
        if not _NAME_RE.fullmatch(value):
            raise ValueError(
                "name must be a portable slug "
                "(letters, numbers, dot, underscore, or hyphen)"
            )
        return value

    @field_validator("version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        return _required_text_value(value, "version", max_length=40)

    @field_validator("description")
    @classmethod
    def _valid_description(cls, value: str) -> str:
        return _required_text_value(value, "description")

    @field_validator("display_name", "license", mode="before")
    @classmethod
    def _optional_text(cls, value: Any) -> str | None:
        return _optional_string(value)

    @field_validator("source", "visibility", mode="before")
    @classmethod
    def _optional_enum_text(cls, value: Any) -> Any:
        return _optional_string(value)


@dataclass(frozen=True, slots=True)
class SkillBundleAsset:
    """A loadable asset discovered inside a skill bundle."""

    path: str
    kind: str
    mime_type: str
    size: int
    sha256: str
    content_text: str | None = None

    @property
    def content_digest(self) -> str:
        return f"sha256:{self.sha256}"

    def to_db_payload(self) -> dict[str, Any]:
        """Return a shape matching the future ``skill_assets`` import row."""
        return {
            "path": self.path,
            "asset_kind": self.kind,
            "mime_type": self.mime_type,
            "size_bytes": self.size,
            "content_digest": self.content_digest,
            "storage_kind": "inline" if self.content_text is not None else "bundle",
            "storage_uri": None,
            "content_text": self.content_text,
            "metadata": {},
        }


@dataclass(frozen=True, slots=True)
class SkillBundle:
    """A fully parsed, digest-verified filesystem skill bundle."""

    root: Path
    manifest: SkillBundleManifest
    skill_markdown: str
    skill_sha256: str
    assets: tuple[SkillBundleAsset, ...]
    digest: str

    @property
    def content_digest(self) -> str:
        return f"sha256:{self.digest}"

    def to_db_bundle_payload(self, *, namespace: str = "local") -> dict[str, Any]:
        """Return stable package identity fields for a future bundle import."""
        return {
            "namespace": namespace,
            "name": self.manifest.name,
            "display_name": _optional_string(self.manifest.raw.get("display_name")),
            "description": self.manifest.description,
            "visibility": self.manifest.visibility or "private_local",
            "source_kind": self.manifest.source or "local",
            "trust_level": self.manifest.visibility or "private_local",
        }

    def to_db_version_payload(self, *, asset_root: str | None = None) -> dict[str, Any]:
        """Return immutable version fields for a future bundle import."""
        raw = self.manifest.raw
        return {
            "semver": self.manifest.semver,
            "content_digest": self.content_digest,
            "manifest": dict(raw),
            "asset_root": asset_root,
            "routing_card": _mapping_payload(raw.get("routing")),
            "permissions": _mapping_payload(raw.get("permissions")),
            "compatibility": _mapping_payload(raw.get("compatibility")),
            "eval_summary": _mapping_payload(raw.get("quality")),
            "signature": _optional_string(raw.get("signature")),
            "provenance": {
                "source": self.manifest.source,
                "bundle_digest": self.content_digest,
            },
        }

    def to_db_asset_payloads(self) -> list[dict[str, Any]]:
        """Return asset rows for future ``skill_assets`` insertion."""
        return [asset.to_db_payload() for asset in self.assets]


def load_skill_bundle(bundle_dir: str | os.PathLike[str]) -> SkillBundle:
    """Parse, validate, and digest a portable skill bundle directory."""
    root = Path(bundle_dir).expanduser()
    if not root.is_dir():
        raise SkillBundleError(f"bundle directory does not exist: {root}")
    root = root.resolve()

    manifest_path = root / MANIFEST_FILENAME
    skill_path = root / SKILL_FILENAME
    _require_regular_file(manifest_path, MANIFEST_FILENAME)
    _require_regular_file(skill_path, SKILL_FILENAME)

    manifest = _load_manifest(manifest_path)
    skill_bytes = skill_path.read_bytes()
    skill_markdown = _decode_utf8(skill_bytes, SKILL_FILENAME)
    skill_sha256 = _sha256_hex(skill_bytes)
    assets = discover_skill_assets(root)
    digest = compute_bundle_digest(manifest, skill_sha256, assets)

    return SkillBundle(
        root=root,
        manifest=manifest,
        skill_markdown=skill_markdown,
        skill_sha256=skill_sha256,
        assets=assets,
        digest=digest,
    )


def parse_skill_bundle(bundle_dir: str | os.PathLike[str]) -> SkillBundle:
    """Alias for callers that prefer parse-oriented naming."""
    return load_skill_bundle(bundle_dir)


def discover_skill_assets(
    bundle_dir: str | os.PathLike[str],
    *,
    max_inline_text_bytes: int = MAX_INLINE_TEXT_BYTES,
) -> tuple[SkillBundleAsset, ...]:
    """Discover safe assets below the known bundle asset roots."""
    root = Path(bundle_dir).expanduser().resolve()
    assets: list[SkillBundleAsset] = []

    for asset_root, asset_kind in ASSET_KINDS.items():
        directory = root / asset_root
        if not directory.exists():
            continue
        if directory.is_symlink():
            raise SkillBundleError(f"asset directory symlinks are not allowed: {asset_root}")
        if not directory.is_dir():
            raise SkillBundleError(f"asset root must be a directory: {asset_root}")

        for current_dir, dirnames, filenames in os.walk(directory, followlinks=False):
            current = Path(current_dir)
            for dirname in list(dirnames):
                child_dir = current / dirname
                if child_dir.is_symlink():
                    rel = _display_relative_path(root, child_dir)
                    raise SkillBundleError(f"asset directory symlinks are not allowed: {rel}")
            dirnames.sort()
            filenames.sort()

            for filename in filenames:
                file_path = current / filename
                if file_path.is_symlink():
                    rel = _display_relative_path(root, file_path)
                    raise SkillBundleError(f"asset file symlinks are not allowed: {rel}")
                if not file_path.is_file():
                    continue
                rel_path = _safe_relative_path(root, file_path)
                _validate_discovered_asset_path(rel_path)
                data = file_path.read_bytes()
                mime_type = _guess_mime_type(file_path)
                assets.append(
                    SkillBundleAsset(
                        path=rel_path,
                        kind=asset_kind,
                        mime_type=mime_type,
                        size=len(data),
                        sha256=_sha256_hex(data),
                        content_text=_maybe_decode_text(
                            file_path,
                            data,
                            mime_type,
                            max_inline_text_bytes=max_inline_text_bytes,
                        ),
                    )
                )

    return tuple(sorted(assets, key=lambda asset: asset.path))


def compute_bundle_digest(
    manifest: SkillBundleManifest,
    skill_sha256: str,
    assets: Sequence[SkillBundleAsset],
) -> str:
    """Compute a deterministic content digest for the bundle artifact."""
    payload = {
        "digest_schema": "illo.skill_bundle.v1",
        "manifest": _canonical_json_data(manifest.raw),
        "skill_md": {"path": SKILL_FILENAME, "sha256": skill_sha256},
        "assets": [
            {
                "path": asset.path,
                "kind": asset.kind,
                "mime_type": asset.mime_type,
                "size": asset.size,
                "sha256": asset.sha256,
            }
            for asset in sorted(assets, key=lambda item: item.path)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_hex(encoded)


def _load_manifest(path: Path) -> SkillBundleManifest:
    try:
        raw_manifest = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SkillBundleError(f"invalid {MANIFEST_FILENAME}: {exc}") from exc

    return validate_skill_bundle_manifest_payload(raw_manifest)


def validate_skill_bundle_manifest_payload(
    raw_manifest: Mapping[str, Any],
) -> SkillBundleManifest:
    """Validate a raw manifest payload without losing its canonical JSON shape."""
    if not isinstance(raw_manifest, Mapping):
        raise SkillBundleError(f"{MANIFEST_FILENAME} must contain a TOML table")

    manifest = _canonical_json_data(raw_manifest)
    if not isinstance(manifest, dict):
        raise SkillBundleError(f"{MANIFEST_FILENAME} must contain a TOML table")
    _normalize_runtime_manifest(manifest)
    _validate_manifest_paths(raw_manifest)

    try:
        parsed = SkillBundleManifest.model_validate(manifest)
    except ValidationError as exc:
        raise SkillBundleError(_format_manifest_validation_error(exc)) from exc
    return parsed.model_copy(update={"raw": manifest})


def _normalize_runtime_manifest(manifest: dict[str, Any]) -> None:
    runtime = manifest.get("runtime")
    if not isinstance(runtime, Mapping):
        return
    normalized = dict(runtime)
    if "default_thinking_tier" not in normalized and normalized.get("default_reasoning_effort"):
        normalized["default_thinking_tier"] = normalized.get("default_reasoning_effort")
    for key in _LEGACY_RUNTIME_PROVIDER_KEYS:
        normalized.pop(key, None)
    manifest["runtime"] = normalized


def coerce_skill_bundle_enum_value(
    value: Any,
    enum_type: type[Enum],
    field_name: str,
) -> str:
    """Return a string enum value with a concise domain error on invalid input."""
    if isinstance(value, enum_type):
        return str(value.value)
    try:
        return str(enum_type(str(value)).value)
    except ValueError as exc:
        allowed = ", ".join(str(item.value) for item in enum_type)
        raise ValueError(f"Invalid {field_name}: {value!r}. Expected one of: {allowed}") from exc


def _require_regular_file(path: Path, label: str) -> None:
    if not path.exists():
        raise SkillBundleError(f"missing required bundle file: {label}")
    if path.is_symlink():
        raise SkillBundleError(f"required bundle file cannot be a symlink: {label}")
    if not path.is_file():
        raise SkillBundleError(f"required bundle path is not a file: {label}")


def _validate_manifest_paths(manifest: Mapping[str, Any]) -> None:
    loading = manifest.get("loading")
    if isinstance(loading, Mapping):
        _validate_path_container(loading, "loading")

    assets = manifest.get("assets")
    if isinstance(assets, Mapping | Sequence) and not isinstance(assets, (str, bytes)):
        _validate_path_container(assets, "assets")

    for key in ASSET_KINDS:
        if key in manifest:
            _validate_path_container(manifest[key], key)


def _validate_path_container(value: Any, field_name: str) -> None:
    if isinstance(value, str):
        _validate_manifest_asset_path(value, field_name)
        return

    if isinstance(value, Mapping):
        for key, child in value.items():
            _validate_path_container(child, f"{field_name}.{key}")
        return

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, child in enumerate(value):
            _validate_path_container(child, f"{field_name}[{index}]")


def _validate_manifest_asset_path(value: str, field_name: str) -> None:
    candidate = value.split("#", 1)[0].strip()
    if not candidate:
        return
    candidate = candidate.rstrip("/")
    if not candidate:
        return
    _validate_portable_path(candidate, field_name)
    root = candidate.split("/", 1)[0]
    if root not in _ALLOWED_MANIFEST_PATH_ROOTS:
        raise SkillBundleError(
            f"manifest path '{field_name}' must point to {SKILL_FILENAME} "
            "or an allowed asset directory"
        )


def _validate_discovered_asset_path(path: str) -> None:
    _validate_portable_path(path, path)
    root = path.split("/", 1)[0]
    if root not in ASSET_KINDS:
        raise SkillBundleError(f"asset path is outside allowed asset roots: {path}")


def _validate_portable_path(path: str, field_name: str) -> None:
    if "\\" in path:
        raise SkillBundleError(f"path '{field_name}' must use POSIX separators")
    if PurePosixPath(path).is_absolute():
        raise SkillBundleError(f"path '{field_name}' must be relative")

    windows_path = PureWindowsPath(path)
    if windows_path.is_absolute() or windows_path.drive:
        raise SkillBundleError(f"path '{field_name}' must be relative")

    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SkillBundleError(f"path '{field_name}' cannot contain traversal segments")


def _safe_relative_path(root: Path, file_path: Path) -> str:
    try:
        rel = file_path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise SkillBundleError(f"asset escapes bundle directory: {file_path}") from exc
    return rel.as_posix()


def _display_relative_path(root: Path, file_path: Path) -> str:
    try:
        return file_path.relative_to(root).as_posix()
    except ValueError:
        return file_path.as_posix()


def _required_text_value(
    value: Any,
    field_name: str,
    *,
    max_length: int | None = None,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    value = value.strip()
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"{field_name} must be {max_length} characters or fewer")
    return value


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _format_manifest_validation_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    loc = ".".join(str(part) for part in first.get("loc", ())) or "manifest"
    message = first.get("msg", "invalid value")
    return f"invalid {MANIFEST_FILENAME} manifest field '{loc}': {message}"


def _mapping_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _canonical_json_data(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical_json_data(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_json_data(item) for item in value]
    if isinstance(value, tuple):
        return [_canonical_json_data(item) for item in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return value


def _guess_mime_type(path: Path) -> str:
    if path.name.endswith(".schema.json"):
        return "application/schema+json"
    override = _MIME_OVERRIDES.get(path.suffix.lower())
    if override:
        return override
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _maybe_decode_text(
    path: Path,
    data: bytes,
    mime_type: str,
    *,
    max_inline_text_bytes: int,
) -> str | None:
    if len(data) > max_inline_text_bytes:
        return None
    if not _looks_like_text(path, mime_type):
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\x00" in text:
        return None
    return text


def _looks_like_text(path: Path, mime_type: str) -> bool:
    return (
        mime_type.startswith("text/")
        or mime_type in _TEXT_MIME_TYPES
        or path.suffix.lower() in _TEXT_SUFFIXES
    )


def _decode_utf8(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillBundleError(f"{label} must be UTF-8 text") from exc


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
