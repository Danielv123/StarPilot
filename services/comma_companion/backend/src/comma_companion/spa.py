from __future__ import annotations

import mimetypes
import os
import re
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


SPA_CONTENT_SECURITY_POLICY = "; ".join((
  "default-src 'self'",
  "base-uri 'none'",
  "connect-src 'self'",
  "font-src 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
  "frame-src 'none'",
  "img-src 'self' data:",
  "media-src 'self' blob:",
  "object-src 'none'",
  "script-src 'self'",
  "style-src 'self'",
  "style-src-attr 'unsafe-inline'",
  "worker-src 'self' blob:",
))

_HASHED_VITE_ASSET = re.compile(
  r".+-[A-Za-z0-9_-]{8}(?:\.[^./]+)+\Z",
)
_CONTENT_TYPES = {
  ".avif": "image/avif",
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json",
  ".map": "application/json",
  ".mjs": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml",
  ".wasm": "application/wasm",
  ".webmanifest": "application/manifest+json",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
}


def _is_api_path(path: str) -> bool:
  normalized = f"/{path.lstrip('/')}"
  return normalized == "/api" or normalized.startswith("/api/")


def _content_type(path: Path) -> str:
  explicit = _CONTENT_TYPES.get(path.suffix.lower())
  if explicit:
    return explicit
  guessed, _ = mimetypes.guess_type(path.name)
  return guessed or "application/octet-stream"


class SPAStaticFiles(StaticFiles):
  def __init__(self, directory: Path) -> None:
    self._dist_dir = directory.resolve()
    super().__init__(directory=self._dist_dir, check_dir=True)

  def _is_hashed_asset(self, path: Path) -> bool:
    try:
      relative = path.resolve().relative_to(self._dist_dir)
    except ValueError:
      return False
    return (
      len(relative.parts) >= 2
      and relative.parts[0] == "assets"
      and _HASHED_VITE_ASSET.fullmatch(relative.name) is not None
    )

  def file_response(
    self,
    full_path: os.PathLike[str],
    stat_result: os.stat_result,
    scope: Scope,
    status_code: int = 200,
  ) -> Response:
    response = super().file_response(
      full_path,
      stat_result,
      scope,
      status_code,
    )
    path = Path(full_path)
    if response.status_code != 304:
      response.headers["Content-Type"] = _content_type(path)
    response.headers["Cache-Control"] = (
      "public, max-age=31536000, immutable"
      if self._is_hashed_asset(path)
      else "no-cache"
    )
    response.headers["Content-Security-Policy"] = SPA_CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

  async def get_response(self, path: str, scope: Scope) -> Response:
    if _is_api_path(scope["path"]):
      raise HTTPException(status_code=404)
    try:
      return await super().get_response(path, scope)
    except HTTPException as exc:
      if exc.status_code != 404 or scope["method"] not in {"GET", "HEAD"}:
        raise
    return await super().get_response("index.html", scope)


def mount_spa(application: FastAPI, dist_dir: str | Path) -> None:
  resolved = Path(dist_dir).resolve()
  index_path = resolved / "index.html"
  if not index_path.is_file():
    raise FileNotFoundError(
      f"Vite build is missing its index: {index_path}",
    )
  application.mount(
    "/",
    SPAStaticFiles(resolved),
    name="spa",
  )
