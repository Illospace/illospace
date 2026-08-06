#!/usr/bin/env bash

set -euo pipefail

changes_result=${1-}
backend_fast_result=${2-}
frontend_result=${3-}
db_contract_result=${4-}

printf 'changes=%s\n' "$changes_result"
printf 'backend-fast=%s\n' "$backend_fast_result"
printf 'frontend=%s\n' "$frontend_result"
printf 'db-contract=%s\n' "$db_contract_result"

if [ "$changes_result" != "success" ]; then
  printf '%s\n' \
    "CI result failed: changes=$changes_result; rule: changes must be exactly success because job selection must complete."
  exit 1
fi

check_suite_result() {
  local job_name=$1
  local result=$2

  if [ "$result" != "success" ] && [ "$result" != "skipped" ]; then
    printf '%s\n' \
      "CI result failed: $job_name=$result; rule: suite result must be exactly success or skipped."
    return 1
  fi
}

check_suite_result "backend-fast" "$backend_fast_result"
check_suite_result "frontend" "$frontend_result"
check_suite_result "db-contract" "$db_contract_result"

echo "Selected CI jobs passed."
