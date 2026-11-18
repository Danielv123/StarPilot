#!/usr/bin/env bash
#
# Reversibly disable the agent. Archives, journal, spool, immutable releases,
# configuration, and the persistent credential are deliberately retained.

set -euo pipefail

script_dir="$(cd -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=deployment-lib.sh
source "$script_dir/deployment-lib.sh"

remove_continue_hook
stop_and_remove_runtime_unit

echo "Comma Companion is disabled. Retained data was not deleted."
echo "Run $agent_dir/restore-device.sh to re-enable it."
