#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path


def main() -> int:
  parser = argparse.ArgumentParser(description="Build the dependency-free historical importer zipapp")
  parser.add_argument(
    "--output",
    type=Path,
    default=Path(__file__).parent / "dist" / "comma-companion-import.pyz",
  )
  args = parser.parse_args()
  args.output.parent.mkdir(parents=True, exist_ok=True)
  package_source = Path(__file__).parent / "comma_companion_importer"
  with tempfile.TemporaryDirectory(prefix="comma-companion-importer-") as temporary:
    stage = Path(temporary)
    shutil.copytree(
      package_source,
      stage / "comma_companion_importer",
      ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    zipapp.create_archive(
      stage,
      target=args.output,
      main="comma_companion_importer.cli:main",
      interpreter="/usr/bin/env python3",
      compressed=True,
    )
  print(args.output.resolve())
  return 0


raise SystemExit(main())
