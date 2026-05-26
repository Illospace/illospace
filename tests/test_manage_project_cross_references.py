import json
from datetime import datetime, timezone
from types import SimpleNamespace

from brain.systems.cortex.project_context.project_root import project_root_path
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers import files, projects


async def test_manage_project_schema_exposes_cross_project_reference_actions():
    from brain.systems.runs.tool_definitions import PROJECT_TOOLS

    tool = next(item for item in PROJECT_TOOLS if item["name"] == "manage_project")
    actions = tool["input_schema"]["properties"]["action"]["enum"]

    assert "search_files" in actions
    assert "mount_reference" in actions
    assert "query" in tool["input_schema"]["properties"]
    assert "paths" in tool["input_schema"]["properties"]
    assert "mount_path" in tool["input_schema"]["properties"]

    guide = json.loads(await projects._handle_manage_project(action="schema", operation="mount_reference"))
    assert guide["operation"] == "mount_reference"
    assert "read-only reference mounts" in guide["effect"]


def _project_profile(project_id: str, *, slug: str, name: str, description: str = ""):
    return SimpleNamespace(
        id=project_id,
        org_id="org-1",
        user_id="user-1",
        slug=slug,
        name=name,
        description=description,
        project_context={
            "version": 1,
            "source": "test",
            "resources": [],
        },
        visibility="public",
        default_environment_binding_id=None,
        active=True,
        metadata_={},
        created_at=datetime.now(timezone.utc),
    )


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, profiles):
        self.profiles = profiles

    async def scalars(self, _stmt):
        return _FakeScalars(self.profiles)

    async def scalar(self, _stmt):
        return self.profiles[0] if self.profiles else None


class _FakeUow:
    def __init__(self, profiles):
        self.session = _FakeSession(profiles)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def _patch_project_profiles(monkeypatch, profiles):
    from brain.platform.db.repositories import unit_of_work

    class FakeUnitOfWork(_FakeUow):
        def __init__(self):
            super().__init__(profiles)

    monkeypatch.setattr(unit_of_work, "UnitOfWork", FakeUnitOfWork)


def _project_root(workspace_root, profile):
    return project_root_path(workspace_root, profile.id)


async def test_manage_project_list_filters_by_project_name_and_resources(monkeypatch):
    payments = _project_profile(
        "project-payments",
        slug="payments",
        name="Payments",
        description="Stripe settlement docs",
    )
    marketing = _project_profile(
        "project-marketing",
        slug="marketing",
        name="Marketing",
        description="Launch plans",
    )
    _patch_project_profiles(monkeypatch, [marketing, payments])

    with bind_agent_context({"org_id": "org-1", "user_id": "user-1"}):
        payload = json.loads(await projects._handle_manage_project(action="list", query="stripe"))

    assert [project["id"] for project in payload["projects"]] == ["project-payments"]


async def test_manage_project_search_files_finds_paths_and_content_without_loading_projects(tmp_path, monkeypatch):
    profile = _project_profile("project-payments", slug="payments", name="Payments")
    root = _project_root(tmp_path / "ideas" / "thread-1", profile)
    (root / "analysis").mkdir(parents=True)
    (root / "analysis" / "summary.md").write_text("Stripe settlement notes\nRefunds reviewed\n", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / "onboarding.pdf").write_bytes(b"%PDF-1.4\n")
    _patch_project_profiles(monkeypatch, [profile])

    with bind_agent_context({"org_id": "org-1", "user_id": "user-1", "workspace_root": str(tmp_path / "ideas" / "thread-1")}):
        content_payload = json.loads(await projects._handle_manage_project(action="search_files", query="stripe"))
        path_payload = json.loads(await projects._handle_manage_project(action="search_files", query="onboarding pdf"))

    assert content_payload["results"][0]["path"] == "analysis/summary.md"
    assert content_payload["results"][0]["snippets"][0]["text"] == "Stripe settlement notes"
    assert path_payload["results"][0]["path"] == "assets/onboarding.pdf"
    assert path_payload["results"][0]["matched_by"] == ["path"]


def test_project_root_path_accepts_workspace_base_and_draft_paths(tmp_path):
    workspace_base = tmp_path / "workspaces"
    workspace_base.mkdir()
    thread_root = workspace_base / "ideas" / "thread-1"
    draft_path = thread_root / ".illo-project-context" / "local" / "project-payments" / "project-root"

    assert project_root_path(workspace_base, "project-payments") == workspace_base / "project-roots" / "project-payments"
    assert project_root_path(draft_path, "project-payments") == workspace_base / "project-roots" / "project-payments"


async def test_manage_project_mount_reference_exposes_read_only_files_to_normal_file_tools(tmp_path, monkeypatch):
    profile = _project_profile("project-payments", slug="payments", name="Payments")
    workspace_root = tmp_path / "ideas" / "thread-1"
    root = _project_root(workspace_root, profile)
    (root / "analysis").mkdir(parents=True)
    (root / "analysis" / "summary.md").write_text("Stripe settlement notes\n", encoding="utf-8")
    _patch_project_profiles(monkeypatch, [profile])
    run = SimpleNamespace(id=123, workspace_ref={}, target_ref={}, metadata_={})

    with bind_agent_context({
        "run": run,
        "org_id": "org-1",
        "user_id": "user-1",
        "workspace_root": str(workspace_root),
    }):
        payload = json.loads(
            await projects._handle_manage_project(
                action="mount_reference",
                project_id="project-payments",
                paths=["analysis/summary.md"],
            )
        )
        mount_path = payload["mounts"][0]["mount_path"]
        read_result = files._handle_read_file(mount_path, _workspace=str(workspace_root))
        write_result = files._handle_write_file(mount_path, "changed", _workspace=str(workspace_root))
        command_result = files._handle_exec_command(
            f"touch {mount_path}",
            _workspace=str(workspace_root),
        )

    assert mount_path == "/references/payments/analysis/summary.md"
    assert "Stripe settlement notes" in read_result["content"]
    assert "read-only Project reference mount" in write_result["error"]
    assert "read-only Project reference mount" in command_result["error"]
    assert run.workspace_ref["project_workspace_manifest"]["mounts"][0]["metadata"]["read_only"] is True
