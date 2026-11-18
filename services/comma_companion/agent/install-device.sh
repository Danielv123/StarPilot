#!/usr/bin/env bash
#
# Initial installation wrapper. update-device.sh owns the transactional
# release switch and health rollback; this script additionally preserves and
# installs the fail-open /data/continue.sh hook.

set -euo pipefail

script_dir="$(cd -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=deployment-lib.sh
source "$script_dir/deployment-lib.sh"

[[ -f "$continue_file" && ! -L "$continue_file" ]] || {
  echo "$continue_file must be a regular, non-symlink file." >&2
  exit 1
}

if [[ -e "$current_link" || -L "$current_link" ]]; then
  if [[ "$script_dir" != "$agent_dir" ]]; then
    echo "An installation already exists; run this bundle's update-device.sh instead." >&2
    exit 2
  fi
  /usr/bin/bash "$agent_dir/restore-device.sh"
  echo "The existing installation was already current and has been restored."
  exit 0
fi

hook_preexisting=false
if continue_hook_is_valid; then
  hook_preexisting=true
fi
install_continue_hook
if ! /usr/bin/bash "$script_dir/update-device.sh"; then
  if [[ "$hook_preexisting" != true ]]; then
    remove_continue_hook || true
  fi
  echo "Initial installation failed; a newly added boot hook was removed." >&2
  exit 1
fi

echo "Initial installation completed."
