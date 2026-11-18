from __future__ import annotations

import hashlib
import json
import base64
import random
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from comma_companion_importer import __version__
from comma_companion_importer.inventory import (
  canonical_json,
  inventory_artifact_type,
)
from comma_companion_importer.scanner import Artifact


RETRYABLE_STATUS = {408, 425, 429, 460, 500, 502, 503, 504}
COMPLETE_STATES = {"complete", "completed", "durable", "verified"}


def _origin(url: str) -> tuple[str, str | None, int | None]:
  parsed = urlsplit(url)
  default_port = 443 if parsed.scheme == "https" else 80
  return parsed.scheme, parsed.hostname, parsed.port or default_port


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
  def redirect_request(
    self,
    request: urllib.request.Request,
    file_pointer: Any,
    code: int,
    message: str,
    headers: Any,
    new_url: str,
  ) -> urllib.request.Request | None:
    if _origin(request.full_url) != _origin(new_url):
      raise urllib.error.HTTPError(
        request.full_url,
        code,
        "cross-origin redirect refused",
        headers,
        file_pointer,
      )
    return super().redirect_request(request, file_pointer, code, message, headers, new_url)


class ApiError(RuntimeError):
  def __init__(
    self,
    status: int | None,
    message: str,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
  ):
    super().__init__(message)
    self.status = status
    self.headers = headers or {}
    self.body = body or {}


@dataclass(frozen=True, slots=True)
class UploadSession:
  upload_id: str
  offset: int
  length: int
  state: str

  @property
  def complete(self) -> bool:
    return self.state.lower() in COMPLETE_STATES


@dataclass(frozen=True, slots=True)
class LatestRouteInventory:
  device_id: str
  route_name: str
  generation: int
  manifest_sha256: str
  manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RouteInventoryAcceptance:
  generation: int
  manifest_sha256: str
  state: str


class UploadProtocol:
  def __init__(
    self,
    server_url: str,
    token: str,
    *,
    timeout: float = 60,
    max_retries: int = 5,
    ca_file: Path | None = None,
    sleep: Any = time.sleep,
  ):
    parsed = urlsplit(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
      raise ValueError("--server must be an absolute http(s) URL")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
      raise ValueError("--server must use HTTPS except for a loopback address")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
      raise ValueError("--server cannot contain credentials, a query, or a fragment")
    try:
      parsed_port = parsed.port
    except ValueError as error:
      raise ValueError("--server contains an invalid port") from error
    hostname = (parsed.hostname or "").lower()
    hostname = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    port = f":{parsed_port}" if parsed_port is not None and parsed_port != default_port else ""
    self.server_url = urlunsplit((parsed.scheme.lower(), f"{hostname}{port}", parsed.path.rstrip("/"), "", ""))
    self.token = token
    self.timeout = timeout
    self.max_retries = max_retries
    self.sleep = sleep
    self.ssl_context = ssl.create_default_context(cafile=str(ca_file)) if ca_file is not None else None
    self.opener = urllib.request.build_opener(
      urllib.request.HTTPSHandler(context=self.ssl_context),
      SameOriginRedirectHandler(),
    )

  def _url(self, endpoint: str) -> str:
    if self.server_url.endswith("/api/v1"):
      return f"{self.server_url}{endpoint.removeprefix('/api/v1')}"
    return f"{self.server_url}{endpoint}"

  @staticmethod
  def _response_state(headers: dict[str, str], body: dict[str, Any]) -> str:
    value = body.get("state", body.get("status", headers.get("upload-state", "")))
    if not value and headers.get("upload-complete", "").lower() in {"1", "true", "yes"}:
      return "complete"
    return str(value)

  @staticmethod
  def _header_dict(headers: Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}

  @staticmethod
  def _json_body(raw: bytes) -> dict[str, Any]:
    if not raw:
      return {}
    try:
      value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
      raise ApiError(None, "server returned an invalid JSON response") from error
    if not isinstance(value, dict):
      raise ApiError(None, "server returned a non-object JSON response")
    return value

  def _backoff(self, attempt: int, headers: dict[str, str]) -> None:
    retry_after = headers.get("retry-after")
    if retry_after:
      try:
        delay = min(float(retry_after), 60.0)
      except ValueError:
        delay = 0.0
    else:
      delay = min(0.5 * (2**attempt), 30.0) + random.uniform(0.0, 0.25)
    self.sleep(delay)

  def _request(
    self,
    method: str,
    endpoint: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    retry: bool = True,
  ) -> tuple[dict[str, str], bytes]:
    request_headers = {
      "Authorization": f"Bearer {self.token}",
      "User-Agent": f"comma-companion-importer/{__version__}",
      **(headers or {}),
    }
    attempts = self.max_retries + 1 if retry else 1
    for attempt in range(attempts):
      request = urllib.request.Request(
        self._url(endpoint),
        data=data,
        headers=request_headers,
        method=method,
      )
      try:
        with self.opener.open(request, timeout=self.timeout) as response:
          return self._header_dict(response.headers), response.read()
      except urllib.error.HTTPError as error:
        response_headers = self._header_dict(error.headers)
        raw = error.read(8192)
        detail = raw.decode("utf-8", errors="replace").strip()
        parsed_response: dict[str, Any] | None = None
        message = f"server returned HTTP {error.code}"
        if detail:
          try:
            parsed_detail = json.loads(detail)
            if isinstance(parsed_detail, dict):
              parsed_response = parsed_detail
              error_document = parsed_detail.get("error")
              if isinstance(error_document, dict):
                detail = str(error_document.get("message", detail))
              else:
                detail = str(
                  parsed_detail.get(
                    "detail",
                    parsed_detail.get("error", detail),
                  ),
                )
          except json.JSONDecodeError:
            pass
          message = f"{message}: {detail[:500]}"
        if retry and error.code in RETRYABLE_STATUS and attempt + 1 < attempts:
          self._backoff(attempt, response_headers)
          continue
        raise ApiError(
          error.code,
          message,
          response_headers,
          parsed_response,
        ) from error
      except (urllib.error.URLError, TimeoutError, OSError) as error:
        if retry and attempt + 1 < attempts:
          self._backoff(attempt, {})
          continue
        raise ApiError(None, f"request failed: {error}") from error
    raise AssertionError("unreachable")

  def create(self, artifact: Artifact, sha256: str, generation: int = 0) -> UploadSession:
    artifact_type = inventory_artifact_type(artifact.artifact_type)
    payload = {
      "device_id": artifact.device_id,
      "route_name": artifact.route_name,
      "segment_number": artifact.segment_number,
      "artifact_type": artifact_type,
      "camera": artifact.camera if artifact_type == "video" else None,
      "relative_path": artifact.relative_path,
      "size": artifact.size,
      "mtime_ns": artifact.mtime_ns,
      "mtime": artifact.recorded_at.isoformat(),
      "sha256": sha256,
      "completion_evidence": ["historical_import"],
      "partial": False,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    idempotency_material = encoded + b"\0" + str(generation).encode()
    idempotency_key = hashlib.sha256(idempotency_material).hexdigest()
    headers, raw = self._request(
      "POST",
      "/api/v1/uploads",
      data=encoded,
      headers={
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
      },
    )
    body = self._json_body(raw)
    upload_id = body.get("upload_id", body.get("id"))
    if not isinstance(upload_id, str) or not upload_id:
      location = headers.get("location", "").rstrip("/")
      upload_id = location.rsplit("/", 1)[-1] if location else ""
    if not upload_id:
      raise ApiError(None, "upload declaration response did not contain an upload ID")
    offset = int(body.get("offset", headers.get("upload-offset", 0)))
    length = int(body.get("length", headers.get("upload-length", artifact.size)))
    return UploadSession(upload_id, offset, length, self._response_state(headers, body))

  def head(self, upload_id: str) -> UploadSession:
    headers, raw = self._request("HEAD", f"/api/v1/uploads/{upload_id}")
    body = self._json_body(raw)
    try:
      offset_value = body["offset"] if "offset" in body else headers["upload-offset"]
      length_value = body["length"] if "length" in body else headers["upload-length"]
      offset = int(offset_value)
      length = int(length_value)
    except (KeyError, TypeError, ValueError) as error:
      raise ApiError(None, "upload HEAD response omitted Upload-Offset or Upload-Length") from error
    return UploadSession(upload_id, offset, length, self._response_state(headers, body))

  def patch(self, upload_id: str, offset: int, length: int, chunk: bytes) -> UploadSession:
    chunk_checksum = base64.b64encode(hashlib.sha256(chunk).digest()).decode("ascii")
    headers, raw = self._request(
      "PATCH",
      f"/api/v1/uploads/{upload_id}",
      data=chunk,
      headers={
        "Content-Type": "application/offset+octet-stream",
        "Upload-Offset": str(offset),
        "Upload-Length": str(length),
        "Upload-Checksum": f"sha256 {chunk_checksum}",
        "Idempotency-Key": f"{upload_id}:{offset}",
      },
      retry=False,
    )
    body = self._json_body(raw)
    try:
      offset_value = body["offset"] if "offset" in body else headers["upload-offset"]
      new_offset = int(offset_value)
    except (KeyError, TypeError, ValueError) as error:
      raise ApiError(None, "upload PATCH response omitted Upload-Offset") from error
    response_length = int(body.get("length", headers.get("upload-length", length)))
    return UploadSession(upload_id, new_offset, response_length, self._response_state(headers, body))

  def latest_route_inventory(
    self,
    device_id: str,
    route_name: str,
  ) -> LatestRouteInventory | None:
    query = urlencode(
      {
        "device_id": device_id,
        "route_name": route_name,
      },
    )
    try:
      _headers, raw = self._request(
        "GET",
        f"/api/v1/route-inventories/latest?{query}",
      )
    except ApiError as error:
      if error.status == 404:
        return None
      raise
    body = self._json_body(raw)
    manifest = body.get("manifest")
    try:
      response_device = body["device_id"]
      response_route = body["route_name"]
      generation = int(body["generation"])
      digest = body["manifest_sha256"]
    except (KeyError, TypeError, ValueError) as error:
      raise ApiError(
        None,
        "latest route inventory response is incomplete",
      ) from error
    if (
      response_device != device_id
      or response_route != route_name
      or generation < 1
      or not isinstance(digest, str)
      or len(digest) != 64
      or not isinstance(manifest, dict)
    ):
      raise ApiError(
        None,
        "latest route inventory response does not match the request",
      )
    return LatestRouteInventory(
      device_id=device_id,
      route_name=route_name,
      generation=generation,
      manifest_sha256=digest,
      manifest=manifest,
    )

  def declare_route_inventory(
    self,
    *,
    device_id: str,
    manifest: dict[str, Any],
    manifest_sha256: str,
  ) -> RouteInventoryAcceptance:
    envelope = {
      "device_id": device_id,
      "manifest": manifest,
      "manifest_sha256": manifest_sha256,
    }
    encoded = canonical_json(envelope)
    _headers, raw = self._request(
      "POST",
      "/api/v1/route-inventories",
      data=encoded,
      headers={
        "Content-Type": "application/json",
        "Idempotency-Key": (f"route-inventory:{manifest_sha256}"),
      },
    )
    body = self._json_body(raw)
    try:
      generation = int(body["generation"])
      response_digest = body["manifest_sha256"]
      response_state = body["state"]
    except (KeyError, TypeError, ValueError) as error:
      raise ApiError(
        None,
        "route inventory acceptance response is incomplete",
      ) from error
    if response_digest != manifest_sha256 or generation != manifest.get("generation") or response_state != "accepted":
      raise ApiError(
        None,
        "route inventory acceptance did not echo the exact manifest",
      )
    return RouteInventoryAcceptance(
      generation=generation,
      manifest_sha256=response_digest,
      state=response_state,
    )
