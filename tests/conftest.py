"""Shared fixtures: a temporary workspaces root holding copies of the synthetic fx and fy workspaces."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SYNTHETIC = TESTS / "workspaces"
FRAMEWORK = TESTS.parent


def synthetic_config(slug: str) -> dict:
    """A synthetic workspace's customer.json. Tests take customer values from here, never as literals."""
    return json.loads((SYNTHETIC / slug / "customer.json").read_text())


FX = synthetic_config("fx")
FY = synthetic_config("fy")


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    """MSEO_WORKSPACES pointed at a temp folder with fx and fy in it; cwd outside every workspace."""
    ws_root = tmp_path / "mseo-workspaces"
    ws_root.mkdir()
    for slug in ("fx", "fy"):
        shutil.copytree(SYNTHETIC / slug, ws_root / slug)
    monkeypatch.setenv("MSEO_WORKSPACES", str(ws_root))
    deny = tmp_path / "operator-deny-read.txt"
    deny.write_text("# synthetic legacy data folder\n" + str(tmp_path / "legacy-data") + "\n")
    monkeypatch.setenv("MSEO_OPERATOR_DENY", str(deny))
    monkeypatch.chdir(tmp_path)
    return ws_root


def edit_config(root: Path, slug: str, change) -> dict:
    """Load a workspace's customer.json, apply `change(config)`, write it back."""
    path = root / slug / "customer.json"
    config = json.loads(path.read_text())
    change(config)
    path.write_text(json.dumps(config, indent=2))
    return config


def seal_for_tests(root: Path, slugs=("fx", "fy"), keychain=None) -> None:
    """Write a current seal (passing live test, no venv sync) so commands that need one can run in a test."""
    from mseo import seal, workspace
    from tests.fakes import FakeKeychain

    def passing(ws, siblings):
        return {"status": "pass", "rows": [{"row": n, "ok": True} for n in range(1, 8)]}

    for slug in slugs:
        ws = workspace.open(slug, cwd=root, keychain=keychain or FakeKeychain())
        seal.seal(ws, live_test=passing, sync=None)
