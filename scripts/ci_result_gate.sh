#!/usr/bin/env bash

set -euo pipefail

needs_json=${NEEDS_JSON-}

if [ -z "$needs_json" ]; then
  if [ "${NEEDS_JSON+x}" = "x" ]; then
    input_state="<empty>"
  else
    input_state="<unset>"
  fi

  printf '%s\n' \
    "CI result failed: NEEDS_JSON=$input_state; rule: NEEDS_JSON must contain a non-empty valid JSON object."
  exit 1
fi

if ! jq -e -s 'length == 1 and (.[0] | type == "object")' \
  >/dev/null 2>&1 <<<"$needs_json"; then
  printf '%s\n' \
    "CI result failed: NEEDS_JSON=<invalid>; rule: NEEDS_JSON must contain a non-empty valid JSON object."
  exit 1
fi

jq -r '
  def job_result:
    if type == "object" then
      if has("result") then .result else "" end
    else
      ""
    end;

  to_entries
  | sort_by(.key)[]
  | (.value | job_result) as $result
  | .key + "=" + ($result | tostring)
' <<<"$needs_json"

if ! jq -e 'has("changes")' >/dev/null <<<"$needs_json"; then
  printf '%s\n' \
    "CI result failed: changes=<missing>; rule: the needs context must include the changes job."
  exit 1
fi

changes_result=$(jq -r '
  def job_result:
    if type == "object" then
      if has("result") then .result else "" end
    else
      ""
    end;

  .changes
  | job_result
  | tostring
' <<<"$needs_json")

if [ "$changes_result" != "success" ]; then
  printf '%s\n' \
    "CI result failed: changes=$changes_result; rule: the changes result must be exactly success."
  exit 1
fi

invalid_entry=$(jq -r '
  def job_result:
    if type == "object" then
      if has("result") then .result else "" end
    else
      ""
    end;

  ([
    to_entries
    | sort_by(.key)[]
    | select(.key != "changes")
    | (.value | job_result) as $result
    | select($result != "success" and $result != "skipped")
    | {job: .key, result: $result}
  ][0] // empty)
  | [.job, (.result | tostring)]
  | @tsv
' <<<"$needs_json")

if [ -n "$invalid_entry" ]; then
  IFS=$'\t' read -r invalid_job invalid_result <<<"$invalid_entry"
  printf '%s\n' \
    "CI result failed: $invalid_job=$invalid_result; rule: every non-changes job result must be exactly success or skipped."
  exit 1
fi

echo "Selected CI jobs passed."
