from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TextIO

from comma_companion_importer.inventory import (
  InventoryConfig,
  build_route_inventory,
  canonical_json,
  content_sha256,
  manifest_sha256,
  route_artifact_groups,
  utc_timestamp,
)
from comma_companion_importer.manifest import Manifest
from comma_companion_importer.protocol import ApiError, RETRYABLE_STATUS, UploadProtocol, UploadSession
from comma_companion_importer.scanner import Artifact


DEFAULT_CHUNK_SIZE = 16 * 1024 * 1024


class InventoryConflict(RuntimeError):
  pass


def format_bytes(value: int) -> str:
  units = ("B", "KiB", "MiB", "GiB", "TiB")
  amount = float(value)
  for unit in units:
    if abs(amount) < 1024 or unit == units[-1]:
      if unit == "B":
        return f"{value} B"
      return f"{amount:.2f} {unit}"
    amount /= 1024
  raise AssertionError("unreachable")


def hash_file(path: Path, expected_size: int, expected_mtime_ns: int) -> str:
  before = path.stat()
  if before.st_size != expected_size or before.st_mtime_ns != expected_mtime_ns:
    raise RuntimeError("source changed since it was scanned")
  digest = hashlib.sha256()
  with path.open("rb") as source:
    while block := source.read(4 * 1024 * 1024):
      digest.update(block)
  after = path.stat()
  if after.st_size != expected_size or after.st_mtime_ns != expected_mtime_ns:
    raise RuntimeError("source changed while it was hashed")
  return digest.hexdigest()


class BandwidthLimiter:
  def __init__(self, bytes_per_second: float, sleep: Callable[[float], None] = time.sleep):
    self.bytes_per_second = bytes_per_second
    self.sleep = sleep
    self._next_slot = time.monotonic()
    self._lock = threading.Lock()

  def acquire(self, byte_count: int) -> None:
    if self.bytes_per_second <= 0 or byte_count <= 0:
      return
    with self._lock:
      now = time.monotonic()
      start = max(now, self._next_slot)
      self._next_slot = start + byte_count / self.bytes_per_second
      delay = start - now
    if delay > 0:
      self.sleep(delay)


class ProgressReporter:
  def __init__(self, artifacts: list[Artifact], initial_offsets: dict[str, int], stream: TextIO = sys.stderr):
    self.total_files = len(artifacts)
    self.total_bytes = sum(item.size for item in artifacts)
    self._offsets = {str(item.source_path): min(max(initial_offsets.get(str(item.source_path), 0), 0), item.size) for item in artifacts}
    self._completed: set[str] = set()
    self._failed: dict[str, str] = {}
    self._inventory_declared: set[tuple[str, str]] = set()
    self._inventory_reused: set[tuple[str, str]] = set()
    self._inventory_failed: dict[tuple[str, str], str] = {}
    self.sent_bytes = 0
    self.stream = stream
    self._lock = threading.Lock()

  def sent(self, byte_count: int) -> None:
    with self._lock:
      self.sent_bytes += byte_count

  def durable(self, artifact: Artifact, offset: int) -> None:
    key = str(artifact.source_path)
    with self._lock:
      self._offsets[key] = min(max(offset, 0), artifact.size)
      durable = sum(self._offsets.values())
      completing_now = offset == artifact.size and key not in self._completed
      complete_files = len(self._completed) + int(completing_now)
      print(
        f"{artifact.relative_path}: {offset}/{artifact.size} bytes; total {durable}/{self.total_bytes} bytes; files {complete_files}/{self.total_files}",
        file=self.stream,
        flush=True,
      )

  def complete(self, artifact: Artifact) -> None:
    key = str(artifact.source_path)
    with self._lock:
      self._offsets[key] = artifact.size
      self._completed.add(key)

  def fail(self, artifact: Artifact, error: str) -> None:
    with self._lock:
      self._failed[str(artifact.source_path)] = error
      print(f"ERROR {artifact.relative_path}: {error}", file=self.stream, flush=True)

  def inventory_declared(
    self,
    device_id: str,
    route_name: str,
    generation: int,
  ) -> None:
    with self._lock:
      self._inventory_declared.add((device_id, route_name))
      print(
        f"{route_name}: route inventory generation {generation} accepted",
        file=self.stream,
        flush=True,
      )

  def inventory_reused(
    self,
    device_id: str,
    route_name: str,
    generation: int,
  ) -> None:
    with self._lock:
      self._inventory_reused.add((device_id, route_name))
      print(
        f"{route_name}: route inventory generation {generation} unchanged",
        file=self.stream,
        flush=True,
      )

  def inventory_fail(
    self,
    device_id: str,
    route_name: str,
    error: str,
  ) -> None:
    with self._lock:
      self._inventory_failed[(device_id, route_name)] = error
      print(
        f"ERROR {route_name} route inventory: {error}",
        file=self.stream,
        flush=True,
      )

  def summary(self) -> str:
    with self._lock:
      durable = sum(self._offsets.values())
      parts = (
        f"{len(self._completed)}/{self.total_files} files complete",
        f"{durable}/{self.total_bytes} durable bytes ({format_bytes(durable)}/{format_bytes(self.total_bytes)})",
        f"{self.sent_bytes} bytes sent this run",
        f"{len(self._failed)} failed",
      )
      result = list(parts)
      inventory_count = len(self._inventory_declared) + len(self._inventory_reused) + len(self._inventory_failed)
      if inventory_count:
        result.extend(
          (
            f"{len(self._inventory_declared)} inventories accepted",
            f"{len(self._inventory_reused)} inventories unchanged",
            f"{len(self._inventory_failed)} inventory failures",
          ),
        )
      return "; ".join(result)

  @property
  def failure_count(self) -> int:
    with self._lock:
      return len(self._failed) + len(self._inventory_failed)

  @property
  def file_failure_count(self) -> int:
    with self._lock:
      return len(self._failed)

  @property
  def inventory_declared_count(self) -> int:
    with self._lock:
      return len(self._inventory_declared)

  @property
  def inventory_reused_count(self) -> int:
    with self._lock:
      return len(self._inventory_reused)

  @property
  def inventory_failure_count(self) -> int:
    with self._lock:
      return len(self._inventory_failed)


@dataclass(frozen=True, slots=True)
class ImportOptions:
  concurrency: int = 2
  chunk_size: int = DEFAULT_CHUNK_SIZE
  bandwidth_bytes_per_second: float = 0
  retries: int = 5
  rehash_completed: bool = False


class Importer:
  def __init__(
    self,
    protocol: UploadProtocol,
    manifest: Manifest,
    options: ImportOptions,
    *,
    inventory_config: InventoryConfig | None = None,
    inventory_artifacts: list[Artifact] | None = None,
    inventory_supersede_heads: dict[tuple[str, str], str] | None = None,
    progress_stream: TextIO = sys.stderr,
  ):
    self.protocol = protocol
    self.manifest = manifest
    self.options = options
    self.inventory_config = inventory_config
    self.inventory_artifacts = inventory_artifacts
    self.inventory_supersede_heads = inventory_supersede_heads or {}
    self.limiter = BandwidthLimiter(options.bandwidth_bytes_per_second)
    self.progress_stream = progress_stream

  def _session(self, artifact: Artifact, sha256: str, upload_id: str | None, generation: int) -> UploadSession:
    if upload_id is not None:
      try:
        session = self.protocol.head(upload_id)
        if session.length != artifact.size:
          raise RuntimeError(f"server upload length {session.length} differs from source length {artifact.size}")
        if session.state.lower() != "failed":
          return session
      except ApiError as error:
        if error.status != 404:
          raise
    return self.protocol.create(artifact, sha256, generation)

  @staticmethod
  def _read_chunk(source: BinaryIO, offset: int, chunk_size: int, total_size: int) -> bytes:
    source.seek(offset)
    expected = min(chunk_size, total_size - offset)
    chunk = source.read(expected)
    if len(chunk) != expected:
      raise RuntimeError(f"unexpected end of source at byte {offset}; expected {expected} bytes")
    return chunk

  def _patch_with_reconciliation(
    self,
    artifact: Artifact,
    source: BinaryIO,
    session: UploadSession,
    reporter: ProgressReporter,
  ) -> UploadSession:
    retries = 0
    current = session
    while current.offset < artifact.size:
      chunk = self._read_chunk(source, current.offset, self.options.chunk_size, artifact.size)
      self.limiter.acquire(len(chunk))
      reporter.sent(len(chunk))
      try:
        updated = self.protocol.patch(current.upload_id, current.offset, artifact.size, chunk)
      except ApiError as error:
        if error.status == 409:
          updated = self.protocol.head(current.upload_id)
          if updated.offset == current.offset:
            if retries >= self.options.retries:
              raise
            self.protocol._backoff(retries, error.headers)
            retries += 1
            continue
        elif (error.status in RETRYABLE_STATUS or error.status is None) and retries < self.options.retries:
          try:
            updated = self.protocol.head(current.upload_id)
          except ApiError as head_error:
            if head_error.status == 404:
              entry = self.manifest.get(artifact.source_path)
              if entry.sha256 is None:
                raise RuntimeError("manifest lost the source SHA-256 during upload recovery") from head_error
              updated = self.protocol.create(artifact, entry.sha256, entry.attempts)
              self.manifest.set_upload(artifact.source_path, updated.upload_id, updated.offset)
            else:
              raise
        else:
          raise

      if updated.length != artifact.size:
        raise RuntimeError(f"server upload length changed from {artifact.size} to {updated.length}")
      if updated.offset < current.offset or updated.offset > artifact.size:
        raise RuntimeError(f"server returned invalid upload offset {updated.offset}")
      if updated.offset == current.offset:
        if retries >= self.options.retries:
          raise RuntimeError(f"server made no progress at byte {current.offset}")
        self.protocol._backoff(retries, {})
        retries += 1
        continue
      retries = 0
      current = updated
      self.manifest.set_offset(artifact.source_path, current.offset)
      if current.offset < artifact.size:
        reporter.durable(artifact, current.offset)
    return current

  def _import_one(self, artifact: Artifact, reporter: ProgressReporter) -> None:
    try:
      entry = self.manifest.get(artifact.source_path)
      if entry.status == "complete" and entry.offset == artifact.size:
        if self.options.rehash_completed:
          current_digest = hash_file(artifact.source_path, artifact.size, artifact.mtime_ns)
          if entry.sha256 != current_digest:
            self.manifest.reset_upload(artifact.source_path)
            self.manifest.set_hash(artifact.source_path, current_digest)
            entry = self.manifest.get(artifact.source_path)
        if entry.upload_id is None:
          self.manifest.reset_upload(artifact.source_path)
          entry = self.manifest.get(artifact.source_path)
        else:
          try:
            completed = self.protocol.head(entry.upload_id)
          except ApiError as error:
            if error.status != 404:
              raise
            self.manifest.reset_upload(artifact.source_path)
            entry = self.manifest.get(artifact.source_path)
          else:
            if completed.length != artifact.size:
              self.manifest.reset_upload(artifact.source_path)
              entry = self.manifest.get(artifact.source_path)
            elif completed.complete and completed.offset == artifact.size:
              reporter.complete(artifact)
              reporter.durable(artifact, artifact.size)
              return

      sha256 = entry.sha256
      if sha256 is None:
        sha256 = hash_file(artifact.source_path, artifact.size, artifact.mtime_ns)
        self.manifest.set_hash(artifact.source_path, sha256)
      session = self._session(artifact, sha256, entry.upload_id, entry.attempts)
      self.manifest.set_upload(artifact.source_path, session.upload_id, session.offset)
      if session.offset < artifact.size:
        reporter.durable(artifact, session.offset)
      if session.offset > artifact.size:
        raise RuntimeError(f"server returned invalid upload offset {session.offset}")

      with artifact.source_path.open("rb") as source:
        session = self._patch_with_reconciliation(artifact, source, session, reporter)

      final = self.protocol.head(session.upload_id)
      if final.offset != artifact.size or not final.complete:
        raise RuntimeError(f"server did not durably acknowledge the complete file ({final.offset}/{artifact.size})")
      after = artifact.source_path.stat()
      if after.st_size != artifact.size or after.st_mtime_ns != artifact.mtime_ns:
        raise RuntimeError("source changed during upload")
      self.manifest.complete(artifact.source_path, artifact.size)
      reporter.complete(artifact)
      reporter.durable(artifact, artifact.size)
    except Exception as error:
      message = str(error) or type(error).__name__
      self.manifest.fail(artifact.source_path, message)
      reporter.fail(artifact, message)

  @staticmethod
  def _validate_source_snapshot(artifact: Artifact) -> None:
    stat = artifact.source_path.stat()
    identity = f"{stat.st_dev}:{stat.st_ino}:{stat.st_ctime_ns}"
    if stat.st_size != artifact.size or stat.st_mtime_ns != artifact.mtime_ns or identity != artifact.source_identity:
      raise RuntimeError("source changed since the static snapshot scan")

  def _route_hashes(
    self,
    artifacts: list[Artifact],
    reporter: ProgressReporter,
    selected_paths: set[str],
  ) -> dict[str, str] | None:
    digests: dict[str, str] = {}
    for artifact in artifacts:
      try:
        entry = self.manifest.get(artifact.source_path)
        digest = entry.sha256
        if digest is None or self.options.rehash_completed:
          current_digest = hash_file(
            artifact.source_path,
            artifact.size,
            artifact.mtime_ns,
          )
          if digest is not None and digest != current_digest:
            self.manifest.reset_upload(artifact.source_path)
          self.manifest.set_hash(
            artifact.source_path,
            current_digest,
          )
          digest = current_digest
        else:
          self._validate_source_snapshot(artifact)
        digests[str(artifact.source_path)] = digest
      except Exception as error:
        message = str(error) or type(error).__name__
        self.manifest.fail(artifact.source_path, message)
        if str(artifact.source_path) in selected_paths:
          reporter.fail(artifact, message)
        return None
    return digests

  def _persist_server_inventory(
    self,
    *,
    device_id: str,
    route_name: str,
    manifest: dict[str, object],
    digest: str,
  ) -> None:
    actual_digest = manifest_sha256(manifest)
    if actual_digest != digest:
      raise RuntimeError(
        "server latest route inventory digest does not match its manifest",
      )
    self.manifest.save_inventory(
      server_scope=self.protocol.server_url,
      device_id=device_id,
      route_name=route_name,
      content_sha256=content_sha256(manifest),
      manifest_sha256=digest,
      generation=int(manifest["generation"]),
      previous_manifest_sha256=manifest.get(
        "previous_manifest_sha256",
      ),
      manifest_json=canonical_json(manifest).decode("utf-8"),
      accepted=True,
    )

  def _declare_route_inventory(
    self,
    *,
    device_id: str,
    route_name: str,
    artifacts: list[Artifact],
    digests: dict[str, str],
    closed_at: str,
    reporter: ProgressReporter,
  ) -> None:
    assert self.inventory_config is not None
    latest = self.protocol.latest_route_inventory(
      device_id,
      route_name,
    )
    generation = 1 if latest is None else latest.generation + 1
    previous = None if latest is None else latest.manifest_sha256
    fresh = build_route_inventory(
      device_id=device_id,
      route_name=route_name,
      artifacts=artifacts,
      digests=digests,
      config=self.inventory_config,
      generation=generation,
      previous_manifest_sha256=previous,
      closed_at=closed_at,
    )
    lease = self.inventory_supersede_heads.get(
      (device_id, route_name),
    )
    if latest is not None and manifest_sha256(latest.manifest) != latest.manifest_sha256:
      raise RuntimeError(
        "server latest route inventory digest does not match its manifest",
      )
    if latest is None:
      if lease is not None:
        raise InventoryConflict(
          f"supersede lease {lease} did not match an empty server head",
        )
    else:
      latest_content = content_sha256(latest.manifest)
      if latest_content == fresh.content_sha256:
        self._persist_server_inventory(
          device_id=device_id,
          route_name=route_name,
          manifest=latest.manifest,
          digest=latest.manifest_sha256,
        )
        reporter.inventory_reused(
          device_id,
          route_name,
          latest.generation,
        )
        return
      known_head = self.manifest.accepted_inventory(
        server_scope=self.protocol.server_url,
        device_id=device_id,
        route_name=route_name,
        manifest_sha256=latest.manifest_sha256,
      )
      if lease is not None and lease != latest.manifest_sha256:
        raise InventoryConflict(
          "supersede lease does not match the latest server inventory " + f"{latest.manifest_sha256}",
        )
      if known_head is None and lease != latest.manifest_sha256:
        raise InventoryConflict(
          "server has a different route inventory head "
          + f"{latest.manifest_sha256}; review it and rerun with "
          + f"--supersede-inventory {route_name}="
          + f"{latest.manifest_sha256}",
        )

    reusable = self.manifest.reusable_inventory(
      server_scope=self.protocol.server_url,
      device_id=device_id,
      route_name=route_name,
      content_sha256=fresh.content_sha256,
      generation=generation,
      previous_manifest_sha256=previous,
    )
    if reusable is not None:
      manifest = json.loads(reusable.manifest_json)
      digest = reusable.manifest_sha256
      if not isinstance(manifest, dict) or manifest_sha256(manifest) != digest:
        raise RuntimeError(
          "persisted route inventory is not canonical or is corrupted",
        )
    else:
      manifest = fresh.manifest
      digest = fresh.manifest_sha256
      self.manifest.save_inventory(
        server_scope=self.protocol.server_url,
        device_id=device_id,
        route_name=route_name,
        content_sha256=fresh.content_sha256,
        manifest_sha256=digest,
        generation=generation,
        previous_manifest_sha256=previous,
        manifest_json=canonical_json(manifest).decode("utf-8"),
      )
    try:
      acceptance = self.protocol.declare_route_inventory(
        device_id=device_id,
        manifest=manifest,
        manifest_sha256=digest,
      )
    except Exception as error:
      self.manifest.fail_inventory(
        server_scope=self.protocol.server_url,
        device_id=device_id,
        route_name=route_name,
        manifest_sha256=digest,
        error=str(error) or type(error).__name__,
      )
      raise
    self.manifest.accept_inventory(
      server_scope=self.protocol.server_url,
      device_id=device_id,
      route_name=route_name,
      manifest_sha256=digest,
    )
    reporter.inventory_declared(
      device_id,
      route_name,
      acceptance.generation,
    )

  def run(self, artifacts: list[Artifact]) -> ProgressReporter:
    initial_offsets: dict[str, int] = {}
    selected_paths = {str(artifact.source_path) for artifact in artifacts}
    inventory_artifacts = self.inventory_artifacts if self.inventory_artifacts is not None else artifacts
    known_artifacts = {str(artifact.source_path): artifact for artifact in inventory_artifacts}
    known_artifacts.update(
      {str(artifact.source_path): artifact for artifact in artifacts},
    )
    for artifact in known_artifacts.values():
      entry = self.manifest.upsert(artifact, self.protocol.server_url)
      if str(artifact.source_path) in selected_paths:
        initial_offsets[str(artifact.source_path)] = entry.offset
    reporter = ProgressReporter(artifacts, initial_offsets, self.progress_stream)
    if not artifacts:
      return reporter

    remaining_by_device: dict[str, list[Artifact]] = {}
    successful_devices: set[str] = set()
    by_device: dict[str, list[Artifact]] = {}
    for artifact in artifacts:
      by_device.setdefault(artifact.device_id, []).append(artifact)
    for device_id in sorted(by_device):
      device_artifacts = by_device[device_id]
      preflight = min(device_artifacts, key=lambda artifact: (artifact.size, artifact.relative_path))
      failures_before = reporter.failure_count
      self._import_one(preflight, reporter)
      if reporter.failure_count != failures_before:
        for artifact in device_artifacts:
          if artifact is preflight:
            continue
          message = f"preflight for device {device_id} failed; file was not hashed or uploaded"
          self.manifest.fail(artifact.source_path, message)
          reporter.fail(artifact, message)
      else:
        successful_devices.add(device_id)
        remaining_by_device[device_id] = [artifact for artifact in device_artifacts if artifact is not preflight]

    failed_routes: set[tuple[str, str]] = set()
    if self.inventory_config is not None:
      snapshot_closed_at = utc_timestamp()
      groups = route_artifact_groups(
        [artifact for artifact in inventory_artifacts if artifact.device_id in successful_devices],
      )
      for (device_id, route_name), route_artifacts in sorted(groups.items()):
        digests = self._route_hashes(
          route_artifacts,
          reporter,
          selected_paths,
        )
        if digests is None:
          failed_routes.add((device_id, route_name))
          reporter.inventory_fail(
            device_id,
            route_name,
            "source snapshot hashing failed",
          )
          continue
        try:
          self._declare_route_inventory(
            device_id=device_id,
            route_name=route_name,
            artifacts=route_artifacts,
            digests=digests,
            closed_at=snapshot_closed_at,
            reporter=reporter,
          )
        except Exception as error:
          failed_routes.add((device_id, route_name))
          reporter.inventory_fail(
            device_id,
            route_name,
            str(error) or type(error).__name__,
          )

    remaining: list[Artifact] = []
    for device_id in sorted(remaining_by_device):
      for artifact in remaining_by_device[device_id]:
        route_key = (device_id, artifact.route_name) if artifact.route_name is not None else None
        if route_key is not None and route_key in failed_routes:
          message = f"route inventory for {artifact.route_name} failed; " + "file was not uploaded"
          self.manifest.fail(artifact.source_path, message)
          reporter.fail(artifact, message)
          continue
        remaining.append(artifact)
    if not remaining:
      return reporter
    with ThreadPoolExecutor(max_workers=self.options.concurrency, thread_name_prefix="historical-import") as executor:
      futures = [executor.submit(self._import_one, artifact, reporter) for artifact in remaining]
      for future in as_completed(futures):
        future.result()
    return reporter
