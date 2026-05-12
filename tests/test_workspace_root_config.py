from pathlib import Path


def test_resolve_workspace_root_prefers_deploy_env(monkeypatch, tmp_path):
    from brain.kernel import config

    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("ILLO_WORKSPACE_ROOT", str(tmp_path / "illo"))

    assert config.resolve_workspace_root() == tmp_path / "workspace"


def test_resolve_workspace_root_accepts_legacy_illo_env(monkeypatch, tmp_path):
    from brain.kernel import config

    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("ILLO_WORKSPACE_ROOT", str(tmp_path / "illo"))

    assert config.resolve_workspace_root() == tmp_path / "illo"


def test_resolve_workspace_root_uses_default_without_env(monkeypatch, tmp_path):
    from brain.kernel import config

    monkeypatch.delenv("ILLO_WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)

    assert config.resolve_workspace_root(default=Path(tmp_path)) == tmp_path
