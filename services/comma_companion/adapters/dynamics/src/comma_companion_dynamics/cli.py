from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from comma_companion_dynamics.contract import DynamicsContractError
from comma_companion_dynamics.service import DynamicsService

MAX_REQUEST_BYTES = 8 * 1024 * 1024


def _reject_nonfinite_json(value: str) -> None:
  raise ValueError(f"Non-finite JSON number: {value}")


def _response(identifier: Any, service: DynamicsService, payload: Any) -> dict[str, Any]:
  try:
    return {
      "id": identifier,
      "ok": True,
      "result": service.handle(payload),
    }
  except DynamicsContractError as error:
    return {
      "id": identifier,
      "ok": False,
      "error": error.as_dict(),
    }
  # The process is a request boundary. Keep one failed inference from terminating
  # the long-running adapter, while returning only a non-sensitive error type.
  except Exception as error:  # noqa: BLE001
    return {
      "id": identifier,
      "ok": False,
      "error": {
        "code": "internal_error",
        "message": "The dynamics adapter could not complete the request.",
        "details": {"error_type": type(error).__name__},
      },
    }


def serve(input_stream: TextIO, output_stream: TextIO, service: DynamicsService) -> int:
  while True:
    raw_line = input_stream.readline(MAX_REQUEST_BYTES + 1)
    if raw_line == "":
      break
    encoded_size = len(raw_line.encode("utf-8"))
    overlong = encoded_size > MAX_REQUEST_BYTES
    if overlong and not raw_line.endswith("\n"):
      while True:
        remainder = input_stream.readline(
          MAX_REQUEST_BYTES + 1,
        )
        if remainder == "" or remainder.endswith("\n"):
          break
    if overlong:
      result = {
        "id": None,
        "ok": False,
        "error": {
          "code": "request_too_large",
          "message": f"A request line may not exceed {MAX_REQUEST_BYTES} bytes.",
          "details": {},
        },
      }
    else:
      try:
        payload = json.loads(
          raw_line,
          parse_constant=_reject_nonfinite_json,
        )
      except (
        json.JSONDecodeError,
        RecursionError,
        ValueError,
      ) as error:
        details = {"line": error.lineno, "column": error.colno} if isinstance(error, json.JSONDecodeError) else {}
        result = {
          "id": None,
          "ok": False,
          "error": {
            "code": "invalid_json",
            "message": "Each input line must contain one complete JSON request.",
            "details": details,
          },
        }
      else:
        identifier = payload.get("id") if isinstance(payload, dict) else None
        result = _response(identifier, service, payload)
    try:
      encoded_result = json.dumps(
        result,
        separators=(",", ":"),
        allow_nan=False,
      )
    except (TypeError, ValueError, RecursionError):
      encoded_result = json.dumps(
        {
          "id": None,
          "ok": False,
          "error": {
            "code": "internal_error",
            "message": ("The dynamics adapter produced an invalid response."),
            "details": {},
          },
        },
        separators=(",", ":"),
      )
    output_stream.write(encoded_result + "\n")
    output_stream.flush()
  return 0


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    description="Read dynamics requests as newline-delimited JSON on stdin.",
  )
  parser.add_argument("--model", type=Path)
  parser.add_argument("--device", default="cpu")
  parser.add_argument(
    "--allow-unverified-model",
    action="store_true",
    help="Allow an artifact whose SHA-256 differs from the reviewed reference model.",
  )
  return parser


def main() -> None:
  args = build_parser().parse_args()
  service = DynamicsService(
    model_path=args.model,
    device=args.device,
    require_reference_hash=not args.allow_unverified_model,
  )
  raise SystemExit(serve(sys.stdin, sys.stdout, service))


if __name__ == "__main__":
  main()
