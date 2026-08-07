#!/usr/bin/env bash
# validate-cortex.sh — Pre-push validation for cortex backend (FastAPI)
#
# Catches:
# 1. Python syntax errors in cortex modules
# 2. No remaining dashboard imports in brain/ or api/
# 3. Unit tests (no DB required)
#
# Usage: ./ops/validate-cortex.sh
# Exit code 0 = all good, 1 = failures found

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ERRORS=0

echo "--- Cortex Validation ---"
echo ""

# -- 1. Python syntax --
echo -n "Python syntax check... "
PY_TARGETS=(
    brain/app/api/routers/cortex
    brain/app/api/routers/cortex_intel.py
    brain/app/api/main.py
    brain/systems/runs/cortex
    brain/platform/events.py
    brain/systems/cortex/encode.py
    brain/systems/cortex/intelligence.py
    brain/systems/cortex/worker.py
    brain/systems/cortex/reply.py
    brain/systems/runs
)
PY_OK=true
if ! python3 -m compileall -q "${PY_TARGETS[@]}" 2>/tmp/cortex-compileall.err; then
    echo -e "${RED}SYNTAX ERROR${NC}"
    head -20 /tmp/cortex-compileall.err
    PY_OK=false
    ERRORS=$((ERRORS + 1))
fi
if $PY_OK; then echo -e "${GREEN}OK${NC} (${#PY_TARGETS[@]} targets)"; fi

# -- 2. No dashboard imports in production code --
echo -n "No dashboard imports in brain/... "
DASHBOARD_REFS=$(grep -rn "from dashboard\.\|import dashboard\." --include="*.py" brain/ 2>/dev/null | grep -v __pycache__ || true)
if [ -n "$DASHBOARD_REFS" ]; then
    echo -e "${RED}FOUND${NC}"
    echo "$DASHBOARD_REFS"
    ERRORS=$((ERRORS + 1))
else
    echo -e "${GREEN}OK${NC}"
fi

# -- 3. Dashboard directory should not exist --
echo -n "Dashboard directory deleted... "
if [ -d "dashboard" ]; then
    echo -e "${RED}STILL EXISTS${NC}"
    ERRORS=$((ERRORS + 1))
else
    echo -e "${GREEN}OK${NC}"
fi

# -- 4. Unit tests (no DB required) --
echo -n "AgentRun/Cortex unit tests... "
set +e
TEST_OUTPUT=$(python3 -m pytest \
    tests/test_agent_run_runtime.py \
    tests/test_agent_runtime_modules.py \
    tests/test_api_cortex.py::test_legacy_agent_status_endpoint_is_retired \
    -x -q --no-header 2>&1)
TEST_EXIT=$?
set -e
if [ $TEST_EXIT -ne 0 ]; then
    echo -e "${RED}FAILED${NC}"
    echo "$TEST_OUTPUT"
    ERRORS=$((ERRORS + 1))
else
    PASSED=$(echo "$TEST_OUTPUT" | grep -oE '[0-9]+ passed' || echo "? passed")
    echo -e "${GREEN}OK${NC} ($PASSED)"
fi

# -- Summary --
echo ""
if [ $ERRORS -gt 0 ]; then
    echo -e "${RED}--- $ERRORS issue(s) found ---${NC}"
    exit 1
else
    echo -e "${GREEN}--- All checks passed ---${NC}"
    exit 0
fi
