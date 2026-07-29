#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from source_manifest import source_sha256


SERVICE_ROOT = Path(__file__).resolve().parent.parent
DOTENV_PATH = SERVICE_ROOT / ".env"
SOURCE_LABEL = "no.danielv.comma-companion.source-bundle.sha256"


def fail(message: str) -> None:
  raise SystemExit(f"image pin failed: {message}")


def inspect_image(reference: str) -> dict[str, object]:
  result = subprocess.run(
    ["docker", "image", "inspect", reference],
    check=False,
    capture_output=True,
    text=True,
  )
  if result.returncode:
    fail(f"cannot inspect {reference}: {result.stderr.strip()}")
  try:
    images = json.loads(result.stdout)
  except json.JSONDecodeError as error:
    fail(f"docker returned invalid image metadata: {error}")
  if not isinstance(images, list) or len(images) != 1:
    fail("docker did not return exactly one image")
  return images[0]


def update_dotenv(image_id: str) -> None:
  try:
    metadata = DOTENV_PATH.lstat()
  except OSError as error:
    fail(f"cannot inspect {DOTENV_PATH}: {error}")
  if (
    not stat.S_ISREG(metadata.st_mode)
    or DOTENV_PATH.is_symlink()
    or stat.S_IMODE(metadata.st_mode) != 0o600
  ):
    fail(".env must be a regular non-symlink file with mode 0600")
  lines = DOTENV_PATH.read_text(encoding="utf-8").splitlines()
  matching = [
    index
    for index, line in enumerate(lines)
    if line.startswith("COMMA_COMPANION_IMAGE=")
  ]
  if len(matching) != 1:
    fail(".env must contain exactly one COMMA_COMPANION_IMAGE assignment")
  lines[matching[0]] = f"COMMA_COMPANION_IMAGE={image_id}"

  descriptor, temporary_name = tempfile.mkstemp(
    prefix=".env.pin.",
    dir=SERVICE_ROOT,
    text=True,
  )
  temporary = Path(temporary_name)
  try:
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
      stream.write("\n".join(lines) + "\n")
      stream.flush()
      os.fsync(stream.fileno())
    os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
    if hasattr(os, "chown"):
      os.chown(temporary, metadata.st_uid, metadata.st_gid)
    os.replace(temporary, DOTENV_PATH)
  finally:
    temporary.unlink(missing_ok=True)


def main() -> int:
  parser = argparse.ArgumentParser(
    description="Pin COMMA_COMPANION_IMAGE to the verified local image ID.",
  )
  parser.add_argument("image", help="freshly built source-tagged image")
  arguments = parser.parse_args()

  image = inspect_image(arguments.image)
  image_id = image.get("Id")
  if not isinstance(image_id, str) or re.fullmatch(
    r"sha256:[0-9a-f]{64}",
    image_id,
  ) is None:
    fail("image has no immutable sha256 config ID")
  config = image.get("Config")
  labels = config.get("Labels") if isinstance(config, dict) else None
  actual_source = source_sha256()
  if not isinstance(labels, dict) or labels.get(SOURCE_LABEL) != actual_source:
    fail("image source label does not match the current deterministic bundle")

  update_dotenv(image_id)
  print(f"pinned COMMA_COMPANION_IMAGE={image_id}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
