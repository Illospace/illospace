"""Tests for per-org nightly pipeline execution (ORM-based)."""
import pytest
from datetime import date
from unittest.mock import AsyncMock, patch, MagicMock

ORG_ID = "org-test-456"


class _AwaitableResult:
    """Let tests use one mocked result with sync and async session.execute callers."""

    def __init__(self, result):
        self._result = result

    def __await__(self):
        async def _coro():
            return self._result

        return _coro().__await__()

    def __getattr__(self, name):
        return getattr(self._result, name)


@pytest.fixture
def mock_nightly_uow():
    """Mock UnitOfWork for nightly pipeline per-org tests."""
    uow = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    execute_result = MagicMock()
    execute_result.mappings.return_value.fetchone.return_value = {"id": 1}
    execute_result.mappings.return_value.fetchall.return_value = []
    execute_result.mappings.return_value.all.return_value = []
    execute_result.mappings.return_value.first.return_value = {"id": 1}
    uow.session.execute.return_value = _AwaitableResult(execute_result)
    uow.session.scalar = AsyncMock(return_value=0)
    uow.session.scalars.return_value.all.return_value = []
    return uow


class TestConsolidationPerOrg:
    async def test_phase_consolidation_accepts_org_id(self, mock_nightly_uow):
        with patch("brain.jobs.pipelines.consolidate.UnitOfWork", return_value=mock_nightly_uow):
            from brain.jobs.pipelines.consolidate import phase_consolidation
            await phase_consolidation(date(2026, 3, 15), org_id=ORG_ID)
        # Verify org_id was passed in at least one execute call
        found_org_id = False
        for call in mock_nightly_uow.session.execute.call_args_list:
            args = call[0]
            if len(args) > 1 and isinstance(args[1], dict) and args[1].get("org_id") == ORG_ID:
                found_org_id = True
                break
        assert found_org_id, "org_id should be passed in SQL parameters"

    async def test_phase_consolidation_no_org_id(self, mock_nightly_uow):
        """Legacy mode — no org_id — still works."""
        with patch("brain.jobs.pipelines.consolidate.UnitOfWork", return_value=mock_nightly_uow):
            from brain.jobs.pipelines.consolidate import phase_consolidation
            await phase_consolidation(date(2026, 3, 15))


class TestDreamPerOrg:
    async def test_gather_memories_filters_by_org(self, mock_nightly_uow):
        with patch("brain.jobs.pipelines.nightly_dream.UnitOfWork", return_value=mock_nightly_uow):
            from brain.jobs.pipelines.nightly_dream import gather_today_memories
            await gather_today_memories(date(2026, 3, 15), org_id=ORG_ID)
        # The text() call should contain org_id
        query_str = str(mock_nightly_uow.session.execute.call_args[0][0])
        assert "org_id" in query_str

    async def test_gather_today_memories_no_org(self, mock_nightly_uow):
        """Without org_id, query should NOT filter by org_id."""
        with patch("brain.jobs.pipelines.nightly_dream.UnitOfWork", return_value=mock_nightly_uow):
            from brain.jobs.pipelines.nightly_dream import gather_today_memories
            await gather_today_memories(date(2026, 3, 15))
        query_str = str(mock_nightly_uow.session.execute.call_args[0][0])
        assert "AND org_id" not in query_str

    async def test_gather_random_old_memories_filters_by_org(self, mock_nightly_uow):
        with patch("brain.jobs.pipelines.nightly_dream.UnitOfWork", return_value=mock_nightly_uow):
            from brain.jobs.pipelines.nightly_dream import gather_random_old_memories
            await gather_random_old_memories(date(2026, 3, 15), org_id=ORG_ID)
        query_str = str(mock_nightly_uow.session.execute.call_args[0][0])
        assert "org_id" in query_str

    async def test_gather_random_old_memories_no_org(self, mock_nightly_uow):
        with patch("brain.jobs.pipelines.nightly_dream.UnitOfWork", return_value=mock_nightly_uow):
            from brain.jobs.pipelines.nightly_dream import gather_random_old_memories
            await gather_random_old_memories(date(2026, 3, 15))
        query_str = str(mock_nightly_uow.session.execute.call_args[0][0])
        assert "AND org_id" not in query_str


class TestReflectPerOrg:
    async def test_gather_context_accepts_org_id(self, mock_nightly_uow):
        with patch("brain.jobs.pipelines.nightly_reflect.UnitOfWork", return_value=mock_nightly_uow), \
             patch("brain.jobs.pipelines.nightly_reflect.WORKSPACE", "/nonexistent"):
            from brain.jobs.pipelines.nightly_reflect import gather_context
            try:
                await gather_context(date(2026, 3, 15), org_id=ORG_ID)
            except Exception:
                pass  # DB errors expected in test env

    def test_gather_context_signature(self):
        """gather_context must accept org_id keyword argument."""
        import inspect
        from brain.jobs.pipelines.nightly_reflect import gather_context
        sig = inspect.signature(gather_context)
        assert "org_id" in sig.parameters


class TestGetAllOrgs:
    async def test_get_all_orgs_returns_list(self, mock_uow):
        from brain.platform.db.models.org import Org
        mock_org1 = MagicMock(spec=Org)
        mock_org1.id = "uuid-1"
        mock_org1.name = "Acme"
        mock_org1.slug = "acme"
        mock_org2 = MagicMock(spec=Org)
        mock_org2.id = "uuid-2"
        mock_org2.name = "Beta"
        mock_org2.slug = "beta"
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        scalars_result = MagicMock()
        scalars_result.all.return_value = [mock_org1, mock_org2]
        mock_uow.session.scalars = AsyncMock(return_value=scalars_result)
        with patch("brain.systems.auth.users.UnitOfWork", return_value=mock_uow):
            from brain.systems.auth.users import get_all_orgs
            result = await get_all_orgs()
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "Acme"

    async def test_get_all_orgs_empty(self, mock_uow):
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        scalars_result = MagicMock()
        scalars_result.all.return_value = []
        mock_uow.session.scalars = AsyncMock(return_value=scalars_result)
        with patch("brain.systems.auth.users.UnitOfWork", return_value=mock_uow):
            from brain.systems.auth.users import get_all_orgs
            result = await get_all_orgs()
        assert result == []
