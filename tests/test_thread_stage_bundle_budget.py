import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = REPO_ROOT / "frontend"
BUDGET_SCRIPT = FRONTEND_ROOT / "scripts/check-thread-stage-bundle.mjs"


def test_bundle_budget_command_is_fixed_at_250kb():
    package = json.loads((FRONTEND_ROOT / "package.json").read_text())
    source = BUDGET_SCRIPT.read_text()

    assert package["scripts"]["check:thread-stage-bundle"] == (
        "node scripts/check-thread-stage-bundle.mjs"
    )
    assert "THREAD_STAGE_GZIP_BUDGET_BYTES = 250_000" in source
    assert "candidate?.name === 'ThreadStageScreen'" in source
    assert "candidate?.isDynamicEntry" in source
    assert "process.exitCode = 1" in source


def test_measurement_walks_static_imports_and_excludes_dynamic_imports(tmp_path: Path):
    client_root = tmp_path / "client"
    (client_root / ".vite").mkdir(parents=True)
    manifest = {
        "stage": {
            "name": "ThreadStageScreen",
            "isDynamicEntry": True,
            "file": "stage.js",
            "imports": ["shared"],
            "dynamicImports": ["rare"],
            "css": ["stage.css"],
        },
        "shared": {"file": "shared.js", "css": ["shared.css"]},
        "rare": {"file": "rare.js", "isDynamicEntry": True},
    }
    (client_root / ".vite/manifest.json").write_text(json.dumps(manifest))
    for filename in ["stage.js", "stage.css", "shared.js", "shared.css", "rare.js"]:
        (client_root / filename).write_text(filename * 8)

    program = f"""
import {{ measureThreadStageStaticClosure }} from {json.dumps(BUDGET_SCRIPT.as_uri())};
console.log(JSON.stringify(measureThreadStageStaticClosure({json.dumps(str(client_root))})));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", program],
        check=True,
        capture_output=True,
        text=True,
    )
    measurement = json.loads(result.stdout)

    assert measurement["fileCount"] == 4
    assert measurement["rawBytes"] == sum(
        (client_root / filename).stat().st_size
        for filename in ["stage.js", "stage.css", "shared.js", "shared.css"]
    )
