from types import SimpleNamespace

from openpilot.starpilot.tailscale_boot import ensure_tailscale_service


def install(root):
  for name in ('tailscale', 'tailscaled', 'tailscaled.service'):
    (root / name).write_text('x')


def test_absent_install_runs_nothing(tmp_path):
  calls = []
  assert ensure_tailscale_service(tmp_path, tmp_path / 'unit', run=lambda *a, **k: calls.append(a)) is False
  assert calls == []


def test_restores_unit_then_starts_service(tmp_path):
  install(tmp_path)
  calls = []

  def run(command, **kwargs):
    calls.append(command)
    return SimpleNamespace(returncode=0)

  unit = tmp_path / 'run' / 'tailscaled.service'
  assert ensure_tailscale_service(tmp_path, unit, run=run) is True
  assert calls == [
    ['sudo', 'install', '-m', '644', str(tmp_path / 'tailscaled.service'), str(unit)],
    ['sudo', 'systemctl', 'daemon-reload'],
    ['sudo', 'systemctl', 'start', 'tailscaled'],
  ]


def test_stops_after_first_failure(tmp_path):
  install(tmp_path)
  calls = []

  def run(command, **kwargs):
    calls.append(command)
    return SimpleNamespace(returncode=1)

  assert ensure_tailscale_service(tmp_path, tmp_path / 'unit', run=run) is False
  assert len(calls) == 1
