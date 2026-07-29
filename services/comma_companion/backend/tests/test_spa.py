from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from comma_companion.spa import mount_spa


INDEX = """<!doctype html>
<html>
  <body><div id="root">Comma Companion</div></body>
</html>
"""


def _dist(tmp_path: Path) -> Path:
  dist = tmp_path / "dist"
  assets = dist / "assets"
  assets.mkdir(parents=True)
  (dist / "index.html").write_bytes(INDEX.encode())
  (dist / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
  (assets / "index-CNg2hcNf.js").write_text(
    "document.body.dataset.ready = 'true'",
    encoding="utf-8",
  )
  (assets / "index-V769e4fO.css").write_text(
    "body { color: white; }",
    encoding="utf-8",
  )
  (assets / "runtime-CNg2hcNf.wasm").write_bytes(b"\0asm")
  return dist


def _client(dist: Path) -> TestClient:
  application = FastAPI()

  @application.get("/api/v1/ping")
  def ping() -> dict[str, bool]:
    return {"ok": True}

  mount_spa(application, dist)
  return TestClient(application)


def test_serves_index_and_history_routes_without_caching(
  tmp_path: Path,
) -> None:
  with _client(_dist(tmp_path)) as client:
    for path in ("/", "/drives/route-id", "/settings/advanced"):
      response = client.get(path)
      assert response.status_code == 200
      assert response.text == INDEX
      assert response.headers["cache-control"] == "no-cache"
      assert response.headers["content-type"] == "text/html; charset=utf-8"


def test_serves_hashed_vite_assets_immutably_with_correct_types(
  tmp_path: Path,
) -> None:
  with _client(_dist(tmp_path)) as client:
    javascript = client.get("/assets/index-CNg2hcNf.js")
    stylesheet = client.get("/assets/index-V769e4fO.css")
    wasm = client.get("/assets/runtime-CNg2hcNf.wasm")
    favicon = client.get("/favicon.svg")

  immutable = "public, max-age=31536000, immutable"
  assert javascript.headers["cache-control"] == immutable
  assert javascript.headers["content-type"] == "text/javascript; charset=utf-8"
  assert stylesheet.headers["cache-control"] == immutable
  assert stylesheet.headers["content-type"] == "text/css; charset=utf-8"
  assert wasm.headers["cache-control"] == immutable
  assert wasm.headers["content-type"] == "application/wasm"
  assert favicon.headers["cache-control"] == "no-cache"
  assert favicon.headers["content-type"] == "image/svg+xml"


def test_spa_csp_supports_built_app_without_unsafe_eval(
  tmp_path: Path,
) -> None:
  with _client(_dist(tmp_path)) as client:
    response = client.get("/")

  policy = response.headers["content-security-policy"]
  assert "script-src 'self'" in policy
  assert "connect-src 'self'" in policy
  assert "media-src 'self' blob:" in policy
  assert "style-src-attr 'unsafe-inline'" in policy
  assert "frame-ancestors 'none'" in policy
  assert "'unsafe-eval'" not in policy


def test_api_routes_and_api_404s_are_never_replaced_by_index(
  tmp_path: Path,
) -> None:
  with _client(_dist(tmp_path)) as client:
    existing = client.get("/api/v1/ping")
    missing = client.get("/api/v1/not-a-route")

  assert existing.status_code == 200
  assert existing.json() == {"ok": True}
  assert missing.status_code == 404
  assert "Comma Companion" not in missing.text
  assert "text/html" not in missing.headers.get("content-type", "")


def test_head_uses_history_fallback_without_a_body(tmp_path: Path) -> None:
  with _client(_dist(tmp_path)) as client:
    response = client.head("/drives/route-id")

  assert response.status_code == 200
  assert response.content == b""
  assert response.headers["cache-control"] == "no-cache"
  assert response.headers["content-type"] == "text/html; charset=utf-8"


def test_mount_requires_a_vite_index(tmp_path: Path) -> None:
  empty_dist = tmp_path / "dist"
  empty_dist.mkdir()

  with pytest.raises(FileNotFoundError, match="index.html"):
    mount_spa(FastAPI(), empty_dist)
