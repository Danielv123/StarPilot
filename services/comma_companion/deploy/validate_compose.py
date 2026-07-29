#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from source_manifest import source_sha256


SERVICE_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = SERVICE_ROOT / "docker-compose.yml"
DATABASE_TARGET = "/var/lib/comma-companion/database"
SESSION_TARGET = "/var/lib/comma-companion/sessions"
ARCHIVE_TARGET = "/archive/comma-companion"
OUTPUT_TARGETS = {
  f"{ARCHIVE_TARGET}/derived",
  f"{ARCHIVE_TARGET}/telemetry",
  f"{ARCHIVE_TARGET}/thumbnails",
}
API_MOUNTS = {
  DATABASE_TARGET,
  SESSION_TARGET,
  ARCHIVE_TARGET,
  "/run/companion-secrets",
}
WORKER_MOUNTS = {
  DATABASE_TARGET,
  "/archive/comma-companion",
} | OUTPUT_TARGETS
SECRET_TARGET = "/run/companion-secrets"
WORKER_ENVIRONMENT = {
  "COMMA_DYNAMICS_MODEL",
  "COMMA_DYNAMICS_TORCH_THREADS",
  "COMPANION_ARCHIVE_ROOT",
  "COMPANION_ARCHIVE_SENTINEL",
  "COMPANION_DATABASE_PATH",
  "COMPANION_DYNAMICS_ADAPTER_COMMAND",
  "COMPANION_JOB_LEASE_SECONDS",
  "COMPANION_JOB_POLL_SECONDS",
  "COMPANION_MAX_ARTIFACT_BYTES",
  "COMPANION_MEDIA_TIMEOUT_SECONDS",
  "COMPANION_MEDIA_WORKER_COMMAND",
  "COMPANION_RLOG_ADAPTER_COMMAND",
  "COMPANION_SESSION_DIR",
  "COMPANION_TRANSCODE_CRF",
  "COMPANION_TRANSCODE_PRESET",
  "OMP_NUM_THREADS",
  "TMPDIR",
}


def fail(message: str) -> None:
  print(f"compose validation failed: {message}", file=sys.stderr)
  raise SystemExit(1)


def rendered_compose() -> dict[str, Any]:
  result = subprocess.run(
    [
      "docker",
      "compose",
      "--project-directory",
      str(SERVICE_ROOT),
      "-f",
      str(COMPOSE_FILE),
      "config",
      "--format",
      "json",
    ],
    check=False,
    capture_output=True,
    text=True,
  )
  if result.returncode:
    fail(result.stderr.strip() or "docker compose config failed")
  try:
    return json.loads(result.stdout)
  except json.JSONDecodeError as error:
    fail(f"docker compose returned invalid JSON: {error}")


def inspect_image(reference: str) -> dict[str, Any]:
  result = subprocess.run(
    ["docker", "image", "inspect", reference],
    check=False,
    capture_output=True,
    text=True,
  )
  if result.returncode:
    fail(f"release image does not exist locally: {reference}")
  try:
    images = json.loads(result.stdout)
  except json.JSONDecodeError as error:
    fail(f"docker image inspect returned invalid JSON: {error}")
  if not isinstance(images, list) or len(images) != 1:
    fail("docker image inspect did not return exactly one image")
  return images[0]


def mount_targets(service: dict[str, Any]) -> set[str]:
  return {
    mount["target"]
    for mount in service.get("volumes", [])
    if isinstance(mount, dict) and isinstance(mount.get("target"), str)
  }


def mounts_by_target(service: dict[str, Any]) -> dict[str, dict[str, Any]]:
  return {
    mount["target"]: mount
    for mount in service.get("volumes", [])
    if isinstance(mount, dict) and isinstance(mount.get("target"), str)
  }


def canonical_host_path(value: object, label: str) -> PurePath:
  if not isinstance(value, str) or not value:
    fail(f"{label} must be a nonempty absolute host path")
  if value.startswith("//"):
    fail(f"{label} must not use a double-slash or network-share root")
  if value.startswith("/"):
    path: PurePath = PurePosixPath(value)
    native = os.name != "nt"
  else:
    path = PureWindowsPath(value)
    native = os.name == "nt"
  if not path.is_absolute() or ".." in path.parts:
    fail(f"{label} must be an absolute host path")
  if path.parent == path:
    fail(f"{label} must not be a filesystem root")
  if native:
    concrete = Path(value)
    current = concrete
    while True:
      if current.is_symlink():
        fail(f"{label} must not contain symlink components")
      if current.parent == current:
        break
      current = current.parent
    resolved = concrete.resolve(strict=False)
    path = (
      PureWindowsPath(resolved)
      if os.name == "nt"
      else PurePosixPath(resolved)
    )
  return path


def paths_overlap(first: PurePath, second: PurePath) -> bool:
  if type(first) is not type(second):
    return False
  return (
    first == second
    or first.is_relative_to(second)
    or second.is_relative_to(first)
  )


def validate_hardening(name: str, service: dict[str, Any]) -> None:
  if service.get("user") != "65532:65532":
    fail(f"{name} must run as UID/GID 65532")
  if service.get("read_only") is not True:
    fail(f"{name} must have a read-only root filesystem")
  if "ALL" not in service.get("cap_drop", []):
    fail(f"{name} must drop all Linux capabilities")
  if "no-new-privileges:true" not in service.get("security_opt", []):
    fail(f"{name} must set no-new-privileges")
  if service.get("memswap_limit") != service.get("mem_limit"):
    fail(f"{name} must not receive swap beyond its memory limit")


def integer_setting(
  environment: dict[str, Any],
  name: str,
  *,
  minimum: int = 1,
  maximum: int = (1 << 63) - 1,
) -> int:
  value = environment.get(name)
  try:
    parsed = int(value)
  except (TypeError, ValueError):
    fail(f"{name} must be a finite integer")
  if not minimum <= parsed <= maximum:
    fail(f"{name} must be between {minimum} and {maximum}")
  return parsed


def float_setting(
  environment: dict[str, Any],
  name: str,
  *,
  minimum: float,
  maximum_exclusive: float,
) -> float:
  value = environment.get(name)
  try:
    parsed = float(value)
  except (TypeError, ValueError):
    fail(f"{name} must be a finite number")
  if (
    not math.isfinite(parsed)
    or parsed < minimum
    or parsed >= maximum_exclusive
  ):
    fail(f"{name} must be at least {minimum} and below {maximum_exclusive}")
  return parsed


def exact_ip_setting(environment: dict[str, Any], name: str) -> set[str]:
  value = environment.get(name)
  if not isinstance(value, str):
    fail(f"{name} must contain comma-separated exact IP addresses")
  raw_addresses = [item.strip() for item in value.split(",") if item.strip()]
  if not raw_addresses:
    fail(f"{name} must contain at least one exact IP address")
  try:
    addresses = {str(ipaddress.ip_address(item)) for item in raw_addresses}
  except ValueError:
    fail(f"{name} must contain exact IP addresses, not CIDRs or wildcards")
  if len(addresses) != len(raw_addresses):
    fail(f"{name} must not contain duplicate IP addresses")
  return addresses


def docker_network_gateway(name: str) -> str:
  result = subprocess.run(
    [
      "docker",
      "network",
      "inspect",
      name,
      "--format",
      "{{(index .IPAM.Config 0).Gateway}}",
    ],
    check=False,
    capture_output=True,
    text=True,
  )
  if result.returncode:
    fail(f"cannot inspect Docker proxy network {name}")
  gateway = result.stdout.strip()
  try:
    return str(ipaddress.ip_address(gateway))
  except ValueError:
    fail(f"Docker proxy network {name} has no exact IP gateway")


def main() -> int:
  arguments = sys.argv[1:]
  if (
    len(arguments) != len(set(arguments))
    or any(argument not in {"--release", "--image"} for argument in arguments)
  ):
    fail("usage: validate_compose.py [--release [--image]]")
  release_mode = "--release" in arguments
  image_mode = "--image" in arguments
  if image_mode and not release_mode:
    fail("--image requires --release")

  config = rendered_compose()
  services = config.get("services", {})
  if set(services) != {"companion", "worker"}:
    fail("expected exactly companion and worker services")
  api = services["companion"]
  worker = services["worker"]

  validate_hardening("companion", api)
  validate_hardening("worker", worker)
  if api.get("image") != worker.get("image"):
    fail("API and worker must use the same image reference")
  build = api.get("build", {})
  build_args = build.get("args", {})
  revision = build_args.get("STARPILOT_COMMIT", "")
  source_hash = build_args.get("COMPANION_SOURCE_SHA256", "")
  if re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
    fail("COMPANION_SOURCE_SHA256 must be 64 lowercase hexadecimal characters")
  if release_mode:
    image_reference = api.get("image", "")
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision) is None:
      fail("release STARPILOT_COMMIT must be a full lowercase Git object ID")
    actual_source_hash = source_sha256()
    if source_hash == "0" * 64 or source_hash != actual_source_hash:
      fail(
        "release COMPANION_SOURCE_SHA256 does not match the deterministic source bundle",
      )
    if (
      not image_reference.endswith(f":{source_hash}")
      and re.fullmatch(r"sha256:[0-9a-f]{64}", image_reference) is None
      and re.search(r"@sha256:[0-9a-f]{64}$", image_reference) is None
    ):
        fail("release image must use its source-bundle tag, immutable local image ID, or registry digest")
  if api.get("command") != ["api"] or worker.get("command") != ["worker"]:
    fail("API and worker entrypoint modes are not explicit")

  api_targets = mount_targets(api)
  worker_targets = mount_targets(worker)
  if api_targets != API_MOUNTS:
    fail("API mount set must be database, sessions, archive, and secrets")
  if worker_targets != WORKER_MOUNTS:
    fail("worker mount set must be database, read-only archive, and output subpaths")
  api_mounts = mounts_by_target(api)
  worker_mounts = mounts_by_target(worker)
  for name, mounts in (("companion", api_mounts), ("worker", worker_mounts)):
    for target, mount in mounts.items():
      if mount.get("type") != "bind":
        fail(f"{name} {target} must be an explicit bind mount")
      if mount.get("bind", {}).get("create_host_path") is not False:
        fail(f"{name} {target} must disable automatic host-path creation")
  api_database_source = canonical_host_path(
    api_mounts[DATABASE_TARGET].get("source"),
    "API database source",
  )
  worker_database_source = canonical_host_path(
    worker_mounts[DATABASE_TARGET].get("source"),
    "worker database source",
  )
  api_session_source = canonical_host_path(
    api_mounts[SESSION_TARGET].get("source"),
    "API session source",
  )
  api_archive_source = canonical_host_path(
    api_mounts[ARCHIVE_TARGET].get("source"),
    "API archive source",
  )
  worker_archive_source = canonical_host_path(
    worker_mounts[ARCHIVE_TARGET].get("source"),
    "worker archive source",
  )
  secret_source = canonical_host_path(
    api_mounts[SECRET_TARGET].get("source"),
    "API secret source",
  )
  if api_database_source != worker_database_source:
    fail("API and worker must share only the database directory")
  if api_archive_source != worker_archive_source:
    fail("API and worker archive views must use the same source")
  state_root = api_database_source.parent
  if api_database_source != state_root / "database":
    fail("database source must be the database child of the local state root")
  if api_session_source != state_root / "sessions":
    fail("session source must be the sessions child of the local state root")
  protected_roots: set[PurePath] = {
    PurePosixPath(value)
    for value in (
      "/",
      "/bin",
      "/boot",
      "/dev",
      "/etc",
      "/home",
      "/lib",
      "/lib64",
      "/opt",
      "/proc",
      "/root",
      "/run",
      "/sbin",
      "/srv",
      "/sys",
      "/tmp",
      "/usr",
      "/var",
      "/var/lib",
    )
  }
  for label, path in (
    ("state root", state_root),
    ("archive source", api_archive_source),
    ("secret source", secret_source),
  ):
    if path in protected_roots:
      fail(f"{label} is an unsafe broad system path")
  durable_roots = {
    "state root": state_root,
    "archive source": api_archive_source,
    "secret source": secret_source,
  }
  for first_name, first_path in durable_roots.items():
    for second_name, second_path in durable_roots.items():
      if first_name < second_name and paths_overlap(first_path, second_path):
        fail(f"{first_name} and {second_name} must not overlap")
  source_root = canonical_host_path(str(SERVICE_ROOT), "source checkout")
  if paths_overlap(state_root, source_root) or paths_overlap(
    api_archive_source,
    source_root,
  ):
    fail("state and archive roots must not overlap the source checkout")
  if secret_source == source_root or source_root.is_relative_to(secret_source):
    fail("secret source must not contain or equal the source checkout")
  if worker_mounts[ARCHIVE_TARGET].get("read_only") is not True:
    fail("worker archive parent must be read-only")
  if api_mounts[ARCHIVE_TARGET].get("read_only") is True:
    fail("API archive mount must be writable for resumable uploads")
  if worker_mounts[DATABASE_TARGET].get("read_only") is True:
    fail("worker database mount must be writable for the durable queue")
  for target in OUTPUT_TARGETS:
    name = PurePosixPath(target).name
    mount = worker_mounts[target]
    if mount.get("read_only") is True:
      fail(f"worker output mount must be writable: {target}")
    output_source = canonical_host_path(
      mount.get("source"),
      f"worker {name} source",
    )
    if output_source != api_archive_source / name:
      fail(f"worker output source must be the archive {name} subdirectory")
  if api_session_source in {
    canonical_host_path(mount.get("source"), f"worker {target} source")
    for target, mount in worker_mounts.items()
  }:
    fail("worker must not mount the API session directory")
  secret_mount = next(
    mount for mount in api.get("volumes", []) if mount.get("target") == SECRET_TARGET
  )
  if secret_mount.get("read_only") is not True:
    fail("API secret mount must be read-only")

  worker_environment = worker.get("environment", {})
  unexpected_environment = set(worker_environment) - WORKER_ENVIRONMENT
  if unexpected_environment:
    fail(
      "worker environment contains unreviewed variables: "
      + ", ".join(sorted(unexpected_environment)),
    )
  missing_environment = WORKER_ENVIRONMENT - set(worker_environment)
  if missing_environment:
    fail(
      "worker environment is missing required variables: "
      + ", ".join(sorted(missing_environment)),
    )
  if api.get("environment", {}).get("COMPANION_SESSION_DIR") != SESSION_TARGET:
    fail("API session directory must use its isolated session mount")
  if not PurePosixPath(worker_environment["COMPANION_SESSION_DIR"]).is_relative_to(
    PurePosixPath(DATABASE_TARGET),
  ):
    fail("worker lock/runtime directory must stay under the database mount")
  if worker.get("network_mode") != "none" or worker.get("networks"):
    fail("worker must use network_mode none and no Docker networks")
  if worker.get("ports") or worker.get("expose"):
    fail("worker must not publish or expose ports")
  if set(api.get("networks", {})) != {"webserver-proxy"}:
    fail("only API may join the Nginx Proxy Manager network")

  ports = api.get("ports", [])
  if len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1":
    fail("API must publish exactly one loopback-only host port")
  api_health = api.get("healthcheck", {}).get("test", [])
  worker_health = worker.get("healthcheck", {}).get("test", [])
  health_command = [
    "CMD",
    "python",
    "/usr/local/lib/comma-companion/healthcheck.py",
  ]
  if api_health != health_command:
    fail("API healthcheck command must use the exact local API probe")
  if worker_health != [*health_command, "worker"]:
    fail("worker healthcheck command must use the exact local worker probe")
  dependency = worker.get("depends_on", {}).get("companion", {})
  if dependency.get("condition") != "service_healthy":
    fail("worker must start after the API has initialized shared storage")

  api_environment = api.get("environment", {})
  importer_clients = exact_ip_setting(
    api_environment,
    "COMPANION_IMPORT_ALLOWED_CLIENTS",
  )
  forwarded_clients = exact_ip_setting(api_environment, "FORWARDED_ALLOW_IPS")
  unsafe_overlap = {
    address
    for address in importer_clients & forwarded_clients
    if not ipaddress.ip_address(address).is_loopback
  }
  if unsafe_overlap:
      fail("importer clients and trusted forwarding proxies must be disjoint so direct host-port requests cannot spoof forwarding headers")
  if image_mode:
    if os.name != "nt":
      for label, root, marker_name in (
        ("state root", state_root, ".comma-companion-state"),
        ("secret root", secret_source, ".comma-companion-secrets"),
      ):
        marker = Path(str(root)) / marker_name
        try:
          metadata = marker.lstat()
        except OSError as error:
          fail(f"{label} ownership marker is unavailable: {error}")
        if (
          marker.is_symlink()
          or not marker.is_file()
          or metadata.st_uid != 0
          or metadata.st_gid != 0
          or metadata.st_mode & 0o777 != 0o400
        ):
          fail(f"{label} ownership marker has unsafe metadata")
    network_name = config.get("networks", {}).get(
      "webserver-proxy",
      {},
    ).get("name")
    if not isinstance(network_name, str) or not network_name:
      fail("proxy network must have an explicit Docker network name")
    gateway = docker_network_gateway(network_name)
    if gateway not in importer_clients:
        fail("release importer clients must include the exact proxy-network gateway observed for the loopback-published host port")
  chunk_bytes = integer_setting(api_environment, "COMPANION_MAX_CHUNK_BYTES")
  artifact_bytes = integer_setting(api_environment, "COMPANION_MAX_ARTIFACT_BYTES")
  typed_artifact_bytes = {
    name: integer_setting(api_environment, name)
    for name in (
      "COMPANION_MAX_VIDEO_ARTIFACT_BYTES",
      "COMPANION_MAX_LOG_ARTIFACT_BYTES",
      "COMPANION_MAX_OTHER_ARTIFACT_BYTES",
    )
  }
  integer_setting(api_environment, "COMPANION_MAX_JSON_BODY_BYTES")
  active_per_device = integer_setting(
    api_environment,
    "COMPANION_MAX_ACTIVE_UPLOADS_PER_DEVICE",
    maximum=128,
  )
  pending_per_device = integer_setting(
    api_environment,
    "COMPANION_MAX_PENDING_UPLOAD_BYTES_PER_DEVICE",
  )
  active_global = integer_setting(
    api_environment,
    "COMPANION_MAX_ACTIVE_UPLOADS_GLOBAL",
    maximum=4096,
  )
  pending_global = integer_setting(
    api_environment,
    "COMPANION_MAX_PENDING_UPLOAD_BYTES_GLOBAL",
  )
  inflight_per_device = integer_setting(
    api_environment,
    "COMPANION_MAX_INFLIGHT_UPLOAD_PATCHES_PER_DEVICE",
    maximum=128,
  )
  inflight_global = integer_setting(
    api_environment,
    "COMPANION_MAX_INFLIGHT_UPLOAD_PATCHES_GLOBAL",
    maximum=4096,
  )
  integer_setting(
    api_environment,
    "COMPANION_MAX_ACTIVE_JOBS",
    maximum=1_000_000,
  )
  integer_setting(
    api_environment,
    "COMPANION_UPLOAD_STALE_SECONDS",
    minimum=60,
  )
  integer_setting(
    api_environment,
    "COMPANION_ARCHIVE_MIN_FREE_BYTES",
    minimum=0,
  )
  float_setting(
    api_environment,
    "COMPANION_ARCHIVE_MIN_FREE_PERCENT",
    minimum=0,
    maximum_exclusive=100,
  )
  if chunk_bytes > artifact_bytes:
    fail("chunk limit must not exceed the artifact limit")
  if any(value > artifact_bytes for value in typed_artifact_bytes.values()):
    fail("typed artifact limits must not exceed the global artifact limit")
  if pending_per_device < artifact_bytes:
    fail("per-device pending-byte limit must cover one maximum-size artifact")
  if active_global < active_per_device:
    fail("global active-upload limit must cover the per-device limit")
  if pending_global < pending_per_device:
    fail("global pending-byte limit must cover the per-device limit")
  if inflight_global < inflight_per_device:
    fail("global in-flight PATCH limit must cover the per-device limit")

  model_filename = build_args.get("COMMA_DYNAMICS_MODEL_FILENAME", "")
  model_image_path = build_args.get("COMMA_DYNAMICS_MODEL_IMAGE_PATH", "")
  model_sha256 = build_args.get("COMMA_DYNAMICS_MODEL_SHA256", "")
  model_context = build.get("additional_contexts", {}).get("model_context", "")
  if (
    not model_filename
    or PurePosixPath(model_filename).name != model_filename
    or model_filename in {".", ".."}
  ):
    fail("dynamics model filename must be one file at the context root")
  image_path = PurePosixPath(model_image_path)
  if (
    not image_path.is_absolute()
    or ".." in image_path.parts
    or image_path.parent != PurePosixPath("/app/artifacts/dynamics")
  ):
    fail("dynamics model image path must be one direct child of /app/artifacts/dynamics")
  if re.fullmatch(r"[0-9a-f]{64}", model_sha256) is None:
    fail("dynamics model SHA-256 must be 64 lowercase hexadecimal characters")
  model_source = Path(model_context) / model_filename
  if not model_source.is_file():
    fail(f"dynamics model source does not exist: {model_source}")
  actual_model_hash = hashlib.sha256(model_source.read_bytes()).hexdigest()
  if actual_model_hash != model_sha256:
    fail(
      "dynamics model source hash does not match COMMA_DYNAMICS_MODEL_SHA256",
    )
  if worker_environment["COMMA_DYNAMICS_MODEL"] != model_image_path:
    fail("worker model path must match the image build argument")
  for name in ("NODE_IMAGE", "PYTHON_IMAGE", "UV_IMAGE"):
    reference = build_args.get(name, "")
    if re.fullmatch(r".+@sha256:[0-9a-f]{64}", reference) is None:
      fail(f"{name} must use a pinned sha256 manifest digest")

  total_memory = int(api.get("mem_limit", 0)) + int(worker.get("mem_limit", 0))
  if total_memory > 3 * 1024 * 1024 * 1024:
    fail("combined memory ceiling exceeds 3 GiB")

  if image_mode:
    image = inspect_image(api["image"])
    image_config = image.get("Config", {})
    labels = image_config.get("Labels", {})
    expected_labels = {
      "org.opencontainers.image.revision": revision,
      "no.danielv.comma-companion.source-bundle.sha256": source_hash,
      "no.danielv.comma-companion.dynamics-model.path": model_image_path,
      "no.danielv.comma-companion.dynamics-model.sha256": model_sha256,
      "no.danielv.comma-companion.base.node": build_args["NODE_IMAGE"],
      "no.danielv.comma-companion.base.python": build_args["PYTHON_IMAGE"],
      "no.danielv.comma-companion.base.uv": build_args["UV_IMAGE"],
    }
    for name, expected in expected_labels.items():
      if labels.get(name) != expected:
        fail(f"release image label {name} does not match rendered Compose")
    if image_config.get("Cmd") != ["api"]:
      fail("release image default command must be api")
    if image_config.get("User") != "65532:65532":
      fail("release image must default to UID/GID 65532")

  message = "compose validation passed: quotas, model, isolated API, and secretless worker"
  if release_mode:
    message += " (release mode)"
  if image_mode:
    message += " with verified image labels"
  print(message)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
