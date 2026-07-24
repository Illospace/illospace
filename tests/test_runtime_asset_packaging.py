"""Runtime data files the code reads must actually ship in the image.

`deploy/compose/provider-alert-severity.json` was added as a runtime
dependency without a matching Dockerfile COPY. Nothing failed until the Slack
connector was recreated on the new image, at which point every provider alert
raised ProviderAlertPolicyError and Illo went silent in the alerts channel.
This guards the whole class: any `BRAIN_DIR/deploy/compose/<file>` the code
resolves at runtime must be copied into the image.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
API_DOCKERFILE = REPO_ROOT / "deploy" / "docker" / "api.Dockerfile"

# Matches: Path(...BRAIN_DIR) / "deploy" / "compose" / "<name>.json"
_COMPOSE_ASSET_RE = re.compile(
    r'BRAIN_DIR\s*\)?\s*/\s*"deploy"\s*/\s*"compose"\s*/\s*"([^"]+)"'
)


def _referenced_compose_assets() -> set[str]:
    assets: set[str] = set()
    for path in (REPO_ROOT / "brain").rglob("*.py"):
        assets.update(_COMPOSE_ASSET_RE.findall(path.read_text(encoding="utf-8")))
    return assets


def test_runtime_compose_assets_are_copied_into_the_image():
    referenced = _referenced_compose_assets()
    assert referenced, "expected to find deploy/compose runtime assets referenced in brain/"

    dockerfile = API_DOCKERFILE.read_text(encoding="utf-8")
    copied = set(re.findall(r"COPY\s+deploy/compose/(\S+)\s", dockerfile))

    missing = sorted(referenced - copied)
    assert not missing, (
        "runtime assets read from deploy/compose are missing a COPY line in "
        f"deploy/docker/api.Dockerfile: {missing}"
    )


def test_referenced_compose_assets_exist_in_the_repo():
    missing = sorted(
        name
        for name in _referenced_compose_assets()
        if not (REPO_ROOT / "deploy" / "compose" / name).is_file()
    )
    assert not missing, f"referenced deploy/compose assets do not exist: {missing}"
