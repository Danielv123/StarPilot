#!/usr/bin/env bash
#
# Explicit, read-only capability discovery. Run this on the comma while it is
# confirmed offroad, before activating a production config.

set -euo pipefail

script_dir="$(cd -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
agent_dir=/data/private-pond-agent
sample_count=4

if (($# > 2)); then
  echo "Usage: $0 [config.json] [sample-count]" >&2
  exit 2
fi

config_path="${1:-$script_dir/config.json}"
if (($# == 2)); then
  sample_count=$2
fi

if [[ "$script_dir" == "$agent_dir" ]]; then
  binary="$agent_dir/current/comma-companion-agent"
else
  binary="$script_dir/dist/comma-companion-agent-linux-arm64"
fi

[[ -x "$binary" ]] || {
  echo "Missing executable agent binary: $binary" >&2
  exit 1
}
[[ -f "$config_path" && ! -L "$config_path" ]] || {
  echo "Config must be a regular, non-symlink file: $config_path" >&2
  exit 1
}
[[ "$sample_count" =~ ^[0-9]+$ ]] || {
  echo "sample-count must be an integer from 2 through 128." >&2
  exit 2
}

exec "$binary" \
  -config "$config_path" \
  -suggest-streams \
  -suggest-segments "$sample_count"
