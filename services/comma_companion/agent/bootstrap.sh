#!/usr/bin/env bash
#
# This script is called near the beginning of /data/continue.sh. Every command
# is time-bounded and every failure returns success so StarPilot boot proceeds.

set +e

agent_dir=/data/private-pond-agent
unit_source="$agent_dir/current/comma-companion-agent.service"
unit_target=/run/systemd/system/comma-companion-agent.service
unit_temporary="${unit_target}.new.$$"

finish() {
  /usr/bin/timeout --kill-after=1s 3s /usr/bin/sudo -n /usr/bin/rm -f -- \
    "$unit_temporary" >/dev/null 2>&1 || true
  exit 0
}
trap finish EXIT

if [[ ! -x "$agent_dir/current/comma-companion-agent" ]] ||
   [[ ! -r "$agent_dir/current/config.json" ]] ||
   [[ ! -r "$unit_source" ]]; then
  exit 0
fi

/usr/bin/timeout --kill-after=1s 3s /usr/bin/sudo -n /usr/bin/install -m 0644 \
  "$unit_source" "$unit_temporary" >/dev/null 2>&1 || exit 0
/usr/bin/timeout --kill-after=1s 3s /usr/bin/sudo -n /usr/bin/mv -fT -- \
  "$unit_temporary" "$unit_target" >/dev/null 2>&1 || exit 0
/usr/bin/timeout --kill-after=1s 3s /usr/bin/sudo -n /usr/bin/systemctl daemon-reload \
  >/dev/null 2>&1 || exit 0
/usr/bin/timeout --kill-after=1s 3s /usr/bin/sudo -n /usr/bin/systemctl start --no-block \
  comma-companion-agent.service >/dev/null 2>&1 || true

exit 0
