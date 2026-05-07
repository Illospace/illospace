#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${1:-python3}"
BROWSER_HARNESS_SPEC="browser-harness @ git+https://github.com/browser-use/browser-harness.git@5acfe37cf844a2f3ac97a5cf8cfa477a748c15f2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BROWSER_RUNTIME_DIR="${ILLO_BROWSER_RUNTIME_DIR:-$ROOT/.runtime/browser}"
CHROME_FOR_TESTING_DIR="${ILLO_BROWSER_CHROME_FOR_TESTING_DIR:-$BROWSER_RUNTIME_DIR/chrome-for-testing}"

export ILLO_PROJECT_ROOT="${ILLO_PROJECT_ROOT:-$ROOT}"
export ILLO_BROWSER_RUNTIME_DIR="$BROWSER_RUNTIME_DIR"
export ILLO_BROWSER_CHROME_FOR_TESTING_DIR="$CHROME_FOR_TESTING_DIR"

venv_harness_path() {
  "$PYTHON_BIN" - <<'PY'
import sys
from pathlib import Path

print(Path(sys.executable).with_name("browser-harness"))
PY
}

runtime_status() {
  "$PYTHON_BIN" - <<'PY'
import os
import platform
import shutil
import sys
from pathlib import Path

def existing(candidates):
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return ""

chrome_for_testing_dir = Path(os.environ["ILLO_BROWSER_CHROME_FOR_TESTING_DIR"])
chrome_for_testing_candidates = []
if sys.platform.startswith("linux"):
    chrome_for_testing_candidates.append(chrome_for_testing_dir / "chrome-linux64" / "chrome")
elif sys.platform == "darwin":
    mac_key = "mac-arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "mac-x64"
    chrome_for_testing_candidates.append(
        chrome_for_testing_dir / f"chrome-{mac_key}" / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing"
    )
harness = existing([
    os.environ.get("ILLO_BROWSER_HARNESS_BIN"),
    str(Path(sys.executable).with_name("browser-harness")),
    shutil.which("browser-harness"),
])
chrome = existing([
    os.environ.get("ILLO_BROWSER_CHROME_BIN"),
    *chrome_for_testing_candidates,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    shutil.which("google-chrome"),
    shutil.which("google-chrome-stable"),
    shutil.which("chromium"),
    shutil.which("chromium-browser"),
    shutil.which("microsoft-edge"),
])
print(f"HARNESS={harness}")
print(f"CHROME={chrome}")
PY
}

install_chrome_for_testing() {
  "$PYTHON_BIN" - "$CHROME_FOR_TESTING_DIR" <<'PY'
import json
import os
import platform
import shutil
import stat
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

target_dir = Path(sys.argv[1]).resolve()

def ensure_chrome_executables(executable: Path) -> None:
    for path in (
        executable,
        executable.with_name("chrome_crashpad_handler"),
        executable.with_name("chrome_sandbox"),
    ):
        if path.exists() and path.is_file():
            path.chmod(path.stat().st_mode | stat.S_IXUSR)

def chrome_platform() -> tuple[str, Path]:
    system = sys.platform
    machine = platform.machine().lower()
    if system.startswith("linux"):
        if machine not in {"x86_64", "amd64"}:
            raise RuntimeError(f"Chrome for Testing linux64 does not support this CPU: {machine}")
        return "linux64", target_dir / "chrome-linux64" / "chrome"
    if system == "darwin":
        key = "mac-arm64" if machine in {"arm64", "aarch64"} else "mac-x64"
        return (
            key,
            target_dir / f"chrome-{key}" / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing",
        )
    raise RuntimeError(f"Automatic Chrome for Testing install is unsupported on {system}")

platform_key, executable = chrome_platform()
if executable.exists():
    ensure_chrome_executables(executable)
    print(executable)
    sys.exit(0)

index_url = os.environ.get(
    "ILLO_CHROME_FOR_TESTING_INDEX_URL",
    "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json",
)
request = urllib.request.Request(index_url, headers={"User-Agent": "illo-browser-runtime-installer"})
with urllib.request.urlopen(request, timeout=30) as response:
    data = json.load(response)

stable = data["channels"]["Stable"]
downloads = stable["downloads"]["chrome"]
download_url = next((item["url"] for item in downloads if item["platform"] == platform_key), None)
if not download_url:
    raise RuntimeError(f"No Chrome for Testing download found for {platform_key}")

target_dir.parent.mkdir(parents=True, exist_ok=True)
target_dir.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="chrome-for-testing-", dir=str(target_dir.parent)) as tmp_name:
    tmp_dir = Path(tmp_name)
    zip_path = tmp_dir / "chrome.zip"
    print(f"Downloading Chrome for Testing {stable.get('version', 'Stable')} ({platform_key})...")
    urllib.request.urlretrieve(download_url, zip_path)

    extract_dir = tmp_dir / "extract"
    extract_dir.mkdir()
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir)

    extracted = extract_dir / f"chrome-{platform_key}"
    if not extracted.exists():
        matches = list(extract_dir.glob("chrome-*"))
        if len(matches) == 1:
            extracted = matches[0]
        else:
            raise RuntimeError(f"Chrome for Testing archive had unexpected layout: {[p.name for p in matches]}")

    final_dir = target_dir / f"chrome-{platform_key}"
    if final_dir.exists():
        shutil.rmtree(final_dir)
    shutil.move(str(extracted), str(final_dir))

if not executable.exists():
    raise RuntimeError(f"Chrome for Testing installed but executable is missing: {executable}")
ensure_chrome_executables(executable)
(target_dir / "VERSION").write_text(str(stable.get("version", "")), encoding="utf-8")
print(executable)
PY
}

extract_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key {print substr($0, length(key) + 2)}'
}

echo "=== Checking Browser Harness runtime ==="
status="$(runtime_status)"
harness_path="$(printf "%s\n" "$status" | extract_value HARNESS)"
chrome_path="$(printf "%s\n" "$status" | extract_value CHROME)"
expected_harness_path="$(venv_harness_path)"

if [ -z "${ILLO_BROWSER_HARNESS_BIN:-}" ] && [ ! -x "$expected_harness_path" ]; then
  echo "Installing Browser Harness into $PYTHON_BIN environment..."
  "$PYTHON_BIN" -m pip install -q "$BROWSER_HARNESS_SPEC"
  status="$(runtime_status)"
  harness_path="$(printf "%s\n" "$status" | extract_value HARNESS)"
  chrome_path="$(printf "%s\n" "$status" | extract_value CHROME)"
elif [ -z "$harness_path" ]; then
  echo "Installing Browser Harness into $PYTHON_BIN environment..."
  "$PYTHON_BIN" -m pip install -q "$BROWSER_HARNESS_SPEC"
  status="$(runtime_status)"
  harness_path="$(printf "%s\n" "$status" | extract_value HARNESS)"
  chrome_path="$(printf "%s\n" "$status" | extract_value CHROME)"
fi

if [ -z "$harness_path" ]; then
  echo "Browser Harness console script was not found after install." >&2
  echo "Expected it next to $PYTHON_BIN or on PATH as browser-harness." >&2
  exit 1
fi

if [ -z "$chrome_path" ]; then
  echo "Chrome/Chromium was not found; installing repo-local Chrome for Testing..."
  install_chrome_for_testing
  status="$(runtime_status)"
  chrome_path="$(printf "%s\n" "$status" | extract_value CHROME)"
fi

if [ -z "$chrome_path" ]; then
  cat >&2 <<EOF
Chrome/Chromium was not found after installing Chrome for Testing.

Expected repo-local browser under:
  $CHROME_FOR_TESTING_DIR

You can still use a custom browser with:
  export ILLO_BROWSER_CHROME_BIN=/absolute/path/to/chrome
EOF
  exit 1
fi

echo "Browser Harness ready: $harness_path"
echo "Chrome/Chromium ready: $chrome_path"
