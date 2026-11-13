"""Run real shell commands in temporary sandbox roots."""

import asyncio
import os
import time

import httpx
import pytest

from sandbox.server import CAP, create_app


@pytest.fixture
async def service(tmp_path):
    work, repos = tmp_path / "work", tmp_path / "repos"
    repos.mkdir()
    app = create_app(work_root=work, repos_root=repos, slot_wait_s=0.05, process_limit=4096)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://sandbox"
    ) as client:
        yield client, work, repos


async def execute(service, command, **kwargs):
    return await service[0].post("/exec", json={"run_id": "run_1", "command": command, **kwargs})


async def test_output_exit_code_environment_and_scripts(service, monkeypatch):
    monkeypatch.setenv("ACME_SECRET_TOKEN", "do-not-pass")
    monkeypatch.setenv("GIT_DIR", "/wrong")
    result = (await execute(service,
        'printf hello; printf error >&2; printf "%s" "${ACME_SECRET_TOKEN-unset}:${GIT_DIR-unset}"; '
        'printf "echo script" > script.sh; bash script.sh; '
        'printf "%s" "$HOME"; git config --global --get safe.directory; exit 7')).json()
    assert result["exit_code"] == 7
    assert result["stdout"] == f"hellounset:unsetscript\n{service[1] / 'run_1'}*\n"
    assert result["stderr"] == "error"
    assert not result["truncated"] and not result["timed_out"]
    assert isinstance(result["duration_ms"], int)
    assert (await service[0].get("/healthz")).json() == {"status": "ok"}


async def test_timeout_kills_process_group(service):
    result = (await execute(service, '(sleep 2; echo alive > survived) & wait', timeout_s=1)).json()
    assert result["timed_out"] and result["exit_code"] < 0
    await asyncio.sleep(1.2)
    assert not (service[1] / "run_1/survived").exists()
    assert 900 <= result["duration_ms"] < 2000


async def test_output_cap_head_tail_and_replacement(service):
    result = (await execute(service, '''python3 - <<'PY'
import os
for fd in (1, 2):
    os.write(fd, b'HEAD' + b'x' * 400000 + b'TAIL\\xff')
PY''')).json()
    for key in ("stdout", "stderr"):
        assert result[key].startswith("HEAD") and result[key].endswith("TAIL\ufffd")
        assert "[output cut]" in result[key]
        assert len(result[key]) <= CAP
    assert result["truncated"]


@pytest.mark.parametrize("run_id", ["../escape", "", "a" * 65, "bad/name"])
async def test_invalid_run(service, run_id):
    response = await service[0].post("/exec", json={"run_id": run_id, "command": "true"})
    assert response.status_code == 422
    assert (await service[0].post("/cleanup", json={"run_id": run_id})).status_code == 422


async def test_cwd_checks_symlinks_and_cross_run(service, tmp_path):
    repos = service[2]
    (repos / "escape").symlink_to(tmp_path)
    for cwd in (str(tmp_path), str(repos / "escape"), str(repos / ".."), "relative", str(repos) + "\x00"):
        assert (await execute(service, "true", cwd=cwd)).status_code == 422
    other = service[1] / "other"
    other.mkdir()
    assert (await execute(service, "true", cwd=str(other))).status_code == 422
    assert (await execute(service, "pwd", cwd=str(repos))).json()["stdout"] == str(repos) + "\n"
    (repos / "loop").symlink_to(repos / "loop")
    assert (await execute(service, "true", cwd=str(repos / "loop"))).status_code == 422
    (repos / "inside").mkdir()
    (repos / "safe").symlink_to(repos / "inside")
    assert (await execute(service, "pwd", cwd=str(repos / "safe"))).status_code == 200
    (service[1] / "evil").symlink_to(tmp_path)
    assert (await service[0].post("/cleanup", json={"run_id": "evil"})).status_code == 422


async def test_cleanup_and_expiry(tmp_path, service):
    await execute(service, "echo keep > file")
    assert (await service[0].post("/cleanup", json={"run_id": "run_1"})).json() == {"ok": True}
    assert not (service[1] / "run_1").exists()
    assert (await service[0].post("/cleanup", json={"run_id": "run_1"})).status_code == 200
    stale = service[1] / "stale.folder"
    stale.mkdir()
    os.utime(stale, (time.time() - 7 * 3600,) * 2)
    app = create_app(work_root=service[1], repos_root=service[2])
    async with app.router.lifespan_context(app):
        assert not stale.exists()


async def test_concurrency_and_cleanup_of_active_run(service):
    running = [asyncio.create_task(execute(service, "sleep 0.4")) for _ in range(2)]
    for _ in range(100):
        if (service[1] / "run_1").exists():
            await asyncio.sleep(0.03)
            break
        await asyncio.sleep(0.01)
    assert (await execute(service, "true")).status_code == 429
    assert (await service[0].post("/cleanup", json={"run_id": "run_1"})).status_code == 409
    assert all(r.status_code == 200 for r in await asyncio.gather(*running))
    assert (await execute(service, "true")).status_code == 200


async def test_limits(service):
    result = (await execute(service, '''python3 - <<'PY'
import resource
print(resource.getrlimit(resource.RLIMIT_AS)[0])
print(resource.getrlimit(resource.RLIMIT_FSIZE)[0])
print(resource.getrlimit(resource.RLIMIT_CPU)[0])
print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])
PY''')).json()
    assert result["stdout"].splitlines() == [str(2 * 1024**3), str(256 * 1024**2), "35", "256"]


@pytest.mark.parametrize("values", [{"timeout_s": 0}, {"timeout_s": 121}, {"timeout_s": True}, {"timeout_s": "30"},
    {"command": "x" * 8001}, {"command": "true\x00"}])
async def test_invalid_limits(service, values):
    response = await service[0].post("/exec", json={"run_id": "x", "command": "true", **values})
    assert response.status_code == 422
