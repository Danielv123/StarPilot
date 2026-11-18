from __future__ import annotations

import json
import hashlib
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from comma_companion.app import create_app
from comma_companion.db import Database, isoformat
from conftest import ADMIN_PASSWORD, ORIGIN


@pytest.fixture
def telemetry_client(settings) -> Iterator[TestClient]:
  application = create_app(settings)
  with TestClient(application, base_url=ORIGIN) as client:
    yield client


def _login(client: TestClient) -> None:
  response = client.post(
    "/api/v1/auth/login",
    headers={"Origin": ORIGIN},
    json={"username": "admin", "password": ADMIN_PASSWORD},
  )
  assert response.status_code == 200, response.text


def _catalog(*signals: tuple[str, str, str | None]) -> str:
  return json.dumps(
    {
      "record": "signal_catalog",
      "signals": [
        {
          "id": signal,
          "value_type": value_type,
          "unit": unit,
          "interpolation": ("step" if value_type in {"bool", "enum", "text"} else "linear"),
        }
        for signal, value_type, unit in signals
      ],
    }
  )


def _insert_index(
  database: Database,
  *,
  drive_id: str | None = None,
  state: str = "complete",
  catalog: str | None = None,
  ndjson_sha256: str = "a" * 64,
  timeline_version: str = "c" * 64,
  manifest: dict[str, object] | None = None,
) -> str:
  selected_drive_id = drive_id or uuid4().hex
  now = isoformat()
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO drives(id, device_id, route_name, created_at)
      VALUES (?, 'device-one', ?, ?)
      """,
      (selected_drive_id, f"route-{selected_drive_id}", now),
    )
    connection.execute(
      """
      INSERT INTO telemetry_indexes(
        drive_id, schema_version, state, ndjson_path, ndjson_sha256,
        manifest_json, signal_catalog_json, source_fingerprint,
        created_at, updated_at
      ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        selected_drive_id,
        state,
        f"telemetry/{selected_drive_id}.ndjson",
        ndjson_sha256,
        json.dumps(
          manifest
          if manifest is not None
          else {
            "state": state,
            "publication_ready": state == "complete",
            "timeline_version": timeline_version,
          },
          separators=(",", ":"),
          sort_keys=True,
        ),
        catalog or _catalog(("vehicle.speed", "float", "m/s")),
        "b" * 64,
        now,
        now,
      ),
    )
  return selected_drive_id


def _insert_chunk(
  database: Database,
  archive_root: Path,
  drive_id: str,
  signal: str,
  tier: str,
  times: list[int],
  values: list[object],
  *,
  kind: str = "continuous",
  unit: str | None = None,
  chunk_index: int = 0,
) -> None:
  payload: dict[str, object] = {
    "record": "series_chunk",
    "signal": signal,
    "tier": tier,
    "chunk": chunk_index,
    "t_us": times,
    "v": values,
  }
  index = database.query_one(
    "SELECT ndjson_path FROM telemetry_indexes WHERE drive_id = ?",
    (drive_id,),
  )
  assert index is not None
  ndjson_path = index["ndjson_path"]
  absolute_path = archive_root / Path(*ndjson_path.split("/"))
  absolute_path.parent.mkdir(parents=True, exist_ok=True)
  record = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
  with absolute_path.open("ab") as stream:
    offset = stream.tell()
    stream.write(record)
  with database.transaction(immediate=True) as connection:
    connection.execute(
      """
      INSERT INTO telemetry_series_chunks(
        drive_id, signal_id, tier, chunk_index, kind, unit,
        start_t_us, end_t_us, ndjson_path, byte_offset, byte_length,
        record_sha256
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      """,
      (
        drive_id,
        signal,
        tier,
        chunk_index,
        kind,
        unit,
        min(times),
        max(times),
        ndjson_path,
        offset,
        len(record),
        hashlib.sha256(record).hexdigest(),
      ),
    )


def _insert_marker(
  database: Database,
  drive_id: str,
  marker_id: str,
  *,
  kind: str,
  start_us: int,
  end_us: int,
  severity: str = "warning",
  label: str = "Telemetry gap",
  attributes: dict[str, object] | None = None,
  point_end_is_null: bool = False,
) -> None:
  record = {
    "record": "marker",
    "id": marker_id,
    "kind": kind,
    "start_us": start_us,
    "end_us": None if point_end_is_null else end_us,
    "severity": severity,
    "label": label,
    "attributes": attributes or {},
  }
  database.execute(
    """
    INSERT INTO telemetry_markers(
      drive_id, marker_id, kind, start_t_us, end_t_us,
      severity, label, data_json
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
      drive_id,
      marker_id,
      kind,
      start_us,
      end_us,
      severity,
      label,
      json.dumps(record, separators=(",", ":"), sort_keys=True),
    ),
  )


def test_series_requires_admin_and_reports_catalog_errors(
  telemetry_client: TestClient,
) -> None:
  unauthenticated = telemetry_client.get(
    "/api/v1/drives/missing/series?signals=vehicle.speed",
  )
  assert unauthenticated.status_code == 401
  assert unauthenticated.json()["error"]["code"] == "authentication_required"

  _login(telemetry_client)
  missing = telemetry_client.get(
    "/api/v1/drives/missing/series?signals=vehicle.speed",
  )
  assert missing.status_code == 404
  assert missing.json()["error"]["code"] == "drive_not_found"

  drive_id = uuid4().hex
  now = isoformat()
  telemetry_client.app.state.database.execute(
    """
    INSERT INTO drives(id, device_id, route_name, created_at)
    VALUES (?, 'device-one', ?, ?)
    """,
    (drive_id, f"route-{drive_id}", now),
  )
  not_ready = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series?signals=vehicle.speed",
  )
  assert not_ready.status_code == 409
  assert not_ready.json()["error"]["code"] == "telemetry_not_ready"


def test_series_selects_finest_fitting_tier_and_includes_neighbors(
  telemetry_client: TestClient,
) -> None:
  _login(telemetry_client)
  database = telemetry_client.app.state.database
  archive_root = telemetry_client.app.state.settings.archive_root
  drive_id = _insert_index(database)
  _insert_chunk(
    database,
    archive_root,
    drive_id,
    "vehicle.speed",
    "full",
    list(range(0, 101, 10)),
    list(range(11)),
    unit="m/s",
  )
  _insert_chunk(
    database,
    archive_root,
    drive_id,
    "vehicle.speed",
    "100ms",
    [0, 30, 40, 50, 60, 70, 80],
    [0, 3, 4, 5, 6, 7, 8],
    unit="m/s",
  )
  _insert_chunk(
    database,
    archive_root,
    drive_id,
    "vehicle.speed",
    "500ms",
    [0, 40, 60, 80],
    [0, 40, 60, 80],
    unit="m/s",
  )

  response = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": " vehicle.speed,vehicle.speed ",
      "start_us": 25,
      "end_us": 75,
      "max_points": 3,
    },
  )
  assert response.status_code == 200, response.text
  payload = response.json()
  assert payload["drive_id"] == drive_id
  assert payload["ndjson_sha256"] == "a" * 64
  assert payload["timeline_version"] == "c" * 64
  assert payload["timeline_origin"] == "stable"
  assert payload["start_t_us"] == 25
  assert payload["end_t_us"] == 75
  assert len(payload["signals"]) == 1
  series = payload["signals"][0]
  assert series["signal"] == "vehicle.speed"
  assert series["kind"] == "continuous"
  assert series["unit"] == "m/s"
  assert [(point["t_us"], point["value"]) for point in series["points"]] == [
    (0, 0.0),
    (40, 40.0),
    (80, 80.0),
  ]
  assert len(series["points"]) <= 3


def test_series_bounds_step_and_event_values_and_keeps_known_empty_signal(
  telemetry_client: TestClient,
) -> None:
  _login(telemetry_client)
  database = telemetry_client.app.state.database
  archive_root = telemetry_client.app.state.settings.archive_root
  drive_id = _insert_index(
    database,
    state="partial",
    catalog=_catalog(
      ("lateral.active", "bool", None),
      ("vehicle.brake_pressed", "bool", None),
      ("alert.event", "event", None),
    ),
  )
  for tier in ("full", "10s"):
    _insert_chunk(
      database,
      archive_root,
      drive_id,
      "lateral.active",
      tier,
      [0, 20, 30, 40, 50],
      [False, True, False, True, False],
      kind="step",
    )
  _insert_chunk(
    database,
    archive_root,
    drive_id,
    "alert.event",
    "10s",
    [10, 20, 30, 40, 50],
    ["one", "two", "three", "four", "five"],
    kind="event",
  )

  response = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": "vehicle.brake_pressed,lateral.active",
      "start_us": 15,
      "end_us": 45,
      "max_points": 4,
    },
  )
  assert response.status_code == 200, response.text
  empty, active = response.json()["signals"]
  assert empty == {
    "signal": "vehicle.brake_pressed",
    "unit": None,
    "kind": "step",
    "points": [],
  }
  assert [(point["t_us"], point["value"]) for point in active["points"]] == [
    (0, False),
    (20, True),
    (30, False),
    (40, True),
  ]
  assert all(isinstance(point["value"], bool) for point in active["points"])
  assert len(active["points"]) <= 4

  bounded_step = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": "lateral.active",
      "start_us": 15,
      "end_us": 45,
      "max_points": 2,
    },
  )
  assert bounded_step.status_code == 200, bounded_step.text
  assert [(point["t_us"], point["value"]) for point in bounded_step.json()["signals"][0]["points"]] == [(0, False), (40, True)]

  bounded_events = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": "alert.event",
      "start_us": 0,
      "end_us": 60,
      "max_points": 2,
    },
  )
  assert bounded_events.status_code == 200, bounded_events.text
  event_series = bounded_events.json()["signals"][0]
  assert event_series["kind"] == "event"
  assert [(point["t_us"], point["value"]) for point in event_series["points"]] == [(10, "one"), (50, "five")]

  unknown = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series?signals=not.a.signal",
  )
  assert unknown.status_code == 422
  assert unknown.json()["error"] == {
    "code": "unknown_signals",
    "message": "One or more requested signals are not indexed",
    "details": {"signals": ["not.a.signal"]},
  }

  too_many = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": ",".join(f"signal.{index}" for index in range(65)),
    },
  )
  assert too_many.status_code == 422
  assert too_many.json()["error"]["code"] == "invalid_signals"

  too_long = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={"signals": "x" * 257},
  )
  assert too_long.status_code == 422
  assert too_long.json()["error"]["code"] == "invalid_signals"


def test_series_thins_coarsest_continuous_envelope_and_validates_data(
  telemetry_client: TestClient,
) -> None:
  _login(telemetry_client)
  database = telemetry_client.app.state.database
  archive_root = telemetry_client.app.state.settings.archive_root
  drive_id = _insert_index(database)
  _insert_chunk(
    database,
    archive_root,
    drive_id,
    "vehicle.speed",
    "10s",
    [0, 10, 20, 30, 40, 50, 60],
    [0, 1, 10, 2, -5, 1, 0],
    unit="m/s",
  )

  response = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": "vehicle.speed",
      "start_us": 0,
      "end_us": 60,
      "max_points": 4,
    },
  )
  assert response.status_code == 200, response.text
  points = response.json()["signals"][0]["points"]
  assert len(points) == 4
  assert {point["value"] for point in points} == {0.0, 10.0, -5.0}

  invalid_drive_id = _insert_index(database)
  _insert_chunk(
    database,
    archive_root,
    invalid_drive_id,
    "vehicle.speed",
    "full",
    [0, 10],
    [1],
    unit="m/s",
  )
  invalid = telemetry_client.get(
    f"/api/v1/drives/{invalid_drive_id}/series?signals=vehicle.speed",
  )
  assert invalid.status_code == 500
  assert invalid.json()["error"]["code"] == "telemetry_index_invalid"


def test_series_rejects_invalid_window(
  telemetry_client: TestClient,
) -> None:
  _login(telemetry_client)
  drive_id = _insert_index(telemetry_client.app.state.database)
  response = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": "vehicle.speed",
      "start_us": 20,
      "end_us": 10,
    },
  )
  assert response.status_code == 422
  assert response.json()["error"]["code"] == "invalid_time_range"

  out_of_range = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": "vehicle.speed",
      "start_us": 2**63,
    },
  )
  assert out_of_range.status_code == 422
  assert out_of_range.json()["error"]["code"] == "invalid_time_range"


def test_series_generation_pins_are_exact_and_fail_closed(
  telemetry_client: TestClient,
) -> None:
  _login(telemetry_client)
  database = telemetry_client.app.state.database
  drive_id = _insert_index(database)

  pinned = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": "vehicle.speed",
      "telemetry_sha256": "a" * 64,
      "timeline_version": "c" * 64,
    },
  )
  assert pinned.status_code == 200, pinned.text
  assert pinned.json()["ndjson_sha256"] == "a" * 64
  assert pinned.json()["timeline_version"] == "c" * 64

  for field in ("telemetry_sha256", "timeline_version"):
    stale = telemetry_client.get(
      f"/api/v1/drives/{drive_id}/series",
      params={
        "signals": "vehicle.speed",
        field: "0" * 64,
      },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "telemetry_generation_changed"

  uppercase = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": "vehicle.speed",
      "telemetry_sha256": "A" * 64,
    },
  )
  assert uppercase.status_code == 422


def test_series_partial_generation_is_generation_pinnable_and_provisional(
  telemetry_client: TestClient,
) -> None:
  _login(telemetry_client)
  drive_id = _insert_index(
    telemetry_client.app.state.database,
    state="partial",
  )

  unpinned = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={"signals": "vehicle.speed"},
  )
  assert unpinned.status_code == 200, unpinned.text
  assert unpinned.json()["ndjson_sha256"] == "a" * 64
  assert unpinned.json()["timeline_version"] == "c" * 64
  assert unpinned.json()["timeline_origin"] == "provisional"

  pinned = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": "vehicle.speed",
      "telemetry_sha256": "a" * 64,
      "timeline_version": "c" * 64,
    },
  )
  assert pinned.status_code == 200, pinned.text
  assert pinned.json()["timeline_origin"] == "provisional"

  changed = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": "vehicle.speed",
      "telemetry_sha256": "0" * 64,
      "timeline_version": "c" * 64,
    },
  )
  assert changed.status_code == 409
  assert changed.json()["error"]["code"] == "telemetry_generation_changed"


def test_series_includes_persisted_gap_markers_and_discloses_truncation(
  telemetry_client: TestClient,
) -> None:
  _login(telemetry_client)
  database = telemetry_client.app.state.database
  drive_id = _insert_index(database)
  _insert_marker(
    database,
    drive_id,
    "m00000001",
    kind="telemetry_gap",
    start_us=10,
    end_us=20,
    attributes={"gap_us": 10, "segment_num": 0},
  )
  _insert_marker(
    database,
    drive_id,
    "m00000002",
    kind="camera_gap",
    start_us=30,
    end_us=30,
    label="road camera index anomaly",
    attributes={"camera": "road"},
    point_end_is_null=True,
  )

  response = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": "vehicle.speed",
      "start_us": 0,
      "end_us": 40,
      "max_markers": 1,
    },
  )

  assert response.status_code == 200, response.text
  payload = response.json()
  assert payload["markers_truncated"] is True
  assert payload["markers"] == [
    {
      "id": "m00000001",
      "kind": "telemetry_gap",
      "start_t_us": 10,
      "end_t_us": 20,
      "severity": "warning",
      "label": "Telemetry gap",
      "attributes": {"gap_us": 10, "segment_num": 0},
    }
  ]

  complete = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": "vehicle.speed",
      "start_us": 0,
      "end_us": 40,
      "max_markers": 2,
    },
  )
  assert complete.status_code == 200, complete.text
  assert complete.json()["markers_truncated"] is False
  assert complete.json()["markers"][1]["start_t_us"] == 30
  assert complete.json()["markers"][1]["end_t_us"] == 30


@pytest.mark.parametrize(
  "manifest",
  [
    {
      "state": "complete",
      "publication_ready": False,
      "timeline_version": "c" * 64,
    },
    {
      "state": "complete",
      "publication_ready": True,
      "timeline_version": "C" * 64,
    },
    {
      "state": "partial",
      "publication_ready": True,
      "timeline_version": "c" * 64,
    },
  ],
)
def test_series_rejects_invalid_generation_manifest(
  telemetry_client: TestClient,
  manifest: dict[str, object],
) -> None:
  _login(telemetry_client)
  drive_id = _insert_index(
    telemetry_client.app.state.database,
    manifest=manifest,
  )

  response = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={"signals": "vehicle.speed"},
  )
  assert response.status_code == 409
  assert response.json()["error"]["code"] == "telemetry_not_ready"


def test_series_reads_signed_referenced_records_and_rejects_tampering(
  telemetry_client: TestClient,
) -> None:
  _login(telemetry_client)
  database = telemetry_client.app.state.database
  archive_root = telemetry_client.app.state.settings.archive_root
  drive_id = _insert_index(database)
  _insert_chunk(
    database,
    archive_root,
    drive_id,
    "vehicle.speed",
    "full",
    [-20, -10, 0, 10],
    [1, 2, 3, 4],
    unit="m/s",
  )

  signed = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={
      "signals": "vehicle.speed",
      "start_us": -15,
      "end_us": 5,
      "max_points": 4,
    },
  )
  assert signed.status_code == 200, signed.text
  assert [(point["t_us"], point["value"]) for point in signed.json()["signals"][0]["points"]] == [
    (-20, 1.0),
    (-10, 2.0),
    (0, 3.0),
    (10, 4.0),
  ]

  database.execute(
    """
    UPDATE telemetry_series_chunks
    SET record_sha256 = ?
    WHERE drive_id = ?
    """,
    ("0" * 64, drive_id),
  )
  tampered = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={"signals": "vehicle.speed"},
  )
  assert tampered.status_code == 500
  assert tampered.json()["error"]["code"] == "telemetry_index_invalid"

  database.execute(
    """
    UPDATE telemetry_indexes
    SET ndjson_path = '../outside.ndjson'
    WHERE drive_id = ?
    """,
    (drive_id,),
  )
  database.execute(
    """
    UPDATE telemetry_series_chunks
    SET ndjson_path = '../outside.ndjson'
    WHERE drive_id = ?
    """,
    (drive_id,),
  )
  escaped = telemetry_client.get(
    f"/api/v1/drives/{drive_id}/series",
    params={"signals": "vehicle.speed"},
  )
  assert escaped.status_code == 500
  assert escaped.json()["error"]["code"] == "telemetry_index_invalid"
