"""Tests for deployment safety hooks."""

import re
import subprocess
from pathlib import Path

from brain.contracts.statuses import ACTIVE_RUN_STATUS_VALUES


def _shell_function_body(content: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(?P<body>.*?)^}}\n",
        content,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"{name} not found")
    return match.group("body")


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

    secrets_idx = content.index("ensure_runtime_secrets")
    doctor_idx = content.index("brain.app.cli.config_doctor --production")
    restart_idx = content.index('echo "=== Restarting services ==="')

    assert secrets_idx < doctor_idx
    assert doctor_idx < restart_idx


def test_ops_deploy_builds_frontend_before_restart():
    deploy_path = Path(__file__).resolve().parents[1] / "ops" / "deploy.sh"
    content = deploy_path.read_text()

    sync_idx = content.index("./ops/sync-frontend-deps.sh")
    build_idx = content.index("npm run build")
    restart_idx = content.index('echo "=== Restarting services ==="')

    assert sync_idx < build_idx < restart_idx


def test_docker_web_proxy_routes_public_webhooks_to_api():
    nginx_path = Path(__file__).resolve().parents[1] / "deploy" / "docker" / "web.nginx.conf"
    content = nginx_path.read_text()

    assert "location = /webhooks" in content
    assert "location /webhooks/" in content
    assert content.count("proxy_pass http://api:8000;") >= 8


def test_docker_build_context_excludes_local_brain_env():
    dockerignore_path = Path(__file__).resolve().parents[1] / ".dockerignore"
    ignored = {
        line.strip()
        for line in dockerignore_path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "brain/.env" in ignored


def test_ops_deploy_drains_worker_instead_of_restarting_active_runs():
    deploy_path = Path(__file__).resolve().parents[1] / "ops" / "deploy.sh"
    content = deploy_path.read_text()

    assert "active_agent_run_count" in content
    assert "restart_or_drain_worker" in content
    assert "start_worker_handoff" in content
    assert "monitor_worker_handoff" in content
    assert "ILLO_WORKER_DISABLE_CYCLE_SCHEDULER=1" in content
    service = (Path(__file__).resolve().parents[1] / "ops" / "cortex-worker.service").read_text()
    assert service.count("ILLO_WORKER_DISABLE_CYCLE_SCHEDULER=1") == 1
    assert "systemctl --user kill --kill-who=main --signal=TERM cortex-worker" in content
    assert "active AgentRun(s); signaling drain instead of restart" in content


def test_ops_deploy_leaves_embedder_running_when_agent_runs_are_active():
    deploy_path = Path(__file__).resolve().parents[1] / "ops" / "deploy.sh"
    content = deploy_path.read_text()

    skip_idx = content.index("if ! should_run_local_embedder; then")
    active_idx = content.index('if [ "$ACTIVE_RUNS" != "0" ]; then')
    leave_idx = content.index("leaving $EMBED_SERVICE running")
    restart_idx = content.index("restart_user_service_if_present illo-embed")

    assert skip_idx < active_idx
    assert active_idx < leave_idx < restart_idx


def test_ops_deploy_skips_local_embedder_when_embedding_backend_is_api():
    deploy_path = Path(__file__).resolve().parents[1] / "ops" / "deploy.sh"
    content = deploy_path.read_text()

    assert "should_run_local_embedder" in content
    assert '[ "${EMBEDDING_BACKEND:-api}" = "gpu" ]' in content
    assert "1|true|TRUE|yes|YES|on|ON" in content
    assert "Embedder:  skipped (EMBEDDING_BACKEND=${EMBEDDING_BACKEND:-api})" in content
    assert "systemctl --user disable --now illo-embed" in content


def test_ops_deploy_renders_systemd_services_for_current_checkout():
    deploy_path = Path(__file__).resolve().parents[1] / "ops" / "deploy.sh"
    content = deploy_path.read_text()

    assert "install_user_service" in content
    assert 'replace("%h/illo-brain", root)' in content
    assert "install_user_service ops/illo-api.service" in content
    assert "install_user_service ops/illo-embed.service" in content
    assert "sync_production_service_env" in content
    assert "systemctl --user enable illo-api" in content
    assert "disable_user_service_if_present illo-dashboard" in content
    assert "systemctl --user restart illo-dashboard" not in content
    assert "stop_legacy_docker_app_containers" in content
    assert "illospace-api-1" in content


def test_ops_deploy_installs_api_service_that_runs_uvicorn():
    root = Path(__file__).resolve().parents[1]
    deploy_content = (root / "ops" / "deploy.sh").read_text()
    service_content = (root / "ops" / "illo-api.service").read_text()

    assert "install_user_service ops/illo-api.service" in deploy_content
    assert "systemctl --user restart illo-api" in deploy_content
    assert "wait_for_api_readiness" in deploy_content
    assert "Last API log lines" in deploy_content
    assert "ExecStart=%h/illo-brain/venv/bin/uvicorn brain.app.api.main:app" in service_content
    assert "CORTEX_INLINE_DISPATCHER=0" in service_content


def test_illo_native_mode_uses_standalone_worker_by_default():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    assert "export CORTEX_INLINE_DISPATCHER=0" in content
    assert "leaving AgentRuns to the standalone worker" in content
    assert 'stop_conflicting_services "illo-dashboard" "illo-api"' in content
    assert "ensure_production_user_services" in content
    assert "Standalone worker service is unavailable; using inline AgentRun dispatcher" in content


def test_illo_native_server_binds_to_loopback_by_default():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    assert 'local api_bind_host="${ILLO_API_HOST:-127.0.0.1}"' in content
    assert '--host "$api_bind_host" --port 8000' in content
    assert "secure with your firewall, tunnel, or reverse proxy" in content


def test_illo_native_mode_installs_current_checkout_worker_services():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    assert "sync_production_service_env" in content
    assert 'install_user_service_template "ops/cortex-worker.service"' in content
    assert 'install_user_service_template "ops/illo-scheduler.service"' in content
    assert "systemctl --user restart cortex-worker.service" in content
    assert "systemctl --user restart illo-scheduler.service" in content


def test_illo_native_mode_autostarts_gpu_when_embedding_backend_is_gpu():
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


def test_illo_agent_run_count_helpers_use_async_unit_of_work():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    for helper in ("active_agent_run_count", "api_owned_active_run_count"):
        body = _shell_function_body(content, helper)
        assert "import asyncio" in body
        assert "async with UnitOfWork()" in body
        assert "await uow.session" in body
        assert "asyncio.run(main())" in body
        assert "with UnitOfWork()" not in body.replace("async with UnitOfWork()", "")


def test_illo_refuses_to_kill_unknown_port_processes_without_force():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    assert "stop_port_processes" in content
    assert "ILLO_FORCE_KILL_PORTS" in content
    assert "non-Illo process(es)" in content
    assert "pids_look_illo_runtime" in content


def test_illo_exposes_native_default_dev_mode_and_compose_deploy():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    assert "start|prod|production)" not in content
    assert "dev-start" not in content
    assert "development)" not in content
    assert "prod|production" not in content
    assert "  dev)" in content
    assert 'run_prod' in content
    assert 'run_dev' in content
    assert "deploy" in content
    assert 'deploy_command "${@:2}"' in content
    assert 'update_command "${@:2}"' in content
    assert "deploy_compose build api web updater" in content
    assert "--no-next" in content
    assert "worker-status" not in content
    assert "worker-drain" not in content


def test_illo_update_auto_selects_native_or_compose_server_mode():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    assert "update_detect_mode" in content
    assert "update_native_services_active" in content
    assert "update_compose_stack_present" in content
    assert 'exec "$ROOT/ops/deploy.sh"' in content
    assert "deploy_command upgrade --build --no-pull" in content
    assert "Self-update is not enabled for local/dev runs." in content


def test_compose_deploy_stays_private_without_builtin_public_ingress():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "deploy" / "compose" / "docker-compose.yml").read_text()
    env_example = (root / "deploy" / "compose" / ".env.production.example").read_text()
    launcher = (root / "illo").read_text()
    services_section = compose.split("services:", 1)[1].rsplit("\nvolumes:", 1)[0]
    service_names = re.findall(r"^  ([a-z][a-z0-9_-]+):$", services_section, flags=re.MULTILINE)

    assert service_names == [
        "postgres",
        "migrate",
        "api",
        "worker",
        "scheduler",
        "slack-connector",
        "updater",
        "web",
    ]
    slack_section = services_section.split("  slack-connector:", 1)[1].split("\n  updater:", 1)[0]
    assert 'profiles: ["slack"]' in slack_section
    assert "\n    ports:" not in slack_section
    assert "127.0.0.1:${ILLO_WEB_PORT:-8080}:8080" in compose
    assert "ILLO_SELF_UPDATE_REQUEST_FILE" in compose
    assert "ILLO_SELF_UPDATE_HEARTBEAT_FILE" in compose
    assert "shared_preload_libraries=pg_stat_statements" in compose
    assert "pg_stat_statements.track=all" in compose
    assert "track_io_timing=on" in compose
    assert "log_min_duration_statement=${POSTGRES_LOG_MIN_DURATION_STATEMENT_MS:-1000}" in compose
    assert "POSTGRES_LOG_MIN_DURATION_STATEMENT_MS=1000" in env_example
    assert 'ILLO_DB_NULLPOOL: "1"' in compose
    assert 'ILLO_WORKER_ENABLE_CYCLE_SCHEDULER: "1"' in compose
    assert 'ILLO_WORKER_DISABLE_CYCLE_SCHEDULER: "0"' in compose
    assert "deploy/docker/updater.Dockerfile" in compose
    assert "illo-self-update-healthcheck" in compose
    assert "/var/run/docker.sock:/var/run/docker.sock" in compose
    assert "../..:/repo" in compose
    assert "deploy " + "publish" not in launcher


def test_compose_self_update_sidecar_can_bootstrap_from_api_queue():
    root = Path(__file__).resolve().parents[1]
    updater_dockerfile = (root / "deploy" / "docker" / "updater.Dockerfile").read_text()
    self_update_daemon = (root / "deploy" / "scripts" / "self-update-daemon.sh").read_text()

    assert "python3" in updater_dockerfile
    assert "curl" in updater_dockerfile
    assert "APP_UID" in self_update_daemon
    assert "chown \"$APP_UID:$APP_GID\"" in self_update_daemon
    assert "chmod 0775" in self_update_daemon
    assert "safe.directory" in self_update_daemon


def test_compose_upgrade_drains_worker_when_agent_runs_are_active():
    upgrade = (Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "upgrade.sh").read_text()

    assert "active_agent_run_count" in upgrade
    assert "status IN" in upgrade
    assert "non_worker_services" in upgrade
    assert "api scheduler web updater" in upgrade
    assert "ILLO_COMPOSE_SKIP_UPDATER_RESTART" in upgrade
    assert "start_worker_handoff" in upgrade
    assert "ILLO_WORKER_DISABLE_CYCLE_SCHEDULER=1" in upgrade
    assert "started handoff worker" in upgrade
    assert "docker update --restart=no" in upgrade
    assert 'docker kill -s TERM "$worker_id"' in upgrade
    assert "wait_for_worker_exit" in upgrade
    assert "compose up -d --force-recreate --no-deps worker" in upgrade
    assert "compose up -d --force-recreate --remove-orphans" in upgrade
    assert "ILLO_COMPOSE_BUILD_NO_CACHE" in upgrade
    assert "ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_FILE" in upgrade
    assert "record_worker_drain_timeout" in upgrade
    assert "avoid killing active AgentRuns" in upgrade


def test_safe_deploy_active_run_guards_match_canonical_active_statuses():
    root = Path(__file__).resolve().parents[1]
    upgrade = (root / "deploy" / "scripts" / "upgrade.sh").read_text()
    launcher = (root / "illo").read_text()
    ops_deploy = (root / "ops" / "deploy.sh").read_text()

    for status in ACTIVE_RUN_STATUS_VALUES:
        assert repr(status) in upgrade
    assert "ACTIVE_RUN_STATUS_VALUES" in launcher
    assert "ACTIVE_RUN_STATUS_VALUES" in ops_deploy
    assert '("starting", "running", "verifying")' not in launcher
    assert '("starting", "running", "verifying")' not in ops_deploy
