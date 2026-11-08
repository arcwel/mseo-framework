"""The installed tool (uv tool install) needs templates/ and workspace-template/ next to the package (2026-11-08 reinstall crash)."""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("uv"), reason="uv is not on PATH")
def test_wheel_ships_templates_and_workspace_template(tmp_path):
    proc = subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(ROOT)], capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-800:]
    wheel = next(tmp_path.glob("mseo-*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    for needed in ("templates/posts/recipes.json", "templates/ads/mseo-report-export.gs", "workspace-template/customer.json",
                   "mseo/schema/customer.v2.json"):
        assert needed in names, f"{needed} missing from the wheel"
    assert not any(n.startswith("templates/posts/") and n.endswith(".check.json") for n in names)
