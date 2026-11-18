#!/usr/bin/env bash
#
# Shared, fixed-path deployment helpers. This file is sourced only by the
# locally invoked device-management scripts; the agent service never runs it.

agent_dir=/data/private-pond-agent
secret_dir=/data/private-pond-agent-secrets
secret_file="$secret_dir/device-token"
releases_dir="$agent_dir/releases"
current_link="$agent_dir/current"
previous_link="$agent_dir/previous"
spool_dir="$agent_dir/spool"
continue_file=/data/continue.sh
continue_backup="$agent_dir/backups/continue.sh.first-install"
service_name=comma-companion-agent.service
unit_target="/run/systemd/system/$service_name"
ready_file=/run/comma-companion-agent/ready
hook_marker="# comma-companion-agent bootstrap"
hook_line="/usr/bin/timeout --kill-after=1s 8s /data/private-pond-agent/bootstrap.sh >/dev/null 2>&1 || true"
health_timeout_seconds=55
health_stable_seconds=5

bounded_root() {
  /usr/bin/timeout --kill-after=1s 8s /usr/bin/sudo -n "$@"
}

ensure_layout() {
  bounded_root /usr/bin/install -d -m 0755 -o root -g root \
    "$agent_dir" "$releases_dir" "$agent_dir/bin"
  bounded_root /usr/bin/install -d -m 0700 -o root -g root \
    "$agent_dir/backups" "$secret_dir"
  # Never recursively chown the spool: its files can be hardlinks to logger
  # output, so changing their ownership would also change the source inode.
  bounded_root /usr/bin/install -d -m 0700 -o comma -g comma "$spool_dir"
}

atomic_install_file() {
  local source=$1
  local target=$2
  local mode=$3
  local owner=$4
  local group=$5
  local parent
  local base
  local temporary

  if [[ ! -f "$source" ]] && ! bounded_root /usr/bin/test -f "$source"; then
    echo "Missing deployment source: $source" >&2
    return 1
  fi
  parent="$(/usr/bin/dirname -- "$target")"
  base="$(/usr/bin/basename -- "$target")"
  temporary="$parent/.$base.new.$$"

  bounded_root /usr/bin/rm -f -- "$temporary" >/dev/null 2>&1 || true
  bounded_root /usr/bin/install -m "$mode" -o "$owner" -g "$group" -- \
    "$source" "$temporary"
  bounded_root /usr/bin/mv -fT -- "$temporary" "$target"
}

atomic_symlink() {
  local target=$1
  local link_path=$2
  local parent
  local base
  local temporary

  parent="$(/usr/bin/dirname -- "$link_path")"
  base="$(/usr/bin/basename -- "$link_path")"
  temporary="$parent/.$base.new.$$"

  bounded_root /usr/bin/rm -f -- "$temporary" >/dev/null 2>&1 || true
  bounded_root /usr/bin/ln -s -- "$target" "$temporary"
  bounded_root /usr/bin/chown -h root:root "$temporary"
  bounded_root /usr/bin/mv -fT -- "$temporary" "$link_path"
}

validate_release_path() {
  local release_path=$1
  [[ "$release_path" =~ ^/data/private-pond-agent/releases/release-[0-9a-f]{32}$ ]] &&
    [[ -d "$release_path" ]] &&
    [[ -x "$release_path/comma-companion-agent" ]] &&
    [[ -r "$release_path/config.json" ]] &&
    [[ -r "$release_path/comma-companion-agent.service" ]]
}

read_release_link() {
  local link_path=$1
  local resolved

  resolved="$(/usr/bin/readlink -f -- "$link_path" 2>/dev/null)" || return 1
  validate_release_path "$resolved" || return 1
  printf '%s\n' "$resolved"
}

sync_current_metadata() {
  local release_path

  release_path="$(read_release_link "$current_link")" || return 1
  atomic_install_file "$release_path/config.json" \
    "$agent_dir/config.json" 0440 root comma
  atomic_install_file "$release_path/comma-companion-agent.service" \
    "$agent_dir/comma-companion-agent.service" 0444 root root
}

install_runtime_unit() {
  local release_path
  local unit_source
  local temporary="${unit_target}.new.$$"

  release_path="$(read_release_link "$current_link")" || {
    echo "The current release link is missing or invalid." >&2
    return 1
  }
  unit_source="$release_path/comma-companion-agent.service"

  bounded_root /usr/bin/rm -f -- "$temporary" >/dev/null 2>&1 || true
  bounded_root /usr/bin/install -m 0644 -o root -g root -- \
    "$unit_source" "$temporary"
  bounded_root /usr/bin/mv -fT -- "$temporary" "$unit_target"

  if [[ -x /usr/bin/systemd-analyze ]]; then
    bounded_root /usr/bin/systemd-analyze verify "$unit_target"
  fi
  bounded_root /usr/bin/systemctl daemon-reload
}

wait_for_healthy_service() {
  local deadline=$((SECONDS + health_timeout_seconds))
  local stable=0

  while ((SECONDS < deadline)); do
    if bounded_root /usr/bin/systemctl is-active --quiet "$service_name" &&
      bounded_root /usr/bin/test -s "$ready_file"; then
      stable=$((stable + 1))
      if ((stable >= health_stable_seconds)); then
        return 0
      fi
    else
      stable=0
    fi
    /usr/bin/sleep 1
  done

  echo "$service_name did not report an accepted heartbeat and remain active for ${health_stable_seconds}s within ${health_timeout_seconds}s." >&2
  bounded_root /usr/bin/systemctl status --no-pager "$service_name" >&2 || true
  return 1
}

current_runtime_is_healthy() {
  local release_path

  release_path="$(read_release_link "$current_link")" || return 1
  [[ -f "$unit_target" ]] || return 1
  /usr/bin/cmp -s \
    "$release_path/comma-companion-agent.service" "$unit_target" || return 1
  bounded_root /usr/bin/systemctl is-active --quiet "$service_name" || return 1
  wait_for_healthy_service
}

wait_for_inactive_service() {
  local deadline=$((SECONDS + 25))
  local active_state

  while ((SECONDS < deadline)); do
    active_state="$(
      bounded_root /usr/bin/systemctl show \
        --property=ActiveState --value "$service_name" 2>/dev/null
    )" || active_state=unknown
    case "$active_state" in
      inactive | failed)
        return 0
        ;;
    esac
    /usr/bin/sleep 1
  done
  echo "$service_name did not stop within 25 seconds." >&2
  return 1
}

activate_current_release() {
  install_runtime_unit || return 1
  bounded_root /usr/bin/rm -f -- "$ready_file" || return 1
  bounded_root /usr/bin/systemctl restart --no-block "$service_name" || return 1
  wait_for_healthy_service
}

stop_and_remove_runtime_unit() {
  local load_state

  load_state="$(
    bounded_root /usr/bin/systemctl show \
      --property=LoadState --value "$service_name" 2>/dev/null
  )" || load_state=not-found
  if [[ "$load_state" != "not-found" ]]; then
    bounded_root /usr/bin/systemctl stop --no-block "$service_name"
    wait_for_inactive_service
  fi
  bounded_root /usr/bin/rm -f -- "$unit_target"
  bounded_root /usr/bin/systemctl daemon-reload
  bounded_root /usr/bin/systemctl reset-failed "$service_name" >/dev/null 2>&1 || true
}

preserve_first_continue_backup() {
  local legacy_backups=()
  local backup_source=$continue_file

  [[ -f "$continue_file" && ! -L "$continue_file" ]] || {
    echo "$continue_file must be a regular, non-symlink file." >&2
    return 1
  }
  ensure_layout
  if bounded_root /usr/bin/test -f "$continue_backup"; then
    return 0
  fi

  # Migrate the earliest backup made by the original installer when present.
  shopt -s nullglob
  legacy_backups=("$continue_file".comma-companion-backup-*)
  shopt -u nullglob
  if ((${#legacy_backups[@]} > 0)); then
    backup_source=${legacy_backups[0]}
  fi
  atomic_install_file "$backup_source" "$continue_backup" 0400 root root
}

continue_hook_is_valid() {
  /usr/bin/awk -v marker="$hook_marker" -v hook="$hook_line" '
    BEGIN { seen = 0; valid = 0 }
    $0 == marker {
      seen = 1
      if ((getline following) > 0 && following == hook) {
        valid = 1
      }
      exit
    }
    END {
      if (!seen || !valid) {
        exit 1
      }
    }
  ' "$continue_file"
}

install_continue_hook() {
  local first_line
  local temporary

  preserve_first_continue_backup
  if /usr/bin/grep -Fqx "$hook_marker" "$continue_file"; then
    if continue_hook_is_valid; then
      echo "The $continue_file hook is already installed."
      return 0
    fi
    echo "Refusing to replace an unrecognized companion hook in $continue_file." >&2
    return 1
  fi

  IFS= read -r first_line <"$continue_file" || true
  [[ "$first_line" == '#!'* ]] || {
    echo "$continue_file does not start with a shebang; no change was made." >&2
    return 1
  }

  temporary="$(/usr/bin/mktemp /data/.continue.sh.comma-companion.XXXXXX)"
  {
    printf '%s\n' "$first_line"
    printf '%s\n' "$hook_marker"
    printf '%s\n' "$hook_line"
    /usr/bin/tail -n +2 "$continue_file"
  } >"$temporary"

  bounded_root /usr/bin/chmod --reference="$continue_file" "$temporary"
  bounded_root /usr/bin/chown --reference="$continue_file" "$temporary"
  bounded_root /usr/bin/mv -fT -- "$temporary" "$continue_file"
  echo "Installed fail-open boot hook; immutable first backup: $continue_backup"
}

remove_continue_hook() {
  local temporary

  if [[ -L "$continue_file" ]]; then
    echo "Refusing to modify symlink $continue_file." >&2
    return 1
  fi
  [[ -f "$continue_file" ]] || return 0
  if ! /usr/bin/grep -Fqx "$hook_marker" "$continue_file"; then
    return 0
  fi
  continue_hook_is_valid || {
    echo "Refusing to remove an unrecognized companion hook in $continue_file." >&2
    return 1
  }

  temporary="$(/usr/bin/mktemp /data/.continue.sh.comma-companion.XXXXXX)"
  if ! /usr/bin/awk -v marker="$hook_marker" -v hook="$hook_line" '
    $0 == marker {
      if ((getline following) <= 0 || following != hook) {
        exit 42
      }
      next
    }
    { print }
  ' "$continue_file" >"$temporary"; then
    /usr/bin/rm -f -- "$temporary"
    echo "The boot hook changed while it was being removed; no change was made." >&2
    return 1
  fi

  bounded_root /usr/bin/chmod --reference="$continue_file" "$temporary"
  bounded_root /usr/bin/chown --reference="$continue_file" "$temporary"
  bounded_root /usr/bin/mv -fT -- "$temporary" "$continue_file"
}
