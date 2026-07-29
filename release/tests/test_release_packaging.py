import shutil
import subprocess
from pathlib import Path

import pytest

from release.release_files import is_release_file


ROOT = Path(__file__).resolve().parents[2]


def test_companion_service_is_excluded_from_release_manifest() -> None:
  assert not is_release_file("services/comma_companion/ARCHITECTURE.md")
  assert not is_release_file(r"services\comma_companion\backend\app.py")
  assert is_release_file("services/comma_companion_tools/keep.py")
  assert is_release_file("services/device_helper/keep.py")


def test_prebuilt_packaging_removes_only_companion_service(tmp_path: Path) -> None:
  bash = shutil.which("bash")
  if bash is None:
    pytest.skip("bash is required to exercise prebuilt packaging")

  tree = tmp_path / "tree"
  script = tree / "scripts" / "ci_package_prebuilt_tree.sh"
  script.parent.mkdir(parents=True)
  script.write_bytes((ROOT / "scripts" / "ci_package_prebuilt_tree.sh").read_bytes().replace(b"\r\n", b"\n"))

  (tree / ".github" / "workflows").mkdir(parents=True)
  (tree / "panda" / "board").mkdir(parents=True)
  (tree / "third_party").mkdir()
  companion_file = tree / "services" / "comma_companion" / "source.py"
  other_service_file = tree / "services" / "device_helper" / "keep.py"
  companion_file.parent.mkdir(parents=True)
  other_service_file.parent.mkdir(parents=True)
  companion_file.write_text("server-only\n", encoding="utf-8")
  other_service_file.write_text("device helper\n", encoding="utf-8")

  subprocess.run([bash, "scripts/ci_package_prebuilt_tree.sh"], cwd=tree, check=True)

  assert not companion_file.parent.exists()
  assert other_service_file.is_file()
