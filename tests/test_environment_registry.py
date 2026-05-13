from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


class _ScalarResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


def _run(run_id=42):
    return SimpleNamespace(id=run_id, idea_id=None, target_metadata={})


def _registry():
    return SimpleNamespace(
        id=1,
        target_kind="repo",
        slug="illo-brain",
        display_name="Illo Brain",
        owner_team="brain",
        repo_url="https://github.com/example-org/example-repo",
        canonical_path="/repos/illo-brain",
        default_branch="main",
        metadata_={},
        active=True,
    )


def _binding(registry_id=1):
    return SimpleNamespace(
        id=2,
        target_registry_id=registry_id,
        env_name="backend",
        branch_pattern="feat/*",
        workspace_root="/repos/backend",
        deploy_target="local",
        org_id=None,
        metadata_={},
    )


def _service(binding_id=2, test_command_id=None):
    return SimpleNamespace(
        id=3,
        binding_id=binding_id,
        service_name="api",
        service_type="backend",
        base_path="/repos/backend/api",
        healthcheck="/health",
        test_command_id=test_command_id,
        verify_contract={"kind": "smoke"},
    )


def _command(binding_id=2, command_id=4, safe_default=True):
    return SimpleNamespace(
        id=command_id,
        binding_id=binding_id,
        command_name="test",
        command="pytest tests/test_smoke.py -q",
        cwd="/repos/backend",
        purpose="Smoke test the backend",
        cost_class="cheap",
        safe_default=safe_default,
        metadata_={"source": "curated"},
    )


async def test_resolve_run_target_binding_resolves_exact_binding_and_persists_row():
    from brain.systems.environment import resolve_run_target_binding

    session = MagicMock()
    session.get = AsyncMock(return_value=_run())
    session.scalars = AsyncMock(side_effect=[
        _ScalarResult([_registry()]),
        _ScalarResult([_binding()]),
        _ScalarResult([_service()]),
        _ScalarResult([]),
    ])

    added = []

    def _track_add(obj):
        added.append(obj)
        obj.id = 99

    session.add.side_effect = _track_add

    with patch("brain.systems.environment.resolver.get_run_target_binding", new=AsyncMock(return_value=None)):
        binding = await resolve_run_target_binding(
            session,
            42,
            raw_target_metadata={
                "repo": "illo-brain",
                "workspace": {"name": "backend", "path": "/repos/backend"},
                "branch": "feat/run-target-registry",
            },
        )

    assert len(added) == 1
    assert binding is added[0]
    assert binding.resolution_status == "resolved"
    assert binding.target_registry_id == 1
    assert binding.environment_binding_id == 2
    assert binding.resolved_workspace_root == "/repos/backend"
    assert binding.resolved_branch == "feat/run-target-registry"
    assert binding.resolved_service_set == [
        {
            "id": 3,
            "service_name": "api",
            "service_type": "backend",
            "base_path": "/repos/backend/api",
            "healthcheck": "/health",
            "test_command_id": None,
            "verify_contract": {"kind": "smoke"},
        }
    ]
    assert binding.resolution_notes["messages"][0].startswith("Exact curated binding matched")
    assert binding.resolution_notes["confidence"] >= 0.9


async def test_resolve_run_target_binding_returns_partial_for_registry_only_match():
    from brain.systems.environment import resolve_run_target_binding

    session = MagicMock()
    session.get = AsyncMock(return_value=_run(43))
    session.scalars = AsyncMock(side_effect=[
        _ScalarResult([_registry()]),
        _ScalarResult([]),
        _ScalarResult([]),
        _ScalarResult([]),
    ])
    session.add.side_effect = lambda obj: setattr(obj, "id", 100)

    with patch("brain.systems.environment.resolver.get_run_target_binding", new=AsyncMock(return_value=None)):
        binding = await resolve_run_target_binding(session, 43, raw_target_metadata={"repo": "illo-brain"})

    assert binding.resolution_status == "partial"
    assert binding.target_registry_id == 1
    assert binding.environment_binding_id is None
    assert binding.resolved_service_set == []
    assert "Registry matched" in binding.resolution_notes["messages"][0]
    assert binding.resolution_notes["confidence"] < 0.8


async def test_resolve_run_target_binding_returns_unknown_for_empty_target():
    from brain.systems.environment import resolve_run_target_binding

    session = MagicMock()
    session.get = AsyncMock(return_value=_run(44))
    session.scalars = AsyncMock(side_effect=[
        _ScalarResult([]),
        _ScalarResult([]),
        _ScalarResult([]),
        _ScalarResult([]),
    ])
    session.add.side_effect = lambda obj: setattr(obj, "id", 101)

    with patch("brain.systems.environment.resolver.get_run_target_binding", new=AsyncMock(return_value=None)):
        binding = await resolve_run_target_binding(session, 44, raw_target_metadata={})

    assert binding.resolution_status == "unknown"
    assert binding.target_registry_id is None
    assert binding.environment_binding_id is None
    assert binding.resolved_service_set == []
    assert "No curated target binding could be confirmed" in binding.resolution_notes["messages"][0]
    assert binding.resolution_notes["confidence"] == 0.0


async def test_resolve_run_target_binding_uses_materialized_project_context_resource():
    from brain.systems.environment import resolve_run_target_binding

    session = MagicMock()
    session.get = AsyncMock(return_value=_run(46))
    session.scalars = AsyncMock(side_effect=[
        _ScalarResult([]),
        _ScalarResult([]),
        _ScalarResult([]),
    ])
    session.add.side_effect = lambda obj: setattr(obj, "id", 103)

    with patch("brain.systems.environment.resolver.get_run_target_binding", new=AsyncMock(return_value=None)):
        binding = await resolve_run_target_binding(
            session,
            46,
            raw_target_metadata={
                "project_context_snapshot": {
                    "name": "example backend",
                    "status": "validated",
                    "resources": [
                        {
                            "kind": "repo",
                            "name": "example-org/example-backend",
                            "repo": "example-org/example-backend",
                            "branch": "main",
                            "path": "/tmp/illo-run-46/.illo-project-context/github/example-org/example-backend",
                        }
                    ],
                }
            },
        )

    assert binding.resolution_status == "resolved"
    assert binding.target_registry_id is None
    assert binding.environment_binding_id is None
    assert binding.resolved_workspace_root.endswith("/example-org/example-backend")
    assert binding.resolved_branch == "main"
    assert binding.resolution_notes["confidence"] >= 0.8
    assert "Project Context" in binding.resolution_notes["messages"][0]


async def test_resolve_run_target_binding_stays_partial_when_multiple_bindings_match():
    from brain.systems.environment import resolve_run_target_binding

    session = MagicMock()
    session.get = AsyncMock(return_value=_run(45))
    session.scalars = AsyncMock(side_effect=[
        _ScalarResult([_registry()]),
        _ScalarResult([
            _binding(),
            SimpleNamespace(
                id=4,
                target_registry_id=1,
                env_name="backend-canary",
                branch_pattern="feat/*",
                workspace_root="/repos/backend",
                deploy_target="local",
                org_id=None,
                metadata_={},
            ),
        ]),
        _ScalarResult([]),
        _ScalarResult([]),
    ])
    session.add.side_effect = lambda obj: setattr(obj, "id", 102)

    with patch("brain.systems.environment.resolver.get_run_target_binding", new=AsyncMock(return_value=None)):
        binding = await resolve_run_target_binding(
            session,
            45,
            raw_target_metadata={
                "repo": "illo-brain",
                "workspace": {"name": "backend", "path": "/repos/backend"},
                "branch": "feat/run-target-registry",
            },
        )

    assert binding.resolution_status == "partial"
    assert binding.environment_binding_id is None
    assert binding.resolution_notes["binding_candidate_count"] == 2
    assert binding.resolution_notes["confidence"] < 0.8


async def test_load_run_target_context_exposes_safe_commands_and_services():
    from brain.systems.environment import load_run_target_context

    session = MagicMock()

    with patch("brain.systems.environment.resolver.get_run_target_binding", new=AsyncMock(return_value=SimpleNamespace(
        id=10,
        run_id=45,
        raw_target_metadata={"repo": "illo-brain"},
        resolution_status="resolved",
        target_registry_id=1,
        environment_binding_id=2,
        resolved_workspace_root="/repos/backend",
        resolved_branch="main",
        resolved_service_set=[],
        resolution_notes={"confidence": 0.95, "messages": ["Exact curated binding matched the explicit target metadata."]},
    ))):
        session.get = AsyncMock(side_effect=lambda model, identity: _registry() if getattr(model, "__name__", "") == "TargetRegistry" else SimpleNamespace(
            id=2,
            target_registry_id=1,
            env_name="backend",
            branch_pattern="feat/*",
            workspace_root="/repos/backend",
            deploy_target="local",
            org_id=None,
            metadata_={},
        ))
        session.scalars = AsyncMock(side_effect=[
            _ScalarResult([_command(), _command(command_id=5, safe_default=False)]),
            _ScalarResult([_service(test_command_id=4)]),
        ])
        context = await load_run_target_context(session, 45)

    assert context["binding"]["resolution_status"] == "resolved"
    assert context["binding"]["resolution_confidence"] == 0.95
    assert context["registry"]["slug"] == "illo-brain"
    assert context["environment_binding"]["env_name"] == "backend"
    assert context["catalog_summary"]["command_count"] == 2
    assert context["services"][0]["service_name"] == "api"
    assert context["commands"][0]["command_name"] == "test"
    assert context["safe_commands"][0]["safe_default"] is True
    assert context["service_test_commands"][0]["command_name"] == "test"


async def test_load_run_target_context_exposes_execution_defaults_for_low_ambiguity_binding():
    from brain.systems.environment import load_run_target_context

    session = MagicMock()

    with patch("brain.systems.environment.resolver.get_run_target_binding", new=AsyncMock(return_value=SimpleNamespace(
        id=10,
        run_id=45,
        raw_target_metadata={"repo": "illo-brain"},
        resolution_status="resolved",
        resolution_confidence=0.95,
        target_registry_id=1,
        environment_binding_id=2,
        resolved_workspace_root="/repos/backend",
        resolved_branch="main",
        resolved_service_set=[],
        resolution_notes={"confidence": 0.95, "messages": ["Exact curated binding matched the explicit target metadata."]},
    ))):
        session.get = AsyncMock(side_effect=lambda model, identity: _registry() if getattr(model, "__name__", "") == "TargetRegistry" else SimpleNamespace(
            id=2,
            target_registry_id=1,
            env_name="backend",
            branch_pattern="feat/*",
            workspace_root="/repos/backend",
            deploy_target="local",
            org_id=None,
            metadata_={},
        ))
        session.scalars = AsyncMock(side_effect=[
            _ScalarResult([_command(), _command(command_id=5, safe_default=False)]),
            _ScalarResult([_service(test_command_id=4)]),
        ])
        context = await load_run_target_context(session, 45)

    defaults = context["execution_defaults"]
    assert defaults["workspace_root"] == "/repos/backend"
    assert defaults["workspace_hint"] == "/repos/backend"
    assert defaults["command_selection_status"] == "selected"
    assert defaults["safe_command"]["command_name"] == "test"


async def test_load_run_target_context_leaves_execution_defaults_empty_when_binding_is_partial():
    from brain.systems.environment import load_run_target_context

    session = MagicMock()

    with patch("brain.systems.environment.resolver.get_run_target_binding", new=AsyncMock(return_value=SimpleNamespace(
        id=11,
        run_id=46,
        raw_target_metadata={"repo": "illo-brain"},
        resolution_status="partial",
        resolution_confidence=0.55,
        target_registry_id=1,
        environment_binding_id=None,
        resolved_workspace_root=None,
        resolved_branch=None,
        resolved_service_set=[],
        resolution_notes={"confidence": 0.55, "messages": ["Registry matched, but there was not enough explicit data to bind a specific environment."]},
    ))):
        session.get = AsyncMock(return_value=_registry())
        session.scalars = AsyncMock(side_effect=[
            _ScalarResult([_command(), _command(command_id=5, safe_default=False)]),
            _ScalarResult([]),
        ])
        context = await load_run_target_context(session, 46)

    defaults = context["execution_defaults"]
    assert defaults["workspace_root"] is None
    assert defaults["safe_command"] is None
    assert defaults["command_selection_status"] == "unavailable"
