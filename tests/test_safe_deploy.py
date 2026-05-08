"""Tests for deployment safety hooks."""

import re
import subprocess
from pathlib import Path


class TestPrePushHook:
    """Test the pre-push hook script logic."""

    def test_blocks_main_push(self):
        hook = str(Path(__file__).resolve().parent.parent / ".githooks" / "pre-push")
        r = subprocess.run(
            ["bash", hook],
            input="abc123 def456 refs/heads/main ghi789\n",
            capture_output=True, text=True,
        )
        assert r.returncode == 1
        assert "blocked" in r.stdout.lower()

    def test_allows_feature_push(self):
        hook = str(Path(__file__).resolve().parent.parent / ".githooks" / "pre-push")
        r = subprocess.run(
            ["bash", hook],
            input="abc123 def456 refs/heads/feature/x ghi789\n",
            capture_output=True, text=True,
        )
        assert r.returncode == 0


def test_ops_deploy_runs_config_doctor_before_restart():
    deploy_path = Path(__file__).resolve().parents[1] / "ops" / "deploy.sh"
    content = deploy_path.read_text()

    doctor_idx = content.index("brain.app.cli.config_doctor --production")
    restart_idx = content.index('echo "=== Restarting services ==="')

    assert doctor_idx < restart_idx


def test_ops_deploy_builds_frontend_before_restart():
    deploy_path = Path(__file__).resolve().parents[1] / "ops" / "deploy.sh"
    content = deploy_path.read_text()

    sync_idx = content.index("./ops/sync-frontend-deps.sh")
    build_idx = content.index("npm run build")
    restart_idx = content.index('echo "=== Restarting services ==="')

    assert sync_idx < build_idx < restart_idx


def test_ops_deploy_drains_worker_instead_of_restarting_active_runs():
    deploy_path = Path(__file__).resolve().parents[1] / "ops" / "deploy.sh"
    content = deploy_path.read_text()

    assert "active_agent_run_count" in content
    assert "restart_or_drain_worker" in content
    assert "systemctl --user kill --kill-who=main --signal=TERM cortex-worker" in content
    assert "active AgentRun(s); signaling drain instead of restart" in content


def test_ops_deploy_leaves_embedder_running_when_agent_runs_are_active():
    deploy_path = Path(__file__).resolve().parents[1] / "ops" / "deploy.sh"
    content = deploy_path.read_text()

    active_idx = content.index('if [ "$ACTIVE_RUNS" != "0" ]; then')
    leave_idx = content.index("leaving $EMBED_SERVICE running")
    restart_idx = content.index("restart_user_service_if_present illo-embed")

    assert active_idx < leave_idx < restart_idx


def test_ops_deploy_renders_systemd_services_for_current_checkout():
    deploy_path = Path(__file__).resolve().parents[1] / "ops" / "deploy.sh"
    content = deploy_path.read_text()

    assert "install_user_service" in content
    assert 'replace("%h/illo-brain", root)' in content
    assert "install_user_service ops/illo-embed.service" in content
    assert "sync_production_service_env" in content


def test_illo_start_uses_standalone_worker_by_default():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    assert "export CORTEX_INLINE_DISPATCHER=0" in content
    assert "leaving AgentRuns to the standalone worker" in content
    assert 'stop_conflicting_services "illo-dashboard" "illo-api"' in content
    assert "ensure_production_user_services" in content
    assert "Standalone worker service is unavailable; using inline AgentRun dispatcher" in content


def test_illo_start_installs_current_checkout_worker_services():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    assert "sync_production_service_env" in content
    assert 'install_user_service_template "ops/cortex-worker.service"' in content
    assert 'install_user_service_template "ops/illo-scheduler.service"' in content
    assert "systemctl --user restart cortex-worker.service" in content
    assert "systemctl --user restart illo-scheduler.service" in content


def test_illo_start_autostarts_gpu_when_embedding_backend_is_gpu():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    assert 'elif [ "${EMBEDDING_BACKEND:-api}" = "gpu" ]; then' in content
    assert "EMBEDDING_BACKEND=gpu - starting local GPU embedder" in content
    assert "unload_unused_embedding_worker" in content


def test_illo_refuses_to_kill_api_owned_agent_runs_without_force():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    assert "guard_api_port_kill" in content
    assert "ILLO_FORCE_KILL_ACTIVE_RUNS" in content
    assert "Refusing to kill API pid(s)" in content


def test_illo_refuses_to_kill_unknown_port_processes_without_force():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    assert "stop_port_processes" in content
    assert "ILLO_FORCE_KILL_PORTS" in content
    assert "non-Illo process(es)" in content
    assert "pids_look_illo_runtime" in content


def test_illo_exposes_dev_start_and_deploy_aliases():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    assert "dev-start" in content
    assert "deploy" in content
    assert "dev-start|dev|development)" in content
    assert 'deploy_command "${@:2}"' in content
    assert "native)" in content
    assert '"$ROOT/ops/deploy.sh" "$@"' in content


def test_compose_deploy_stays_private_without_builtin_public_ingress():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "deploy" / "compose" / "docker-compose.yml").read_text()
    launcher = (root / "illo").read_text()
    services_section = compose.split("services:", 1)[1].rsplit("\nvolumes:", 1)[0]
    service_names = re.findall(r"^  ([a-z][a-z0-9_-]+):$", services_section, flags=re.MULTILINE)

    assert service_names == ["postgres", "migrate", "api", "worker", "scheduler", "web"]
    assert "127.0.0.1:${ILLO_WEB_PORT:-8080}:8080" in compose
    assert "deploy " + "publish" not in launcher
