#!/usr/bin/env bash
# This helper is also the documented path for a missing Compose plugin.
set -euo pipefail
version=v2.39.4
[ "$(uname -s)" = Linux ] || { echo 'Install Docker Compose v2 for this system.' >&2; exit 1; }
case "$(uname -m)" in
    x86_64|aarch64) arch=$(uname -m) ;;
    *) echo 'This CPU needs a manual Docker Compose install.' >&2; exit 1 ;;
esac
command -v curl >/dev/null
command -v sha256sum >/dev/null
plugin_dir=${DOCKER_CONFIG:-$HOME/.docker}/cli-plugins
mkdir -p "$plugin_dir"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
asset=docker-compose-linux-$arch
base=https://github.com/docker/compose/releases/download/$version
curl --fail --silent --show-error --location "$base/$asset" -o "$tmp/$asset"
curl --fail --silent --show-error --location "$base/$asset.sha256" -o "$tmp/checksum"
expected=$(awk '{print $1}' "$tmp/checksum")
[[ "$expected" =~ ^[a-fA-F0-9]{64}$ ]] || { echo 'The Compose checksum is invalid.' >&2; exit 1; }
printf '%s  %s\n' "$expected" "$tmp/$asset" | sha256sum --check --status
install -m 755 "$tmp/$asset" "$plugin_dir/docker-compose"
printf 'Docker Compose is installed at %s/docker-compose.\n' "$plugin_dir"
docker compose version
