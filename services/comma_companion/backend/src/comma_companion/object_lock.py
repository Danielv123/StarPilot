from __future__ import annotations

import os
from pathlib import Path


class ObjectLock:
  def __init__(self, root: Path, sha256: str):
    if (
      len(sha256) != 64
      or any(character not in "0123456789abcdef" for character in sha256)
    ):
      raise ValueError("object lock requires a lowercase SHA-256")
    self.path = Path(root) / f"{sha256}.lock"
    self._descriptor: int | None = None

  def __enter__(self) -> ObjectLock:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
    if os.name == "nt":
      import msvcrt

      if os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b"\0")
      os.lseek(descriptor, 0, os.SEEK_SET)
      msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
    else:
      import fcntl

      fcntl.flock(descriptor, fcntl.LOCK_EX)
    self._descriptor = descriptor
    return self

  def __exit__(self, *_args: object) -> None:
    descriptor = self._descriptor
    self._descriptor = None
    if descriptor is None:
      return
    try:
      if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
      else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
      os.close(descriptor)
