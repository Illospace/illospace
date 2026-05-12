"""
User authentication helpers for the Illo Brain dashboard.

Handles password verification, user lookup, and session management.
bcrypt is used for password hashing — never stores plaintext.
"""
from __future__ import annotations

import logging
import random
import re
from typing import Optional

import bcrypt
from sqlalchemy import select

from brain.platform.db.models.org import Org, User
from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work

logger = logging.getLogger(__name__)

# Curated palette for dark backgrounds — user identity colors
_USER_COLORS = [
    '#e07050', '#50a0e0', '#e0a040', '#60c090', '#c070d0',
    '#e06090', '#50c0c0', '#a0b030', '#7080e0', '#d08060',
]


def _slug_base(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().strip()).strip("-")
    return (slug or "workspace")[:50].strip("-") or "workspace"


def _unique_org_slug(uow: UnitOfWork, name: str) -> str:
    base = _slug_base(name)
    slug = base
    suffix = 2
    while uow.session.scalars(select(Org.id).where(Org.slug == slug).limit(1)).first() is not None:
        suffix_text = f"-{suffix}"
        slug = f"{base[:50 - len(suffix_text)].rstrip('-')}{suffix_text}"
        suffix += 1
    return slug


async def _async_unique_org_slug(uow: UnitOfWork, name: str) -> str:
    base = _slug_base(name)
    slug = base
    suffix = 2
    while True:
        result = await uow.session.scalars(select(Org.id).where(Org.slug == slug).limit(1))
        if result.first() is None:
            return slug
        suffix_text = f"-{suffix}"
        slug = f"{base[:50 - len(suffix_text)].rstrip('-')}{suffix_text}"
        suffix += 1


def _user_to_dict(user: User, org: Org | None = None) -> dict:
    """Convert a User ORM object to a dict matching the old API."""
    d = {
        "id": str(user.id),
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "color": user.color,
        "org_id": str(user.org_id),
        "attribution_enabled": user.attribution_enabled,
        "approved": user.approved,
        "default_provider": user.default_provider,
        "password_hash": user.password_hash,
    }
    if org:
        d["org_name"] = org.name
        d["org_slug"] = org.slug
    return d


def _user_with_org(uow: UnitOfWork, user: User) -> dict:
    """Fetch a user's org and return the combined dict."""
    org = uow.orgs.get(user.org_id)
    return _user_to_dict(user, org)


async def _async_user_with_org(uow: UnitOfWork, user: User) -> dict:
    """Fetch a user's org and return the combined dict using async DB access."""

    org = await uow.orgs.get(user.org_id)
    return _user_to_dict(user, org)


def get_user_by_email(email: str) -> Optional[dict]:
    """Return user row by email, or None if not found."""
    with open_unit_of_work(UnitOfWork) as uow:
        user = uow.team.get_by_email(email.lower().strip())
        if not user:
            return None
        return _user_with_org(uow, user)


async def async_get_user_by_email(email: str) -> Optional[dict]:
    """Return user row by email via async DB access, or None if not found."""

    async with UnitOfWork() as uow:
        user = await uow.team.get_by_email(email.lower().strip())
        if not user:
            return None
        return await _async_user_with_org(uow, user)


def get_user_by_id(user_id: str) -> Optional[dict]:
    """Return user row by UUID, or None if not found."""
    with open_unit_of_work(UnitOfWork) as uow:
        user = uow.team.get_by_id(str(user_id))
        if not user:
            return None
        return _user_with_org(uow, user)


async def async_get_user_by_id(user_id: str) -> Optional[dict]:
    """Return user row by UUID via async DB access, or None if not found."""

    async with UnitOfWork() as uow:
        user = await uow.team.get_by_id(str(user_id))
        if not user:
            return None
        return await _async_user_with_org(uow, user)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain password matches the bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception as e:
        logger.warning("bcrypt verify failed: %s", e)
        return False


# Pre-compute a dummy hash to avoid timing attacks without computing one per-call
_DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt()).decode()


def authenticate(email: str, password: str) -> Optional[dict]:
    """
    Attempt login. Returns user dict on success, None on failure.
    Never raises — caller handles the None case.
    """
    user = get_user_by_email(email)
    if not user:
        # Constant-time dummy check to prevent user enumeration
        bcrypt.checkpw(b"dummy", _DUMMY_HASH.encode())
        return None
    if not user.get("password_hash"):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


async def async_authenticate(email: str, password: str) -> Optional[dict]:
    """Async DB variant of authenticate."""

    user = await async_get_user_by_email(email)
    if not user:
        bcrypt.checkpw(b"dummy", _DUMMY_HASH.encode())
        return None
    if not user.get("password_hash"):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def has_any_users() -> bool:
    """Return True if at least one user exists."""
    with open_unit_of_work(UnitOfWork) as uow:
        return uow.team.has_any()


async def async_has_any_users() -> bool:
    """Return True if at least one user exists using async DB access."""

    async with UnitOfWork() as uow:
        return bool(await uow.team.has_any())


def create_first_user(name: str, email: str, password: str, org_name: str) -> dict:
    """Create the first org + user (setup flow). Returns user dict."""
    return create_workspace_owner(name, email, password, org_name)


async def async_create_first_user(name: str, email: str, password: str, org_name: str) -> dict:
    """Async variant for first-user setup."""

    return await async_create_workspace_owner(name, email, password, org_name)


def create_workspace_owner(name: str, email: str, password: str, org_name: str) -> dict:
    """Create a new workspace and make the registering user its approved owner."""
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with open_unit_of_work(UnitOfWork) as uow:
        slug = _unique_org_slug(uow, org_name)
        org = Org(name=org_name.strip(), slug=slug)
        uow.session.add(org)
        uow.session.flush()  # get org.id

        user = User(
            name=name.strip(),
            email=email.lower().strip(),
            password_hash=hashed,
            role="owner",
            org_id=org.id,
            approved=True,
            color=random.choice(_USER_COLORS),
        )
        uow.session.add(user)
        uow.session.flush()
        user_id = user.id
    # Re-fetch with org for safe_user_context
    return get_user_by_id(user_id)


async def async_create_workspace_owner(name: str, email: str, password: str, org_name: str) -> dict:
    """Create a new workspace and approved owner using async DB access."""

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    async with UnitOfWork() as uow:
        slug = await _async_unique_org_slug(uow, org_name)
        org = Org(name=org_name.strip(), slug=slug)
        uow.session.add(org)
        await uow.session.flush()

        user = User(
            name=name.strip(),
            email=email.lower().strip(),
            password_hash=hashed,
            role="owner",
            org_id=org.id,
            approved=True,
            color=random.choice(_USER_COLORS),
        )
        uow.session.add(user)
        await uow.session.flush()
        user_id = user.id
    result = await async_get_user_by_id(user_id)
    if result is None:
        raise RuntimeError("created user could not be reloaded")
    return result


def create_user(name: str, email: str, password: str, org_id: str, role: str = "member") -> dict:
    """Create a new user in an existing org. Returns user dict."""
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    with open_unit_of_work(UnitOfWork) as uow:
        user = User(
            name=name.strip(),
            email=email.lower().strip(),
            password_hash=hashed,
            role=role,
            org_id=org_id,
            color=random.choice(_USER_COLORS),
        )
        uow.session.add(user)
        uow.session.flush()
        user_id = user.id
    return get_user_by_id(user_id)


async def async_create_user(name: str, email: str, password: str, org_id: str, role: str = "member") -> dict:
    """Create a new user in an existing org using async DB access."""

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    async with UnitOfWork() as uow:
        user = User(
            name=name.strip(),
            email=email.lower().strip(),
            password_hash=hashed,
            role=role,
            org_id=org_id,
            color=random.choice(_USER_COLORS),
        )
        uow.session.add(user)
        await uow.session.flush()
        user_id = user.id
    result = await async_get_user_by_id(user_id)
    if result is None:
        raise RuntimeError("created user could not be reloaded")
    return result


def get_default_org_id() -> Optional[str]:
    """Return the first org's ID (for self-registration on single-org deploys)."""
    summary = get_default_org_summary()
    return summary["id"] if summary else None


async def async_get_default_org_id() -> Optional[str]:
    summary = await async_get_default_org_summary()
    return summary["id"] if summary else None


def _org_summary(org: Org | None) -> dict | None:
    if not org:
        return None
    return {"id": str(org.id), "name": org.name, "slug": org.slug}


def get_default_org_summary() -> dict | None:
    """Return the first org summary shown on the public signup screen."""
    with open_unit_of_work(UnitOfWork) as uow:
        org = uow.orgs.get_first()
        return _org_summary(org)


async def async_get_default_org_summary() -> dict | None:
    """Return the first org summary using async DB access."""

    async with UnitOfWork() as uow:
        org = await uow.orgs.get_first()
        return _org_summary(org)


def get_org_summary_by_slug(slug: str) -> dict | None:
    """Return an org summary by slug for invite/signup links."""
    cleaned = (slug or "").strip().lower()
    if not cleaned:
        return None
    with open_unit_of_work(UnitOfWork) as uow:
        stmt = select(Org).where(Org.slug == cleaned).limit(1)
        return _org_summary(uow.session.scalars(stmt).first())


async def async_get_org_summary_by_slug(slug: str) -> dict | None:
    """Return an org summary by slug using async DB access."""

    cleaned = (slug or "").strip().lower()
    if not cleaned:
        return None
    async with UnitOfWork() as uow:
        stmt = select(Org).where(Org.slug == cleaned).limit(1)
        result = await uow.session.scalars(stmt)
        return _org_summary(result.first())


def get_org_users(org_id: str) -> list[dict]:
    """Return all users in the given org (for sharing pickers, etc.)."""
    with open_unit_of_work(UnitOfWork) as uow:
        users = uow.team.list_by_org(org_id)
        return [{"id": str(u.id), "name": u.name, "email": u.email, "color": u.color} for u in users]


def get_all_users() -> list[dict]:
    """Return all users (for nightly per-user processing)."""
    with open_unit_of_work(UnitOfWork) as uow:
        users = uow.team.list_all()
        return [{"id": str(u.id), "name": u.name} for u in users]


def get_all_orgs() -> list[dict]:
    """Return all orgs."""
    with open_unit_of_work(UnitOfWork) as uow:
        from sqlalchemy import select
        orgs = uow.session.scalars(select(Org).order_by(Org.name)).all()
        return [{"id": str(o.id), "name": o.name, "slug": o.slug} for o in orgs]


def safe_user_context(user: dict) -> dict:
    """
    Strip sensitive fields before injecting into session / agent context.
    Returns only the fields safe to expose.
    """
    return {
        "id":                  str(user["id"]),
        "name":                user["name"],
        "email":               user["email"],
        "role":                user["role"],
        "color":               user["color"],
        "org_id":              str(user["org_id"]),
        "org_name":            user.get("org_name", ""),
        "org_slug":            user.get("org_slug", ""),
        "attribution_enabled": user.get("attribution_enabled", True),
        "approved":            user.get("approved", False),
        "default_provider":    user.get("default_provider"),
    }


def get_pending_users(org_id: str) -> list[dict]:
    """Return unapproved users in the org."""
    with open_unit_of_work(UnitOfWork) as uow:
        users = uow.team.list_pending(org_id)
        return [
            {"id": str(u.id), "name": u.name, "email": u.email, "color": u.color, "created_at": u.created_at}
            for u in users
        ]


def approve_user(user_id: str, approver_id: str) -> bool:
    """Approve a pending user in the approver's org."""
    with open_unit_of_work(UnitOfWork) as uow:
        approver = uow.team.get_by_id(approver_id)
        if not approver or not approver.approved:
            return False
        user = uow.team.get_by_id(user_id)
        if not user or user.approved or str(user.org_id) != str(approver.org_id):
            return False
        user.approved = True
        return True


def reject_user(user_id: str, approver_id: str) -> bool:
    """Reject (delete) a pending user. Only owners can reject."""
    with open_unit_of_work(UnitOfWork) as uow:
        approver = uow.team.get_by_id(approver_id)
        if not approver or approver.role != "owner":
            return False
        user = uow.team.get_by_id(user_id)
        if not user or user.approved or str(user.org_id) != str(approver.org_id):
            return False
        uow.session.delete(user)
        return True
