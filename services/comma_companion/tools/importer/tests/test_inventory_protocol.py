from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from comma_companion_importer.inventory import manifest_sha256
from comma_companion_importer.protocol import UploadProtocol


class InventoryHandler(BaseHTTPRequestHandler):
  state: dict[str, Any] = {}

  def log_message(self, _format: str, *args: object) -> None:
    pass

  def _response(self, status: int, body: dict[str, Any]) -> None:
    encoded = json.dumps(body, separators=(",", ":")).encode()
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(encoded)))
    self.end_headers()
    self.wfile.write(encoded)

  def do_GET(self) -> None:
    parsed = urlsplit(self.path)
    assert parsed.path == "/api/v1/route-inventories/latest"
    assert parse_qs(parsed.query) == {
      "device_id": ["device"],
      "route_name": ["route"],
    }
    assert self.headers["Authorization"] == "Bearer importer-token"
    latest = self.state.get("latest")
    if latest is None:
      self._response(
        404,
        {
          "error": {
            "code": "route_inventory_not_found",
            "message": "No route inventory has been accepted",
            "details": {},
          },
        },
      )
      return
    self._response(200, latest)

  def do_POST(self) -> None:
    assert self.path == "/api/v1/route-inventories"
    assert self.headers["Authorization"] == "Bearer importer-token"
    length = int(self.headers["Content-Length"])
    raw = self.rfile.read(length)
    body = json.loads(raw)
    digest = body["manifest_sha256"]
    assert self.headers["Idempotency-Key"] == (f"route-inventory:{digest}")
    assert self.headers["Content-Type"] == "application/json"
    assert manifest_sha256(body["manifest"]) == digest
    self.state["raw"] = raw
    self.state["latest"] = {
      "device_id": body["device_id"],
      "route_name": body["manifest"]["route_name"],
      "generation": body["manifest"]["generation"],
      "manifest_sha256": digest,
      "manifest": body["manifest"],
    }
    self._response(
      201,
      {
        "generation": body["manifest"]["generation"],
        "manifest_sha256": digest,
        "state": "accepted",
      },
    )


def test_exact_route_inventory_latest_and_declaration_contract() -> None:
  InventoryHandler.state = {}
  server = ThreadingHTTPServer(("127.0.0.1", 0), InventoryHandler)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  try:
    protocol = UploadProtocol(
      f"http://127.0.0.1:{server.server_port}",
      "importer-token",
      max_retries=0,
    )
    assert protocol.latest_route_inventory("device", "route") is None
    manifest = {
      "closed_at": "2026-07-29T00:00:00Z",
      "generation": 1,
      "previous_manifest_sha256": None,
      "route_name": "route",
      "special": "<&>\u2028\u2029",
    }
    digest = manifest_sha256(manifest)
    accepted = protocol.declare_route_inventory(
      device_id="device",
      manifest=manifest,
      manifest_sha256=digest,
    )
    assert accepted.generation == 1
    assert accepted.manifest_sha256 == digest
    assert accepted.state == "accepted"
    assert InventoryHandler.state["raw"] == (
      b'{"device_id":"device","manifest":'
      + b'{"closed_at":"2026-07-29T00:00:00Z","generation":1,'
      + b'"previous_manifest_sha256":null,"route_name":"route",'
      + b'"special":"\\u003c\\u0026\\u003e\\u2028\\u2029"},'
      + b'"manifest_sha256":"'
      + digest.encode()
      + b'"}'
    )
    latest = protocol.latest_route_inventory("device", "route")
    assert latest is not None
    assert latest.manifest_sha256 == digest
    assert latest.manifest == manifest
  finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
