import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE_ENV_VARS = (
    "DATABASE_URL",
    "DB_URL",
    "BRAIN_DB_URL",
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
)


def _write_launcher_functions(tmp_path: Path) -> Path:
    launcher = ROOT / "illo"
    lines = []
    for line in launcher.read_text(encoding="utf-8").splitlines():
        lines.append(line)
        if line.startswith("# ") and " CLI " in line:
            break

    functions_file = tmp_path / "illo-functions.sh"
    functions_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return functions_file


def test_uninstall_marker_ignores_inherited_database_env_once(tmp_path):
    functions_file = _write_launcher_functions(tmp_path)
    env = os.environ.copy()
    for var in DATABASE_ENV_VARS:
        env[var] = f"old-{var.lower()}"

    script = """
set -euo pipefail
cd "$1"
source "$2"
touch "$UNINSTALL_DB_ENV_RESET_MARKER"
apply_uninstall_database_env_reset
for var in DATABASE_URL DB_URL BRAIN_DB_URL DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD; do
  if [ "${!var+x}" = x ]; then
    echo "$var was not reset"
    exit 1
  fi
done
test ! -e "$UNINSTALL_DB_ENV_RESET_MARKER"
"""

    subprocess.run(
        ["bash", "-c", script, "bash", str(tmp_path), str(functions_file)],
        env=env,
        check=True,
    )


def test_uninstall_marker_preserves_explicit_env_file_config(tmp_path):
    functions_file = _write_launcher_functions(tmp_path)
    env = os.environ.copy()
    env["DB_PORT"] = "5432"

    script = """
set -euo pipefail
cd "$1"
source "$2"
touch .env "$UNINSTALL_DB_ENV_RESET_MARKER"
apply_uninstall_database_env_reset
test "${DB_PORT:-}" = "5432"
test ! -e "$UNINSTALL_DB_ENV_RESET_MARKER"
"""

    subprocess.run(
        ["bash", "-c", script, "bash", str(tmp_path), str(functions_file)],
        env=env,
        check=True,
    )


def test_runtime_secrets_are_generated_for_first_start(tmp_path):
    functions_file = _write_launcher_functions(tmp_path)

    script = """
set -euo pipefail
cd "$1"
source "$2"
unset SECRET_KEY VAULT_MASTER_KEY ILLO_PRIVATE_HOME ILLO_RUNTIME_ENV_FILE
ensure_runtime_secrets
test -s .illo/runtime.env
grep '^SECRET_KEY=' .illo/runtime.env >/dev/null
grep '^VAULT_MASTER_KEY=' .illo/runtime.env >/dev/null
test -n "${SECRET_KEY:-}"
test -n "${VAULT_MASTER_KEY:-}"
"""

    subprocess.run(
        ["bash", "-c", script, "bash", str(tmp_path), str(functions_file)],
        env=os.environ.copy(),
        check=True,
    )


def test_generated_runtime_env_fills_empty_env_example_values(tmp_path):
    functions_file = _write_launcher_functions(tmp_path)
    (tmp_path / ".env").write_text("SECRET_KEY=\nVAULT_MASTER_KEY=\n", encoding="utf-8")

    script = """
set -euo pipefail
cd "$1"
source "$2"
unset SECRET_KEY VAULT_MASTER_KEY ILLO_PRIVATE_HOME ILLO_RUNTIME_ENV_FILE
source_env
test -z "${SECRET_KEY:-}"
test -z "${VAULT_MASTER_KEY:-}"
ensure_runtime_secrets
secret="$SECRET_KEY"
vault="$VAULT_MASTER_KEY"
unset SECRET_KEY VAULT_MASTER_KEY
source_env
test "$SECRET_KEY" = "$secret"
test "$VAULT_MASTER_KEY" = "$vault"
"""

    subprocess.run(
        ["bash", "-c", script, "bash", str(tmp_path), str(functions_file)],
        env=os.environ.copy(),
        check=True,
    )


def test_dotenv_values_override_generated_runtime_env(tmp_path):
    functions_file = _write_launcher_functions(tmp_path)
    (tmp_path / ".env").write_text("SECRET_KEY=from-dotenv\nVAULT_MASTER_KEY=from-dotenv-vault\n", encoding="utf-8")
    runtime_dir = tmp_path / ".illo"
    runtime_dir.mkdir()
    (runtime_dir / "runtime.env").write_text("SECRET_KEY=generated\nVAULT_MASTER_KEY=generated-vault\n", encoding="utf-8")

    script = """
set -euo pipefail
cd "$1"
source "$2"
unset SECRET_KEY VAULT_MASTER_KEY ILLO_PRIVATE_HOME ILLO_RUNTIME_ENV_FILE
source_env
test "$SECRET_KEY" = "from-dotenv"
test "$VAULT_MASTER_KEY" = "from-dotenv-vault"
"""

    subprocess.run(
        ["bash", "-c", script, "bash", str(tmp_path), str(functions_file)],
        env=os.environ.copy(),
        check=True,
    )


def test_production_service_env_mirrors_active_runtime(tmp_path):
    functions_file = _write_launcher_functions(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "EMBEDDING_BACKEND=api",
                "EMBEDDING_API_PROVIDER=openai",
                "EMBEDDING_API_MODEL=text-embedding-3-small",
                "EMBEDDING_API_KEY=embed-key",
                "",
            ]
        ),
        encoding="utf-8",
    )
    runtime_dir = tmp_path / ".illo"
    runtime_dir.mkdir()
    (runtime_dir / "runtime.env").write_text(
        "SECRET_KEY=runtime-secret\nVAULT_MASTER_KEY=runtime-vault\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HOME"] = str(home)

    script = """
set -euo pipefail
cd "$1"
source "$2"
unset SECRET_KEY VAULT_MASTER_KEY EMBEDDING_BACKEND EMBEDDING_API_KEY ILLO_PRIVATE_HOME ILLO_RUNTIME_ENV_FILE
source_env
export DB_HOST=127.0.0.1
export DB_PORT=55432
export DB_NAME=illo_memory
export DB_USER=illo
export DB_PASSWORD=illo
sync_production_service_env >/dev/null
prod="$HOME/.config/illo-brain/production.env"
test -s "$prod"
mode="$(python3 - "$prod" <<'PY'
import os
import sys
print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])
PY
)"
test "$mode" = "600"
grep '^DB_PORT=55432$' "$prod" >/dev/null
grep '^SECRET_KEY=runtime-secret$' "$prod" >/dev/null
grep '^VAULT_MASTER_KEY=runtime-vault$' "$prod" >/dev/null
grep '^EMBEDDING_BACKEND=api$' "$prod" >/dev/null
grep '^EMBEDDING_API_KEY=embed-key$' "$prod" >/dev/null
"""

    subprocess.run(
        ["bash", "-c", script, "bash", str(tmp_path), str(functions_file)],
        env=env,
        check=True,
    )


def test_install_user_service_template_renders_current_checkout_path(tmp_path):
    functions_file = _write_launcher_functions(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    ops = tmp_path / "ops"
    ops.mkdir()
    (ops / "cortex-worker.service").write_text(
        "WorkingDirectory=%h/illo-brain\nExecStart=%h/illo-brain/venv/bin/python3 -m worker\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HOME"] = str(home)

    script = """
set -euo pipefail
cd "$1"
source "$2"
install_user_service_template ops/cortex-worker.service >/dev/null
svc="$HOME/.config/systemd/user/cortex-worker.service"
test -s "$svc"
grep "WorkingDirectory=$1" "$svc" >/dev/null
grep "ExecStart=$1/venv/bin/python3" "$svc" >/dev/null
"""

    subprocess.run(
        ["bash", "-c", script, "bash", str(tmp_path), str(functions_file)],
        env=env,
        check=True,
    )


def test_postgres_admin_probe_never_prompts_for_password(tmp_path):
    functions_file = _write_launcher_functions(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "psql-calls.log"
    (fake_bin / "psql").write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {calls!s}\nexit 1\n",
        encoding="utf-8",
    )
    (fake_bin / "sudo").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    (fake_bin / "psql").chmod(0o755)
    (fake_bin / "sudo").chmod(0o755)
    sql_file = tmp_path / "bootstrap.sql"
    sql_file.write_text("SELECT 1;\n", encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    script = """
set -euo pipefail
cd "$1"
source "$2"
run_postgres_admin_sql 127.0.0.1 5432 "$3" || true
grep -- '-w' "$4" >/dev/null
"""

    subprocess.run(
        [
            "bash",
            "-c",
            script,
            "bash",
            str(tmp_path),
            str(functions_file),
            str(sql_file),
            str(calls),
        ],
        env=env,
        check=True,
    )


def test_managed_postgres_can_prepare_repo_local_runtime(tmp_path):
    functions_file = _write_launcher_functions(tmp_path)
    fake_bin = tmp_path / "pgbin"
    fake_bin.mkdir()
    (fake_bin / "initdb").write_text(
        """#!/usr/bin/env bash
set -e
data_dir=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-D" ]; then
    shift
    data_dir="$1"
  fi
  shift || true
done
mkdir -p "$data_dir"
printf '16\\n' > "$data_dir/PG_VERSION"
""",
        encoding="utf-8",
    )
    (fake_bin / "pg_ctl").write_text(
        """#!/usr/bin/env bash
case "$*" in
  *" status"*) exit 1 ;;
  *" start"*) exit 0 ;;
  *" stop"*) exit 0 ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    (fake_bin / "psql").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (fake_bin / "postgres").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    for path in fake_bin.iterdir():
        path.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    script = """
set -euo pipefail
cd "$1"
source "$2"
mkdir -p "$LOCAL_POSTGRES_RUNTIME_DIR"
printf '55432\n' > "$LOCAL_POSTGRES_PORT_FILE"
ensure_managed_postgres_db illo illo illo_memory
test "$DB_HOST" = "127.0.0.1"
test -n "$DB_PORT"
test "$MANAGED_POSTGRES_PORT" = "$DB_PORT"
test -s "$LOCAL_POSTGRES_PORT_FILE"
"""

    subprocess.run(
        ["bash", "-c", script, "bash", str(tmp_path), str(functions_file)],
        env=env,
        check=True,
    )
