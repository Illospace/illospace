"""Tests for cortex garden palette -- colors, opacity, status transitions.

Validates the garden palette:
- Svelte COLORS constant covers all statuses
- bubbleOpacity logic
- API accepts all status values via PUT

Closes #35

Run: pytest tests/test_cortex_palette.py -v --tb=short
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from brain.app.api.main import app

# ── Expected palette ──────────────────────────────────────────
EXPECTED_COLORS = {
    'idle':    '#f0f0fa',
    'working': '#f0f0fa',
    'done':    '#f0f0fa',
}

# Statuses accepted by the cortex API update endpoint
API_STATUSES = ['emerged', 'queued', 'active', 'working', 'needs_input',
                'unread_reply', 'blocked', 'failed', 'resolved', 'stale']

# Frontend source paths (visual helpers + scene component that consume them)
FRONTEND_SRC = Path(__file__).parent.parent / 'frontend' / 'src'
VISUALS_PATH = FRONTEND_SRC / 'lib' / 'utils' / 'cortexSvgVisuals.ts'
SCENE_PATH = FRONTEND_SRC / 'lib' / 'features' / 'workspace-scene' / 'components' / 'WorkspaceScene.svelte'


# ── Fixtures ──────────────────────────────────────────────────
@pytest.fixture
def svelte_source():
    assert VISUALS_PATH.exists(), f"cortexSvgVisuals.ts not found at {VISUALS_PATH}"
    assert SCENE_PATH.exists(), f"WorkspaceScene.svelte not found at {SCENE_PATH}"
    return VISUALS_PATH.read_text() + "\n" + SCENE_PATH.read_text()


def _fake_idea(id="idea-001", title="Test", status="emerged"):
    m = MagicMock()
    m.id = id
    m.title = title
    m.display_title = title
    m.description = None
    m.status = status
    m.origin = "user_created"
    m.origin_ref = None
    m.salience_score = 5.0
    m.position_x = None
    m.position_y = None
    m.position_sticky = False
    m.parent_id = None
    m.user_id = None
    m.org_id = None
    from datetime import datetime, timezone
    m.created_at = datetime(2026, 3, 20, tzinfo=timezone.utc)
    m.updated_at = datetime(2026, 3, 20, tzinfo=timezone.utc)
    m.archived_at = None
    return m


def _fake_async_db():
    db = MagicMock()
    empty_result = MagicMock()
    empty_result.all.return_value = []
    empty_result.one_or_none.return_value = None
    db.execute = AsyncMock(return_value=empty_result)
    db.scalar = AsyncMock(return_value=None)
    db.scalars = AsyncMock(return_value=empty_result)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.fixture()
def client():
    """TestClient with auth and DB dependencies overridden."""
    from brain.app.api.auth import get_current_user
    from brain.app.api.authorization import service_principal_context
    from brain.app.api.deps import get_db

    app.dependency_overrides[get_current_user] = lambda: service_principal_context("test-cortex-palette")
    app.dependency_overrides[get_db] = _fake_async_db
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


# ── Unit tests: Svelte COLORS constant ────────────────────────
class TestSvelteColors:
    """Verify COLORS object in CortexSVG.svelte has all expected entries."""

    def _parse_colors(self, svelte_source):
        """Extract COLORS object entries from Svelte/TS source."""
        # Match TypeScript const COLORS: Record<string, string> = { ... };
        match = re.search(r'const COLORS[^=]*=\s*\{([^}]+)\}', svelte_source)
        assert match, "COLORS object not found in CortexSVG.svelte"
        block = match.group(1)
        entries = {}
        for m in re.finditer(r"(\w+)\s*:\s*'(#[0-9A-Fa-f]{6})'", block):
            entries[m.group(1)] = m.group(2)
        return entries

    def test_all_status_colors_defined(self, svelte_source):
        colors = self._parse_colors(svelte_source)
        for status, hex_val in EXPECTED_COLORS.items():
            assert status in colors, f"Status '{status}' missing from COLORS"
            assert colors[status] == hex_val, f"COLORS['{status}'] = {colors[status]}, expected {hex_val}"

    def test_no_old_neon_colors(self, svelte_source):
        """Ensure old harsh colors are gone."""
        old_colors = ['#00ffff', '#ff6b6b', '#ffd93d', '#6bff6b']
        colors = self._parse_colors(svelte_source)
        for hex_val in colors.values():
            assert hex_val.lower() not in [c.lower() for c in old_colors], \
                f"Old neon color {hex_val} still present"


class TestBubbleOpacity:
    """Verify bubbleOpacity function logic in Svelte source."""

    def test_opacity_function_exists(self, svelte_source):
        assert 'bubbleOpacity' in svelte_source

    def test_opacity_matches_visual_status_contract(self, svelte_source):
        assert "return state === 'working' ? 0.95 : 1;" in svelte_source


class TestArchiveSwallowTarget:
    """Verify archive drag animation uses the resolved archive-bin target."""

    def test_cortex_archive_swallow_uses_resolved_target(self, svelte_source):
        match = re.search(
            r"if \(archiveTarget && dragDist > 30\) \{([\s\S]*?)\n    \} else if \(orbitAnchor\) \{",
            svelte_source,
        )
        assert match, 'archive swallow branch not found in dragEnd'
        archive_branch = match.group(1)

        assert 'const archiveTarget = archiveBinTargetFromDrag(e, d);' in svelte_source
        assert 'animateArchiveToBin(d, archiveTarget);' in archive_branch
        assert 'd.fx = startX + (coreX - startX) * eased;' not in archive_branch
        assert 'd.fy = startY + (coreY - startY) * eased;' not in archive_branch
        assert "translate(${coreX},${coreY})" not in archive_branch


# ── Integration tests: API status handling ────────────────────
class TestCortexAPIStatuses:
    """Verify cortex API accepts all palette statuses via PUT."""

    @pytest.mark.parametrize("status", API_STATUSES)
    @patch("brain.app.api.routers.cortex._ideas.IdeaRepository")
    def test_update_idea_status(self, MockIdeaRepo, client, status):
        fake = _fake_idea(status="emerged")
        MockIdeaRepo.return_value.a_get = AsyncMock(return_value=fake)

        with patch("brain.app.api.routers.cortex._helpers.IdeaRepository") as MockHelperIdeaRepo:
            MockHelperIdeaRepo.return_value.a_get = AsyncMock(return_value=fake)
            resp = client.put(
                "/api/cortex/ideas/idea-001",
                json={"status": status},
            )
        assert resp.status_code == 200, f"PUT status='{status}' failed: {resp.text}"
        data = resp.json()
        assert data["status"] == status

    @patch("brain.app.api.routers.cortex._ideas.IdeaRepository")
    def test_filter_by_status(self, MockIdeaRepo, client):
        MockIdeaRepo.return_value.a_list_by_status = AsyncMock(return_value=[])
        resp = client.get("/api/cortex/ideas?status=emerged")
        assert resp.status_code == 200
