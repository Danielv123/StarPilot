#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
unit="$script_dir/comma-companion-agent.service"
config="$script_dir/config.example.json"
all_camera_inventory="$script_dir/inventory.openpilot-all-cameras.json"

fail() {
  echo "deployment static test failed: $*" >&2
  exit 1
}

require_line() {
  local expected=$1
  local file=$2
  /usr/bin/grep -Fqx "$expected" "$file" ||
    fail "missing '$expected' in $file"
}

for script in "$script_dir"/*.sh; do
  /usr/bin/bash -n "$script"
done

require_line '$requiredGoVersion = [version]"1.26.5"' "$script_dir/build.ps1"
require_line '$govulncheckVersion = "v1.6.0"' "$script_dir/build.ps1"
require_line 'go 1.26.5' "$script_dir/go.mod"

if /usr/bin/grep -Fq "ConditionPathIsExecutable=" "$unit"; then
  fail "invalid ConditionPathIsExecutable directive remains"
fi
require_line \
  "ConditionFileIsExecutable=/data/private-pond-agent/current/comma-companion-agent" \
  "$unit"
require_line "NoNewPrivileges=true" "$unit"
require_line \
  "LoadCredential=device-token:/data/private-pond-agent-secrets/device-token" \
  "$unit"
require_line "Environment=COMMA_COMPANION_TOKEN_FILE=%d/device-token" "$unit"
require_line \
  "Environment=COMMA_COMPANION_READY_FILE=%t/comma-companion-agent/ready" \
  "$unit"
require_line "RuntimeDirectory=comma-companion-agent" "$unit"
require_line "ProtectSystem=full" "$unit"
require_line \
  "ReadOnlyPaths=/data/openpilot /data/continue.sh /data/params" \
  "$unit"
if /usr/bin/grep -E '^(ReadOnlyPaths|ReadWritePaths|BindReadOnlyPaths)=' "$unit" |
  /usr/bin/grep -Eq '(^|[=[:space:]])/data($|[[:space:]])|realdata|private-pond-agent/spool'; then
  fail "logging roots and spool must remain on the same writable mount object"
fi
if /usr/bin/grep -Eq 'device-token|private-pond-agent-secrets|secret_file' \
  "$script_dir/bootstrap.sh"; then
  fail "unprivileged bootstrap attempts to traverse the root-only secret tree"
fi

/usr/bin/python3 - "$config" <<'PY'
import json
import sys

config = json.load(open(sys.argv[1], encoding="utf-8"))
assert not config.get("token"), "example config contains an embedded token"
assert not config.get("token_file"), "example config bypasses systemd credentials"
commands = config["commands"]
assert commands["allow_agent_restart"] is False
assert commands["allow_starpilot_restart"] is False
assert commands["allow_power_commands"] is False
assert config["storage"]["emergency_behavior"] == "pause", (
    "production example must preserve retained archive data under pressure"
)
inventory = config.get("inventory")
assert isinstance(inventory, dict), "example config lacks the inventory block"
assert inventory.get("expected_streams") == [], (
    "example config must remain explicitly fail-closed until device discovery"
)
PY

/usr/bin/python3 - "$all_camera_inventory" <<'PY'
import json
import sys

inventory = json.load(open(sys.argv[1], encoding="utf-8"))
streams = inventory.get("expected_streams")
assert isinstance(streams, list) and streams, (
    "all-camera inventory must define expected streams"
)
roles = {
    (
        stream.get("root_name"),
        stream.get("artifact_type"),
        stream.get("camera", ""),
    )
    for stream in streams
}
assert len(roles) == len(streams), "all-camera inventory contains duplicate roles"
for root_name in ("realdata", "realdata_HD", "realdata_konik"):
    assert (root_name, "rlog", "") in roles
    assert (root_name, "video", "driver") in roles, (
        f"all-camera inventory must include dcamera for {root_name}"
    )
PY

/usr/bin/python3 - "$script_dir/update-device.sh" <<'PY'
import sys

text = open(sys.argv[1], encoding="utf-8").read()
blocked = text.split("blocked = (", 1)[1].split(")", 1)[0]
assert '"allow_starpilot_restart"' in blocked
assert '"allow_power_commands"' in blocked
assert '"allow_agent_restart"' not in blocked
PY

if /usr/bin/grep -Eq 'control-helper|CONTROL_TOKEN' \
  "$unit" \
  "$script_dir/bootstrap.sh" \
  "$script_dir/install-device.sh" \
  "$script_dir/update-device.sh"; then
  fail "the staged privileged helper must not be installed or activated"
fi
if /usr/bin/grep -Fq 'DoUserReboot' \
  "$script_dir/internal/controlhelper/platform_linux.go"; then
  fail "privileged helper bypasses manager reboot deferral"
fi
if ! /usr/bin/grep -Fq 'putParam(paramsBase, "DoReboot", "1")' \
  "$script_dir/internal/controlhelper/platform_linux.go"; then
  fail "privileged helper lacks the manager-deferred reboot path"
fi

require_line \
  '  atomic_symlink "$release_dir" "$current_link"' \
  "$script_dir/update-device.sh"
require_line \
  '  bounded_root /usr/bin/systemctl restart --no-block "$service_name" || return 1' \
  "$script_dir/deployment-lib.sh"
require_line \
  '    bounded_root /usr/bin/systemctl stop --no-block "$service_name"' \
  "$script_dir/deployment-lib.sh"
require_line \
  '    wait_for_inactive_service' \
  "$script_dir/deployment-lib.sh"
require_line \
  '    atomic_symlink "$old_release" "$current_link"' \
  "$script_dir/update-device.sh"
require_line \
  'continue_backup="$agent_dir/backups/continue.sh.first-install"' \
  "$script_dir/deployment-lib.sh"
require_line \
  'hook_line="/usr/bin/timeout --kill-after=1s 8s /data/private-pond-agent/bootstrap.sh >/dev/null 2>&1 || true"' \
  "$script_dir/deployment-lib.sh"
for ignored_token in \
  "/device-token" \
  "/device-token.*" \
  "/device-token-*" \
  "/.device-token*"; do
  require_line "$ignored_token" "$script_dir/.gitignore"
done
require_line \
  'remove_plaintext_token_sources || {' \
  "$script_dir/update-device.sh"
require_line \
  '  [[ -f "$token_source" && ! -L "$token_source" && -s "$token_source" ]] || {' \
  "$script_dir/update-device.sh"
require_line \
  'enabled = [name for name in blocked if commands.get(name) is not False]' \
  "$script_dir/update-device.sh"
require_line \
  '        "suggest-streams-device.sh discovery before activation"' \
  "$script_dir/update-device.sh"
require_line \
  '    suggest-streams-device.sh; do' \
  "$script_dir/update-device.sh"
require_line \
  '  -suggest-streams \' \
  "$script_dir/suggest-streams-device.sh"

if /usr/bin/grep -En \
  '(^|[[:space:]])eval([[:space:]]|$)|/usr/bin/sudo.*(/bin/)?(ba)?sh[[:space:]]+-c' \
  "$script_dir"/deployment-lib.sh \
  "$script_dir"/install-device.sh \
  "$script_dir"/update-device.sh \
  "$script_dir"/rollback-device.sh \
  "$script_dir"/restore-device.sh \
  "$script_dir"/uninstall-device.sh \
  "$script_dir"/suggest-streams-device.sh; then
  fail "deployment scripts contain dynamic privileged shell execution"
fi

echo "deployment static checks passed"
