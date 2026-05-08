"""Tests for brain.systems.auth.users — ORM-based implementation.

Tests mock UnitOfWork at the module boundary so no DB is needed.
"""
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from brain.platform.db.models.org import Org, User


def _make_org(id="org-1", name="Test Org", slug="test-org"):
    org = MagicMock(spec=Org)
    org.id = id
    org.name = name
    org.slug = slug
    org.created_at = None
    return org


def _make_user(
    id="user-1",
    name="Alice",
    email="alice@test.com",
    role="owner",
    color="#e07050",
    org_id="org-1",
    password_hash=None,
    approved=True,
    attribution_enabled=True,
):
    user = MagicMock(spec=User)
    user.id = id
    user.name = name
    user.email = email
    user.role = role
    user.color = color
    user.org_id = org_id
    user.password_hash = password_hash
    user.approved = approved
    user.attribution_enabled = attribution_enabled
    user.created_at = None
    return user


class _FakeUoW:
    """Minimal UnitOfWork stub that supports context manager and repos."""

    def __init__(self):
        self.team = MagicMock()
        self.orgs = MagicMock()
        self.session = MagicMock()
        self._committed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if not args[0]:
            self._committed = True

    def commit(self):
        self._committed = True


@pytest.fixture
def uow():
    return _FakeUoW()


@pytest.fixture
def patch_uow(uow):
    with patch("brain.systems.auth.users.UnitOfWork", return_value=uow):
        yield uow


# ── get_user_by_email ──────────────────────────────────────────

class TestGetUserByEmail:
    def test_returns_dict_when_found(self, patch_uow):
        from brain.systems.auth.users import get_user_by_email

        user = _make_user()
        org = _make_org()
        patch_uow.team.get_by_email.return_value = user
        patch_uow.orgs.get.return_value = org

        result = get_user_by_email("Alice@Test.com")

        patch_uow.team.get_by_email.assert_called_once_with("alice@test.com")
        assert result["id"] == "user-1"
        assert result["name"] == "Alice"
        assert result["org_name"] == "Test Org"

    def test_returns_none_when_not_found(self, patch_uow):
        from brain.systems.auth.users import get_user_by_email

        patch_uow.team.get_by_email.return_value = None
        assert get_user_by_email("nobody@test.com") is None


# ── get_user_by_id ─────────────────────────────────────────────

class TestGetUserById:
    def test_returns_dict_when_found(self, patch_uow):
        from brain.systems.auth.users import get_user_by_id

        user = _make_user()
        org = _make_org()
        patch_uow.team.get_by_id.return_value = user
        patch_uow.orgs.get.return_value = org

        result = get_user_by_id("user-1")

        assert result["id"] == "user-1"
        assert result["org_slug"] == "test-org"

    def test_returns_none_when_not_found(self, patch_uow):
        from brain.systems.auth.users import get_user_by_id

        patch_uow.team.get_by_id.return_value = None
        assert get_user_by_id("nope") is None


# ── has_any_users ──────────────────────────────────────────────

class TestHasAnyUsers:
    def test_true(self, patch_uow):
        from brain.systems.auth.users import has_any_users

        patch_uow.team.has_any.return_value = True
        assert has_any_users() is True

    def test_false(self, patch_uow):
        from brain.systems.auth.users import has_any_users

        patch_uow.team.has_any.return_value = False
        assert has_any_users() is False


# ── authenticate ───────────────────────────────────────────────

class TestAuthenticate:
    def test_success(self, patch_uow):
        from brain.systems.auth.users import authenticate
        import bcrypt

        pw_hash = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode()
        user = _make_user(password_hash=pw_hash)
        org = _make_org()
        patch_uow.team.get_by_email.return_value = user
        patch_uow.orgs.get.return_value = org

        result = authenticate("alice@test.com", "secret123")
        assert result is not None
        assert result["id"] == "user-1"

    def test_wrong_password(self, patch_uow):
        from brain.systems.auth.users import authenticate
        import bcrypt

        pw_hash = bcrypt.hashpw(b"secret123", bcrypt.gensalt()).decode()
        user = _make_user(password_hash=pw_hash)
        org = _make_org()
        patch_uow.team.get_by_email.return_value = user
        patch_uow.orgs.get.return_value = org

        result = authenticate("alice@test.com", "wrong")
        assert result is None

    def test_user_not_found(self, patch_uow):
        from brain.systems.auth.users import authenticate

        patch_uow.team.get_by_email.return_value = None
        result = authenticate("nobody@test.com", "pass")
        assert result is None

    def test_no_password_hash(self, patch_uow):
        from brain.systems.auth.users import authenticate

        user = _make_user(password_hash=None)
        org = _make_org()
        patch_uow.team.get_by_email.return_value = user
        patch_uow.orgs.get.return_value = org

        result = authenticate("alice@test.com", "pass")
        assert result is None


# ── create_first_user ──────────────────────────────────────────

class TestCreateFirstUser:
    def test_creates_org_and_user(self, patch_uow):
        from brain.systems.auth.users import create_first_user

        # flush() assigns IDs via side_effect
        flush_count = [0]
        def fake_flush():
            flush_count[0] += 1

        patch_uow.session.flush.side_effect = fake_flush
        patch_uow.session.scalars.return_value.first.return_value = None
        # After create, get_user_by_id is called — mock that path
        user = _make_user(id="new-user")
        org = _make_org()
        patch_uow.team.get_by_id.return_value = user
        patch_uow.orgs.get.return_value = org

        result = create_first_user("Alice", "alice@test.com", "pass123", "My Org")

        assert result["id"] == "new-user"
        assert patch_uow.session.add.call_count == 2  # org + user
        assert patch_uow.session.flush.call_count == 2


# ── get_default_org_id ─────────────────────────────────────────

class TestGetDefaultOrgId:
    def test_returns_id(self, patch_uow):
        from brain.systems.auth.users import get_default_org_id

        patch_uow.orgs.get_first.return_value = _make_org(id="org-42")
        assert get_default_org_id() == "org-42"

    def test_returns_none(self, patch_uow):
        from brain.systems.auth.users import get_default_org_id

        patch_uow.orgs.get_first.return_value = None
        assert get_default_org_id() is None


# ── get_org_users ──────────────────────────────────────────────

class TestGetOrgUsers:
    def test_returns_list(self, patch_uow):
        from brain.systems.auth.users import get_org_users

        u1 = _make_user(id="u1", name="Alice", email="a@t.com", color="#aaa")
        u2 = _make_user(id="u2", name="Bob", email="b@t.com", color="#bbb")
        patch_uow.team.list_by_org.return_value = [u1, u2]

        result = get_org_users("org-1")
        assert len(result) == 2
        assert result[0]["name"] == "Alice"
        assert result[1]["name"] == "Bob"


# ── approve_user ───────────────────────────────────────────────

class TestApproveUser:
    def test_owner_can_approve(self, patch_uow):
        from brain.systems.auth.users import approve_user

        approver = _make_user(id="owner-1", role="owner")
        pending = _make_user(id="pending-1", approved=False)
        patch_uow.team.get_by_id.side_effect = lambda uid: approver if uid == "owner-1" else pending

        assert approve_user("pending-1", "owner-1") is True
        assert pending.approved is True

    def test_member_can_approve_same_org(self, patch_uow):
        from brain.systems.auth.users import approve_user

        member = _make_user(id="member-1", role="member")
        pending = _make_user(id="pending-1", approved=False)
        patch_uow.team.get_by_id.side_effect = lambda uid: member if uid == "member-1" else pending

        assert approve_user("pending-1", "member-1") is True
        assert pending.approved is True

    def test_pending_member_cannot_approve(self, patch_uow):
        from brain.systems.auth.users import approve_user

        member = _make_user(id="member-1", role="member", approved=False)
        patch_uow.team.get_by_id.return_value = member

        assert approve_user("pending-1", "member-1") is False

    def test_cannot_approve_outside_org(self, patch_uow):
        from brain.systems.auth.users import approve_user

        member = _make_user(id="member-1", role="member", org_id="org-1")
        pending = _make_user(id="pending-1", approved=False, org_id="org-2")
        patch_uow.team.get_by_id.side_effect = lambda uid: member if uid == "member-1" else pending

        assert approve_user("pending-1", "member-1") is False
        assert pending.approved is False


# ── reject_user ────────────────────────────────────────────────

class TestRejectUser:
    def test_owner_can_reject(self, patch_uow):
        from brain.systems.auth.users import reject_user

        approver = _make_user(id="owner-1", role="owner")
        pending = _make_user(id="pending-1", approved=False)
        patch_uow.team.get_by_id.side_effect = lambda uid: approver if uid == "owner-1" else pending

        assert reject_user("pending-1", "owner-1") is True
        patch_uow.session.delete.assert_called_once_with(pending)

    def test_cannot_reject_approved(self, patch_uow):
        from brain.systems.auth.users import reject_user

        approver = _make_user(id="owner-1", role="owner")
        approved = _make_user(id="user-1", approved=True)
        patch_uow.team.get_by_id.side_effect = lambda uid: approver if uid == "owner-1" else approved

        assert reject_user("user-1", "owner-1") is False

    def test_cannot_reject_outside_org(self, patch_uow):
        from brain.systems.auth.users import reject_user

        approver = _make_user(id="owner-1", role="owner", org_id="org-1")
        pending = _make_user(id="pending-1", approved=False, org_id="org-2")
        patch_uow.team.get_by_id.side_effect = lambda uid: approver if uid == "owner-1" else pending

        assert reject_user("pending-1", "owner-1") is False
        patch_uow.session.delete.assert_not_called()


# ── safe_user_context ──────────────────────────────────────────

class TestSafeUserContext:
    def test_strips_sensitive_fields(self):
        from brain.systems.auth.users import safe_user_context

        user = {
            "id": "u1", "name": "Alice", "email": "a@t.com",
            "role": "owner", "color": "#aaa", "org_id": "o1",
            "org_name": "Org", "org_slug": "org",
            "password_hash": "SECRET", "attribution_enabled": True,
            "approved": True, "default_provider": "openai",
        }
        result = safe_user_context(user)
        assert "password_hash" not in result
        assert result["name"] == "Alice"
        assert result["approved"] is True
        assert result["default_provider"] == "openai"
