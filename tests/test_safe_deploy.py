"""Tests for deployment safety hooks."""

import os
import re
import subprocess
from pathlib import Path

from brain.contracts.statuses import OPEN_RUN_STATUS_VALUES
from brain.contracts.worker_swap import (
    WorkerSwapDecision,
    parse_worker_swap_snapshot,
    worker_swap_rows_sql,
    worker_swap_snapshot,
)


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


def test_docker_web_caches_fingerprinted_frontend_assets_immutably():
    nginx_path = Path(__file__).resolve().parents[1] / "deploy" / "docker" / "web.nginx.conf"
    content = nginx_path.read_text()

    immutable_location = content.split("location ^~ /_app/immutable/ {", 1)[1].split("}", 1)[0]

    assert "try_files $uri =404;" in immutable_location
    assert "expires 1y;" in immutable_location
    assert 'add_header Cache-Control "public, immutable" always;' in immutable_location
    assert content.index("location ^~ /_app/immutable/") < content.index("location / {")


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
    worker_swap_lib = (
        Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "worker-swap-lib.sh"
    ).read_text()

    assert 'source "$ROOT/deploy/scripts/worker-swap-lib.sh"' in content
    assert "worker_swap_snapshot" in content
    assert "restart_or_drain_worker" in content
    assert "start_worker_handoff" in content
    assert "monitor_worker_handoff" in content
    assert "ILLO_WORKER_DISABLE_CYCLE_SCHEDULER=1" in content
    service = (Path(__file__).resolve().parents[1] / "ops" / "cortex-worker.service").read_text()
    assert service.count("ILLO_WORKER_DISABLE_CYCLE_SCHEDULER=1") == 1
    assert "systemctl --user kill --kill-who=main --signal=TERM cortex-worker" in content
    assert "AgentRun(s); signaling drain instead of restart; affected run ids:" in content
    assert "worker_swap_snapshot_report" in content
    assert "worker_swap_snapshot_report" in worker_swap_lib


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


def test_illo_api_owned_run_count_helper_uses_async_unit_of_work():
    illo_path = Path(__file__).resolve().parents[1] / "illo"
    content = illo_path.read_text()

    body = _shell_function_body(content, "api_owned_active_run_count")
    assert "import asyncio" in body
    assert "async with UnitOfWork()" in body
    assert "await uow.session" in body
    assert "asyncio.run(main())" in body
    assert "with UnitOfWork()" not in body.replace("async with UnitOfWork()", "")
    assert 'source "$ROOT/deploy/scripts/worker-swap-lib.sh"' in content


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
    assert "ILLO_RUNTIME_SERVICES_REQUEST_FILE" in compose
    assert "ILLO_RUNTIME_SERVICES_STATUS_FILE" in compose
    assert "ILLO_RUNTIME_SERVICES_HEARTBEAT_FILE" in compose
    assert "ILLO_WORKSPACE_TOOLS_ROOT" in compose
    assert "ILLO_WORKSPACE_TOOLS_REQUEST_FILE" in compose
    assert "ILLO_WORKSPACE_TOOLS_STATUS_FILE" in compose
    assert "ILLO_WORKSPACE_TOOLS_HEARTBEAT_FILE" in compose
    assert "/data/private/runtime-services/heartbeat.json" in compose
    assert "/data/private/workspace-tools/heartbeat.json" in compose
    assert "ILLO_RUNTIME_SERVICES_HEARTBEAT_FILE: /data/private/self-update/heartbeat.json" not in compose
    assert "shared_preload_libraries=pg_stat_statements" in compose
    assert "pg_stat_statements.track=all" in compose
    assert "track_io_timing=on" in compose
    assert "log_min_duration_statement=${POSTGRES_LOG_MIN_DURATION_STATEMENT_MS:-1000}" in compose
    assert "POSTGRES_LOG_MIN_DURATION_STATEMENT_MS=1000" in env_example
    assert 'ILLO_DB_NULLPOOL: "1"' in compose
    assert 'ILLO_WORKER_ENABLE_CYCLE_SCHEDULER: "1"' in compose
    assert 'ILLO_WORKER_DISABLE_CYCLE_SCHEDULER: "0"' in compose
    assert "deploy/docker/updater.Dockerfile" in compose
    assert 'command: ["bash", "/repo/deploy/scripts/self-update-daemon.sh"]' in compose
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
    assert "ILLO_DOCTOR_SKIP_LOCAL_HTTP_PROBES=1" in self_update_daemon
    assert "process_runtime_services_request" in self_update_daemon
    assert "RUNTIME_SERVICES_HEARTBEAT_FILE" in self_update_daemon
    assert "write_runtime_services_heartbeat" in self_update_daemon
    assert "deploy/scripts/runtime-services.sh" in self_update_daemon
    assert "process_workspace_tools_request" in self_update_daemon
    assert "WORKSPACE_TOOLS_HEARTBEAT_FILE" in self_update_daemon
    assert "write_workspace_tools_heartbeat" in self_update_daemon
    assert "deploy/scripts/workspace-tools.sh" in self_update_daemon
    workspace_tools = (root / "deploy" / "scripts" / "workspace-tools.sh").read_text()
    assert '${10:-{}}' not in workspace_tools
    assert '[ -n "$health_json" ] || health_json="{}"' in workspace_tools
    assert '[ -n "$metadata_json" ] || metadata_json="{}"' in workspace_tools
    assert 'npm install --global --prefix "$npm_prefix"' in workspace_tools
    assert "Workspace tool target missing" in workspace_tools
    assert "tool_status=$?" in workspace_tools
    assert 'if [ "$tool_status" -ne 0 ] || [ -z "$tool_version" ]; then' in workspace_tools


def test_compose_doctor_can_skip_host_local_http_probes_from_sidecars():
    root = Path(__file__).resolve().parents[1]
    doctor = (root / "deploy" / "scripts" / "doctor.sh").read_text()

    assert "ILLO_DOCTOR_SKIP_LOCAL_HTTP_PROBES" in doctor
    assert "outside the host network namespace" in doctor


def test_compose_upgrade_drains_worker_when_agent_runs_are_active():
    root = Path(__file__).resolve().parents[1]
    upgrade = (root / "deploy" / "scripts" / "upgrade.sh").read_text()
    runtime_lib = (root / "deploy" / "scripts" / "compose-runtime-lib.sh").read_text()
    combined = upgrade + runtime_lib

    assert 'source "$SCRIPT_DIR/compose-runtime-lib.sh"' in upgrade
    assert 'source "$COMPOSE_RUNTIME_LIB_DIR/worker-swap-lib.sh"' in runtime_lib
    assert "worker_swap_snapshot_acquire" in runtime_lib
    assert "non_worker_services" in upgrade
    assert "api scheduler web updater" in upgrade
    assert "ILLO_COMPOSE_SKIP_UPDATER_RESTART" in upgrade
    assert "schedule_updater_refresh_after_self_update" in upgrade
    assert "ILLO_COMPOSE_UPDATER_SELF_REFRESH_DELAY_SECONDS" in upgrade
    assert "--name \"$job_name\"" in upgrade
    assert 'docker rm -f \\"$job_name\\"' in upgrade
    assert "up -d --force-recreate --no-deps updater" in upgrade
    assert "start_worker_handoff" in runtime_lib
    assert "ILLO_WORKER_DISABLE_CYCLE_SCHEDULER=1" in runtime_lib
    assert "started handoff worker" in runtime_lib
    assert "docker update --restart=no" in runtime_lib
    assert 'docker kill -s TERM "$worker_id"' in runtime_lib
    assert "wait_for_worker_exit" in runtime_lib
    assert "compose up -d --force-recreate --no-deps worker" in runtime_lib
    assert "compose up -d --force-recreate --remove-orphans" in upgrade
    assert "replace_idle_worker" in combined
    assert "no interactive AgentRuns" in runtime_lib
    assert "refusing to kill it" in runtime_lib
    assert "remove_worker_handoff_after_drain_timeout" in runtime_lib
    assert 'docker rm -f "$handoff_id"' in runtime_lib
    assert "FORCED WORKER SWAP: killing old worker; affected run ids:" in runtime_lib
    assert "assert_single_running_worker" in runtime_lib
    assert "assert_single_running_worker" in upgrade
    assert "ILLO_COMPOSE_BUILD_NO_CACHE" in upgrade
    assert "ILLO_COMPOSE_WORKER_DRAIN_TIMEOUT_FILE" in upgrade


def test_compose_worker_drain_timeout_removes_handoff_and_preserves_original_worker(tmp_path):
    runtime_lib = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "compose-runtime-lib.sh"
    docker_log = tmp_path / "docker.log"
    script = f'''
source "{runtime_lib}"
worker_swap_snapshot_count() {{ printf '1\\n'; }}
worker_swap_snapshot_run_ids() {{ printf '477\\n'; }}
worker_swap_snapshot_details() {{ printf '477:running\\n'; }}
worker_swap_snapshot_report() {{ printf 'Worker pre-swap check'; }}
worker_container_id() {{ printf 'original-worker\\n'; }}
start_worker_handoff() {{ printf 'handoff-worker\\n'; }}
wait_for_worker_exit() {{ return 1; }}
docker() {{
  printf '%s\\n' "$*" >> "{docker_log}"
  if [ "$1" = "inspect" ]; then
    return 1
  fi
}}
update_worker_after_drain snapshot
'''

    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert result.returncode == 1
    docker_calls = docker_log.read_text()
    assert "update --restart=unless-stopped original-worker" in docker_calls
    assert "rm -f handoff-worker" in docker_calls
    assert "rm -f original-worker" not in docker_calls
    assert "removed temporary handoff worker handoff-worker" in result.stderr
    assert "New AgentRuns may remain queued" in result.stderr


def test_compose_worker_restart_asserts_exactly_one_running_worker():
    runtime_lib = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "compose-runtime-lib.sh"
    script = f'''
source "{runtime_lib}"
worker_swap_snapshot() {{ printf 'snapshot\\n'; }}
worker_swap_snapshot_decision() {{ printf 'replace\\n'; }}
replace_idle_worker() {{ return 0; }}
compose() {{
  if [ "$*" = "ps --status running -q worker" ]; then
    printf 'regular-worker\\nhandoff-worker\\n'
  fi
}}
restart_runtime_worker_service
'''

    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert result.returncode == 1
    assert "expected exactly one running worker container, found 2" in result.stderr
    assert "keep the intended regular worker" in result.stderr


def test_compose_runtime_service_restart_supports_one_many_or_all_services():
    root = Path(__file__).resolve().parents[1]
    runtime_services = (root / "deploy" / "scripts" / "runtime-services.sh").read_text()
    runtime_lib = (root / "deploy" / "scripts" / "compose-runtime-lib.sh").read_text()
    catalog = (root / "deploy" / "compose" / "runtime-services.json").read_text()

    assert "runtime_service_ids" in runtime_services
    assert 'source "$SCRIPT_DIR/compose-runtime-lib.sh"' in runtime_services
    assert "expand_runtime_services" in runtime_lib
    assert "runtime_service_ids_for_all" in runtime_lib
    assert "host_controller" in catalog
    assert '"include_in_all": false' in catalog
    assert "slack_connector" in catalog
    assert "slack-connector" in catalog
    assert "runtime-services.json" in runtime_lib
    assert "compose up -d --force-recreate --no-deps" in runtime_lib
    assert "restart_runtime_worker_service" in runtime_lib
    assert "worker_swap_snapshot" in runtime_lib
    assert "started handoff worker" in runtime_lib
    assert "refusing to kill it" in runtime_lib


def test_compose_pre_swap_check_reports_nonterminal_run_ids():
    runtime_lib = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "compose-runtime-lib.sh"
    script = f'''
source "{runtime_lib}"
compose() {{
  printf '[{{"id":2327,"status":"paused"}},{{"id":2330,"status":"running"}},{{"id":2331,"status":"queued"}}]\\n'
}}
snapshot="$(worker_swap_snapshot)"
echo "$(worker_swap_snapshot_report "$snapshot")."
'''

    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "Worker pre-swap check: 3 interactive run(s) in flight "
        "(run ids: 2327,2330,2331; id/status: 2327:paused,2330:running,2331:queued)."
    )


def test_worker_swap_snapshot_derives_decision_and_presentation_from_canonical_policy():
    snapshot = worker_swap_snapshot(
        [
            (2331, "queued"),
            (2327, "paused"),
            (2330, "running"),
        ]
    )
    parsed = parse_worker_swap_snapshot(snapshot.as_json())

    assert parsed.decision is WorkerSwapDecision.DRAIN
    assert parsed.count == 3
    assert parsed.run_ids == (2327, 2330, 2331)
    assert parsed.details == "2327:paused,2330:running,2331:queued"
    for status in OPEN_RUN_STATUS_VALUES:
        assert repr(status) in worker_swap_rows_sql()


def test_safe_deploy_scripts_cannot_restate_worker_swap_status_policy():
    root = Path(__file__).resolve().parents[1]
    consumers = [
        root / "illo",
        root / "ops" / "deploy.sh",
        root / "deploy" / "scripts" / "compose-runtime-lib.sh",
        root / "deploy" / "scripts" / "upgrade.sh",
        root / "deploy" / "scripts" / "runtime-services.sh",
        root / "deploy" / "scripts" / "worker-swap-lib.sh",
    ]
    forbidden = (
        "OPEN_RUN_STATUS_VALUES",
        "SELECT id, status FROM agent_runs",
        "nonterminal_agent_run_details",
        "active_agent_run_count",
    )

    for path in consumers:
        content = path.read_text()
        for copied_policy_shape in forbidden:
            assert copied_policy_shape not in content, (
                f"{path.relative_to(root)} restates the worker-swap policy via "
                f"{copied_policy_shape!r}"
            )
        for status in OPEN_RUN_STATUS_VALUES:
            assert repr(status) not in content
            assert f'"{status}"' not in content

    contract = (root / "brain" / "contracts" / "worker_swap.py").read_text()
    native_adapter = (root / "brain" / "app" / "cli" / "worker_swap_snapshot.py").read_text()
    assert "OPEN_RUN_STATUS_VALUES" in contract
    assert "OPEN_RUN_STATUS_VALUES" in native_adapter


def test_compose_worker_survives_a_host_reboot_like_every_other_service():
    """The worker must come back on boot, and must be able to exit in time to.

    Regression for #527: the worker was the only service that stayed down after a
    power outage. Its declared policy was already correct -- what stranded it was
    a 24h stop grace it could never clear inside the daemon's shutdown budget.
    """
    root = Path(__file__).resolve().parents[1]
    compose = (root / "deploy" / "compose" / "docker-compose.yml").read_text()
    services_section = compose.split("services:", 1)[1].rsplit("\nvolumes:", 1)[0]
    worker_section = services_section.split("  worker:", 1)[1].split("\n  scheduler:", 1)[0]
    # Assert on directives, not on the prose explaining them.
    worker_directives = "\n".join(
        line for line in worker_section.splitlines() if not line.lstrip().startswith("#")
    )
    anchor = compose.split("x-backend-service:", 1)[1].split("\nservices:", 1)[0]

    # The worker inherits restart: unless-stopped from the backend anchor and
    # must never opt out of it the way one-shot migrate does.
    assert "restart: unless-stopped" in anchor
    assert "restart:" not in worker_directives

    assert "stop_grace_period: ${ILLO_WORKER_STOP_GRACE_PERIOD:-10s}" in worker_directives
    assert "24h" not in worker_directives


def test_worker_restart_policy_suspension_is_restored_when_a_deploy_is_interrupted(tmp_path):
    """An interrupted swap must not strand the worker at restart=no (#527)."""
    runtime_lib = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "compose-runtime-lib.sh"
    docker_log = tmp_path / "docker.log"
    script = f'''
source "{runtime_lib}"
docker() {{ printf '%s\\n' "$*" >> "{docker_log}"; }}
suspend_worker_restart_policy stranded-worker
# Simulate the deploy dying mid-drain: Ctrl-C, SSH drop, dockerd snap refresh.
exit 130
'''

    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert result.returncode == 130
    docker_calls = docker_log.read_text()
    assert "update --restart=no stranded-worker" in docker_calls
    assert "update --restart=unless-stopped stranded-worker" in docker_calls


def test_worker_restart_policy_is_not_restored_onto_a_replaced_worker(tmp_path):
    """Restoring the policy on the outgoing container would arm a zombie worker.

    The replacement already carries the declared policy; re-arming the container
    the handoff just retired would bring a second worker back on the next boot,
    which is the #486 failure mode one reboot later.
    """
    runtime_lib = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "compose-runtime-lib.sh"
    docker_log = tmp_path / "docker.log"
    script = f'''
source "{runtime_lib}"
docker() {{ printf '%s\\n' "$*" >> "{docker_log}"; }}
compose() {{ :; }}
worker_container_id() {{ printf 'old-worker\\n'; }}
worker_swap_snapshot() {{ printf 'snapshot\\n'; }}
worker_swap_snapshot_decision() {{ printf 'replace\\n'; }}
wait_for_worker_exit() {{ return 0; }}
replace_idle_worker
'''

    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    docker_calls = docker_log.read_text()
    assert "update --restart=no old-worker" in docker_calls
    assert "update --restart=unless-stopped old-worker" not in docker_calls


def test_worker_restart_policy_drift_is_repaired_before_a_restart(tmp_path):
    """A worker already stranded by an earlier interruption must self-heal."""
    runtime_lib = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "compose-runtime-lib.sh"
    docker_log = tmp_path / "docker.log"
    script = f'''
source "{runtime_lib}"
worker_container_id() {{ printf 'drifted-worker\\n'; }}
docker() {{
  if [ "$1" = "inspect" ]; then printf 'no\\n'; return 0; fi
  printf '%s\\n' "$*" >> "{docker_log}"
}}
reconcile_worker_restart_policy
'''

    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "update --restart=unless-stopped drifted-worker" in docker_log.read_text()
    assert "restart policy had drifted to 'no'" in result.stderr
    assert "survives a host reboot" in result.stderr


def _inert_check_script(runtime_lib: Path, running: str) -> str:
    return f'''
source "{runtime_lib}"
compose() {{
  if [ "$*" = "ps --services --status running" ]; then
    printf '{running}'
  fi
}}
assert_stack_not_inert
'''


def test_inert_stack_check_fails_loudly_when_only_the_worker_is_absent():
    """The healthy-but-inert state: every probe passes, Illo does nothing (#527)."""
    runtime_lib = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "compose-runtime-lib.sh"
    running = "postgres\\napi\\nweb\\nscheduler\\n"

    result = subprocess.run(
        ["bash", "-c", _inert_check_script(runtime_lib, running)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 3
    assert "Stack is INERT: 4 service(s) running but required service(s) absent: worker." in result.stderr
    assert "structurally incapable of doing any work" in result.stderr
    assert "up -d --no-deps worker" in result.stderr


def test_inert_stack_check_separates_a_fully_down_stack_from_an_inert_one():
    """A monitor must be able to tell an invisible failure from an obvious one."""
    runtime_lib = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "compose-runtime-lib.sh"

    down = subprocess.run(
        ["bash", "-c", _inert_check_script(runtime_lib, "")],
        capture_output=True,
        text=True,
    )
    healthy = subprocess.run(
        ["bash", "-c", _inert_check_script(runtime_lib, "postgres\\napi\\nweb\\nworker\\nscheduler\\n")],
        capture_output=True,
        text=True,
    )

    assert down.returncode == 4
    assert "Stack is fully down" in down.stderr
    assert healthy.returncode == 0
    assert healthy.stderr == ""


def test_inert_stack_check_is_reachable_as_a_standalone_monitor_entrypoint():
    root = Path(__file__).resolve().parents[1]
    check = root / "deploy" / "scripts" / "inert-stack-check.sh"
    launcher = (root / "illo").read_text()

    assert check.exists()
    content = check.read_text()
    assert 'source "$SCRIPT_DIR/compose-runtime-lib.sh"' in content
    assert "assert_stack_not_inert" in content
    # The entrypoint may document the override, but must not define its own list.
    assert not re.search(r"^STACK_REQUIRED_SERVICES=", content, flags=re.MULTILINE)
    assert "deploy/scripts/inert-stack-check.sh" in launcher


def test_deploy_doctor_delegates_stack_presence_to_the_shared_invariant():
    root = Path(__file__).resolve().parents[1]
    doctor = (root / "deploy" / "scripts" / "doctor.sh").read_text()

    assert 'source "$SCRIPT_DIR/compose-runtime-lib.sh"' in doctor
    assert "assert_stack_not_inert $DOCTOR_REQUIRED_SERVICES" in doctor
    # Presence has exactly one owner; doctor only grades health of what is present.
    assert "service is not running" not in doctor
    assert "compose() {" not in doctor


def test_boot_unit_binds_to_the_docker_unit_that_actually_exists(tmp_path):
    """A hardcoded docker.service silently fails on a Docker snap host (#527)."""
    root = Path(__file__).resolve().parents[1]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        "if [ \"$1\" = list-unit-files ]; then\n"
        "  if [ \"$2\" = snap.docker.dockerd.service ]; then\n"
        "    echo 'snap.docker.dockerd.service enabled'\n"
        "    exit 0\n"
        "  fi\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    systemctl.chmod(0o755)
    docker = bin_dir / "docker"
    docker.write_text("#!/usr/bin/env bash\nexit 0\n")
    docker.chmod(0o755)

    env_file = tmp_path / ".env"
    env_file.write_text("COMPOSE_PROFILES=slack\n")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["ILLO_COMPOSE_ENV_FILE"] = str(env_file)

    result = subprocess.run(
        [str(root / "deploy" / "scripts" / "install-boot-unit.sh"), "--print"],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    unit = result.stdout
    assert "Requires=snap.docker.dockerd.service" in unit
    assert "After=snap.docker.dockerd.service" in unit
    assert "docker.service" not in unit.replace("snap.docker.dockerd.service", "")
    assert "Type=oneshot" in unit
    assert "RemainAfterExit=yes" in unit
    assert "WantedBy=multi-user.target" in unit
    exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
    # Profile-gated services must be part of the boot reconcile.
    assert "--profile slack" in exec_start
    assert exec_start.endswith("up -d")
    # A reconcile, never a redeploy: force-recreate here would fight a running
    # handoff, and an ExecStop would tear the stack down on `systemctl stop`.
    assert "--force-recreate" not in exec_start
    assert "ExecStop=" not in unit


def test_worker_restart_policy_suspension_preserves_an_existing_exit_trap(tmp_path):
    """doctor.sh sources this lib and installs its own cleanup trap."""
    runtime_lib = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "compose-runtime-lib.sh"
    docker_log = tmp_path / "docker.log"
    script = f'''
source "{runtime_lib}"
docker() {{ printf '%s\\n' "$*" >> "{docker_log}"; }}
caller_cleanup() {{ printf 'caller cleanup ran\\n'; }}
trap caller_cleanup EXIT
suspend_worker_restart_policy some-worker
exit 7
'''

    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert result.returncode == 7
    assert "update --restart=unless-stopped some-worker" in docker_log.read_text()
    assert "caller cleanup ran" in result.stdout


def test_worker_restart_policy_is_restored_when_the_replacement_fails_to_start(tmp_path):
    """A failed recreate leaves the outgoing worker as the only worker.

    Abandoning the suspension there would strand it at restart=no, which is the
    exact silent-inertness this whole path exists to prevent (#527).
    """
    runtime_lib = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "compose-runtime-lib.sh"
    docker_log = tmp_path / "docker.log"
    script = f'''
source "{runtime_lib}"
docker() {{ printf '%s\\n' "$*" >> "{docker_log}"; }}
compose() {{ return 1; }}
worker_container_id() {{ printf 'old-worker\\n'; }}
worker_swap_snapshot() {{ printf 'snapshot\\n'; }}
worker_swap_snapshot_decision() {{ printf 'replace\\n'; }}
wait_for_worker_exit() {{ return 0; }}
replace_idle_worker
'''

    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    docker_calls = docker_log.read_text()
    assert "update --restart=no old-worker" in docker_calls
    assert "update --restart=unless-stopped old-worker" in docker_calls
