#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CATALOG="${ILLO_WORKSPACE_TOOL_CATALOG:-$ROOT/deploy/compose/workspace-tools.json}"
TOOLS_ROOT="${ILLO_WORKSPACE_TOOLS_ROOT:-/data/private/workspace-tools}"
PRIVATE_VOLUME="${ILLO_PRIVATE_VOLUME:-${COMPOSE_PROJECT_NAME:-illospace}_illo_private}"
BUILDER_IMAGE="${ILLO_WORKSPACE_TOOL_BUILDER_IMAGE:-python:3.13-slim-bookworm}"
APP_UID="${ILLO_APP_UID:-10001}"
APP_GID="${ILLO_APP_GID:-10001}"

usage() {
  cat <<'USAGE'
Usage: deploy/scripts/workspace-tools.sh list
       deploy/scripts/workspace-tools.sh install <bundle-id> <org-id> [version]
       deploy/scripts/workspace-tools.sh check <bundle-id> <org-id> [version]
USAGE
}

safe_part() {
  printf '%s' "$1" | tr -cs '[:alnum:]_.-' '-' | sed -e 's/^[.-]*//' -e 's/[.-]*$//' | cut -c 1-160
}

bundle_json() {
  local bundle_id="$1"
  jq -e --arg id "$bundle_id" '.bundles[] | select(.id == $id)' "$CATALOG"
}

bundle_field() {
  local bundle_id="$1"
  local expression="$2"
  bundle_json "$bundle_id" | jq -r "$expression"
}

require_bundle() {
  local bundle_id="$1"
  if ! bundle_json "$bundle_id" >/dev/null; then
    echo "Unknown workspace tool bundle: $bundle_id" >&2
    echo "Allowed bundles:" >&2
    jq -r '.bundles[].id' "$CATALOG" >&2
    exit 2
  fi
}

tool_paths_json() {
  local bundle_id="$1"
  local org_id="$2"
  local version="$3"
  local org_part bundle_part version_part bundle_root install_root current_root bin_path manifest_path
  org_part="$(safe_part "$org_id")"
  bundle_part="$(safe_part "$bundle_id")"
  version_part="$(safe_part "${version:-default}")"
  bundle_root="$TOOLS_ROOT/orgs/$org_part/$bundle_part"
  install_root="$bundle_root/versions/$version_part"
  current_root="$bundle_root/current"
  bin_path="$current_root/bin"
  manifest_path="$current_root/illo-tool.json"
  jq -n \
    --arg bundle_root "$bundle_root" \
    --arg install_root "$install_root" \
    --arg current_root "$current_root" \
    --arg bin_path "$bin_path" \
    --arg manifest_path "$manifest_path" \
    '{
      bundle_root: $bundle_root,
      install_root: $install_root,
      current_root: $current_root,
      bin_path: $bin_path,
      manifest_path: $manifest_path
    }'
}

write_manifest() {
  local manifest_path="$1"
  local bundle_id="$2"
  local name="$3"
  local version="$4"
  local install_root="$5"
  local current_root="$6"
  local bin_path="$7"
  local status="$8"
  local detail="$9"
  local health_json="${10:-}"
  local metadata_json="${11:-}"
  [ -n "$health_json" ] || health_json="{}"
  [ -n "$metadata_json" ] || metadata_json="{}"
  mkdir -p "$(dirname "$manifest_path")"
  jq -n \
    --arg bundle_id "$bundle_id" \
    --arg name "$name" \
    --arg version "$version" \
    --arg install_root "$current_root" \
    --arg concrete_install_root "$install_root" \
    --arg bin_path "$bin_path" \
    --arg status "$status" \
    --arg detail "$detail" \
    --arg checked_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    --arg installed_at "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" \
    --argjson health "$health_json" \
    --argjson metadata "$metadata_json" \
    '{
      bundle_id: $bundle_id,
      name: $name,
      version: $version,
      status: $status,
      detail: $detail,
      install_root: $install_root,
      concrete_install_root: $concrete_install_root,
      bin_path: $bin_path,
      path_entries: [$bin_path],
      health: $health,
      metadata: $metadata,
      checked_at: $checked_at,
      installed_at: (if $status == "installed" then $installed_at else null end)
    }' > "${manifest_path}.tmp"
  mv "${manifest_path}.tmp" "$manifest_path"
  chown "$APP_UID:$APP_GID" "$manifest_path" 2>/dev/null || true
}

docker_builder() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required for workspace tool installation" >&2
    exit 2
  fi
  docker run --rm \
    -e APP_UID="$APP_UID" \
    -e APP_GID="$APP_GID" \
    -e INSTALL_ROOT="$INSTALL_ROOT" \
    -e CURRENT_ROOT="$CURRENT_ROOT" \
    -e BIN_PATH="$BIN_PATH" \
    -e PLANTUML_VERSION="$PLANTUML_VERSION" \
    -e AWS_ICONS_VERSION="$AWS_ICONS_VERSION" \
    -v "$PRIVATE_VOLUME:/data/private" \
    "$BUILDER_IMAGE" \
    bash -lc "$1"
}

install_aws_diagrams() {
  local bundle_id="$1"
  local org_id="$2"
  local version="$3"
  local paths name metadata_json health_json

  paths="$(tool_paths_json "$bundle_id" "$org_id" "$version")"
  INSTALL_ROOT="$(jq -r '.install_root' <<<"$paths")"
  CURRENT_ROOT="$(jq -r '.current_root' <<<"$paths")"
  BIN_PATH="$(jq -r '.bin_path' <<<"$paths")"
  local manifest_path
  manifest_path="$(jq -r '.manifest_path' <<<"$paths")"
  name="$(bundle_field "$bundle_id" '.name')"
  PLANTUML_VERSION="$(bundle_field "$bundle_id" '.metadata.plantuml_version')"
  AWS_ICONS_VERSION="$(bundle_field "$bundle_id" '.metadata.aws_icons_version')"
  metadata_json="$(bundle_json "$bundle_id" | jq -c '.metadata // {}')"

  export INSTALL_ROOT CURRENT_ROOT BIN_PATH PLANTUML_VERSION AWS_ICONS_VERSION

  docker_builder '
    set -euo pipefail
    export MAMBA_ROOT_PREFIX="$INSTALL_ROOT/mamba-root"
    apt-get update
    apt-get install -y --no-install-recommends bzip2 ca-certificates curl unzip
    rm -rf /var/lib/apt/lists/*

    install_bin_path="$INSTALL_ROOT/bin"
    mkdir -p "$INSTALL_ROOT" "$install_bin_path"
    if [ ! -x "$INSTALL_ROOT/bin/micromamba" ]; then
      curl -fsSL https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xvj -C "$INSTALL_ROOT" bin/micromamba
    fi

    if [ ! -x "$INSTALL_ROOT/env/bin/java" ] || [ ! -x "$INSTALL_ROOT/env/bin/dot" ]; then
      "$INSTALL_ROOT/bin/micromamba" create -y -p "$INSTALL_ROOT/env" -c conda-forge openjdk=17 graphviz
    fi

    curl -fsSL -o "$INSTALL_ROOT/plantuml.jar" \
      "https://github.com/plantuml/plantuml/releases/download/v${PLANTUML_VERSION}/plantuml-${PLANTUML_VERSION}.jar"

    rm -rf "$INSTALL_ROOT/aws-icons-src" "$INSTALL_ROOT/aws-icons.zip" "$INSTALL_ROOT/aws-icons"
    mkdir -p "$INSTALL_ROOT/aws-icons-src" "$INSTALL_ROOT/aws-icons"
    curl -fsSL -o "$INSTALL_ROOT/aws-icons.zip" \
      "https://github.com/awslabs/aws-icons-for-plantuml/archive/refs/tags/${AWS_ICONS_VERSION}.zip"
    unzip -q "$INSTALL_ROOT/aws-icons.zip" -d "$INSTALL_ROOT/aws-icons-src"
    dist_dir="$(find "$INSTALL_ROOT/aws-icons-src" -path "*/dist/AWSCommon.puml" -print -quit)"
    if [ -z "$dist_dir" ]; then
      echo "AWS icon dist directory not found in ${AWS_ICONS_VERSION}" >&2
      exit 1
    fi
    cp -a "$(dirname "$dist_dir")" "$INSTALL_ROOT/aws-icons/dist"

    cat > "$install_bin_path/plantuml" <<'"'"'WRAPPER'"'"'
#!/usr/bin/env bash
set -euo pipefail
TOOL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GRAPHVIZ_DOT="$TOOL_ROOT/env/bin/dot"
exec "$TOOL_ROOT/env/bin/java" \
  -DGRAPHVIZ_DOT="$TOOL_ROOT/env/bin/dot" \
  -Dplantuml.include.path="$TOOL_ROOT/aws-icons/dist" \
  -jar "$TOOL_ROOT/plantuml.jar" "$@"
WRAPPER
    cat > "$install_bin_path/java" <<'"'"'WRAPPER'"'"'
#!/usr/bin/env bash
set -euo pipefail
TOOL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$TOOL_ROOT/env/bin/java" "$@"
WRAPPER
    cat > "$install_bin_path/dot" <<'"'"'WRAPPER'"'"'
#!/usr/bin/env bash
set -euo pipefail
TOOL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$TOOL_ROOT/env/bin/dot" "$@"
WRAPPER
    chmod +x "$install_bin_path/plantuml" "$install_bin_path/java" "$install_bin_path/dot"

    rm -rf "$CURRENT_ROOT"
    ln -s "$INSTALL_ROOT" "$CURRENT_ROOT"
    chown -R "$APP_UID:$APP_GID" "$INSTALL_ROOT" "$(dirname "$CURRENT_ROOT")"
  '

  local plantuml_version dot_version java_version
  plantuml_version="$(docker run --rm -v "$PRIVATE_VOLUME:/data/private" "$BUILDER_IMAGE" bash -lc "\"$BIN_PATH/plantuml\" -version | head -n 1" 2>&1 || true)"
  dot_version="$(docker run --rm -v "$PRIVATE_VOLUME:/data/private" "$BUILDER_IMAGE" bash -lc "\"$BIN_PATH/dot\" -V" 2>&1 || true)"
  java_version="$(docker run --rm -v "$PRIVATE_VOLUME:/data/private" "$BUILDER_IMAGE" bash -lc "\"$BIN_PATH/java\" -version" 2>&1 | head -n 1 || true)"
  health_json="$(jq -n \
    --arg plantuml "$plantuml_version" \
    --arg dot "$dot_version" \
    --arg java "$java_version" \
    '{plantuml: $plantuml, graphviz: $dot, java: $java}')"

  write_manifest "$manifest_path" "$bundle_id" "$name" "$version" "$INSTALL_ROOT" "$CURRENT_ROOT" "$BIN_PATH" \
    "installed" "Workspace tool bundle installed." "$health_json" "$metadata_json"
  echo "Installed $bundle_id for org $org_id at $CURRENT_ROOT"
}

install_npm_package_cli() {
  local bundle_id="$1"
  local org_id="$2"
  local version="$3"
  local paths name metadata_json health_json package_name package_version tool_bin builder_image

  paths="$(tool_paths_json "$bundle_id" "$org_id" "$version")"
  INSTALL_ROOT="$(jq -r '.install_root' <<<"$paths")"
  CURRENT_ROOT="$(jq -r '.current_root' <<<"$paths")"
  BIN_PATH="$(jq -r '.bin_path' <<<"$paths")"
  local manifest_path
  manifest_path="$(jq -r '.manifest_path' <<<"$paths")"
  name="$(bundle_field "$bundle_id" '.name')"
  package_name="$(bundle_field "$bundle_id" '.metadata.npm_package')"
  package_version="$(bundle_field "$bundle_id" '.metadata.package_version // "latest"')"
  tool_bin="$(bundle_field "$bundle_id" '.metadata.bin // .provided_commands[0]')"
  metadata_json="$(bundle_json "$bundle_id" | jq -c '.metadata // {}')"
  builder_image="${ILLO_NPM_WORKSPACE_TOOL_BUILDER_IMAGE:-node:22-bookworm-slim}"

  docker run --rm \
    -e APP_UID="$APP_UID" \
    -e APP_GID="$APP_GID" \
    -e INSTALL_ROOT="$INSTALL_ROOT" \
    -e CURRENT_ROOT="$CURRENT_ROOT" \
    -e BIN_PATH="$BIN_PATH" \
    -e NPM_PACKAGE="$package_name" \
    -e NPM_PACKAGE_VERSION="$package_version" \
    -e TOOL_BIN="$tool_bin" \
    -v "$PRIVATE_VOLUME:/data/private" \
    "$builder_image" \
    bash -lc '
      set -euo pipefail
      install_bin_path="$INSTALL_ROOT/bin"
      npm_prefix="$INSTALL_ROOT/npm-prefix"
      npm_cache="$INSTALL_ROOT/npm-cache"
      mkdir -p "$INSTALL_ROOT" "$install_bin_path" "$npm_prefix" "$npm_cache"

      package_spec="$NPM_PACKAGE"
      if [ -n "${NPM_PACKAGE_VERSION:-}" ] && [ "$NPM_PACKAGE_VERSION" != "latest" ]; then
        package_spec="${NPM_PACKAGE}@${NPM_PACKAGE_VERSION}"
      fi

      npm install --global --prefix "$npm_prefix" --cache "$npm_cache" "$package_spec"
      if [ ! -x "$npm_prefix/bin/$TOOL_BIN" ]; then
        echo "Workspace tool target missing after npm install: $npm_prefix/bin/$TOOL_BIN" >&2
        ls -la "$npm_prefix/bin" >&2 || true
        exit 1
      fi

      cat > "$install_bin_path/$TOOL_BIN" <<'"'"'WRAPPER'"'"'
#!/usr/bin/env bash
set -euo pipefail
TOOL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOL_BIN_NAME="$(basename "$0")"
TARGET="$TOOL_ROOT/npm-prefix/bin/$TOOL_BIN_NAME"
if [ ! -x "$TARGET" ]; then
  echo "Workspace tool target missing: $TARGET" >&2
  exit 127
fi
exec "$TARGET" "$@"
WRAPPER
      chmod +x "$install_bin_path/$TOOL_BIN"

      rm -rf "$CURRENT_ROOT"
      ln -s "$INSTALL_ROOT" "$CURRENT_ROOT"
      chown -R "$APP_UID:$APP_GID" "$INSTALL_ROOT" "$(dirname "$CURRENT_ROOT")"
    '

  local tool_output tool_status tool_version node_version npm_version
  set +e
  tool_output="$(docker run --rm -v "$PRIVATE_VOLUME:/data/private" "$builder_image" bash -lc "\"$BIN_PATH/$tool_bin\" --version" 2>&1)"
  tool_status=$?
  set -e
  tool_version="${tool_output%%$'\n'*}"
  node_version="$(docker run --rm "$builder_image" node --version 2>&1 | head -n 1 || true)"
  npm_version="$(docker run --rm "$builder_image" npm --version 2>&1 | head -n 1 || true)"
  health_json="$(jq -n \
    --arg tool_bin "$tool_bin" \
    --arg tool "$tool_version" \
    --arg tool_status "$tool_status" \
    --arg node "$node_version" \
    --arg npm "$npm_version" \
    '{($tool_bin): $tool, cli_exit_code: ($tool_status | tonumber), node: $node, npm: $npm}')"

  if [ "$tool_status" -ne 0 ] || [ -z "$tool_version" ]; then
    write_manifest "$manifest_path" "$bundle_id" "$name" "$version" "$INSTALL_ROOT" "$CURRENT_ROOT" "$BIN_PATH" \
      "failed" "Workspace tool bundle health check failed." "$health_json" "$metadata_json"
    echo "Workspace tool bundle health check failed for $bundle_id: ${tool_version:-no output}" >&2
    exit 1
  fi

  write_manifest "$manifest_path" "$bundle_id" "$name" "$version" "$INSTALL_ROOT" "$CURRENT_ROOT" "$BIN_PATH" \
    "installed" "Workspace tool bundle installed." "$health_json" "$metadata_json"
  echo "Installed $bundle_id for org $org_id at $CURRENT_ROOT"
}

check_aws_diagrams() {
  local bundle_id="$1"
  local org_id="$2"
  local version="$3"
  local paths manifest_path bin_path name metadata_json health_json status detail
  paths="$(tool_paths_json "$bundle_id" "$org_id" "$version")"
  manifest_path="$(jq -r '.manifest_path' <<<"$paths")"
  bin_path="$(jq -r '.bin_path' <<<"$paths")"
  name="$(bundle_field "$bundle_id" '.name')"
  metadata_json="$(bundle_json "$bundle_id" | jq -c '.metadata // {}')"
  if [ ! -x "$bin_path/plantuml" ]; then
    health_json="$(jq -n '{plantuml: "missing"}')"
    write_manifest "$manifest_path" "$bundle_id" "$name" "$version" "$(jq -r '.install_root' <<<"$paths")" "$(jq -r '.current_root' <<<"$paths")" "$bin_path" \
      "failed" "Workspace tool bundle is not installed or plantuml is missing." "$health_json" "$metadata_json"
    exit 1
  fi
  local plantuml_version dot_version java_version
  plantuml_version="$(docker run --rm -v "$PRIVATE_VOLUME:/data/private" "$BUILDER_IMAGE" bash -lc "\"$bin_path/plantuml\" -version | head -n 1" 2>&1 || true)"
  dot_version="$(docker run --rm -v "$PRIVATE_VOLUME:/data/private" "$BUILDER_IMAGE" bash -lc "\"$bin_path/dot\" -V" 2>&1 || true)"
  java_version="$(docker run --rm -v "$PRIVATE_VOLUME:/data/private" "$BUILDER_IMAGE" bash -lc "\"$bin_path/java\" -version" 2>&1 | head -n 1 || true)"
  health_json="$(jq -n --arg plantuml "$plantuml_version" --arg dot "$dot_version" --arg java "$java_version" '{plantuml: $plantuml, graphviz: $dot, java: $java}')"
  status="installed"
  detail="Workspace tool bundle health check passed."
  if [ -z "$plantuml_version" ] || [ -z "$dot_version" ] || [ -z "$java_version" ]; then
    status="failed"
    detail="Workspace tool bundle health check failed."
  fi
  write_manifest "$manifest_path" "$bundle_id" "$name" "$version" "$(jq -r '.install_root' <<<"$paths")" "$(jq -r '.current_root' <<<"$paths")" "$bin_path" \
    "$status" "$detail" "$health_json" "$metadata_json"
  [ "$status" = "installed" ]
}

check_npm_package_cli() {
  local bundle_id="$1"
  local org_id="$2"
  local version="$3"
  local paths manifest_path bin_path name metadata_json health_json status detail tool_bin builder_image
  paths="$(tool_paths_json "$bundle_id" "$org_id" "$version")"
  manifest_path="$(jq -r '.manifest_path' <<<"$paths")"
  bin_path="$(jq -r '.bin_path' <<<"$paths")"
  name="$(bundle_field "$bundle_id" '.name')"
  metadata_json="$(bundle_json "$bundle_id" | jq -c '.metadata // {}')"
  tool_bin="$(bundle_field "$bundle_id" '.metadata.bin // .provided_commands[0]')"
  builder_image="${ILLO_NPM_WORKSPACE_TOOL_BUILDER_IMAGE:-node:22-bookworm-slim}"
  if [ ! -x "$bin_path/$tool_bin" ]; then
    health_json="$(jq -n --arg tool_bin "$tool_bin" '{($tool_bin): "missing"}')"
    write_manifest "$manifest_path" "$bundle_id" "$name" "$version" "$(jq -r '.install_root' <<<"$paths")" "$(jq -r '.current_root' <<<"$paths")" "$bin_path" \
      "failed" "Workspace tool bundle is not installed or ${tool_bin} is missing." "$health_json" "$metadata_json"
    exit 1
  fi
  local tool_output tool_status tool_version node_version npm_version
  set +e
  tool_output="$(docker run --rm -v "$PRIVATE_VOLUME:/data/private" "$builder_image" bash -lc "\"$bin_path/$tool_bin\" --version" 2>&1)"
  tool_status=$?
  set -e
  tool_version="${tool_output%%$'\n'*}"
  node_version="$(docker run --rm "$builder_image" node --version 2>&1 | head -n 1 || true)"
  npm_version="$(docker run --rm "$builder_image" npm --version 2>&1 | head -n 1 || true)"
  health_json="$(jq -n --arg tool_bin "$tool_bin" --arg tool "$tool_version" --arg tool_status "$tool_status" --arg node "$node_version" --arg npm "$npm_version" '{($tool_bin): $tool, cli_exit_code: ($tool_status | tonumber), node: $node, npm: $npm}')"
  status="installed"
  detail="Workspace tool bundle health check passed."
  if [ "$tool_status" -ne 0 ] || [ -z "$tool_version" ]; then
    status="failed"
    detail="Workspace tool bundle health check failed."
  fi
  write_manifest "$manifest_path" "$bundle_id" "$name" "$version" "$(jq -r '.install_root' <<<"$paths")" "$(jq -r '.current_root' <<<"$paths")" "$bin_path" \
    "$status" "$detail" "$health_json" "$metadata_json"
  [ "$status" = "installed" ]
}

check_tool() {
  local bundle_id="$1"
  local org_id="$2"
  local version="$3"
  case "$(bundle_field "$bundle_id" '.install_profile')" in
    aws_diagrams_micromamba)
      check_aws_diagrams "$bundle_id" "$org_id" "$version"
      ;;
    npm_package_cli)
      check_npm_package_cli "$bundle_id" "$org_id" "$version"
      ;;
    *)
      echo "Bundle $bundle_id has no supported install_profile" >&2
      exit 2
      ;;
  esac
}

main() {
  local action="${1:-}"
  case "$action" in
    list)
      jq '.bundles' "$CATALOG"
      ;;
    install)
      local bundle_id="${2:-}"
      local org_id="${3:-}"
      local version="${4:-}"
      [ -n "$bundle_id" ] && [ -n "$org_id" ] || { usage >&2; exit 2; }
      require_bundle "$bundle_id"
      version="${version:-$(bundle_field "$bundle_id" '.version // "default"')}"
      case "$(bundle_field "$bundle_id" '.install_profile')" in
        aws_diagrams_micromamba)
          install_aws_diagrams "$bundle_id" "$org_id" "$version"
          ;;
        npm_package_cli)
          install_npm_package_cli "$bundle_id" "$org_id" "$version"
          ;;
        *)
          echo "Bundle $bundle_id has no supported install_profile" >&2
          exit 2
          ;;
      esac
      ;;
    check)
      local bundle_id="${2:-}"
      local org_id="${3:-}"
      local version="${4:-}"
      [ -n "$bundle_id" ] && [ -n "$org_id" ] || { usage >&2; exit 2; }
      require_bundle "$bundle_id"
      version="${version:-$(bundle_field "$bundle_id" '.version // "default"')}"
      check_tool "$bundle_id" "$org_id" "$version"
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
