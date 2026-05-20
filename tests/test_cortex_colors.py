"""Tests for cortex color schemas and team color API."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from brain.app.api.routers.team import _normalize_profile_color, async_update_profile
from brain.app.api.schemas.team import CortexColorRead, TeamMemberRead, UserProfileUpdate


class TestCortexColorRead:
    def test_basic(self):
        c = CortexColorRead(id="abc-123", name="Alex", cortex_color="#FF6B35")
        assert c.cortex_color == "#FF6B35"
        assert c.id == "abc-123"

    def test_id_serialized_to_string(self):
        from uuid import uuid4
        uid = uuid4()
        c = CortexColorRead(id=uid, name="Riley", cortex_color="#4ECDC4")
        assert c.model_dump()["id"] == str(uid)


class TestTeamMemberReadCortexColor:
    def test_cortex_color_populated_from_color(self):
        from datetime import datetime, timezone
        m = TeamMemberRead(
            id="u1", name="Alex", email="a@x.co", role="admin",
            color="#FF6B35", created_at=datetime.now(timezone.utc),
        )
        assert m.cortex_color == "#FF6B35"

    def test_cortex_color_explicit_override(self):
        from datetime import datetime, timezone
        m = TeamMemberRead(
            id="u1", name="Alex", email="a@x.co", role="admin",
            color="#FF6B35", cortex_color="#000000",
            created_at=datetime.now(timezone.utc),
        )
        assert m.cortex_color == "#000000"


class TestUserProfileUpdate:
    def test_color_update(self):
        u = UserProfileUpdate(color="#FF6B35")
        assert u.color == "#FF6B35"

    def test_partial_update(self):
        u = UserProfileUpdate()
        dumped = u.model_dump(exclude_unset=True)
        assert dumped == {}


class TestUpdateProfile:
    def test_normalizes_short_hex_color(self):
        assert _normalize_profile_color("#AbC") == "#aabbcc"

    @pytest.mark.asyncio
    async def test_rejects_duplicate_workspace_color(self):
        class _Db:
            async def scalar(self, stmt):
                return "user-2"

            async def flush(self):
                pass

        current_user = SimpleNamespace(id="user-1", org_id="org-1", name="Alex", color="#5ea898")

        with patch("brain.app.api.routers.team.TeamRepository") as MockRepo:
            MockRepo.return_value.a_get = AsyncMock(return_value=current_user)

            with pytest.raises(HTTPException) as exc:
                await async_update_profile(
                    body=UserProfileUpdate(color="#63abc4"),
                    db=_Db(),
                    user={"id": "user-1"},
                )

        assert exc.value.status_code == 409
        assert "color is already taken" in exc.value.detail

    @pytest.mark.asyncio
    async def test_rejects_duplicate_workspace_name(self):
        class _Db:
            async def scalar(self, stmt):
                return "user-2"

            async def flush(self):
                pass

        current_user = SimpleNamespace(id="user-1", org_id="org-1", name="Alex", color="#5ea898")

        with patch("brain.app.api.routers.team.TeamRepository") as MockRepo:
            MockRepo.return_value.a_get = AsyncMock(return_value=current_user)

            with pytest.raises(HTTPException) as exc:
                await async_update_profile(
                    body=UserProfileUpdate(name="Maya"),
                    db=_Db(),
                    user={"id": "user-1"},
                )

        assert exc.value.status_code == 409
        assert "name is already taken" in exc.value.detail

    @pytest.mark.asyncio
    async def test_updates_unique_name_and_color(self):
        class _Db:
            def __init__(self):
                self.flushed = False

            async def scalar(self, stmt):
                return None

            async def flush(self):
                self.flushed = True

        current_user = SimpleNamespace(id="user-1", org_id="org-1", name="Alex", color="#5ea898")
        db = _Db()

        with patch("brain.app.api.routers.team.TeamRepository") as MockRepo:
            MockRepo.return_value.a_get = AsyncMock(return_value=current_user)
            result = await async_update_profile(
                body=UserProfileUpdate(name="Alex E", color="#ABC"),
                db=db,
                user={"id": "user-1"},
            )

        assert result == {"updated": True}
        assert current_user.name == "Alex E"
        assert current_user.color == "#aabbcc"
        assert db.flushed is True

    @pytest.mark.asyncio
    async def test_async_updates_unique_name_and_color(self):
        class _Db:
            def __init__(self):
                self.flushed = False

            async def get(self, model, id):
                assert id == "user-1"
                return current_user

            async def scalar(self, stmt):
                return None

            async def flush(self):
                self.flushed = True

        current_user = SimpleNamespace(id="user-1", org_id="org-1", name="Alex", color="#5ea898")
        db = _Db()

        result = await async_update_profile(
            body=UserProfileUpdate(name="Alex E", color="#ABC"),
            db=db,
            user={"id": "user-1"},
        )

        assert result == {"updated": True}
        assert current_user.name == "Alex E"
        assert current_user.color == "#aabbcc"
        assert db.flushed is True
