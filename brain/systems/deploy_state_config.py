"""Runtime activation and timing settings for deploy-state wiring."""

from __future__ import annotations

import logging
import os
from datetime import timedelta


logger = logging.getLogger("illo.deploy_state")


def watched_deploy_repos() -> frozenset[str]:
    return frozenset(
        repo.strip().casefold()
        for repo in os.environ.get("ILLO_DEPLOY_SWEEP_REPOS", "").split(",")
        if repo.strip()
    )


def deploy_feature_enabled(repo: str | None = None) -> bool:
    watched = watched_deploy_repos()
    return bool(watched) and (repo is None or str(repo).casefold() in watched)


def _env_duration(name: str, default: float, unit: str) -> timedelta:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        logger.warning("invalid %s=%r; using %s", name, raw, default)
        value = default
    return timedelta(**{unit: max(value, 0)})


def deploy_ticket_object_keys() -> frozenset[str]:
    """Object-type keys that hold tracker tickets.

    The live tracker's object key is runtime data, not a constant — the
    deployed instance uses ``ticket`` while earlier code and fixtures assumed
    ``github_ticket``. Both are matched by default; override with
    ILLO_DEPLOY_TICKET_OBJECT_KEYS when a workspace names its type differently.
    """
    raw = os.environ.get("ILLO_DEPLOY_TICKET_OBJECT_KEYS", "").strip()
    keys = frozenset(key.strip() for key in raw.split(",") if key.strip())
    return keys or frozenset({"ticket", "github_ticket"})


def deploy_settle_window() -> timedelta:
    return _env_duration("ILLO_DEPLOY_SETTLE_MINUTES", 30, "minutes")


def deploy_quiet_window() -> timedelta:
    return _env_duration("ILLO_DEPLOY_QUIET_HOURS", 24, "hours")
