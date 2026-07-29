from __future__ import annotations

import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import pytest

from comma_companion_importer.cli import main


def test_dry_run_does_not_create_manifest(tmp_path: Path) -> None:
  source = tmp_path / "logs"
  artifact = source / "device" / "realdata" / "route--0" / "rlog"
  artifact.parent.mkdir(parents=True)
  artifact.write_bytes(b"log")
  manifest = tmp_path / "should-not-exist.sqlite3"

  assert main([str(source), "--dry-run", "--manifest", str(manifest)]) == 0
  assert not manifest.exists()


def test_json_dry_run_keeps_stdout_machine_readable(tmp_path: Path) -> None:
  artifact = tmp_path / "logs" / "device" / "realdata" / "route--0" / "rlog"
  artifact.parent.mkdir(parents=True)
  artifact.write_bytes(b"log")
  stdout = StringIO()
  stderr = StringIO()

  with redirect_stdout(stdout), redirect_stderr(stderr):
    assert main([str(tmp_path / "logs"), "--dry-run", "--json-summary"]) == 0

  assert json.loads(stdout.getvalue()) == {
    "dry_run": True,
    "selected_files": 1,
    "selected_bytes": 3,
    "routes": 1,
  }
  assert "Selected 1 files" in stderr.getvalue()


def test_route_upload_requires_explicit_inventory_config(
  tmp_path: Path,
) -> None:
  artifact = tmp_path / "logs" / "device" / "realdata" / "route--0" / "rlog"
  artifact.parent.mkdir(parents=True)
  artifact.write_bytes(b"log")
  with pytest.raises(SystemExit) as error:
    main(
      [
        str(tmp_path / "logs"),
        "--server",
        "http://127.0.0.1:8000",
        "--token",
        "token",
      ],
    )
  assert error.value.code == 2


def test_dry_run_reports_inventory_completeness(
  tmp_path: Path,
) -> None:
  artifact = tmp_path / "logs" / "device" / "realdata" / "route--2" / "rlog"
  artifact.parent.mkdir(parents=True)
  artifact.write_bytes(b"log")
  config = tmp_path / "inventory.json"
  config.write_text(
    """
    {
      "inventory": {
        "expected_streams": [
          {
            "root_name": "realdata",
            "artifact_type": "rlog",
            "camera": ""
          }
        ]
      }
    }
    """,
    encoding="utf-8",
  )
  stdout = StringIO()
  stderr = StringIO()
  with redirect_stdout(stdout), redirect_stderr(stderr):
    assert (
      main(
        [
          str(tmp_path / "logs"),
          "--dry-run",
          "--json-summary",
          "--inventory-config",
          str(config),
        ],
      )
      == 0
    )
  summary = json.loads(stdout.getvalue())
  assert summary["route_inventories"] == [
    {
      "device_id": "device",
      "route_name": "route",
      "state": "partial",
      "present_segments": 1,
      "missing_segments": 2,
      "missing_streams": 0,
    },
  ]


def test_filtered_tail_cannot_shorten_authoritative_route_inventory(
  tmp_path: Path,
) -> None:
  root = tmp_path / "logs" / "device" / "realdata"
  rlog = root / "route--0" / "rlog"
  filtered_tail = root / "route--3" / "dcamera.hevc"
  rlog.parent.mkdir(parents=True)
  filtered_tail.parent.mkdir(parents=True)
  rlog.write_bytes(b"log")
  filtered_tail.write_bytes(b"camera")
  config = tmp_path / "inventory.json"
  config.write_text(
    """
    {
      "inventory": {
        "expected_streams": [
          {
            "root_name": "realdata",
            "artifact_type": "rlog",
            "camera": ""
          }
        ]
      }
    }
    """,
    encoding="utf-8",
  )
  stdout = StringIO()
  stderr = StringIO()
  with redirect_stdout(stdout), redirect_stderr(stderr):
    assert (
      main(
        [
          str(tmp_path / "logs"),
          "--dry-run",
          "--json-summary",
          "--cameras",
          "none",
          "--logs",
          "rlog",
          "--no-other",
          "--inventory-config",
          str(config),
        ],
      )
      == 0
    )
  summary = json.loads(stdout.getvalue())
  assert summary["selected_files"] == 1
  assert summary["route_inventories"] == [
    {
      "device_id": "device",
      "route_name": "route",
      "state": "partial",
      "present_segments": 2,
      "missing_segments": 2,
      "missing_streams": 2,
    },
  ]
