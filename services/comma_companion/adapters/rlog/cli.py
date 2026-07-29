from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from .extractor import (
  MAX_CHUNK_SIZE,
  MAX_OUTPUT_BYTES,
  MAX_OUTPUT_RECORDS,
  MIN_CHUNK_SIZE,
  InputError,
  ResourceLimitError,
  discover_route,
  iter_route_records,
)


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    prog="comma-companion-rlog",
    description="Extract a stable, server-facing telemetry stream from local comma rlogs.",
  )
  parser.add_argument(
    "inputs",
    nargs="+",
    type=Path,
    help="A log root, one route segment directory, or explicit rlog/qlog files from one route.",
  )
  parser.add_argument("--route-id", help="Select or override the source route ID.")
  parser.add_argument("--log-type", choices=("rlog", "qlog"), default="rlog")
  parser.add_argument("--chunk-size", type=int, default=4096, help="Maximum scalar samples per NDJSON chunk.")
  parser.add_argument("--format", choices=("ndjson", "json"), default="ndjson")
  parser.add_argument("--output", type=Path, help="Write output here instead of stdout.")
  parser.add_argument("--pretty", action="store_true", help="Indent JSON output (not valid with NDJSON).")
  return parser


class _OutputBudget:
  def __init__(self) -> None:
    self.bytes = 0
    self.records = 0

  def record(self) -> None:
    self.records += 1
    if self.records > MAX_OUTPUT_RECORDS:
      raise ResourceLimitError(
        "output_record_count",
        MAX_OUTPUT_RECORDS,
        self.records,
      )

  def write(self, stream: Any, value: str) -> None:
    encoded_bytes = len(value.encode("utf-8"))
    observed = self.bytes + encoded_bytes
    if observed > MAX_OUTPUT_BYTES:
      raise ResourceLimitError(
        "output_bytes",
        MAX_OUTPUT_BYTES,
        observed,
      )
    stream.write(value)
    self.bytes = observed


def _write_ndjson(records: Any, stream: Any) -> None:
  budget = _OutputBudget()
  for record in records:
    budget.record()
    budget.write(
      stream,
      json.dumps(
        record,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
      )
      + "\n",
    )


def _write_json(records: Any, stream: Any, pretty: bool) -> None:
  budget = _OutputBudget()
  budget.write(
    stream,
    '{\n  "records": [\n' if pretty else '{"records":[',
  )
  first = True
  for record in records:
    budget.record()
    serialized = json.dumps(
      record,
      allow_nan=False,
      indent=2 if pretty else None,
      separators=None if pretty else (",", ":"),
      sort_keys=True,
    )
    if pretty:
      serialized = "\n".join("    " + line for line in serialized.splitlines())
      prefix = "" if first else ",\n"
    else:
      prefix = "" if first else ","
    budget.write(stream, prefix + serialized)
    first = False
  budget.write(stream, '\n  ]\n}\n' if pretty else "]}\n")


def main(argv: list[str] | None = None) -> int:
  args = build_parser().parse_args(argv)
  if args.pretty and args.format != "json":
    raise SystemExit("--pretty requires --format json")
  if not MIN_CHUNK_SIZE <= args.chunk_size <= MAX_CHUNK_SIZE:
    raise SystemExit(
      "--chunk-size must be from " + f"{MIN_CHUNK_SIZE} through {MAX_CHUNK_SIZE}",
    )

  try:
    route = discover_route(args.inputs, route_id=args.route_id, log_type=args.log_type)
  except InputError as exc:
    print(f"input error: {exc}", file=sys.stderr)
    return 2
  except ResourceLimitError as exc:
    print(
      f"extraction failed: {type(exc).__name__}: {exc}",
      file=sys.stderr,
    )
    return 1

  temporary_output: Path | None = None
  output: Any = sys.stdout
  temporary_stream_opened = False
  succeeded = False
  try:
    if args.output:
      args.output.parent.mkdir(parents=True, exist_ok=True)
      temporary_output = args.output.with_name(
        f".{args.output.name}.{os.getpid()}.{uuid4().hex}.tmp",
      )
      output = temporary_output.open(
        "x",
        encoding="utf-8",
        newline="\n",
      )
      temporary_stream_opened = True
    records = iter_route_records(route, chunk_size=args.chunk_size)
    if args.format == "ndjson":
      _write_ndjson(records, output)
    else:
      _write_json(records, output, args.pretty)
    if args.output:
      output.flush()
      os.fsync(output.fileno())
      output.close()
      assert temporary_output is not None
      os.replace(temporary_output, args.output)
    succeeded = True
  except BrokenPipeError:
    return 0
  except Exception as exc:
    print(f"extraction failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1
  finally:
    if temporary_stream_opened and not output.closed:
      try:
        output.close()
      except Exception:
        pass
    if not succeeded and temporary_output is not None:
      try:
        temporary_output.unlink(missing_ok=True)
      except Exception as cleanup_exc:
        print(
          "temporary output cleanup failed: " + f"{type(cleanup_exc).__name__}: {cleanup_exc}",
          file=sys.stderr,
        )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
