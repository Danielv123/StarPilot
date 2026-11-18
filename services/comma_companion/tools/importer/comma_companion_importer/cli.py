from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from comma_companion_importer.importer import DEFAULT_CHUNK_SIZE, Importer, ImportOptions, format_bytes
from comma_companion_importer.inventory import (
  InventoryConfig,
  load_inventory_config,
  preview_inventories,
)
from comma_companion_importer.manifest import Manifest
from comma_companion_importer.protocol import UploadProtocol
from comma_companion_importer.scanner import Artifact, ScanFilter, scan


CAMERAS = frozenset({"road", "wide", "driver", "qcamera"})
LOGS = frozenset({"rlog", "qlog"})
ARTIFACT_TYPES = frozenset({"video", "rlog", "qlog", "bootlog", "crash", "metadata", "stats", "user_flag", "other"})


def default_manifest_path() -> Path:
  if local_app_data := os.environ.get("LOCALAPPDATA"):
    return Path(local_app_data) / "CommaCompanion" / "historical-import.sqlite3"
  if state_home := os.environ.get("XDG_STATE_HOME"):
    return Path(state_home) / "comma-companion" / "historical-import.sqlite3"
  return Path.home() / ".local" / "state" / "comma-companion" / "historical-import.sqlite3"


def parse_datetime(value: str) -> datetime:
  normalized = value.strip()
  if normalized.endswith("Z"):
    normalized = f"{normalized[:-1]}+00:00"
  try:
    parsed = datetime.fromisoformat(normalized)
  except ValueError as error:
    raise argparse.ArgumentTypeError(f"invalid date/time {value!r}; use YYYY-MM-DD or an ISO 8601 timestamp") from error
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=UTC)
  return parsed.astimezone(UTC)


def parse_selection(value: str, allowed: frozenset[str], option: str) -> frozenset[str]:
  items = {item.strip().lower() for item in value.split(",") if item.strip()}
  if items == {"all"}:
    return allowed
  if items == {"none"} or not items:
    return frozenset()
  unknown = items - allowed
  if unknown:
    message = f"{option} contains unknown values: {', '.join(sorted(unknown))}; choose from {', '.join(sorted(allowed))}, all, or none"
    raise argparse.ArgumentTypeError(message)
  return frozenset(items)


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    prog="comma-companion-import",
    description="Safely scan historical comma logs and upload them using Comma Companion's resumable protocol. Source files are opened read-only.",
  )
  parser.add_argument("source", type=Path, help="log archive root, realdata directory, or parent containing device archives")
  parser.add_argument("--server", default=os.environ.get("COMMA_COMPANION_URL"), help="server origin; env: COMMA_COMPANION_URL")
  parser.add_argument(
    "--token",
    default=os.environ.get("COMMA_COMPANION_IMPORT_TOKEN"),
    help="import bearer token; prefer COMMA_COMPANION_IMPORT_TOKEN to avoid shell history",
  )
  parser.add_argument("--device-id", help="override device ID (otherwise inferred from <device>/realdata)")
  parser.add_argument("--manifest", type=Path, default=default_manifest_path(), help="local resume database (never stored under the source by default)")
  parser.add_argument("--dry-run", action="store_true", help="scan and report without creating a manifest or contacting the server")
  parser.add_argument("--list", action="store_true", help="print every selected source file")
  parser.add_argument("--route", action="append", default=[], metavar="GLOB", help="include matching route; repeatable")
  parser.add_argument("--exclude-route", action="append", default=[], metavar="GLOB", help="exclude matching route; repeatable")
  parser.add_argument("--since", type=parse_datetime, help="include files at or after this UTC date/time")
  parser.add_argument("--until", type=parse_datetime, help="exclude files at or after this UTC date/time")
  parser.add_argument("--cameras", default="all", metavar="LIST", help="road,wide,driver,qcamera; all or none")
  parser.add_argument("--exclude-cameras", default="", metavar="LIST", help="camera names to remove from --cameras")
  parser.add_argument("--logs", default="all", metavar="LIST", help="rlog,qlog; all or none")
  parser.add_argument("--exclude-logs", default="", metavar="LIST", help="log names to remove from --logs")
  parser.add_argument("--no-other", action="store_true", help="exclude metadata, crash, stats, bootlog, and unclassified artifacts")
  parser.add_argument("--include-artifact", action="append", default=[], metavar="TYPE", help="only include this artifact type; repeatable")
  parser.add_argument("--exclude-artifact", action="append", default=[], metavar="TYPE", help="exclude this artifact type; repeatable")
  parser.add_argument("--concurrency", type=int, default=2, help="simultaneous file uploads (default: 2)")
  parser.add_argument("--bandwidth-mbps", type=float, default=0, help="global decimal Mbit/s limit; 0 is unlimited")
  parser.add_argument("--chunk-size-mib", type=int, default=DEFAULT_CHUNK_SIZE // (1024 * 1024), help="chunk size in MiB (default: 16)")
  parser.add_argument("--retries", type=int, default=5, help="retry count per request/chunk (default: 5)")
  parser.add_argument("--timeout", type=float, default=60, help="HTTP request timeout in seconds")
  parser.add_argument("--ca-file", type=Path, help="custom HTTPS certificate authority bundle")
  parser.add_argument("--rehash-completed", action="store_true", help="re-read completed sources to detect content changes even when metadata is unchanged")
  parser.add_argument(
    "--inventory-config",
    type=Path,
    help=("JSON inventory capability config using the agent's " + "inventory.expected_streams schema; required for route uploads"),
  )
  parser.add_argument(
    "--supersede-inventory",
    action="append",
    default=[],
    metavar="ROUTE=SHA256",
    help=("force-with-lease for a differing server inventory head; " + "repeat per exact route"),
  )
  parser.add_argument("--json-summary", action="store_true", help="emit a machine-readable final summary")
  return parser


def _validated_args(parser: argparse.ArgumentParser, argv: list[str] | None) -> argparse.Namespace:
  args = parser.parse_args(argv)
  if args.concurrency < 1 or args.concurrency > 32:
    parser.error("--concurrency must be between 1 and 32")
  if args.bandwidth_mbps < 0:
    parser.error("--bandwidth-mbps cannot be negative")
  if args.chunk_size_mib < 1 or args.chunk_size_mib > 16:
    parser.error("--chunk-size-mib must be between 1 and 16")
  if args.retries < 0:
    parser.error("--retries cannot be negative")
  if args.timeout <= 0:
    parser.error("--timeout must be positive")
  if args.since is not None and args.until is not None and args.since >= args.until:
    parser.error("--since must be earlier than --until")
  source_root = args.source.expanduser().resolve()
  manifest_path = args.manifest.expanduser().resolve()
  if not args.dry_run and (manifest_path == source_root or source_root in manifest_path.parents):
    parser.error("--manifest must be outside the source archive")
  try:
    included_cameras = parse_selection(args.cameras, CAMERAS, "--cameras")
    excluded_cameras = parse_selection(args.exclude_cameras, CAMERAS, "--exclude-cameras")
    included_logs = parse_selection(args.logs, LOGS, "--logs")
    excluded_logs = parse_selection(args.exclude_logs, LOGS, "--exclude-logs")
  except argparse.ArgumentTypeError as error:
    parser.error(str(error))
  args.selected_cameras = included_cameras - excluded_cameras
  args.selected_logs = included_logs - excluded_logs
  requested_artifact_types = set(args.include_artifact) | set(args.exclude_artifact)
  unknown_artifact_types = requested_artifact_types - ARTIFACT_TYPES
  if unknown_artifact_types:
    parser.error(f"unknown artifact type(s): {', '.join(sorted(unknown_artifact_types))}")
  if not args.dry_run:
    if not args.server:
      parser.error("--server or COMMA_COMPANION_URL is required unless --dry-run is used")
    if not args.token:
      parser.error("--token or COMMA_COMPANION_IMPORT_TOKEN is required unless --dry-run is used")
  return args


def _print_scan(
  artifacts: list[Artifact],
  *,
  list_files: bool,
  inventory_config: InventoryConfig | None = None,
  inventory_artifacts: list[Artifact] | None = None,
  stream: TextIO = sys.stdout,
) -> None:
  if list_files:
    for artifact in artifacts:
      fields = (
        artifact.size,
        artifact.device_id,
        artifact.route_name or "-",
        artifact.segment_number if artifact.segment_number is not None else "-",
        artifact.artifact_type,
        artifact.camera or "-",
        artifact.relative_path,
        artifact.source_path,
      )
      print("\t".join(str(field) for field in fields), file=stream)
  total_bytes = sum(artifact.size for artifact in artifacts)
  routes = {(artifact.device_id, artifact.route_name) for artifact in artifacts if artifact.route_name is not None}
  print(
    f"Selected {len(artifacts)} files, {total_bytes} bytes ({format_bytes(total_bytes)}), {len(routes)} routes",
    file=stream,
  )
  if inventory_config is not None:
    inventory_sources = inventory_artifacts or artifacts
    for preview in preview_inventories(
      inventory_sources,
      inventory_config,
    ):
      print(
        f"Inventory {preview.device_id}/{preview.route_name}: "
        + f"{preview.state}; {len(preview.present_segments)} present segments; "
        + f"{len(preview.missing_segment_numbers)} missing segments; "
        + f"{preview.missing_stream_count} missing streams",
        file=stream,
      )


def _supersede_heads(
  values: list[str],
  artifacts: list[Artifact],
  parser: argparse.ArgumentParser,
) -> dict[tuple[str, str], str]:
  route_keys = {(artifact.device_id, artifact.route_name) for artifact in artifacts if artifact.route_name is not None}
  by_route: dict[str, list[tuple[str, str]]] = {}
  for key in route_keys:
    by_route.setdefault(key[1], []).append(key)
  result: dict[tuple[str, str], str] = {}
  for raw in values:
    route_name, separator, digest = raw.rpartition("=")
    if not separator:
      parser.error(
        "--supersede-inventory must use ROUTE=SHA256",
      )
    if len(digest) != 64 or digest.lower() != digest or any(character not in "0123456789abcdef" for character in digest):
      parser.error(
        "--supersede-inventory SHA256 must be 64 lowercase hex characters",
      )
    matches = by_route.get(route_name, [])
    if not matches:
      parser.error(
        f"--supersede-inventory names unselected route {route_name!r}",
      )
      if len(matches) > 1:
        parser.error(
          f"route {route_name!r} exists for multiple devices; " + "import one device at a time when superseding inventory",
        )
    key = matches[0]
    if key in result and result[key] != digest:
      parser.error(
        f"conflicting inventory leases for route {route_name!r}",
      )
    result[key] = digest
  return result


def main(argv: list[str] | None = None) -> int:
  parser = _parser()
  args = _validated_args(parser, argv)
  scan_filter = ScanFilter(
    cameras=args.selected_cameras,
    logs=args.selected_logs,
    include_other=not args.no_other,
    routes=tuple(args.route),
    exclude_routes=tuple(args.exclude_route),
    since=args.since,
    until=args.until,
    include_artifacts=frozenset(args.include_artifact),
    exclude_artifacts=frozenset(args.exclude_artifact),
  )
  authoritative_filter = ScanFilter(
    cameras=CAMERAS,
    logs=LOGS,
    include_other=True,
    routes=tuple(args.route),
    exclude_routes=tuple(args.exclude_route),
  )
  try:
    authoritative_artifacts = scan(
      args.source,
      authoritative_filter,
      args.device_id,
    )
  except (OSError, ValueError) as error:
    parser.error(str(error))
  artifacts = [artifact for artifact in authoritative_artifacts if scan_filter.accepts(artifact)]
  inventory_config: InventoryConfig | None = None
  if args.inventory_config is not None:
    try:
      inventory_config = load_inventory_config(args.inventory_config)
    except ValueError as error:
      parser.error(str(error))
  selected_route_keys = {(artifact.device_id, artifact.route_name) for artifact in artifacts if artifact.route_name is not None}
  route_count = len(selected_route_keys)
  inventory_artifacts = [
    artifact for artifact in authoritative_artifacts if (artifact.route_name is not None and (artifact.device_id, artifact.route_name) in selected_route_keys)
  ]
  if not args.dry_run and route_count:
    if inventory_config is None:
      parser.error(
        "--inventory-config is required for route uploads so expected " + "streams are explicit",
      )
    if not inventory_config.expected_streams:
      parser.error(
        "inventory.expected_streams must contain at least one explicit " + "stream for route uploads",
      )
  supersede_heads = _supersede_heads(
    args.supersede_inventory,
    artifacts,
    parser,
  )
  _print_scan(
    artifacts,
    list_files=args.list,
    inventory_config=inventory_config,
    inventory_artifacts=inventory_artifacts,
    stream=sys.stderr if args.json_summary else sys.stdout,
  )
  if args.dry_run:
    if args.json_summary:
      summary: dict[str, object] = {
        "dry_run": True,
        "selected_files": len(artifacts),
        "selected_bytes": sum(artifact.size for artifact in artifacts),
        "routes": route_count,
      }
      if inventory_config is not None:
        summary["route_inventories"] = [
          {
            "device_id": preview.device_id,
            "route_name": preview.route_name,
            "state": preview.state,
            "present_segments": len(preview.present_segments),
            "missing_segments": len(
              preview.missing_segment_numbers,
            ),
            "missing_streams": preview.missing_stream_count,
          }
          for preview in preview_inventories(
            inventory_artifacts,
            inventory_config,
          )
        ]
      print(json.dumps(summary, separators=(",", ":")))
    return 0

  try:
    protocol = UploadProtocol(
      args.server,
      args.token,
      timeout=args.timeout,
      max_retries=args.retries,
      ca_file=args.ca_file,
    )
  except (OSError, ValueError) as error:
    parser.error(str(error))
  options = ImportOptions(
    concurrency=args.concurrency,
    chunk_size=args.chunk_size_mib * 1024 * 1024,
    bandwidth_bytes_per_second=args.bandwidth_mbps * 1_000_000 / 8,
    retries=args.retries,
    rehash_completed=args.rehash_completed,
  )
  with Manifest(args.manifest) as manifest:
    reporter = Importer(
      protocol,
      manifest,
      options,
      inventory_config=inventory_config,
      inventory_artifacts=inventory_artifacts,
      inventory_supersede_heads=supersede_heads,
    ).run(artifacts)
  if args.json_summary:
    print(
      json.dumps(
        {
          "selected_files": reporter.total_files,
          "selected_bytes": reporter.total_bytes,
          "sent_bytes": reporter.sent_bytes,
          "failed_files": reporter.file_failure_count,
          "inventories_accepted": reporter.inventory_declared_count,
          "inventories_unchanged": reporter.inventory_reused_count,
          "inventory_failures": reporter.inventory_failure_count,
        },
        separators=(",", ":"),
      )
    )
  else:
    print(reporter.summary())
  return 1 if reporter.failure_count else 0
