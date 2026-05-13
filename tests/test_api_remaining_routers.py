"""Smoke tests for skills, vault, system, team, costs routers."""
from contextlib import contextmanager
from datetime import datetime, date, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.fixture(autouse=True)
def mock_session_factory():
    session = MagicMock()
    yield session


def _mock_obj(**fields):
    obj = MagicMock()
    for k, v in fields.items():
        setattr(obj, k, v)
    return obj


VAULT_USER = {
    "id": "user-1",
    "org_id": "org-1",
    "role": "owner",
    "permissions": ["vault:share", "vault:audit"],
}


@contextmanager
def _vault_user():
    from brain.app.api.auth import get_current_user
    from brain.app.api.main import app

    app.dependency_overrides[get_current_user] = lambda: VAULT_USER
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest_asyncio.fixture
async def client():
    from brain.app.api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---- Skills ----

@pytest.mark.asyncio
async def test_list_skills(client, mock_session_factory):
    skill = _mock_obj(
        id=1,
        name="test_skill",
        description="A test skill",
        procedure="do something",
        version=1,
        skill_type="skill",
        maturity="emerging",
        confidence=0.5,
        use_count=10,
        success_count=8,
        failure_count=1,
        partial_count=1,
        avg_duration_sec=2.5,
        last_used=datetime.now(timezone.utc),
        pitfalls=[],
        refinements=[],
        triggers=[],
        auto_emerged=False,
        provider=None,
        model_name=None,
        reasoning_effort=None,
        service_tier=None,
        auth_mode=None,
        model_tier="medium",
        thinking_tier="medium",
        success_rate=0.8,
        bundle_digest=None,
        effective_digest=None,
        source_kind="legacy_db",
        trust_level="private_local",
        children=[],
        executions=[],
        archived=False,
    )
    with patch("brain.app.api.routers.skills.SkillRepository") as MockRepo:
        MockRepo.return_value.a_list_active_with_executions = AsyncMock(return_value=[skill])
        resp = await client.get("/api/skills/")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "test_skill"


# ---- Vault ----

@pytest.mark.asyncio
async def test_vault_pin_status(client, mock_session_factory):
    with _vault_user(), \
         patch("brain.systems.vault.async_get_pin_status", return_value={
             "has_pin": False,
             "failed_attempts": 0,
             "locked_until": None,
         }):
        resp = await client.get("/api/vault/pin-status")
    assert resp.status_code == 200
    assert resp.json()["has_pin"] is False


@pytest.mark.asyncio
async def test_vault_unlock(client, mock_session_factory):
    expires = datetime.now(timezone.utc)
    with _vault_user(), \
         patch("brain.systems.vault.async_unlock_vault", return_value=("vault-token", expires)):
        resp = await client.post("/api/vault/unlock", json={"pin": "1234"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["unlocked"] is True
    assert data["token"] == "vault-token"


@pytest.mark.asyncio
async def test_vault_list_secrets(client, mock_session_factory):
    secret = {
        "id": 1,
        "key_name": "API_KEY",
        "description": "Test key",
        "category": "general",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "last_accessed_at": None,
        "access_count": 0,
        "user_id": VAULT_USER["id"],
        "is_shared": False,
        "shared_by_name": None,
    }
    with _vault_user(), \
         patch("brain.systems.vault.async_has_pin", return_value=False), \
         patch("brain.systems.vault.async_list_secrets", return_value=[secret]) as list_secrets:
        resp = await client.get("/api/vault/")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    list_secrets.assert_called_once_with(VAULT_USER["id"], category=None, org_id=VAULT_USER["org_id"])


# ---- System ----

@pytest.mark.asyncio
async def test_system_info(client, mock_session_factory):
    resp = await client.get("/api/system")
    assert resp.status_code == 200
    data = resp.json()
    assert "version" in data
    assert data["version"] == "6.0.0"


@pytest.mark.asyncio
async def test_list_metrics(client, mock_session_factory):
    metric = _mock_obj(
        id=1,
        metric_date=date(2026, 3, 17),
        avg_valence=0.5,
        avg_arousal=0.3,
        total_exchanges=100,
        retrieval_attempts=50,
        retrieval_hits=40,
    )
    with patch("brain.app.api.routers.system.DailyMetricsRepository") as MockRepo:
        MockRepo.return_value.a_list_recent = AsyncMock(return_value=[metric])
        resp = await client.get("/api/metrics")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ---- Team ----

@pytest.mark.asyncio
async def test_list_team_members(client, mock_session_factory):
    member = _mock_obj(
        id="user1",
        name="Alice",
        email="alice@example.com",
        role="admin",
        color="#ff0000",
        cortex_color=None,
        attribution_enabled=True,
        approved=True,
        created_at=datetime.now(timezone.utc),
    )
    with patch("brain.app.api.routers.team.TeamRepository") as MockRepo:
        MockRepo.return_value.list_by_org.return_value = [member]
        resp = await client.get("/api/team/members")
    assert resp.status_code == 200
    # Localhost auth returns user with no org_id, so this should return []
    # because the router checks org_id
    data = resp.json()
    assert isinstance(data, list)


# ---- Costs ----

@pytest.mark.asyncio
async def test_list_costs(client, mock_session_factory):
    cost = _mock_obj(
        id=1,
        idea_id="idea1",
        skill_used="code",
        model_used="medium",
        tokens_input=100,
        tokens_output=200,
        tokens_total=300,
        estimated_cost=0.01,
        created_at=datetime.now(timezone.utc),
    )
    with patch("brain.app.api.routers.costs.async_summarize_recent_run_usage", return_value=[cost]):
        resp = await client.get("/api/costs/")
    assert resp.status_code == 200
    data = resp.json()
    assert "runs" in data
    assert len(data["runs"]) == 1


@pytest.mark.asyncio
async def test_system_info_omits_cortex_concurrency_settings(client, mock_session_factory):
    with patch("brain.app.api.routers.system._get_llm_info", new=AsyncMock(return_value={
        "harvest_model": "gpt-5-mini",
        "consolidation_model": "gpt-5-mini",
    })):
        resp = await client.get("/api/system")
    assert resp.status_code == 200
    data = resp.json()
    assert data["llm"]["harvest_model"] == "gpt-5-mini"
    assert "cortex_default_concurrency" not in data["llm"]


@pytest.mark.asyncio
async def test_scheduler_state_surface(client, mock_session_factory):
    with patch("brain.app.api.routers.system.async_scheduler_health_snapshot", new=AsyncMock(return_value={
        "now": "2026-04-21T03:01:00+00:00",
        "daemon": {"owner_mode": "scheduler", "service_ready": True},
        "summary": {
            "jobs_total": 1,
            "jobs_in_scope": 1,
            "jobs_enabled": 1,
            "jobs_paused": 0,
            "jobs_by_owner_mode": {"scheduler": 1},
            "runs_by_status": {},
            "active_leases": 0,
            "expired_leases": 0,
            "lagging_jobs": 0,
            "lag_seconds": 0,
        },
        "health": {"status": "healthy", "reasons": []},
        "pause": {"paused_job_keys": [], "paused_jobs": [], "global_pause": False},
        "lag": {"lag_seconds": 0, "oldest_due_at": None, "lagging_jobs": []},
        "jobs": [],
        "runs": [],
    })):
        resp = await client.get("/api/system/scheduler")

    assert resp.status_code == 200
    data = resp.json()
    assert data["health"]["status"] == "healthy"
    assert data["summary"]["jobs_total"] == 1


@pytest.mark.asyncio
async def test_scheduler_drain_control_surface(client, mock_session_factory):
    from brain.app.api.main import app
    from brain.app.api.routers import system as system_router

    app.dependency_overrides[system_router.get_current_user] = lambda: {"role": "owner"}
    try:
        with patch("brain.app.api.routers.system.async_scheduler_daemon_tick", new=AsyncMock(return_value={
            "ok": True,
            "owner_mode": "scheduler",
            "reclaimed": 0,
            "reclaimed_run_ids": [],
            "drain": {"ok": True, "executed": 1, "results": []},
            "snapshot": {"health": {"status": "healthy", "reasons": []}},
        })) as mock_tick:
            resp = await client.post(
                "/api/system/scheduler/drain",
                json={"owner_mode": "scheduler", "job_key": "nightly_sleep", "max_runs": 2, "resume": True},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()
    assert data["drain"]["executed"] == 1
    mock_tick.assert_awaited_once()


@pytest.mark.asyncio
async def test_journal_supports_bounded_pages(client, tmp_path):
    from brain.app.api.routers import journal as journal_router

    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    for day in ("2026-04-28", "2026-04-29", "2026-04-30"):
        (journal_dir / f"{day}.md").write_text(f"# {day}\n", encoding="utf-8")

    with patch.object(journal_router, "_journal_dir", return_value=journal_dir):
        resp = await client.get("/api/journal?limit=2&offset=1")

    assert resp.status_code == 200
    assert [entry["filename"] for entry in resp.json()] == [
        "2026-04-29.md",
        "2026-04-28.md",
    ]


def test_tail_text_lines_reads_only_requested_tail(tmp_path):
    from brain.app.api.routers.system import _tail_text_lines

    log_path = tmp_path / "worker.log"
    log_path.write_text("\n".join(f"line-{i}" for i in range(200)), encoding="utf-8")

    assert _tail_text_lines(log_path, 3) == ["line-197", "line-198", "line-199"]
