from __future__ import annotations

import hashlib
import json
import threading
import base64
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from comma_companion_importer.importer import Importer, ImportOptions
from comma_companion_importer.inventory import (
  manifest_sha256,
  parse_inventory_config,
)
from comma_companion_importer.manifest import Manifest
from comma_companion_importer.protocol import (
  ApiError,
  LatestRouteInventory,
  RouteInventoryAcceptance,
  UploadProtocol,
  UploadSession,
)
from comma_companion_importer.scanner import ScanFilter, scan


def test_server_url_is_normalized_and_plaintext_is_loopback_only() -> None:
  protocol = UploadProtocol("https://Comma.Example:443/api/v1/", "token", max_retries=0)
  assert protocol.server_url == "https://comma.example/api/v1"

  with pytest.raises(ValueError, match="must use HTTPS"):
    UploadProtocol("http://comma.example", "token")


class UploadHandler(BaseHTTPRequestHandler):
  state: dict[str, Any] = {}

  def log_message(self, _format: str, *args: object) -> None:
    pass

  def _headers(self, status: int, body: bytes = b"") -> None:
    self.send_response(status)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Content-Type", "application/json")
    self.end_headers()
    if body:
      self.wfile.write(body)

  def do_POST(self) -> None:
    assert self.path == "/api/v1/uploads"
    assert self.headers["Authorization"] == "Bearer importer-token"
    length = int(self.headers["Content-Length"])
    declaration = json.loads(self.rfile.read(length))
    self.state["declaration"] = declaration
    self.state["post_count"] += 1
    response = json.dumps(
      {
        "upload_id": "upload-1",
        "offset": len(self.state["content"]),
        "length": declaration["size"],
        "state": "uploading",
      }
    ).encode()
    self._headers(201, response)

  def do_HEAD(self) -> None:
    assert self.path == "/api/v1/uploads/upload-1"
    self.state["head_count"] += 1
    self.send_response(200)
    self.send_header("Upload-Offset", str(len(self.state["content"])))
    self.send_header("Upload-Length", str(self.state["length"]))
    state = "complete" if len(self.state["content"]) == self.state["length"] else "uploading"
    self.send_header("Upload-State", state)
    self.send_header("Content-Length", "0")
    self.end_headers()

  def do_PATCH(self) -> None:
    assert self.path == "/api/v1/uploads/upload-1"
    offset = int(self.headers["Upload-Offset"])
    if offset != len(self.state["content"]):
      self._headers(409, b'{"detail":"offset mismatch"}')
      return
    length = int(self.headers["Content-Length"])
    chunk = self.rfile.read(length)
    expected_checksum = base64.b64encode(hashlib.sha256(chunk).digest()).decode("ascii")
    assert self.headers["Upload-Checksum"] == f"sha256 {expected_checksum}"
    self.state["content"].extend(chunk)
    if self.state.get("conflict_once"):
      self.state["conflict_once"] = False
      self._headers(409, b'{"detail":"simulated lost PATCH response"}')
      return
    self.send_response(204)
    self.send_header("Upload-Offset", str(len(self.state["content"])))
    self.send_header("Upload-Length", str(self.state["length"]))
    self.send_header(
      "Upload-State",
      "complete" if len(self.state["content"]) == self.state["length"] else "uploading",
    )
    self.send_header("Content-Length", "0")
    self.end_headers()


def test_nonstream_upload_uses_agent_artifact_identity(
  tmp_path: Path,
) -> None:
  source = tmp_path / "device" / "realdata" / "route--0" / "route_metadata.json"
  source.parent.mkdir(parents=True)
  source.write_bytes(b"metadata")
  artifact = scan(tmp_path, ScanFilter())[0]
  assert artifact.artifact_type == "metadata"
  UploadHandler.state = {
    "content": bytearray(),
    "length": artifact.size,
    "declaration": None,
    "post_count": 0,
    "head_count": 0,
  }
  server = ThreadingHTTPServer(("127.0.0.1", 0), UploadHandler)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  try:
    protocol = UploadProtocol(
      f"http://127.0.0.1:{server.server_port}",
      "importer-token",
      max_retries=0,
    )
    protocol.create(
      artifact,
      hashlib.sha256(b"metadata").hexdigest(),
    )
  finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
  declaration = UploadHandler.state["declaration"]
  assert declaration["artifact_type"] == "artifact"
  assert declaration["camera"] is None


def test_resumes_server_offset_and_persists_completion(tmp_path: Path) -> None:
  payload = b"abcdefghijklmnopqrstuvwxyz"
  source = tmp_path / "device" / "realdata" / "route--4" / "rlog.zst"
  source.parent.mkdir(parents=True)
  source.write_bytes(payload)
  artifact = scan(tmp_path, ScanFilter())[0]

  UploadHandler.state = {
    "content": bytearray(payload[:5]),
    "length": len(payload),
    "declaration": None,
    "post_count": 0,
    "head_count": 0,
    "conflict_once": True,
  }
  server = ThreadingHTTPServer(("127.0.0.1", 0), UploadHandler)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  try:
    protocol = UploadProtocol(
      f"http://127.0.0.1:{server.server_port}",
      "importer-token",
      max_retries=0,
    )
    output = StringIO()
    manifest_path = tmp_path / "manifest.sqlite3"
    with Manifest(manifest_path) as manifest:
      reporter = Importer(
        protocol,
        manifest,
        ImportOptions(concurrency=1, chunk_size=4, retries=0),
        progress_stream=output,
      ).run([artifact])
      entry = manifest.get(source)
      assert entry.status == "complete"
      assert entry.offset == len(payload)
      assert entry.sha256 == hashlib.sha256(payload).hexdigest()

    assert bytes(UploadHandler.state["content"]) == payload
    declaration = UploadHandler.state["declaration"]
    assert declaration == {
      "device_id": "device",
      "route_name": "route",
      "segment_number": 4,
      "artifact_type": "rlog",
      "camera": None,
      "relative_path": "realdata/route--4/rlog.zst",
      "size": len(payload),
      "mtime_ns": artifact.mtime_ns,
      "mtime": artifact.recorded_at.isoformat(),
      "sha256": hashlib.sha256(payload).hexdigest(),
      "completion_evidence": ["historical_import"],
      "partial": False,
    }
    assert reporter.sent_bytes == len(payload) - 5
    assert reporter.failure_count == 0

    with Manifest(manifest_path) as manifest:
      heads_before = UploadHandler.state["head_count"]
      second = Importer(
        protocol,
        manifest,
        ImportOptions(concurrency=1, chunk_size=4, retries=0),
        progress_stream=StringIO(),
      ).run([artifact])
    assert second.sent_bytes == 0
    assert UploadHandler.state["post_count"] == 1
    assert UploadHandler.state["head_count"] > heads_before
  finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


class InventoryProtocolStub:
  def __init__(self) -> None:
    self.server_url = "https://comma.example"
    self.latest: LatestRouteInventory | None = None
    self.sessions: dict[str, UploadSession] = {}
    self.declarations: list[tuple[str, dict[str, Any], str]] = []
    self.attempted_inventory_digests: list[str] = []
    self.fail_declare_once = False

  def create(
    self,
    artifact: Any,
    _sha256: str,
    _generation: int = 0,
  ) -> UploadSession:
    upload_id = hashlib.sha256(
      artifact.relative_path.encode(),
    ).hexdigest()
    session = UploadSession(
      upload_id,
      artifact.size,
      artifact.size,
      "complete",
    )
    self.sessions[upload_id] = session
    return session

  def head(self, upload_id: str) -> UploadSession:
    return self.sessions[upload_id]

  def patch(
    self,
    _upload_id: str,
    _offset: int,
    _length: int,
    _chunk: bytes,
  ) -> UploadSession:
    raise AssertionError("complete stub uploads must not be patched")

  def latest_route_inventory(
    self,
    _device_id: str,
    _route_name: str,
  ) -> LatestRouteInventory | None:
    return self.latest

  def declare_route_inventory(
    self,
    *,
    device_id: str,
    manifest: dict[str, Any],
    manifest_sha256: str,
  ) -> RouteInventoryAcceptance:
    self.attempted_inventory_digests.append(manifest_sha256)
    if self.fail_declare_once:
      self.fail_declare_once = False
      raise ApiError(None, "simulated declaration transport failure")
    self.declarations.append(
      (device_id, copy.deepcopy(manifest), manifest_sha256),
    )
    self.latest = LatestRouteInventory(
      device_id=device_id,
      route_name=manifest["route_name"],
      generation=manifest["generation"],
      manifest_sha256=manifest_sha256,
      manifest=copy.deepcopy(manifest),
    )
    return RouteInventoryAcceptance(
      generation=manifest["generation"],
      manifest_sha256=manifest_sha256,
      state="accepted",
    )


def _rlog_inventory_config() -> Any:
  return parse_inventory_config(
    {
      "inventory": {
        "expected_streams": [
          {
            "root_name": "realdata",
            "artifact_type": "rlog",
            "camera": "",
          },
        ],
      },
    },
  )


def test_inventory_is_persisted_and_unchanged_rerun_reuses_server_head(
  tmp_path: Path,
) -> None:
  source = tmp_path / "device" / "realdata" / "route--0" / "rlog.zst"
  source.parent.mkdir(parents=True)
  source.write_bytes(b"route-log")
  artifact = scan(tmp_path, ScanFilter())[0]
  protocol = InventoryProtocolStub()
  manifest_path = tmp_path / "manifest.sqlite3"

  with Manifest(manifest_path) as manifest:
    first = Importer(
      protocol,  # type: ignore[arg-type]
      manifest,
      ImportOptions(concurrency=1),
      inventory_config=_rlog_inventory_config(),
      progress_stream=StringIO(),
    ).run([artifact])
  assert first.failure_count == 0
  assert first.inventory_declared_count == 1
  assert len(protocol.declarations) == 1
  first_digest = protocol.declarations[0][2]
  first_closed_at = protocol.declarations[0][1]["closed_at"]

  with Manifest(manifest_path) as manifest:
    second = Importer(
      protocol,  # type: ignore[arg-type]
      manifest,
      ImportOptions(concurrency=1),
      inventory_config=_rlog_inventory_config(),
      progress_stream=StringIO(),
    ).run([artifact])
    accepted = manifest.accepted_inventory(
      server_scope=protocol.server_url,
      device_id="device",
      route_name="route",
      manifest_sha256=first_digest,
    )
  assert second.failure_count == 0
  assert second.inventory_reused_count == 1
  assert len(protocol.declarations) == 1
  assert protocol.latest is not None
  assert protocol.latest.manifest["closed_at"] == first_closed_at
  assert accepted is not None


def test_inventory_hashes_filtered_files_without_uploading_them(
  tmp_path: Path,
) -> None:
  root = tmp_path / "device" / "realdata"
  rlog = root / "route--0" / "rlog"
  filtered_video = root / "route--3" / "dcamera.hevc"
  rlog.parent.mkdir(parents=True)
  filtered_video.parent.mkdir(parents=True)
  rlog.write_bytes(b"log")
  filtered_video.write_bytes(b"camera")
  all_artifacts = scan(tmp_path, ScanFilter())
  selected = [artifact for artifact in all_artifacts if artifact.artifact_type == "rlog"]
  video_artifact = next(artifact for artifact in all_artifacts if artifact.artifact_type == "video")
  protocol = InventoryProtocolStub()
  with Manifest(tmp_path / "manifest.sqlite3") as manifest:
    reporter = Importer(
      protocol,  # type: ignore[arg-type]
      manifest,
      ImportOptions(concurrency=1),
      inventory_config=_rlog_inventory_config(),
      inventory_artifacts=all_artifacts,
      progress_stream=StringIO(),
    ).run(selected)
    video_entry = manifest.get(video_artifact.source_path)
  assert reporter.failure_count == 0
  assert reporter.total_files == 1
  assert len(protocol.sessions) == 1
  assert video_entry.sha256 == hashlib.sha256(b"camera").hexdigest()
  inventory = protocol.declarations[0][1]
  assert [segment["number"] for segment in inventory["segments"]] == [
    0,
    3,
  ]
  assert inventory["missing_segment_numbers"] == [1, 2]
  assert any(file["relative_path"] == video_artifact.relative_path for segment in inventory["segments"] for file in segment["files"])


def test_pending_inventory_retry_replays_exact_immutable_manifest(
  tmp_path: Path,
) -> None:
  source = tmp_path / "device" / "realdata" / "route--0" / "rlog"
  source.parent.mkdir(parents=True)
  source.write_bytes(b"log")
  artifact = scan(tmp_path, ScanFilter())[0]
  protocol = InventoryProtocolStub()
  protocol.fail_declare_once = True
  manifest_path = tmp_path / "manifest.sqlite3"

  with Manifest(manifest_path) as manifest:
    failed = Importer(
      protocol,  # type: ignore[arg-type]
      manifest,
      ImportOptions(concurrency=1),
      inventory_config=_rlog_inventory_config(),
      progress_stream=StringIO(),
    ).run([artifact])
  assert failed.inventory_failure_count == 1
  assert len(protocol.attempted_inventory_digests) == 1

  with Manifest(manifest_path) as manifest:
    retried = Importer(
      protocol,  # type: ignore[arg-type]
      manifest,
      ImportOptions(concurrency=1),
      inventory_config=_rlog_inventory_config(),
      progress_stream=StringIO(),
    ).run([artifact])
  assert retried.failure_count == 0
  assert retried.inventory_declared_count == 1
  assert protocol.attempted_inventory_digests == [
    protocol.attempted_inventory_digests[0],
    protocol.attempted_inventory_digests[0],
  ]


def test_unknown_different_server_head_requires_exact_supersede_lease(
  tmp_path: Path,
) -> None:
  source = tmp_path / "device" / "realdata" / "route--0" / "rlog"
  source.parent.mkdir(parents=True)
  source.write_bytes(b"log")
  artifact = scan(tmp_path, ScanFilter())[0]
  protocol = InventoryProtocolStub()

  seed_manifest_path = tmp_path / "seed.sqlite3"
  with Manifest(seed_manifest_path) as manifest:
    seeded = Importer(
      protocol,  # type: ignore[arg-type]
      manifest,
      ImportOptions(concurrency=1),
      inventory_config=_rlog_inventory_config(),
      progress_stream=StringIO(),
    ).run([artifact])
  assert seeded.failure_count == 0
  assert protocol.latest is not None
  different = copy.deepcopy(protocol.latest.manifest)
  different["capability_source"] = "configured+route_union"
  different["closure_evidence"] = sorted(
    set(different["closure_evidence"]) | {"missing_expected_streams"},
  )
  different["expected_streams"].append(
    {
      "artifact_type": "video",
      "camera": "road",
      "role": "realdata|video|road",
      "root_name": "realdata",
    },
  )
  different["segments"][0]["streams"].append(
    {
      "mtime_ns": None,
      "relative_path": None,
      "role": "realdata|video|road",
      "sha256": None,
      "size": None,
      "status": "missing",
    },
  )
  different["state"] = "partial"
  different["generation"] = 7
  different["previous_manifest_sha256"] = "c" * 64
  head_digest = manifest_sha256(different)
  protocol.latest = LatestRouteInventory(
    device_id="device",
    route_name="route",
    generation=7,
    manifest_sha256=head_digest,
    manifest=different,
  )
  protocol.declarations.clear()
  unknown_manifest_path = tmp_path / "unknown.sqlite3"

  with Manifest(unknown_manifest_path) as manifest:
    refused = Importer(
      protocol,  # type: ignore[arg-type]
      manifest,
      ImportOptions(concurrency=1),
      inventory_config=_rlog_inventory_config(),
      progress_stream=StringIO(),
    ).run([artifact])
  assert refused.inventory_failure_count == 1
  assert protocol.declarations == []

  with Manifest(unknown_manifest_path) as manifest:
    accepted = Importer(
      protocol,  # type: ignore[arg-type]
      manifest,
      ImportOptions(concurrency=1),
      inventory_config=_rlog_inventory_config(),
      inventory_supersede_heads={
        ("device", "route"): head_digest,
      },
      progress_stream=StringIO(),
    ).run([artifact])
  assert accepted.failure_count == 0
  assert accepted.inventory_declared_count == 1
  assert protocol.declarations[0][1]["generation"] == 8
  assert protocol.declarations[0][1]["previous_manifest_sha256"] == head_digest
