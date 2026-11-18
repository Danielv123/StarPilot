"""Pure-ASGI request body size enforcement."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

from starlette.responses import JSONResponse

ASGIMessage: TypeAlias = dict[str, Any]
ASGIReceive: TypeAlias = Callable[[], Awaitable[ASGIMessage]]
ASGISend: TypeAlias = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp: TypeAlias = Callable[[dict[str, Any], ASGIReceive, ASGISend], Awaitable[None]]


class RequestBodyLimitMiddleware:
  """Reject request bodies that exceed a route-appropriate byte limit.

  ``Content-Length`` is checked before dispatch. Actual bytes are then counted
  transparently while the downstream application consumes them, so upload
  authentication can run before a large streaming body is read.
  """

  def __init__(
    self,
    app: ASGIApp,
    *,
    json_max_bytes: int,
    upload_patch_max_bytes: int,
    inventory_json_max_bytes: int | None = None,
    default_max_bytes: int = 64 * 1024,
    api_path_prefix: str = "/api/",
    upload_path_prefix: str = "/api/v1/uploads/",
  ) -> None:
    self.app = app
    self.json_max_bytes = self._validate_limit("json_max_bytes", json_max_bytes)
    self.upload_patch_max_bytes = self._validate_limit(
      "upload_patch_max_bytes",
      upload_patch_max_bytes,
    )
    self.inventory_json_max_bytes = self._validate_limit(
      "inventory_json_max_bytes",
      (
        json_max_bytes
        if inventory_json_max_bytes is None
        else inventory_json_max_bytes
      ),
    )
    self.default_max_bytes = self._validate_limit(
      "default_max_bytes",
      default_max_bytes,
    )
    if not api_path_prefix.startswith("/"):
      raise ValueError("api_path_prefix must start with '/'")
    if not upload_path_prefix.startswith("/"):
      raise ValueError("upload_path_prefix must start with '/'")
    self.api_path_prefix = api_path_prefix
    self.upload_path_prefix = upload_path_prefix

  async def __call__(
    self,
    scope: dict[str, Any],
    receive: ASGIReceive,
    send: ASGISend,
  ) -> None:
    if scope.get("type") != "http":
      await self.app(scope, receive, send)
      return

    headers = self._headers(scope)
    limit = self._request_limit(scope, headers)
    content_length, invalid_content_length = self._content_length(headers)
    if invalid_content_length:
      await self._error_response(
        scope,
        receive,
        send,
        status_code=400,
        code="invalid_content_length",
        message="Content-Length must be a non-negative decimal integer.",
      )
      return
    if content_length is not None and content_length > limit:
      await self._error_response(
        scope,
        receive,
        send,
        status_code=413,
        code="request_too_large",
        message="Request body exceeds the configured limit.",
      )
      return

    received_bytes = 0
    response_started = False

    async def limited_receive() -> ASGIMessage:
      nonlocal received_bytes
      message = await receive()
      if message.get("type") == "http.request":
        received_bytes += len(message.get("body", b""))
        if received_bytes > limit:
          raise _RequestBodyTooLarge
      return message

    async def tracked_send(message: ASGIMessage) -> None:
      nonlocal response_started
      if message.get("type") == "http.response.start":
        response_started = True
      await send(message)

    try:
      await self.app(scope, limited_receive, tracked_send)
    except _RequestBodyTooLarge:
      if response_started:
        raise
      await self._error_response(
        scope,
        receive,
        send,
        status_code=413,
        code="request_too_large",
        message="Request body exceeds the configured limit.",
      )

  @staticmethod
  def _validate_limit(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
      raise ValueError(f"{name} must be a non-negative integer")
    return value

  @staticmethod
  def _headers(scope: dict[str, Any]) -> list[tuple[bytes, bytes]]:
    return [(bytes(name).lower(), bytes(value)) for name, value in scope.get("headers", [])]

  def _request_limit(
    self,
    scope: dict[str, Any],
    headers: list[tuple[bytes, bytes]],
  ) -> int:
    method = str(scope.get("method", "")).upper()
    path = str(scope.get("path", ""))
    content_type = self._first_header(headers, b"content-type")
    media_type = content_type.split(b";", 1)[0].strip().lower()

    if method == "PATCH" and path.startswith(self.upload_path_prefix) and media_type == b"application/offset+octet-stream":
      return self.upload_patch_max_bytes
    if (
      method == "POST"
      and path == "/api/v1/route-inventories"
      and media_type == b"application/json"
    ):
      return self.inventory_json_max_bytes
    if path.startswith(self.api_path_prefix) and (media_type == b"application/json" or media_type.endswith(b"+json")):
      return self.json_max_bytes
    return self.default_max_bytes

  @staticmethod
  def _content_length(
    headers: list[tuple[bytes, bytes]],
  ) -> tuple[int | None, bool]:
    values = [value.strip() for name, value in headers if name == b"content-length"]
    if not values:
      return None, False
    if len(values) != 1:
      return None, True
    value = values[0]
    if not value or any(byte < ord("0") or byte > ord("9") for byte in value):
      return None, True
    return int(value), False

  @staticmethod
  def _first_header(
    headers: list[tuple[bytes, bytes]],
    name: bytes,
  ) -> bytes:
    for header_name, value in headers:
      if header_name == name:
        return value
    return b""

  @staticmethod
  async def _error_response(
    scope: dict[str, Any],
    receive: ASGIReceive,
    send: ASGISend,
    *,
    status_code: int,
    code: str,
    message: str,
  ) -> None:
    response = JSONResponse(
      status_code=status_code,
      content={
        "error": {
          "code": code,
          "message": message,
          "details": {},
        }
      },
    )
    await response(scope, receive, send)


class _RequestBodyTooLarge(Exception):
  pass
