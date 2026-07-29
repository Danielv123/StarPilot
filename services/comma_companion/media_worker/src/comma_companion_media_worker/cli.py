from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .contract import ContractError, EncodeJob
from .worker import CancelledError, MediaWorker, MediaWorkerError


def _json_line(value: dict[str, Any], *, stream: Any = sys.stdout) -> None:
  stream.write(json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n")
  stream.flush()


def _load_job(path: str) -> EncodeJob:
  try:
    if path == "-":
      payload = json.load(sys.stdin)
    else:
      payload = json.loads(Path(path).read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError) as exc:
    raise ContractError(f"could not read job JSON: {exc}") from exc
  return EncodeJob.from_dict(payload)


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(prog="comma-companion-media")
  parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable")
  parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe executable")
  subparsers = parser.add_subparsers(dest="command", required=True)

  probe = subparsers.add_parser("probe", help="probe media through an explicitly forced demuxer")
  probe.add_argument("path")
  probe.add_argument("--input-format", required=True, choices=("raw_hevc", "mpegts", "webm"))
  probe.add_argument("--timeout-seconds", type=float, default=120)

  encode = subparsers.add_parser("encode", help="run a versioned JSON encode job")
  encode.add_argument("job", help="job JSON path, or - for stdin")

  validate = subparsers.add_parser("validate", help="validate an AV1 WebM output")
  validate.add_argument("path")
  validate.add_argument("--source", help="optional source file for dimensions/frame comparison")
  validate.add_argument("--source-input-format", choices=("raw_hevc", "mpegts"))
  validate.add_argument("--raw-hevc-frame-rate", type=int, default=20)
  validate.add_argument("--timeout-seconds", type=float, default=3_600)
  validate.add_argument("--no-full-decode", action="store_true")
  return parser


def main(argv: Sequence[str] | None = None) -> int:
  args = _parser().parse_args(argv)
  worker = MediaWorker(ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
  cancel_event = threading.Event()

  def cancel(_signum: int, _frame: Any) -> None:
    cancel_event.set()

  previous_handlers: dict[int, Any] = {}
  for signal_number in (signal.SIGINT, signal.SIGTERM):
    previous_handlers[signal_number] = signal.signal(signal_number, cancel)

  try:
    if args.command == "probe":
      result = worker.probe(
        args.path,
        input_format=args.input_format,
        timeout_seconds=args.timeout_seconds,
        cancel_event=cancel_event,
      )
      _json_line({"event": "result", "result": result.as_dict()})
      return 0

    if args.command == "validate":
      if args.source and not args.source_input_format:
        raise ContractError("--source-input-format is required with --source")
      source = (
        worker.probe(
          args.source,
          input_format=args.source_input_format,
          timeout_seconds=args.timeout_seconds,
          cancel_event=cancel_event,
        )
        if args.source
        else None
      )
      result = worker.validate(
        args.path,
        source=source,
        raw_hevc_frame_rate=args.raw_hevc_frame_rate,
        full_decode=not args.no_full_decode,
        timeout_seconds=args.timeout_seconds,
        cancel_event=cancel_event,
        progress=lambda event: _json_line({"event": "worker_event", **event}),
      )
      _json_line({"event": "result", "result": result.as_dict()})
      return 0

    job = _load_job(args.job)

    def emit(event: dict[str, Any]) -> None:
      _json_line({"event": "worker_event", "job_id": job.job_id, **event})

    result = worker.encode(job, progress=emit, cancel_event=cancel_event)
    _json_line({"event": "result", "job_id": job.job_id, "result": result})
    return 0
  except (ContractError, MediaWorkerError) as exc:
    error_code = exc.code if isinstance(exc, MediaWorkerError) else "invalid_contract"
    _json_line(
      {
        "event": "error",
        "error": {
          "code": error_code,
          "message": str(exc),
          "retryable": isinstance(exc, (CancelledError,)),
        },
      },
      stream=sys.stderr,
    )
    return 130 if isinstance(exc, CancelledError) else 1
  finally:
    for signal_number, previous in previous_handlers.items():
      signal.signal(signal_number, previous)


if __name__ == "__main__":
  raise SystemExit(main())
