#!/usr/bin/env bash
#
# Select the last release recorded by update-device.sh. Re-running this script
# after a successful rollback is a no-op apart from a health restart.

set -euo pipefail

script_dir="$(cd -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=deployment-lib.sh
source "$script_dir/deployment-lib.sh"

current_release="$(read_release_link "$current_link")" || {
  echo "There is no valid current release to roll back." >&2
  exit 1
}
rollback_release="$(read_release_link "$previous_link")" || {
  echo "There is no valid previous release to select." >&2
  exit 1
}

if [[ "$current_release" == "$rollback_release" ]]; then
  if ! current_runtime_is_healthy; then
    activate_current_release
  fi
  sync_current_metadata || {
    echo "Warning: could not synchronize retained config and unit copies." >&2
  }
  echo "Rollback target is already active: $current_release"
  exit 0
fi

atomic_symlink "$rollback_release" "$current_link"
if activate_current_release; then
  sync_current_metadata || {
    echo "Warning: could not synchronize retained config and unit copies." >&2
  }
  echo "Rolled back to: $rollback_release"
  exit 0
fi

echo "Rollback target was unhealthy; restoring $current_release." >&2
atomic_symlink "$current_release" "$current_link"
if ! activate_current_release; then
  echo "The original release was reselected but did not become healthy." >&2
else
  sync_current_metadata || {
    echo "Warning: could not synchronize retained config and unit copies." >&2
  }
fi
exit 1
