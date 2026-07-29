#!/usr/bin/env python3
import os
import re
from pathlib import Path

HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = HERE + "/.."

blacklist = [
  ".git/",
  ".github/workflows/",

  "matlab.*.md",

  # no LFS or submodules in release
  ".lfsconfig",
  ".gitattributes",
  ".git$",
  ".gitmodules",

  # server-only companion service
  r"^services/comma_companion(?:/|$)",
]

# gets you through the blacklist
whitelist: list[str] = [
]


def is_release_file(relative_path: str) -> bool:
  relative_path = relative_path.replace("\\", "/")
  blacklisted = any(re.search(pattern, relative_path) for pattern in blacklist)
  whitelisted = any(re.search(pattern, relative_path) for pattern in whitelist)
  return not blacklisted or whitelisted


if __name__ == "__main__":
  for f in Path(ROOT).rglob("**/*"):
    if not (f.is_file() or f.is_symlink()):
      continue

    rf = f.relative_to(ROOT).as_posix()
    if not is_release_file(rf):
      continue

    print(rf)
