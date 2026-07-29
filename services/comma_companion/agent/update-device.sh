#!/usr/bin/env bash
#
# Install or update one immutable release and atomically select it. Run this
# script from a newly copied bundle, not from a server command.

set -euo pipefail

script_dir="$(cd -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=deployment-lib.sh
source "$script_dir/deployment-lib.sh"

replace_token=false
if (($# > 1)); then
  echo "Usage: $0 [--replace-token]" >&2
  exit 2
fi
if (($# == 1)); then
  [[ "$1" == "--replace-token" ]] || {
    echo "Usage: $0 [--replace-token]" >&2
    exit 2
  }
  replace_token=true
fi

if [[ "$script_dir" == "$agent_dir" ]] &&
  [[ -e "$current_link" || -L "$current_link" ]]; then
  echo "Installed files are immutable; copy a new bundle to a separate staging directory." >&2
  exit 2
fi

continue_hook_is_valid || {
  echo "The fail-open boot hook is missing or changed; run the installed restore-device.sh first." >&2
  exit 1
}

binary_source="$script_dir/dist/comma-companion-agent-linux-arm64"
unit_source="$script_dir/comma-companion-agent.service"
bootstrap_source="$script_dir/bootstrap.sh"
config_source="$script_dir/config.json"
legacy_token="$agent_dir/device-token"
token_source="$script_dir/device-token"
token_changed=false
token_preexisted=false
token_backup="$secret_dir/.device-token.rollback.$$"
deployment_complete=false

if [[ ! -f "$config_source" ]]; then
  current_release="$(read_release_link "$current_link" 2>/dev/null || true)"
  if [[ -n "$current_release" ]]; then
    config_source="$current_release/config.json"
  else
    echo "Create $script_dir/config.json from config.example.json first." >&2
    exit 1
  fi
fi

for required in "$binary_source" "$unit_source" "$bootstrap_source" \
  "$script_dir/deployment-lib.sh" "$script_dir/rollback-device.sh" \
  "$script_dir/restore-device.sh" "$script_dir/uninstall-device.sh" \
  "$script_dir/suggest-streams-device.sh"; do
  [[ -f "$required" ]] || {
    echo "Missing deployment file: $required" >&2
    exit 1
  }
done

validate_config() {
  local candidate_config=$1
  /usr/bin/python3 - "$candidate_config" <<'PY'
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

path = Path(sys.argv[1])
try:
    config = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    raise SystemExit(f"invalid config JSON: {exc}")
if not isinstance(config, dict):
    raise SystemExit("config must be a JSON object")
for name in ("server_url", "device_id", "spool_dir"):
    if not isinstance(config.get(name), str) or not config[name].strip():
        raise SystemExit(f"{name} must be a non-empty string")
if str(config.get("token", "")).strip():
    raise SystemExit("config must not contain an embedded device token")
server = urlparse(config["server_url"])
if server.scheme != "https" and not (
    server.scheme == "http" and config.get("allow_insecure_http") is True
):
    raise SystemExit("server_url must use HTTPS")
if not server.netloc or server.username or server.password:
    raise SystemExit("server_url must be an origin without credentials")
commands = config.get("commands")
if not isinstance(commands, dict):
    raise SystemExit("commands must explicitly disable all disruptive actions")
blocked = (
    "allow_starpilot_restart",
    "allow_power_commands",
)
if not isinstance(commands.get("allow_agent_restart"), bool):
    raise SystemExit("allow_agent_restart must be explicitly true or false")
enabled = [name for name in blocked if commands.get(name) is not False]
if enabled:
    raise SystemExit(
        "disruptive commands require an audited fixed root helper and must "
        f"remain disabled in this deployment: {', '.join(enabled)}"
    )
roots = config.get("roots")
if not isinstance(roots, list) or not roots:
    raise SystemExit("roots must be a non-empty list")
root_names = {
    item.get("name")
    for item in roots
    if isinstance(item, dict) and isinstance(item.get("name"), str)
}
inventory = config.get("inventory")
if not isinstance(inventory, dict):
    raise SystemExit("inventory must be an object")
streams = inventory.get("expected_streams")
if not isinstance(streams, list) or not streams:
    raise SystemExit(
        "inventory.expected_streams must be populated from the explicit "
        "suggest-streams-device.sh discovery before activation"
    )
seen_roles = set()
inventory_roots = set()
rlog_roots = set()
for index, stream in enumerate(streams):
    if not isinstance(stream, dict):
        raise SystemExit(f"inventory.expected_streams[{index}] must be an object")
    root_name = stream.get("root_name")
    artifact_type = stream.get("artifact_type")
    camera = stream.get("camera", "")
    if root_name not in root_names:
        raise SystemExit(
            f"inventory.expected_streams[{index}].root_name is not configured"
        )
    if artifact_type == "video":
        if not isinstance(camera, str) or not camera:
            raise SystemExit(
                f"inventory.expected_streams[{index}].camera is required for video"
            )
    elif artifact_type in ("rlog", "qlog"):
        if camera not in ("", None):
            raise SystemExit(
                f"inventory.expected_streams[{index}].camera must be empty for logs"
            )
        camera = ""
        if artifact_type == "rlog":
            rlog_roots.add(root_name)
    else:
        raise SystemExit(
            f"inventory.expected_streams[{index}].artifact_type is unsupported"
        )
    role = f"{root_name}|{artifact_type}|{camera or '-'}"
    if role in seen_roles:
        raise SystemExit(f"duplicate inventory stream role: {role}")
    seen_roles.add(role)
    inventory_roots.add(root_name)
missing_rlog_roots = sorted(inventory_roots - rlog_roots)
if missing_rlog_roots:
    raise SystemExit(
        "each configured inventory root profile must include rlog: "
        + ", ".join(missing_rlog_roots)
    )
PY
}

validate_binary() {
  local candidate_binary=$1
  /usr/bin/python3 - "$candidate_binary" <<'PY'
import sys

with open(sys.argv[1], "rb") as candidate:
    header = candidate.read(20)
if len(header) < 20 or header[:4] != b"\x7fELF":
    raise SystemExit("candidate binary is not an ELF executable")
if header[4] != 2 or header[5] != 1:
    raise SystemExit("candidate binary must be little-endian ELF64")
machine = int.from_bytes(header[18:20], "little")
if machine != 183:
    raise SystemExit("candidate binary must target Linux ARM64")
PY
}

file_sha256() {
  /usr/bin/sha256sum -- "$1" | /usr/bin/awk '{print $1}'
}

release_matches() {
  local release_path=$1
  [[ "$(file_sha256 "$release_path/comma-companion-agent")" == "$binary_sha" ]] &&
    [[ "$(file_sha256 "$release_path/config.json")" == "$config_sha" ]] &&
    [[ "$(file_sha256 "$release_path/comma-companion-agent.service")" == "$unit_sha" ]]
}

install_management_files() {
  local name
  for name in deployment-lib.sh update-device.sh rollback-device.sh \
    restore-device.sh uninstall-device.sh bootstrap.sh install-device.sh \
    suggest-streams-device.sh; do
    if [[ -f "$script_dir/$name" ]]; then
      atomic_install_file "$script_dir/$name" "$agent_dir/$name" 0555 root root
    fi
  done
  atomic_install_file "$release_dir/comma-companion-agent.service" \
    "$agent_dir/comma-companion-agent.service" 0444 root root
  atomic_install_file "$release_dir/config.json" \
    "$agent_dir/config.json" 0440 root comma
  if [[ -f "$script_dir/config.example.json" ]]; then
    atomic_install_file "$script_dir/config.example.json" \
      "$agent_dir/config.example.json" 0444 root root
  fi
  if [[ -f "$script_dir/README.md" ]]; then
    atomic_install_file "$script_dir/README.md" "$agent_dir/README.md" 0444 root root
  fi
}

harden_canonical_sources() {
  local tree
  local file
  for tree in "$agent_dir/cmd" "$agent_dir/internal" "$agent_dir/dist"; do
    if [[ -d "$tree" ]]; then
      bounded_root /usr/bin/chown -R root:root "$tree"
      bounded_root /usr/bin/chmod -R go-w "$tree"
    fi
  done
  for file in "$agent_dir/.gitignore" "$agent_dir/go.mod" \
    "$agent_dir/build.ps1" "$agent_dir/deployment-static-test.sh"; do
    if [[ -f "$file" ]]; then
      bounded_root /usr/bin/chown root:root "$file"
      bounded_root /usr/bin/chmod go-w "$file"
    fi
  done
  bounded_root /usr/bin/chown root:root "$agent_dir"
  bounded_root /usr/bin/chmod 0755 "$agent_dir"
  bounded_root /usr/bin/chown comma:comma "$spool_dir"
  bounded_root /usr/bin/chmod 0700 "$spool_dir"
}

restore_token_on_failure() {
  if [[ "$token_changed" != true ]]; then
    return 0
  fi
  if [[ "$token_preexisted" == true ]]; then
    atomic_install_file "$token_backup" "$secret_file" 0600 root root || true
  else
    bounded_root /usr/bin/rm -f -- "$secret_file" || true
  fi
  bounded_root /usr/bin/rm -f -- "$token_backup" >/dev/null 2>&1 || true
  token_changed=false
}

remove_plaintext_token_sources() {
  local candidate
  for candidate in "$script_dir/device-token" "$legacy_token"; do
    if [[ -e "$candidate" || -L "$candidate" ]]; then
      bounded_root /usr/bin/rm -f -- "$candidate" || {
        echo "Could not unlink plaintext staging credential: $candidate" >&2
        return 1
      }
    fi
  done
}

deployment_exit() {
  if [[ "$deployment_complete" != true ]]; then
    restore_token_on_failure
  fi
}
trap deployment_exit EXIT

validate_config "$config_source"
validate_binary "$binary_source"
ensure_layout

binary_sha="$(file_sha256 "$binary_source")"
config_sha="$(file_sha256 "$config_source")"
unit_sha="$(file_sha256 "$unit_source")"
release_hash="$(
  printf 'binary:%s\nconfig:%s\nunit:%s\n' \
    "$binary_sha" "$config_sha" "$unit_sha" |
    /usr/bin/sha256sum |
    /usr/bin/awk '{print substr($1, 1, 32)}'
)"
release_dir="$releases_dir/release-$release_hash"

if [[ -e "$current_link" || -L "$current_link" ]]; then
  old_release="$(read_release_link "$current_link")" || {
    echo "Refusing to replace an invalid $current_link." >&2
    exit 1
  }
else
  old_release=
fi

if [[ -d "$release_dir" ]]; then
  release_matches "$release_dir" || {
    echo "Existing immutable release does not match its content ID: $release_dir" >&2
    exit 1
  }
  validate_config "$release_dir/config.json"
else
  release_stage="$releases_dir/.release-$release_hash.new.$$"
  [[ ! -e "$release_stage" ]] || {
    echo "Unexpected release staging path already exists: $release_stage" >&2
    exit 1
  }
  bounded_root /usr/bin/install -d -m 0755 -o root -g root "$release_stage"
  bounded_root /usr/bin/install -m 0555 -o root -g root -- \
    "$binary_source" "$release_stage/comma-companion-agent"
  bounded_root /usr/bin/install -m 0440 -o root -g comma -- \
    "$config_source" "$release_stage/config.json"
  bounded_root /usr/bin/install -m 0444 -o root -g root -- \
    "$unit_source" "$release_stage/comma-companion-agent.service"
  validate_config "$release_stage/config.json"
  validate_binary "$release_stage/comma-companion-agent"
  release_matches "$release_stage" || {
    echo "Release changed while it was being staged." >&2
    exit 1
  }
  bounded_root /usr/bin/mv -T -- "$release_stage" "$release_dir"
fi

install_management_files
harden_canonical_sources

if bounded_root /usr/bin/test -s "$secret_file"; then
  token_preexisted=true
fi
if [[ "$replace_token" == true ]] || [[ "$token_preexisted" != true ]]; then
  if [[ ! -s "$token_source" && -s "$legacy_token" ]]; then
    token_source=$legacy_token
  fi
  [[ -f "$token_source" && ! -L "$token_source" && -s "$token_source" ]] || {
    echo "A non-empty regular, non-symlink $script_dir/device-token is required." >&2
    exit 1
  }
  token_mode="$(/usr/bin/stat -c '%a' -- "$token_source")"
  case "$token_mode" in
    400 | 600) ;;
    *)
      echo "$token_source must have mode 0400 or 0600, got $token_mode." >&2
      exit 1
      ;;
  esac
  /usr/bin/grep -q '[^[:space:]]' "$token_source" || {
    echo "The device token contains only whitespace." >&2
    exit 1
  }
  if [[ "$token_preexisted" == true ]]; then
    atomic_install_file "$secret_file" "$token_backup" 0600 root root
  fi
  atomic_install_file "$token_source" "$secret_file" 0600 root root
  token_changed=true
fi
bounded_root /usr/bin/grep -q '[^[:space:]]' "$secret_file" || {
  echo "The persistent device credential is empty." >&2
  exit 1
}

atomic_symlink "../current/comma-companion-agent" \
  "$agent_dir/bin/comma-companion-agent"
if [[ "$old_release" != "$release_dir" ]]; then
  atomic_symlink "$release_dir" "$current_link"
fi

activation_required=true
if [[ "$old_release" == "$release_dir" ]] &&
  [[ "$token_changed" != true ]] &&
  current_runtime_is_healthy; then
  activation_required=false
fi

if [[ "$activation_required" == true ]] && ! activate_current_release; then
  echo "Activation failed; restoring the previous release." >&2
  restore_token_on_failure
  if [[ -n "$old_release" ]]; then
    atomic_symlink "$old_release" "$current_link"
    if ! activate_current_release; then
      echo "The previous release was selected but did not become healthy." >&2
    else
      sync_current_metadata || {
        echo "Warning: could not synchronize retained config and unit copies." >&2
      }
    fi
  else
    if stop_and_remove_runtime_unit; then
      bounded_root /usr/bin/rm -f -- "$current_link"
    else
      echo "The failed first release remains selected because the service did not stop." >&2
    fi
  fi
  exit 1
fi

bounded_root /usr/bin/rm -f -- "$token_backup" >/dev/null 2>&1 || {
  echo "Warning: could not remove the root-only credential rollback copy." >&2
}
token_changed=false
deployment_complete=true
if [[ -n "$old_release" && "$old_release" != "$release_dir" ]]; then
  atomic_symlink "$old_release" "$previous_link" || {
    echo "Warning: the release is healthy, but the rollback pointer was not updated." >&2
  }
fi
remove_plaintext_token_sources || {
  echo "The release is active, but plaintext credential cleanup failed." >&2
  exit 1
}

echo "Active release: $release_dir"
bounded_root /usr/bin/systemctl status --no-pager "$service_name" || true
