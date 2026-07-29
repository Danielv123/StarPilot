from __future__ import annotations

import json
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

import deploy.preflight_storage as preflight_storage


def test_hardlink_probe_verifies_alias_and_cleans_up(tmp_path: Path) -> None:
  assert preflight_storage._hardlink_probe(tmp_path) == (True, None)
  assert list(tmp_path.iterdir()) == []


def test_primary_storage_gate_requires_hardlink() -> None:
  result: dict[str, object] = {
    "atomic_replace": True,
    "file_fsync": True,
    "hardlink": False,
    "state_writable": True,
    "state_is_local": True,
    "sqlite_locking": True,
    "archive_is_cifs": True,
    "state_and_archive_share_device": False,
  }

  assert preflight_storage._storage_ok(result, require_cifs=True) is False
  result["hardlink"] = True
  assert preflight_storage._storage_ok(result, require_cifs=True) is True


def test_worker_layout_gate_requires_hardlink_for_every_output(
  tmp_path: Path,
  monkeypatch: MonkeyPatch,
  capsys: CaptureFixture[str],
) -> None:
  archive = tmp_path / "archive"
  database = tmp_path / "state"
  archive.mkdir()
  database.mkdir()
  sentinel = archive / ".comma-companion-archive"
  sentinel.write_text("private archive\n", encoding="utf-8")
  monkeypatch.setenv("COMPANION_ARCHIVE_ROOT", str(archive))
  monkeypatch.setenv("COMPANION_ARCHIVE_SENTINEL", str(sentinel))
  monkeypatch.setenv("COMPANION_DATABASE_PATH", str(database / "companion.sqlite3"))
  monkeypatch.setenv("COMPANION_PREFLIGHT_REQUIRE_CIFS", "0")
  monkeypatch.setattr(preflight_storage, "_filesystem_type", lambda _path: "ext4")
  monkeypatch.setattr(preflight_storage, "_read_only_probe", lambda _path: (True, None))
  monkeypatch.setattr(preflight_storage, "_write_probe", lambda _path: (True, None))
  monkeypatch.setattr(preflight_storage, "_sqlite_lock_probe", lambda _path: (True, None))
  monkeypatch.setattr(preflight_storage, "_hardlink_probe", lambda _path: (False, "unsupported"))

  assert preflight_storage.worker_layout_preflight() == 1

  result = json.loads(capsys.readouterr().out)
  assert set(result["outputs_hardlink"]) == {"derived", "telemetry", "thumbnails"}
  assert all(record == {"ok": False, "error": "unsupported"} for record in result["outputs_hardlink"].values())
