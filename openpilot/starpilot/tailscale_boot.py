"""Restore a user-installed Tailscale service at boot.

AGNOS keeps / read-only and /run volatile, so a Tailscale install under
/data/tailscale has no systemd unit after a reboot. Ported from the
Starpilot-20inch-ioniq5 fork. Does nothing unless /data/tailscale holds the
binaries and the saved unit.
"""

from pathlib import Path
import subprocess

TAILSCALE_DIR = Path('/data/tailscale')
RUNTIME_UNIT = Path('/run/systemd/system/tailscaled.service')


def ensure_tailscale_service(tailscale_dir=TAILSCALE_DIR, runtime_unit=RUNTIME_UNIT, run=subprocess.run) -> bool:
  persistent_unit = tailscale_dir / 'tailscaled.service'
  required = (tailscale_dir / 'tailscale', tailscale_dir / 'tailscaled', persistent_unit)
  if not all(path.is_file() for path in required):
    return False
  # Restore the volatile /run unit synchronously. A detached boot thread can race
  # manager process startup and disappear before systemd has accepted the unit.
  for command in (
    ['sudo', 'install', '-m', '644', str(persistent_unit), str(runtime_unit)],
    ['sudo', 'systemctl', 'daemon-reload'],
    ['sudo', 'systemctl', 'start', 'tailscaled'],
  ):
    try:
      if run(command, capture_output=True, timeout=30).returncode != 0:
        return False
    except (OSError, subprocess.SubprocessError):
      return False
  return True
