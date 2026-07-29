#!/usr/bin/env bash
#
# Re-enable a retained installation after uninstall, or restore its boot hook
# after a full StarPilot UI reinstall rewrote /data/continue.sh.

set -euo pipefail

script_dir="$(cd -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=deployment-lib.sh
source "$script_dir/deployment-lib.sh"

read_release_link "$current_link" >/dev/null || {
  echo "There is no valid installed release to restore." >&2
  exit 1
}
bounded_root /usr/bin/test -s "$secret_file" || {
  echo "Missing persistent credential: $secret_file" >&2
  exit 1
}

install_continue_hook
activate_current_release
sync_current_metadata || {
  echo "Warning: could not synchronize retained config and unit copies." >&2
}
echo "The companion service and fail-open boot hook are active."
