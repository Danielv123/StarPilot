#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4


def fail(message: str) -> None:
  print(f"importer auth smoke failed: {message}", file=sys.stderr)
  raise SystemExit(1)


def read_token(path: Path) -> str:
  flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
  try:
    descriptor = os.open(path, flags)
  except OSError as error:
    fail(f"cannot open token file: {error}")
  try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
      fail("token path is not a regular file")
    if metadata.st_mode & 0o077:
      fail("token file must not be accessible by group or other users")
    with os.fdopen(descriptor, encoding="utf-8") as stream:
      descriptor = -1
      token = stream.read().rstrip("\r\n")
  finally:
    if descriptor >= 0:
      os.close(descriptor)
  if not token:
    fail("token file is empty")
  return token


def validate_url(value: str) -> str:
  parsed = urllib.parse.urlsplit(value)
  if (
    parsed.scheme not in {"http", "https"}
    or not parsed.hostname
    or parsed.username
    or parsed.password
    or parsed.query
    or parsed.fragment
  ):
    fail("--url must be a plain HTTP(S) origin")
  if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1"}:
    fail("plain HTTP is allowed only for a loopback origin")
  return value.rstrip("/")


def response_payload(error: urllib.error.HTTPError) -> dict[str, object]:
  try:
    payload = json.loads(error.read())
  except (json.JSONDecodeError, UnicodeDecodeError) as parse_error:
    fail(f"server returned a non-JSON HTTP {error.code}: {parse_error}")
  if not isinstance(payload, dict):
    fail(f"server returned an unexpected HTTP {error.code} response")
  return payload


def main() -> int:
  parser = argparse.ArgumentParser(
    description="Prove importer-token origin enforcement without storing an upload.",
  )
  parser.add_argument("--url", required=True, help="Comma Companion origin")
  parser.add_argument(
    "--token-file",
    type=Path,
    default=Path("secrets/import-token"),
  )
  parser.add_argument(
    "--expect",
    required=True,
    choices=("allowed", "rejected"),
  )
  parser.add_argument(
    "--spoof-forwarded-for",
    help="optional forged X-Forwarded-For value for the public rejection probe",
  )
  arguments = parser.parse_args()

  origin = validate_url(arguments.url)
  token = read_token(arguments.token_file)
  nonce = uuid4().hex
  body = json.dumps(
    {
      "device_id": f"__importer_connectivity_probe_{nonce}",
      "artifact_type": "importer-auth-probe",
      "relative_path": f"importer-auth-probes/{nonce}.probe",
      "size": 0,
    },
    separators=(",", ":"),
  ).encode()
  headers = {
    "Accept": "application/json",
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "Idempotency-Key": f"importer-auth-probe-{nonce}",
  }
  if arguments.spoof_forwarded_for:
    headers["X-Forwarded-For"] = arguments.spoof_forwarded_for
  request = urllib.request.Request(
    f"{origin}/api/v1/uploads",
    data=body,
    headers=headers,
    method="POST",
  )
  try:
    urllib.request.urlopen(request, timeout=15)
  except urllib.error.HTTPError as error:
    payload = response_payload(error)
    api_error = payload.get("error")
    code = api_error.get("code") if isinstance(api_error, dict) else None
    expected = (
      (404, "device_not_found")
      if arguments.expect == "allowed"
      else (403, "importer_source_not_allowed")
    )
    if (error.code, code) != expected:
        fail(f"expected HTTP {expected[0]} {expected[1]}, received HTTP {error.code} {code!r}")
  except urllib.error.URLError as error:
    fail(f"request failed: {error}")
  else:
    fail("probe unexpectedly created an upload")

  print(f"importer auth smoke passed: source was {arguments.expect}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
