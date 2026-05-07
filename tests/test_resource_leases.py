from __future__ import annotations

from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class _FakeUOW:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeQuery:
    def __init__(self, entry=None):
        self.entry = entry

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return self.entry


class _FakeSession:
    def __init__(self, entry=None):
        self.entry = entry
        self.added = []

    def query(self, *args, **kwargs):
        return _FakeQuery(self.entry)

    def get(self, model, row_id):
        return self.entry

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        return None

    def refresh(self, obj):
        return None


def test_resource_lease_manager_acquire_and_release(monkeypatch):
    import brain.systems.cortex.resources.leases as leases_mod
    from brain.systems.cortex.resources.leases import ResourceLeaseManager

    session = MagicMock()
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query

    captured = {}

    def first_side_effect():
        return captured.get("lease")

    def add_side_effect(obj):
        captured["lease"] = obj
        obj.id = 7

    query.first.side_effect = first_side_effect
    session.query.return_value = query
    session.add.side_effect = add_side_effect
    session.flush.side_effect = lambda: None

    fake_uow = _FakeUOW(session)
    monkeypatch.setattr(leases_mod, "UnitOfWork", lambda: fake_uow)
    monkeypatch.setattr(leases_mod, "publish_safe", lambda *args, **kwargs: None)

    manager = ResourceLeaseManager(default_ttl_seconds=60)
    decision = manager.acquire_lease(
        "workspace",
        "repo-root:head:runtime",
        owner_run_id=11,
        owner_worker_id="worker-1",
        lease_token="lease-123",
    )

    assert decision.acquired is True
    assert decision.lease_token == "lease-123"
    assert decision.lease_id == 7
    assert captured["lease"].resource_type == "workspace"
    assert captured["lease"].resource_id == "repo-root:head:runtime"

    released = manager.release_lease("lease-123", release_reason="test_complete")

    assert released is True
    assert captured["lease"].released_at is not None
    assert captured["lease"].release_reason == "test_complete"


def test_resource_lease_manager_reclaims_expired_lease_before_reacquiring(monkeypatch):
    import brain.systems.cortex.resources.leases as leases_mod
    from brain.systems.cortex.resources.leases import LeaseDecision, ResourceLeaseManager

    expired = SimpleNamespace(
        id=3,
        resource_type="workspace",
        resource_id="repo-root:head:runtime",
        owner_run_id=7,
        owner_worker_id="worker-old",
        lease_token="old-lease",
        expires_at=leases_mod._utcnow() - leases_mod.timedelta(seconds=1),
        released_at=None,
        release_reason=None,
        created_at=leases_mod._utcnow(),
        heartbeat_at=None,
    )
    captured = {}
    session = _FakeSession()
    query = MagicMock()
    query.filter.return_value = query
    query.order_by.return_value = query
    query.first.side_effect = [expired, None]
    session.query = MagicMock(return_value=query)

    def add_side_effect(obj):
        captured["lease"] = obj
        obj.id = 9

    session.add = MagicMock(side_effect=add_side_effect)
    fake_uow = _FakeUOW(session)
    monkeypatch.setattr(leases_mod, "UnitOfWork", lambda: fake_uow)
    monkeypatch.setattr(leases_mod, "publish_safe", lambda *args, **kwargs: None)

    manager = ResourceLeaseManager(default_ttl_seconds=60)
    decision = manager.acquire_lease(
        "workspace",
        "repo-root:head:runtime",
        owner_run_id=11,
        owner_worker_id="worker-1",
        lease_token="lease-123",
    )

    assert expired.released_at is not None
    assert expired.release_reason == "expired"
    assert decision.acquired is True
    assert decision.lease_token == "lease-123"
    assert captured["lease"].id == 9


def test_workspace_pool_plan_falls_back_to_cold_when_disabled(monkeypatch):
    from brain.systems.cortex.resources.pools import WorkspacePoolManager

    monkeypatch.delenv("CORTEX_WARM_WORKSPACE_POOL_ENABLED", raising=False)
    plan = WorkspacePoolManager().plan(
        repo_root="/repo",
        base_commit="abc123",
        runtime_fingerprint="host:sha:venv",
        allow_warm_reuse=True,
    )

    assert plan.resource_kind == "workspace"
    assert plan.mode == "cold"
    assert plan.warm_start_used is False
    assert plan.reason == "warm workspace pool disabled"
    assert plan.summary["workspace"]["mode"] == "cold"


def test_workspace_pool_plan_acquires_warm_candidate_and_materializes_copy(monkeypatch, tmp_path):
    import brain.systems.cortex.resources.pools as pools_mod
    from brain.systems.cortex.resources.pools import WorkspacePoolManager
    from brain.systems.cortex.resources.leases import LeaseDecision

    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("warm pool\n")
    target = tmp_path / "target"

    entry = SimpleNamespace(
        id=1,
        repo_root=str(tmp_path),
        base_commit="abc123",
        runtime_fingerprint="host:sha:venv",
        pool_key=None,
        status="ready",
        base_path=str(source),
        mode="copy",
        last_used_at=None,
        ttl_expires_at=None,
        health={},
        created_at=pools_mod._utcnow(),
    )
    entry.pool_key = WorkspacePoolManager().workspace_pool_key(
        repo_root=entry.repo_root,
        base_commit=entry.base_commit,
        runtime_fingerprint=entry.runtime_fingerprint,
        mode=entry.mode,
    )

    session = _FakeSession(entry)
    fake_uow = _FakeUOW(session)

    def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        if cmd[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setenv("CORTEX_WARM_WORKSPACE_POOL_ENABLED", "1")
    monkeypatch.setattr(pools_mod, "UnitOfWork", lambda: fake_uow)
    monkeypatch.setattr(pools_mod.subprocess, "run", fake_run)

    manager = WorkspacePoolManager()
    manager.lease_manager.acquire_lease = lambda *args, **kwargs: LeaseDecision(
        acquired=True,
        resource_type="workspace_pool_entry",
        resource_id="1",
        lease_token="lease-1",
        owner_run_id=kwargs.get("owner_run_id"),
        owner_worker_id=kwargs.get("owner_worker_id"),
        expires_at=pools_mod._utcnow(),
        lease_id=55,
    )

    plan = manager.plan(
        repo_root=entry.repo_root,
        base_commit=entry.base_commit,
        runtime_fingerprint=entry.runtime_fingerprint,
        mode="copy",
        allow_warm_reuse=True,
        run_id=42,
        target_path=str(target),
    )

    assert plan.warm_start_used is True
    assert plan.lease_token == "lease-1"
    assert plan.resource_path == str(target)
    assert target.exists()
    assert (target / "README.md").read_text() == "warm pool\n"
    assert entry.status == "leased"


@pytest.mark.parametrize("mode", ["reflink", "snapshot"])
def test_workspace_pool_plan_downgrades_unsupported_advanced_modes_to_copy(monkeypatch, tmp_path, mode):
    import brain.systems.cortex.resources.pools as pools_mod
    from brain.systems.cortex.resources.pools import WorkspacePoolManager
    from brain.systems.cortex.resources.leases import LeaseDecision

    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("warm pool\n")
    target = tmp_path / "target"

    entry = SimpleNamespace(
        id=4,
        repo_root=str(tmp_path),
        base_commit="abc123",
        runtime_fingerprint="host:sha:venv",
        pool_key=None,
        status="ready",
        base_path=str(source),
        mode="copy",
        last_used_at=None,
        ttl_expires_at=None,
        health={},
        created_at=pools_mod._utcnow(),
    )
    entry.pool_key = WorkspacePoolManager().workspace_pool_key(
        repo_root=entry.repo_root,
        base_commit=entry.base_commit,
        runtime_fingerprint=entry.runtime_fingerprint,
        mode=entry.mode,
    )

    session = _FakeSession(entry)
    fake_uow = _FakeUOW(session)
    commands: list[list[str]] = []

    def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        commands.append(list(cmd))
        if cmd[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setenv("CORTEX_WARM_WORKSPACE_POOL_ENABLED", "1")
    monkeypatch.setattr(pools_mod, "UnitOfWork", lambda: fake_uow)
    monkeypatch.setattr(pools_mod, "_probe_workspace_clone_support", lambda probe_root: False)
    monkeypatch.setattr(pools_mod.subprocess, "run", fake_run)

    manager = WorkspacePoolManager()
    manager.lease_manager.acquire_lease = lambda *args, **kwargs: LeaseDecision(
        acquired=True,
        resource_type="workspace_pool_entry",
        resource_id="4",
        lease_token="lease-4",
        owner_run_id=kwargs.get("owner_run_id"),
        owner_worker_id=kwargs.get("owner_worker_id"),
        expires_at=pools_mod._utcnow(),
        lease_id=77,
    )

    plan = manager.plan(
        repo_root=entry.repo_root,
        base_commit=entry.base_commit,
        runtime_fingerprint=entry.runtime_fingerprint,
        mode=mode,
        allow_warm_reuse=True,
        run_id=42,
        target_path=str(target),
    )

    assert plan.warm_start_used is True
    assert plan.mode == "copy"
    assert plan.reason == "warm workspace pool hit"
    assert target.exists()
    assert (target / "README.md").read_text() == "warm pool\n"
    assert all(command[:2] != ["cp", "-cR"] for command in commands)


def test_workspace_pool_plan_uses_clone_mode_when_supported(monkeypatch, tmp_path):
    import brain.systems.cortex.resources.pools as pools_mod
    from brain.systems.cortex.resources.pools import WorkspacePoolManager
    from brain.systems.cortex.resources.leases import LeaseDecision

    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("warm pool\n")
    (source / "nested").mkdir()
    (source / "nested" / "payload.txt").write_text("clone\n")
    target = tmp_path / "target"

    entry = SimpleNamespace(
        id=5,
        repo_root=str(tmp_path),
        base_commit="abc123",
        runtime_fingerprint="host:sha:venv",
        pool_key=None,
        status="ready",
        base_path=str(source),
        mode="reflink",
        last_used_at=None,
        ttl_expires_at=None,
        health={},
        created_at=pools_mod._utcnow(),
    )
    entry.pool_key = WorkspacePoolManager().workspace_pool_key(
        repo_root=entry.repo_root,
        base_commit=entry.base_commit,
        runtime_fingerprint=entry.runtime_fingerprint,
        mode=entry.mode,
    )

    session = _FakeSession(entry)
    fake_uow = _FakeUOW(session)
    commands: list[list[str]] = []

    def fake_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        commands.append(list(cmd))
        if cmd[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
        if cmd[:2] == ["cp", "-cR"]:
            clone_source = Path(str(cmd[2]).removesuffix("/."))
            clone_target = Path(cmd[3])
            shutil.copytree(clone_source, clone_target)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["cp", "--reflink=always", "-R"]:
            clone_source = Path(str(cmd[3]).removesuffix("/."))
            clone_target = Path(cmd[4])
            shutil.copytree(clone_source, clone_target)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setenv("CORTEX_WARM_WORKSPACE_POOL_ENABLED", "1")
    monkeypatch.setattr(pools_mod, "UnitOfWork", lambda: fake_uow)
    monkeypatch.setattr(pools_mod, "_probe_workspace_clone_support", lambda probe_root: True)
    monkeypatch.setattr(pools_mod.subprocess, "run", fake_run)

    manager = WorkspacePoolManager()
    manager.lease_manager.acquire_lease = lambda *args, **kwargs: LeaseDecision(
        acquired=True,
        resource_type="workspace_pool_entry",
        resource_id="5",
        lease_token="lease-5",
        owner_run_id=kwargs.get("owner_run_id"),
        owner_worker_id=kwargs.get("owner_worker_id"),
        expires_at=pools_mod._utcnow(),
        lease_id=78,
    )

    plan = manager.plan(
        repo_root=entry.repo_root,
        base_commit=entry.base_commit,
        runtime_fingerprint=entry.runtime_fingerprint,
        mode="reflink",
        allow_warm_reuse=True,
        run_id=43,
        target_path=str(target),
    )

    assert plan.warm_start_used is True
    assert plan.mode == "reflink"
    assert plan.summary["workspace"]["mode"] == "reflink"
    assert target.exists()
    assert (target / "README.md").read_text() == "warm pool\n"
    assert (target / "nested" / "payload.txt").read_text() == "clone\n"
    assert any(
        command[:2] == ["cp", "-cR"] or command[:3] == ["cp", "--reflink=always", "-R"]
        for command in commands
    )


def test_workspace_pool_validation_failure_destroys_suspicious_candidate(monkeypatch, tmp_path):
    import brain.systems.cortex.resources.pools as pools_mod
    from brain.systems.cortex.resources.pools import WorkspacePoolManager

    source = tmp_path / "dirty"
    source.mkdir()
    entry = SimpleNamespace(
        id=2,
        repo_root=str(tmp_path),
        base_commit="abc123",
        runtime_fingerprint="host:sha:venv",
        pool_key=None,
        status="ready",
        base_path=str(source),
        mode="copy",
        last_used_at=None,
        ttl_expires_at=None,
        health={},
        created_at=pools_mod._utcnow(),
    )
    entry.pool_key = WorkspacePoolManager().workspace_pool_key(
        repo_root=entry.repo_root,
        base_commit=entry.base_commit,
        runtime_fingerprint=entry.runtime_fingerprint,
        mode=entry.mode,
    )

    session = _FakeSession(entry)
    fake_uow = _FakeUOW(session)

    def dirty_run(cmd, cwd=None, capture_output=None, text=None, timeout=None):
        if cmd[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout=" M README.md\n", stderr="")
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setenv("CORTEX_WARM_WORKSPACE_POOL_ENABLED", "1")
    monkeypatch.setattr(pools_mod, "UnitOfWork", lambda: fake_uow)
    monkeypatch.setattr(pools_mod.subprocess, "run", dirty_run)

    manager = WorkspacePoolManager()
    manager.lease_manager.acquire_lease = MagicMock()

    plan = manager.plan(
        repo_root=entry.repo_root,
        base_commit=entry.base_commit,
        runtime_fingerprint=entry.runtime_fingerprint,
        mode="copy",
        allow_warm_reuse=True,
        run_id=42,
        target_path=str(tmp_path / "target"),
    )

    assert plan.warm_start_used is False
    assert plan.reason == "no safe warm workspace candidate available"
    assert entry.status == "destroyed"
    assert not source.exists()
    manager.lease_manager.acquire_lease.assert_not_called()
