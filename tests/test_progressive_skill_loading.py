"""Progressive skill loading MCP contract tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class _Mappings:
    def __init__(self, *, one_value=None, all_value=None):
        self._one_value = one_value
        self._all_value = all_value

    def one(self):
        return self._one_value

    def first(self):
        return self._one_value

    def all(self):
        return self._all_value


class _ExecuteResult:
    def __init__(self, *, one_value=None, all_value=None):
        self._mappings = _Mappings(one_value=one_value, all_value=all_value)

    def mappings(self):
        return self._mappings


class _FakeUow:
    def __init__(self, *, skill=None, assets=None, execute_results=None):
        self.skills = SimpleNamespace(get_by_name=lambda name: skill)
        self.skill_bundles = SimpleNamespace(list_assets=lambda version_id: assets or [])
        self.memories = SimpleNamespace(
            guardrail_memories_for_task=MagicMock(return_value=[{"content": "Check migrations before deploy"}])
        )
        self.session = SimpleNamespace(
            execute=MagicMock(side_effect=execute_results or [])
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _skill(**overrides):
    defaults = {
        "name": "develop",
        "description": "Ship focused changes.",
        "procedure": "1. Inspect\n2. Patch\n3. Test\n",
        "version": 3,
        "bundle_version_id": 7,
        "bundle_digest": "sha256:bundle",
        "overlay_revision": None,
        "effective_digest": "sha256:effective",
        "source_kind": "illo-core",
        "trust_level": "illo_core",
        "pitfalls": [{"text": "Do not skip tests", "severity": "high"}],
        "triggers": [{"pattern": "fix bug"}],
        "guardrails": [],
        "graduated_steps": [{"condition": "touch migrations", "action": "run alembic"}],
        "maturity": "proficient",
        "confidence": 0.8,
        "use_count": 4,
        "success_count": 3,
        "failure_count": 1,
        "model_tier": "medium",
        "thinking_tier": "medium",
        "skill_type": "skill",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_brain_skills_returns_catalog_card_without_procedure():
    from brain.app.mcp.server import tool_brain_skills

    asset = SimpleNamespace(
        path="examples/happy.md",
        asset_kind="example",
        mime_type="text/markdown",
    )
    row = {
        "id": 1,
        "name": "develop",
        "description": "Ship focused changes.",
        "version": 3,
        "maturity": "proficient",
        "confidence": 0.8,
        "use_count": 4,
        "success_rate": 0.75,
        "model_tier": "medium",
        "thinking_tier": "medium",
        "pitfalls": [],
        "triggers": [],
        "bundle_version_id": 7,
        "bundle_digest": "sha256:bundle",
        "overlay_revision": None,
        "effective_digest": "sha256:effective",
        "source_kind": "illo-core",
        "trust_level": "illo_core",
        "skill_match": 0.91,
        "centroid_match": None,
        "centroid_count": 0,
    }
    uow = _FakeUow(
        assets=[asset],
        execute_results=[
            _ExecuteResult(one_value={"cnt": 1}),
            _ExecuteResult(all_value=[row]),
        ],
    )

    with patch("brain.app.mcp.server.UnitOfWork", return_value=uow), \
         patch("brain.systems.memory.embeddings.embed_query", return_value=[0.1]), \
         patch("brain.systems.memory.embeddings.vec_to_pg", return_value="[0.1]"):
        result = tool_brain_skills("fix a bug")

    assert {"task", "strategy", "recommended_skills", "guardrails"} <= set(result)
    card = result["recommended_skills"][0]
    assert card["name"] == "develop"
    assert card["description"] == "Ship focused changes."
    assert card["loaded_sections"] == ["catalog"]
    assert card["available_sections"][:3] == ["card", "summary", "procedure"]
    assert card["load_tools"]["card"]["arguments"] == {"name": "develop", "section": "card"}
    assert card["load_tools"]["summary"]["arguments"] == {"name": "develop", "section": "summary"}
    assert card["load_tools"]["procedure"]["tool"] == "skill_view"
    assert card["assets"] == [{
        "path": "examples/happy.md",
        "kind": "example",
        "mime_type": "text/markdown",
    }]
    assert card["load_tools"]["assets"]["available_paths"] == ["examples/happy.md"]
    assert "procedure" not in card
    assert "maturity" not in card
    assert "confidence" not in card
    assert "composite_score" not in card


def test_brain_skills_surfaces_manage_domains_for_domain_tool_tasks():
    from brain.app.mcp.server import tool_brain_skills

    develop_row = {
        "id": 1,
        "name": "develop",
        "description": "Ship focused changes.",
        "version": 3,
        "maturity": "proficient",
        "confidence": 0.8,
        "use_count": 4,
        "success_rate": 0.75,
        "model_tier": "medium",
        "thinking_tier": "medium",
        "pitfalls": [],
        "triggers": [],
        "bundle_version_id": 7,
        "bundle_digest": "sha256:bundle",
        "overlay_revision": None,
        "effective_digest": "sha256:effective",
        "source_kind": "illo-core",
        "trust_level": "illo_core",
        "skill_match": 0.22,
        "centroid_match": None,
        "centroid_count": 0,
    }
    domain_row = {
        **develop_row,
        "id": 2,
        "name": "manage-domains",
        "description": "Create and maintain org-wide custom Domains.",
        "skill_match": 1.0,
    }
    uow = _FakeUow(
        execute_results=[
            _ExecuteResult(one_value={"cnt": 2}),
            _ExecuteResult(all_value=[develop_row]),
            _ExecuteResult(one_value=domain_row),
        ],
    )

    with patch("brain.app.mcp.server.UnitOfWork", return_value=uow), \
         patch("brain.systems.memory.embeddings.embed_query", return_value=[0.1]), \
         patch("brain.systems.memory.embeddings.vec_to_pg", return_value="[0.1]"):
        result = tool_brain_skills("using the domains tool, do you see it?")

    assert result["recommended_skills"][0]["name"] == "manage-domains"


def test_brain_skills_uses_structured_trigger_matches_for_private_db_skills():
    from brain.app.mcp.server import tool_brain_skills

    develop_row = {
        "id": 1,
        "name": "develop",
        "description": "Ship focused changes.",
        "version": 3,
        "maturity": "proficient",
        "confidence": 0.8,
        "use_count": 4,
        "success_rate": 0.75,
        "model_tier": "medium",
        "thinking_tier": "medium",
        "pitfalls": [],
        "triggers": [],
        "bundle_version_id": None,
        "bundle_digest": None,
        "overlay_revision": None,
        "effective_digest": None,
        "source_kind": "private_local",
        "trust_level": "private_local",
        "skill_match": 0.91,
        "centroid_match": None,
        "centroid_count": 0,
    }
    server_ops_row = {
        **develop_row,
        "id": 2,
        "name": "server-ops",
        "description": "Inspect hosted server state.",
        "triggers": [{"direction": "for", "pattern": "server logs"}],
        "skill_match": 0.12,
    }
    uow = _FakeUow(
        execute_results=[
            _ExecuteResult(one_value={"cnt": 2}),
            _ExecuteResult(all_value=[develop_row, server_ops_row]),
        ],
    )

    with patch("brain.app.mcp.server.UnitOfWork", return_value=uow), \
         patch("brain.systems.memory.embeddings.embed_query", return_value=[0.1]), \
         patch("brain.systems.memory.embeddings.vec_to_pg", return_value="[0.1]"):
        result = tool_brain_skills("check the server logs")

    top = result["recommended_skills"][0]
    assert top["name"] == "server-ops"


def test_brain_skills_keeps_manage_domains_top_when_embeddings_degrade():
    from brain.app.mcp.server import tool_brain_skills

    develop_row = {
        "id": 1,
        "name": "develop",
        "description": "Ship focused changes.",
        "version": 3,
        "maturity": "proficient",
        "confidence": 0.9,
        "use_count": 20,
        "success_rate": 0.95,
        "model_tier": "medium",
        "thinking_tier": "medium",
        "pitfalls": [],
        "triggers": [],
        "bundle_version_id": 7,
        "bundle_digest": "sha256:bundle",
        "overlay_revision": None,
        "effective_digest": "sha256:effective",
        "source_kind": "illo-core",
        "trust_level": "illo_core",
        "skill_match": 0.0,
        "centroid_match": None,
        "centroid_count": 0,
    }
    domain_row = {
        **develop_row,
        "id": 2,
        "name": "manage-domains",
        "description": "Create and maintain org-wide custom Domains.",
        "confidence": 0.3,
        "use_count": 0,
        "success_rate": 0.0,
        "skill_match": 1.0,
    }
    uow = _FakeUow(
        execute_results=[
            _ExecuteResult(one_value={"cnt": 2}),
            _ExecuteResult(all_value=[develop_row]),
            _ExecuteResult(one_value=domain_row),
        ],
    )

    with patch("brain.app.mcp.server.UnitOfWork", return_value=uow), \
         patch("brain.systems.memory.embeddings.embed_query", side_effect=RuntimeError("embedding unavailable")):
        result = tool_brain_skills("using the domains tool, do you see it?")

    assert result["degraded"] is True
    assert result["recommended_skills"][0]["name"] == "manage-domains"


def test_recordful_workspace_app_requests_surface_domain_first_then_app_builder():
    from brain.app.mcp.server import tool_brain_skills

    develop_row = {
        "id": 1,
        "name": "develop",
        "description": "Ship focused changes.",
        "version": 3,
        "maturity": "proficient",
        "confidence": 0.8,
        "use_count": 4,
        "success_rate": 0.75,
        "model_tier": "medium",
        "thinking_tier": "medium",
        "pitfalls": [],
        "triggers": [],
        "bundle_version_id": 7,
        "bundle_digest": "sha256:bundle",
        "overlay_revision": None,
        "effective_digest": "sha256:effective",
        "source_kind": "illo-core",
        "trust_level": "illo_core",
        "skill_match": 0.22,
        "centroid_match": None,
        "centroid_count": 0,
    }
    workspace_row = {
        **develop_row,
        "id": 2,
        "name": "build-workspace-app",
        "description": "Create durable generated workspace apps.",
        "skill_match": 1.0,
    }
    domain_row = {
        **develop_row,
        "id": 3,
        "name": "manage-domains",
        "description": "Create and maintain org-wide custom Domains.",
        "skill_match": 1.0,
    }
    uow = _FakeUow(
        execute_results=[
            _ExecuteResult(one_value={"cnt": 3}),
            _ExecuteResult(all_value=[develop_row]),
            _ExecuteResult(one_value=workspace_row),
            _ExecuteResult(one_value=domain_row),
        ],
    )

    with patch("brain.app.mcp.server.UnitOfWork", return_value=uow), \
         patch("brain.systems.memory.embeddings.embed_query", return_value=[0.1]), \
         patch("brain.systems.memory.embeddings.vec_to_pg", return_value="[0.1]"):
        result = tool_brain_skills("build a quick to-do workspace app")

    assert [skill["name"] for skill in result["recommended_skills"][:2]] == [
        "manage-domains",
        "build-workspace-app",
    ]


def test_brain_skills_degrades_when_embedding_unavailable():
    from brain.app.mcp.server import tool_brain_skills

    row = {
        "id": 1,
        "name": "develop",
        "description": "Ship focused changes.",
        "version": 3,
        "maturity": "proficient",
        "confidence": 0.8,
        "use_count": 4,
        "success_rate": 0.75,
        "model_tier": "medium",
        "thinking_tier": "medium",
        "pitfalls": [],
        "triggers": [],
        "bundle_version_id": 7,
        "bundle_digest": "sha256:bundle",
        "overlay_revision": None,
        "effective_digest": "sha256:effective",
        "source_kind": "illo-core",
        "trust_level": "illo_core",
        "skill_match": 0.0,
        "centroid_match": None,
        "centroid_count": 0,
    }
    uow = _FakeUow(
        execute_results=[
            _ExecuteResult(one_value={"cnt": 1}),
            _ExecuteResult(all_value=[row]),
        ],
    )

    with patch("brain.app.mcp.server.UnitOfWork", return_value=uow), \
         patch("brain.systems.memory.embeddings.embed_query", side_effect=RuntimeError("worker_unavailable: embedding failed")):
        result = tool_brain_skills("fix a bug")

    assert result["degraded"] is True
    assert result["recommended_skills"][0]["name"] == "develop"
    assert result["guardrails"] == []
    uow.memories.guardrail_memories_for_task.assert_not_called()


def test_skill_view_loads_procedure_with_digest_metadata():
    from brain.app.mcp.server import tool_skill_view

    with patch("brain.app.mcp.server.UnitOfWork", return_value=_FakeUow(skill=_skill())):
        result = tool_skill_view("develop", section="procedure", max_chars=10)

    assert result["name"] == "develop"
    assert result["section"] == "procedure"
    assert result["content"] == "1. Inspect"
    assert result["truncated"] is True
    assert result["effective_digest"] == "sha256:effective"
    assert result["loaded_sections"] == ["procedure"]


def test_skill_view_loads_minimal_card():
    from brain.app.mcp.server import tool_skill_view

    with patch("brain.app.mcp.server.UnitOfWork", return_value=_FakeUow(skill=_skill())):
        result = tool_skill_view("develop", section="card")

    assert result == {
        "name": "develop",
        "description": "Ship focused changes.",
        "section": "card",
        "loaded_sections": ["card"],
    }


def test_skill_view_loads_summary_before_full_procedure():
    from brain.app.mcp.server import tool_skill_view

    with patch("brain.app.mcp.server.UnitOfWork", return_value=_FakeUow(skill=_skill())):
        result = tool_skill_view("develop", section="summary", max_chars=1200)

    assert result["name"] == "develop"
    assert result["section"] == "summary"
    assert result["description"] == "Ship focused changes."
    assert result["content_type"] == "text/markdown"
    assert "Description: Ship focused changes." in result["content"]
    assert "Procedure preview:" in result["content"]
    assert "1. Inspect" in result["content"]
    assert "Pitfalls:" in result["content"]
    assert result["loaded_sections"] == ["summary"]
    assert result["effective_digest"] == "sha256:effective"


def test_skill_view_loads_structured_sections():
    from brain.app.mcp.server import tool_skill_view

    with patch("brain.app.mcp.server.UnitOfWork", return_value=_FakeUow(skill=_skill())):
        result = tool_skill_view("develop", section="pitfalls")

    assert result["items"] == [{"text": "Do not skip tests", "severity": "high"}]
    assert result["loaded_sections"] == ["pitfalls"]


def test_skill_asset_loads_bundle_asset_and_rejects_traversal():
    from brain.app.mcp.server import tool_skill_asset

    asset = SimpleNamespace(
        path="examples/happy.md",
        asset_kind="example",
        mime_type="text/markdown",
        size_bytes=14,
        content_digest="sha256:asset",
        storage_kind="inline",
        storage_uri=None,
        content_text="Use the skill.\n",
    )
    with patch(
        "brain.app.mcp.server.UnitOfWork",
        return_value=_FakeUow(skill=_skill(), assets=[asset]),
    ):
        result = tool_skill_asset("develop", "examples/happy.md")
        bad = tool_skill_asset("develop", "../secret.md")

    assert result["path"] == "examples/happy.md"
    assert result["asset_kind"] == "example"
    assert result["content"] == "Use the skill.\n"
    assert result["loaded_sections"] == ["asset:examples/happy.md"]
    assert "traversal" in bad["error"]
