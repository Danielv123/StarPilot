from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from comma_companion.request_limits import RequestBodyLimitMiddleware

ASGIMessage = dict[str, Any]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]


class RecordingApp:
  def __init__(self) -> None:
    self.calls = 0
    self.messages: list[ASGIMessage] = []

  async def __call__(
    self,
    _scope: dict[str, Any],
    receive: ASGIReceive,
    send: ASGISend,
  ) -> None:
    self.calls += 1
    while True:
      message = await receive()
      self.messages.append(message)
      if message["type"] != "http.request" or not message.get("more_body", False):
        break
    await send(
      {
        "type": "http.response.start",
        "status": 204,
        "headers": [],
      }
    )
    await send({"type": "http.response.body", "body": b""})


def _scope(
  *,
  path: str = "/api/v1/example",
  method: str = "POST",
  headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
  return {
    "type": "http",
    "asgi": {"version": "3.0"},
    "http_version": "1.1",
    "method": method,
    "scheme": "https",
    "path": path,
    "raw_path": path.encode(),
    "query_string": b"",
    "headers": headers or [],
    "client": ("127.0.0.1", 12345),
    "server": ("testserver", 443),
  }


async def _invoke(
  middleware: RequestBodyLimitMiddleware,
  *,
  scope: dict[str, Any],
  incoming: list[ASGIMessage],
) -> list[ASGIMessage]:
  messages = iter(incoming)
  sent: list[ASGIMessage] = []

  async def receive() -> ASGIMessage:
    return next(messages)

  async def send(message: ASGIMessage) -> None:
    sent.append(message)

  await middleware(scope, receive, send)
  return sent


def _response(sent: list[ASGIMessage]) -> tuple[int, dict[str, Any] | None]:
  start = next(message for message in sent if message["type"] == "http.response.start")
  bodies = [message.get("body", b"") for message in sent if message["type"] == "http.response.body"]
  body = b"".join(bodies)
  return start["status"], json.loads(body) if body else None


@pytest.mark.parametrize(
  "content_length",
  [b"", b"-1", b"+1", b"1.0", b"one", b"1, 1"],
)
def test_invalid_content_length_is_rejected_before_downstream(
  content_length: bytes,
) -> None:
  app = RecordingApp()
  middleware = RequestBodyLimitMiddleware(
    app,
    json_max_bytes=10,
    upload_patch_max_bytes=20,
  )

  sent = asyncio.run(
    _invoke(
      middleware,
      scope=_scope(headers=[(b"content-length", content_length)]),
      incoming=[],
    )
  )

  assert app.calls == 0
  assert _response(sent) == (
    400,
    {
      "error": {
        "code": "invalid_content_length",
        "message": "Content-Length must be a non-negative decimal integer.",
        "details": {},
      }
    },
  )


def test_duplicate_content_length_is_rejected() -> None:
  app = RecordingApp()
  middleware = RequestBodyLimitMiddleware(
    app,
    json_max_bytes=10,
    upload_patch_max_bytes=20,
  )

  sent = asyncio.run(
    _invoke(
      middleware,
      scope=_scope(
        headers=[
          (b"content-length", b"1"),
          (b"Content-Length", b"1"),
        ]
      ),
      incoming=[],
    )
  )

  assert app.calls == 0
  assert _response(sent)[0] == 400
  assert _response(sent)[1]["error"]["code"] == "invalid_content_length"


@pytest.mark.parametrize(
  ("path", "method", "content_type", "length", "expected_status"),
  [
    ("/api/v1/login", "POST", b"application/json", 11, 413),
    ("/api/v1/login", "POST", b"application/problem+json", 11, 413),
    (
      "/api/v1/uploads/upload-1",
      "PATCH",
      b"application/offset+octet-stream",
      21,
      413,
    ),
    ("/form", "POST", b"application/x-www-form-urlencoded", 7, 413),
    ("/api/v1/login", "POST", b"application/json", 10, 204),
    (
      "/api/v1/uploads/upload-1",
      "PATCH",
      b"application/offset+octet-stream; charset=binary",
      20,
      204,
    ),
    ("/form", "POST", b"application/x-www-form-urlencoded", 6, 204),
  ],
)
def test_route_specific_declared_length_limits(
  path: str,
  method: str,
  content_type: bytes,
  length: int,
  expected_status: int,
) -> None:
  app = RecordingApp()
  middleware = RequestBodyLimitMiddleware(
    app,
    json_max_bytes=10,
    upload_patch_max_bytes=20,
    default_max_bytes=6,
  )
  incoming = [{"type": "http.request", "body": b"x" * length}]

  sent = asyncio.run(
    _invoke(
      middleware,
      scope=_scope(
        path=path,
        method=method,
        headers=[
          (b"content-type", content_type),
          (b"content-length", str(length).encode()),
        ],
      ),
      incoming=incoming,
    ),
  )

  assert _response(sent)[0] == expected_status
  assert app.calls == (1 if expected_status == 204 else 0)


def test_chunked_body_cannot_bypass_limit_with_lying_content_length() -> None:
  app = RecordingApp()
  middleware = RequestBodyLimitMiddleware(
    app,
    json_max_bytes=7,
    upload_patch_max_bytes=20,
  )

  sent = asyncio.run(
    _invoke(
      middleware,
      scope=_scope(
        headers=[
          (b"content-type", b"application/json"),
          (b"content-length", b"1"),
        ]
      ),
      incoming=[
        {"type": "http.request", "body": b"abcd", "more_body": True},
        {"type": "http.request", "body": b"efgh", "more_body": False},
      ],
    )
  )

  assert app.calls == 1
  assert _response(sent) == (
    413,
    {
      "error": {
        "code": "request_too_large",
        "message": "Request body exceeds the configured limit.",
        "details": {},
      }
    },
  )


def test_accepted_messages_are_replayed_unchanged() -> None:
  app = RecordingApp()
  middleware = RequestBodyLimitMiddleware(
    app,
    json_max_bytes=8,
    upload_patch_max_bytes=20,
  )
  incoming = [
    {"type": "http.request", "body": b"abcd", "more_body": True},
    {"type": "http.request", "body": b"efgh", "more_body": False},
  ]

  sent = asyncio.run(
    _invoke(
      middleware,
      scope=_scope(headers=[(b"content-type", b"application/json")]),
      incoming=incoming,
    )
  )

  assert _response(sent)[0] == 204
  assert app.calls == 1
  assert app.messages == incoming


def test_receive_failure_propagates_without_starting_response() -> None:
  app = RecordingApp()
  middleware = RequestBodyLimitMiddleware(
    app,
    json_max_bytes=8,
    upload_patch_max_bytes=20,
  )
  sent: list[ASGIMessage] = []

  async def receive() -> ASGIMessage:
    raise ConnectionError("request stream failed")

  async def send(message: ASGIMessage) -> None:
    sent.append(message)

  async def exercise() -> None:
    with pytest.raises(ConnectionError, match="request stream failed"):
      await middleware(_scope(), receive, send)

  asyncio.run(exercise())
  assert app.calls == 1
  assert sent == []


def test_non_http_scope_passes_through() -> None:
  calls = 0

  async def app(
    scope: dict[str, Any],
    _receive: ASGIReceive,
    _send: ASGISend,
  ) -> None:
    nonlocal calls
    calls += 1
    assert scope["type"] == "lifespan"

  middleware = RequestBodyLimitMiddleware(
    app,
    json_max_bytes=8,
    upload_patch_max_bytes=20,
  )

  async def receive() -> ASGIMessage:
    return {"type": "lifespan.startup"}

  async def send(_message: ASGIMessage) -> None:
    pass

  asyncio.run(middleware({"type": "lifespan"}, receive, send))

  assert calls == 1


@pytest.mark.parametrize(
  ("name", "value"),
  [
    ("json_max_bytes", -1),
    ("upload_patch_max_bytes", True),
    ("default_max_bytes", 1.5),
  ],
)
def test_constructor_rejects_invalid_limits(name: str, value: Any) -> None:
  kwargs: dict[str, Any] = {
    "json_max_bytes": 8,
    "upload_patch_max_bytes": 20,
    "default_max_bytes": 6,
  }
  kwargs[name] = value

  with pytest.raises(ValueError, match=f"{name} must be a non-negative integer"):
    RequestBodyLimitMiddleware(RecordingApp(), **kwargs)
