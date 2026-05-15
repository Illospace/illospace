"""Memory API routes must pass real viewer scope to repositories."""

from unittest.mock import AsyncMock, MagicMock, patch


ORG_ID = "org-test-123"
USER_ID = "user-test-456"


def _user(org_id=ORG_ID):
    return {
        "id": USER_ID,
        "org_id": org_id,
        "role": "owner",
        "name": "Test",
        "email": "test@test.com",
    }


def _uow():
    uow = MagicMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    uow.memories.get_graph_data = AsyncMock(return_value={"nodes": [], "edges": []})
    uow.memories.search_visible = AsyncMock(return_value=[])
    uow.memories.get_or_raise_visible = AsyncMock(return_value=MagicMock(id=1, content="Scoped memory"))
    uow.memories.list_org_memories = AsyncMock(return_value=[])
    uow.edges.neighborhood = AsyncMock(return_value=[])
    return uow


async def test_memory_routes_build_org_scoped_visibility_contexts():
    from brain.app.api.routers.memory import (
        get_graph,
        get_memory,
        get_neighborhood,
        list_org_memories,
        search_memories,
    )

    uow = _uow()
    with patch("brain.app.api.routers.memory.UnitOfWork", return_value=uow):
        await get_graph(user=_user())
        await search_memories("query", user=_user())
        await get_memory(1, user=_user())
        await get_neighborhood(1, user=_user())
        await list_org_memories(limit=25, offset=5, user=_user())

    contexts = [
        uow.memories.get_graph_data.call_args.kwargs["context"],
        uow.memories.search_visible.call_args.args[1],
        uow.memories.get_or_raise_visible.call_args.args[1],
        uow.edges.neighborhood.call_args.kwargs["context"],
        uow.memories.list_org_memories.call_args.args[0],
    ]
    assert [(context.user_id, context.org_id, context.allow_global) for context in contexts] == [
        (USER_ID, ORG_ID, False),
        (USER_ID, ORG_ID, False),
        (USER_ID, ORG_ID, False),
        (USER_ID, ORG_ID, False),
        (USER_ID, ORG_ID, False),
    ]


async def test_org_memory_list_without_org_avoids_repository():
    from brain.app.api.routers.memory import list_org_memories

    with patch("brain.app.api.routers.memory.UnitOfWork") as unit_of_work:
        result = await list_org_memories(limit=25, offset=0, user=_user(org_id=None))

    assert result == []
    unit_of_work.assert_not_called()
