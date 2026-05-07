from sqlalchemy import inspect
from brain.platform.db.models.org import Org, User, UserApiKey, OrgApiKey, ApiKeyShare


def test_org_columns():
    cols = {c.name for c in inspect(Org).columns}
    assert cols >= {"id", "name", "slug", "created_at"}


def test_org_tablename():
    assert Org.__tablename__ == "orgs"


def test_user_columns():
    cols = {c.name for c in inspect(User).columns}
    assert cols >= {
        "id", "org_id", "name", "email", "role", "password_hash",
        "approved", "created_at", "color", "vault_salt",
        "attribution_enabled", "default_api_key_id",
    }


def test_user_tablename():
    assert User.__tablename__ == "users"


def test_user_api_key_columns():
    cols = {c.name for c in inspect(UserApiKey).columns}
    assert cols >= {
        "id", "user_id", "provider", "encrypted_key",
        "is_active", "label", "total_tokens_used", "estimated_cost_usd",
    }


def test_org_api_key_columns():
    cols = {c.name for c in inspect(OrgApiKey).columns}
    assert cols >= {
        "id", "org_id", "provider", "encrypted_key",
        "label", "total_tokens_used", "estimated_cost_usd",
    }


def test_api_key_share_columns():
    cols = {c.name for c in inspect(ApiKeyShare).columns}
    assert cols >= {
        "id", "api_key_id", "shared_with_user_id",
        "shared_by_user_id", "shared_at", "revoked_at",
    }
