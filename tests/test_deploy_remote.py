from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_remote_deploy_defaults_to_streaming_readiness():
    content = (ROOT / "ops" / "deploy-remote.sh").read_text()
    old_private_host = ".".join(("100", "67", "122", "99"))

    assert 'MODE="${ILLO_DEPLOY_MODE:-stream}"' in content
    assert 'REMOTE_HOST="${ILLO_DEPLOY_HOST:-}"' in content
    assert "Missing remote host" in content
    assert old_private_host not in content
    assert "Defaults match the current production box" not in content
    assert "Default." in content
    assert "http://127.0.0.1:8000/api/health/ready" in content
    assert "tail -n +1 -F" in content
    assert 'cd "$PWD"' not in content
    assert "git checkout -f -B main origin/main" in content
    assert "git reset --hard origin/main" in content
    assert "source venv/bin/activate" not in content
    assert "exec ./ops/deploy.sh" in content
    assert "nohup ./ops/deploy.sh" in content
    assert "Deploy process finished; waiting for readiness" in content
    assert 'if [ "\\$deploy_done" = "0" ] && ! kill -0 "\\$deploy_pid"' in content
    assert "exec ./illo start" not in content
