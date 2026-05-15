"""Core product journeys that should keep working for real users.

These are not narrow route tests. They are executable product promises: if one
of these fails, a user-facing behaviour broke.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
import uuid
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient, Response
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.main import app
from brain.app.api.services.notifications import async_build_notification_summary
from brain.platform.db.models.chat import (
    CHAT_CONVERSATION_ROOM,
    CHAT_VISIBILITY_ORG,
    ChatConversation,
    ChatConversationMember,
    ChatConversationRead,
)
from brain.platform.db.models.org import Org, User
from brain.systems.runs.domain import AgentRunRequest
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AsyncAgentRunStore

pytestmark = [pytest.mark.requires_db, pytest.mark.product_journey]


@dataclass(frozen=True)
class JourneyUser:
    id: str
    org_id: str
    name: str
    email: str
    color: str
    role: str


@dataclass(frozen=True)
class JourneyOrg:
    id: str
    owner: JourneyUser
    teammate: JourneyUser
    outsider: JourneyUser


@pytest.fixture
async def product_org(db_session: AsyncSession) -> JourneyOrg:
    org_id = str(uuid.uuid4())
    suffix = uuid.uuid4().hex
    owner = JourneyUser(
        id=str(uuid.uuid4()),
        org_id=org_id,
        name="Product Owner",
        email=f"owner-{suffix}@example.com",
        color="#111111",
        role="owner",
    )
    teammate = JourneyUser(
        id=str(uuid.uuid4()),
        org_id=org_id,
        name="Product Teammate",
        email=f"teammate-{suffix}@example.com",
        color="#222222",
        role="member",
    )
    outsider_org_id = str(uuid.uuid4())
    outsider = JourneyUser(
        id=str(uuid.uuid4()),
        org_id=outsider_org_id,
        name="Outside User",
        email=f"outsider-{suffix}@example.com",
        color="#333333",
        role="owner",
    )

    db_session.add(Org(id=org_id, name="Product Journey Org", slug=f"journey-{suffix[:16]}"))
    db_session.add(
        Org(
            id=outsider_org_id,
            name="Outside Journey Org",
            slug=f"outside-{suffix[:16]}",
        )
    )
    db_session.add_all(
        [
            User(
                id=owner.id,
                org_id=owner.org_id,
                name=owner.name,
                email=owner.email,
                color=owner.color,
                role=owner.role,
                approved=True,
            ),
            User(
                id=teammate.id,
                org_id=teammate.org_id,
                name=teammate.name,
                email=teammate.email,
                color=teammate.color,
                role=teammate.role,
                approved=True,
            ),
            User(
                id=outsider.id,
                org_id=outsider.org_id,
                name=outsider.name,
                email=outsider.email,
                color=outsider.color,
                role=outsider.role,
                approved=True,
            ),
        ]
    )
    await db_session.commit()
    return JourneyOrg(id=org_id, owner=owner, teammate=teammate, outsider=outsider)


@pytest.fixture
def product_request(db_session: AsyncSession) -> Callable[..., Awaitable[Response]]:
    async def override_db() -> AsyncIterator[AsyncSession]:
        try:
            yield db_session
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            raise

    async def request_as(user: JourneyUser, method: str, path: str, **kwargs) -> Response:
        async def override_user() -> dict[str, object]:
            return {
                "id": user.id,
                "org_id": user.org_id,
                "name": user.name,
                "email": user.email,
                "role": user.role,
                "color": user.color,
                "principal_type": "human",
                "permissions": ["run:manage"],
            }

        previous_overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = override_user
        app.dependency_overrides[rate_limit] = lambda: None
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.request(method, path, **kwargs)
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous_overrides)

    return request_as


async def _create_product_idea(
    product_org: JourneyOrg,
    product_request: Callable[..., Awaitable[Response]],
    *,
    title: str,
    description: str,
    status: str = "emerged",
) -> dict[str, object]:
    with (
        patch(
            "brain.app.api.routers.cortex._ideas.generate_and_store_idea_display_title",
            new=AsyncMock(),
        ),
        patch(
            "brain.app.api.routers.cortex._ideas.ws_manager.broadcast_product_event",
            new=AsyncMock(),
        ),
    ):
        created = await product_request(
            product_org.owner,
            "POST",
            "/api/cortex/ideas",
            json={
                "title": title,
                "description": description,
                "status": status,
            },
        )

    assert created.status_code == 201, created.text
    return created.json()


async def test_onboarding_journey_creates_first_workspace_owner(
    unit_of_work_for_session,
):
    suffix = uuid.uuid4().hex
    with patch("brain.systems.auth.users.UnitOfWork", unit_of_work_for_session):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            setup = await client.get("/api/auth/setup-check")
            assert setup.status_code == 200, setup.text
            assert setup.json()["setup_required"] is True

            registered = await client.post(
                "/api/register",
                json={
                    "name": "First Owner",
                    "email": f"first-owner-{suffix}@example.com",
                    "password": "password123",
                    "org_name": "First Workspace",
                },
            )
            assert registered.status_code == 200, registered.text
            user = registered.json()
            assert user["role"] == "owner"
            assert user["approved"] is True
            assert user["org_name"] == "First Workspace"

            me = await client.get("/api/me")
            assert me.status_code == 200, me.text
            assert me.json()["id"] == user["id"]
            assert me.json()["org_id"] == user["org_id"]


async def test_workspace_thread_journey_survives_real_route_stack(
    product_org: JourneyOrg,
    product_request: Callable[..., Awaitable[Response]],
):
    idea = await _create_product_idea(
        product_org,
        product_request,
        title="Investigate reports that normal workspace use crashes",
        description="A user should be able to create a workspace thread and keep talking.",
    )
    assert idea["title"] == "Investigate reports that normal workspace use crashes"
    assert idea["org_id"] == product_org.id
    assert idea["user_id"] == product_org.owner.id

    posted_message = await product_request(
        product_org.owner,
        "POST",
        f"/api/cortex/ideas/{idea['id']}/threads",
        json={"role": "user", "content": "Here is the reproduction path from real usage."},
    )
    assert posted_message.status_code == 201, posted_message.text
    assert posted_message.json()["content"] == "Here is the reproduction path from real usage."

    history = await product_request(
        product_org.owner,
        "GET",
        f"/api/cortex/ideas/{idea['id']}/threads",
    )
    assert history.status_code == 200, history.text
    assert [message["content"] for message in history.json()] == [
        "Here is the reproduction path from real usage."
    ]

    done = await product_request(
        product_org.owner,
        "PATCH",
        f"/api/cortex/ideas/{idea['id']}/status",
        json={"status": "done", "trigger": "product_journey_test"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done"

    outsider_read = await product_request(
        product_org.outsider,
        "GET",
        f"/api/cortex/ideas/{idea['id']}",
    )
    assert outsider_read.status_code == 404, outsider_read.text


async def test_project_context_upload_attach_list_journey_blocks_other_org(
    product_org: JourneyOrg,
    product_request: Callable[..., Awaitable[Response]],
):
    idea = await _create_product_idea(
        product_org,
        product_request,
        title="Use uploaded project notes while investigating a production issue",
        description="A user should be able to upload project context, attach it, and keep it scoped to their org.",
    )

    uploaded = await product_request(
        product_org.owner,
        "POST",
        "/api/cortex/project-context/local-files",
        files=[("files", ("incident-notes.md", b"# Incident notes\nUsers lose context.\n", "text/markdown"))],
        data=[("relative_paths", "docs/incident-notes.md")],
    )
    assert uploaded.status_code == 200, uploaded.text
    uploaded_file = uploaded.json()["files"][0]
    assert uploaded_file["relative_path"] == "docs/incident-notes.md"

    project_context = {
        "name": "Incident Notes",
        "resources": [
            {
                "id": "incident-notes",
                "kind": "file",
                "name": "Incident notes",
                "path": uploaded_file["storage_path"],
                "uri": uploaded_file["uri"],
                "mode": "read",
            }
        ],
    }
    created_profile = await product_request(
        product_org.owner,
        "POST",
        "/api/cortex/project-context/profiles",
        json={
            "slug": f"incident-notes-{uuid.uuid4().hex[:8]}",
            "name": "Incident Notes",
            "description": "Uploaded notes for a product investigation",
            "project_context": project_context,
            "metadata": {"source": "product_journey"},
        },
    )
    assert created_profile.status_code == 201, created_profile.text
    profile = created_profile.json()
    assert profile["org_id"] == product_org.id
    assert profile["project_context"]["resources"][0]["path"] == uploaded_file["storage_path"]

    attached = await product_request(
        product_org.owner,
        "POST",
        f"/api/cortex/ideas/{idea['id']}/project-context",
        json={
            "project_profile_id": profile["id"],
            "metadata": {"attached_from": "uploaded_profile"},
        },
    )
    assert attached.status_code == 201, attached.text
    attachment = attached.json()
    assert attachment["idea_id"] == idea["id"]
    assert attachment["project_profile_id"] == profile["id"]
    assert attachment["snapshot"]["name"] == "Incident Notes"
    assert attachment["snapshot"]["resources"][0]["path"] == uploaded_file["storage_path"]
    assert attachment["status"] == "validated"

    owner_attachments = await product_request(
        product_org.owner,
        "GET",
        f"/api/cortex/ideas/{idea['id']}/project-context",
    )
    assert owner_attachments.status_code == 200, owner_attachments.text
    assert [item["id"] for item in owner_attachments.json()] == [attachment["id"]]

    owner_profiles = await product_request(product_org.owner, "GET", "/api/cortex/project-context/profiles")
    assert owner_profiles.status_code == 200, owner_profiles.text
    assert profile["id"] in {item["id"] for item in owner_profiles.json()}

    outsider_profiles = await product_request(product_org.outsider, "GET", "/api/cortex/project-context/profiles")
    assert outsider_profiles.status_code == 200, outsider_profiles.text
    assert profile["id"] not in {item["id"] for item in outsider_profiles.json()}

    outsider_profile_read = await product_request(
        product_org.outsider,
        "GET",
        f"/api/cortex/project-context/profiles/{profile['id']}",
    )
    assert outsider_profile_read.status_code == 404, outsider_profile_read.text

    outsider_attachments = await product_request(
        product_org.outsider,
        "GET",
        f"/api/cortex/ideas/{idea['id']}/project-context",
    )
    assert outsider_attachments.status_code == 404, outsider_attachments.text


async def test_cortex_active_run_journey_streams_cancel_and_blocks_other_org(
    db_session: AsyncSession,
    unit_of_work_for_session,
    product_org: JourneyOrg,
    product_request: Callable[..., Awaitable[Response]],
):
    idea = await _create_product_idea(
        product_org,
        product_request,
        title="Cancel a stuck Cortex run from the workspace",
        description="Users should see active work and be able to stop it without leaking across orgs.",
    )
    store = AsyncAgentRunStore(db_session)
    run = await store.create_run(
        AgentRunRequest(
            thread_id=str(idea["id"]),
            message="Investigate the product failure",
            org_id=product_org.id,
            user_id=product_org.owner.id,
            profile="fast",
            recipe="worker",
        )
    )
    await store.set_status(run.id, RunStatus.STARTING, reason="product_journey_setup")
    await store.set_status(run.id, RunStatus.RUNNING, reason="product_journey_setup")
    await db_session.flush()

    with patch("brain.app.api.routers.cortex._idea_ops.UnitOfWork", unit_of_work_for_session):
        active_stream = await product_request(
            product_org.owner,
            "GET",
            f"/api/cortex/ideas/{idea['id']}/unified-stream",
        )
        assert active_stream.status_code == 200, active_stream.text
        active_run = next(
            item for item in active_stream.json()
            if item.get("id") == str(run.id)
        )
        assert active_run["status"] == "running"

        outsider_cancel = await product_request(
            product_org.outsider,
            "POST",
            f"/api/cortex/ideas/{idea['id']}/cancel-all",
        )
        assert outsider_cancel.status_code == 404, outsider_cancel.text

        canceled = await product_request(
            product_org.owner,
            "POST",
            f"/api/cortex/ideas/{idea['id']}/cancel-all",
        )
        assert canceled.status_code == 200, canceled.text
        assert canceled.json()["canceled"] == 1

        canceled_stream = await product_request(
            product_org.owner,
            "GET",
            f"/api/cortex/ideas/{idea['id']}/unified-stream",
        )
        assert canceled_stream.status_code == 200, canceled_stream.text
        canceled_run = next(
            item for item in canceled_stream.json()
            if item.get("id") == str(run.id)
        )
        assert canceled_run["status"] == "canceled"
        assert any(
            entry["kind"] == "run.canceled"
            for entry in canceled_run.get("activity_trace", [])
        )


async def test_chat_dm_unread_journey_survives_real_route_stack(
    product_org: JourneyOrg,
    product_request: Callable[..., Awaitable[Response]],
):
    owner_bootstrap = await product_request(product_org.owner, "GET", "/api/chat/bootstrap")
    assert owner_bootstrap.status_code == 200, owner_bootstrap.text
    assert owner_bootstrap.json()["room"]["participant_count"] == 2

    dm_response = await product_request(
        product_org.owner,
        "POST",
        "/api/chat/dms",
        json={"user_id": product_org.teammate.id},
    )
    assert dm_response.status_code == 200, dm_response.text
    dm = dm_response.json()
    assert dm["counterpart"]["id"] == product_org.teammate.id

    sent = await product_request(
        product_org.owner,
        "POST",
        f"/api/chat/conversations/{dm['id']}/messages",
        json={"body": "This should appear unread for the teammate."},
    )
    assert sent.status_code == 200, sent.text
    sent_message = sent.json()
    assert sent_message["body"] == "This should appear unread for the teammate."

    teammate_bootstrap = await product_request(product_org.teammate, "GET", "/api/chat/bootstrap")
    assert teammate_bootstrap.status_code == 200, teammate_bootstrap.text
    assert teammate_bootstrap.json()["unread_summary"] == {"room": 0, "dms": 1, "total": 1}

    message_history = await product_request(
        product_org.teammate,
        "GET",
        f"/api/chat/conversations/{dm['id']}/messages",
    )
    assert message_history.status_code == 200, message_history.text
    assert [message["body"] for message in message_history.json()["messages"]] == [
        "This should appear unread for the teammate."
    ]

    outsider_history = await product_request(
        product_org.outsider,
        "GET",
        f"/api/chat/conversations/{dm['id']}/messages",
    )
    assert outsider_history.status_code == 404, outsider_history.text

    marked_read = await product_request(
        product_org.teammate,
        "POST",
        f"/api/chat/conversations/{dm['id']}/read",
        json={
            "last_read_conversation_seq": sent_message["conversation_seq"],
            "last_read_message_id": sent_message["id"],
        },
    )
    assert marked_read.status_code == 200, marked_read.text
    assert marked_read.json() == {"room": 0, "dms": 0, "total": 0}


async def test_workspace_app_journey_persists_state_and_blocks_other_org(
    product_org: JourneyOrg,
    product_request: Callable[..., Awaitable[Response]],
):
    app_payload = {
        "key": "triage-board",
        "name": "Triage Board",
        "renderer_key": "sandboxed-html-app",
        "source_kind": "html",
        "source_code": """
<main class="illo-app">
  <section class="illo-panel illo-stack">
    <h1 class="illo-title">Triage Board</h1>
  </section>
</main>
""",
        "manifest": {
            "contract_version": 1,
            "state_key": "default",
            "data_plan": {"mode": "app_local", "scope": "ui_state"},
            "design_contract": {
                "kit": "constellation-app-kit",
                "theme_modes": ["dark", "light"],
            },
        },
        "visual_spec": {
            "thumbnail": {
                "label": "Triage",
                "status": "Ready",
            }
        },
        "initial_state": {
            "columns": ["New"],
            "cards": [],
            "settings": {
                "density": "compact",
                "filters": {"owner": "me", "status": "open"},
            },
        },
    }

    created = await product_request(
        product_org.owner,
        "POST",
        "/api/workspace-apps/",
        json=app_payload,
    )
    assert created.status_code == 201, created.text
    created_app = created.json()
    assert created_app["key"] == "triage-board"
    assert created_app["contract_validation"]["status"] == "passed"

    saved_state = await product_request(
        product_org.owner,
        "GET",
        f"/api/workspace-apps/{created_app['id']}/state/default",
    )
    assert saved_state.status_code == 200, saved_state.text
    assert saved_state.json()["data"] == app_payload["initial_state"]

    updated_state = await product_request(
        product_org.owner,
        "PUT",
        f"/api/workspace-apps/{created_app['id']}/state/default",
        json={"data_patch": {"cards": [{"title": "Regression found in normal use"}]}},
    )
    assert updated_state.status_code == 200, updated_state.text
    assert updated_state.json()["data"] == {
        "columns": ["New"],
        "cards": [{"title": "Regression found in normal use"}],
        "settings": {
            "density": "compact",
            "filters": {"owner": "me", "status": "open"},
        },
    }

    owner_list = await product_request(product_org.owner, "GET", "/api/workspace-apps/")
    assert owner_list.status_code == 200, owner_list.text
    assert created_app["id"] in {app["id"] for app in owner_list.json()}

    outsider_list = await product_request(product_org.outsider, "GET", "/api/workspace-apps/")
    assert outsider_list.status_code == 200, outsider_list.text
    assert created_app["id"] not in {app["id"] for app in outsider_list.json()}

    outsider_read = await product_request(
        product_org.outsider,
        "GET",
        f"/api/workspace-apps/{created_app['id']}",
    )
    assert outsider_read.status_code == 404, outsider_read.text

    outsider_state = await product_request(
        product_org.outsider,
        "GET",
        f"/api/workspace-apps/{created_app['id']}/state/default",
    )
    assert outsider_state.status_code == 404, outsider_state.text

    archived = await product_request(product_org.owner, "DELETE", f"/api/workspace-apps/{created_app['id']}")
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"]["id"] == created_app["id"]

    active_after_archive = await product_request(product_org.owner, "GET", "/api/workspace-apps/")
    assert active_after_archive.status_code == 200, active_after_archive.text
    assert created_app["id"] not in {app["id"] for app in active_after_archive.json()}

    archived_list = await product_request(product_org.owner, "GET", "/api/workspace-apps/archived")
    assert archived_list.status_code == 200, archived_list.text
    assert created_app["id"] in {app["id"] for app in archived_list.json()}

    restored = await product_request(product_org.owner, "POST", f"/api/workspace-apps/{created_app['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived_at"] is None

    restored_state = await product_request(
        product_org.owner,
        "GET",
        f"/api/workspace-apps/{created_app['id']}/state/default",
    )
    assert restored_state.status_code == 200, restored_state.text
    assert restored_state.json()["data"]["cards"] == [{"title": "Regression found in normal use"}]

    nested_patch = await product_request(
        product_org.owner,
        "PUT",
        f"/api/workspace-apps/{created_app['id']}/state/default",
        json={"data_patch": {"settings": {"filters": {"status": "done"}}}},
    )
    assert nested_patch.status_code == 200, nested_patch.text
    assert nested_patch.json()["data"]["settings"] == {
        "density": "compact",
        "filters": {"owner": "me", "status": "done"},
    }


async def test_notification_summary_never_shows_negative_chat_unread_total(
    db_session: AsyncSession,
    product_org: JourneyOrg,
):
    conversation_id = str(uuid.uuid4())
    db_session.add(
        ChatConversation(
            id=conversation_id,
            org_id=product_org.id,
            type=CHAT_CONVERSATION_ROOM,
            stable_key=f"legacy-room-{uuid.uuid4().hex}",
            title="Legacy room",
            visibility=CHAT_VISIBILITY_ORG,
            last_message_seq=3,
        )
    )
    db_session.add(
        ChatConversationMember(
            conversation_id=conversation_id,
            user_id=product_org.owner.id,
        )
    )
    db_session.add(
        ChatConversationRead(
            conversation_id=conversation_id,
            user_id=product_org.owner.id,
            last_read_conversation_seq=10,
        )
    )
    await db_session.flush()

    summary = await async_build_notification_summary(
        db_session,
        user_id=product_org.owner.id,
        org_id=product_org.id,
    )

    assert summary.chat_unread_total == 0
