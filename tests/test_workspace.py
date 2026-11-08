"""workspace.open: slug rules, the path resolver, the cwd guard, read-only config, speed."""

import os
import statistics
import time
from pathlib import Path

import pytest

from mseo import workspace
from mseo.workspace import ConfigInvalid, OutsideWorkspace, WorkspaceMismatch
from tests.conftest import FX


@pytest.mark.parametrize("slug", ["", "a", "Fx", "1fx", "fx/../fy", "../fy", "/tmp/fx", "fx_y", "x" * 40, "fx.y"])
def test_bad_slugs_are_refused(root, slug):
    with pytest.raises(ConfigInvalid):
        workspace.open(slug)


def test_missing_workspace(root):
    with pytest.raises(ConfigInvalid, match="no workspace"):
        workspace.open("zz")


def test_symlinked_workspace_pointing_outside_is_refused(root, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "customer.json").write_text((root / "fx" / "customer.json").read_text().replace('"fx"', '"fz"'))
    os.symlink(outside, root / "fz")
    with pytest.raises(ConfigInvalid, match="directly under"):
        workspace.open("fz")


def test_symlinked_workspace_pointing_at_a_sibling_is_refused(root):
    os.symlink(root / "fx", root / "fz")
    with pytest.raises(ConfigInvalid):
        workspace.open("fz")


def test_workspaces_root_from_env(root):
    assert workspace.workspaces_root() == root.resolve()


def test_workspaces_root_default(monkeypatch):
    monkeypatch.delenv("MSEO_WORKSPACES", raising=False)
    assert workspace.workspaces_root() == Path(os.path.expanduser("~/Projects/mseo-workspaces")).resolve()


def test_path_joins_under_root(root):
    ws = workspace.open("fx")
    assert ws.path("data", "content_log.json") == (root / "fx" / "data" / "content_log.json").resolve()
    assert ws.root == (root / "fx").resolve()


@pytest.mark.parametrize("parts", [("..", "fy", "customer.json"), ("/etc/passwd",), ("data", "..", "..", "fy")])
def test_path_escapes_raise(root, parts):
    ws = workspace.open("fx")
    with pytest.raises(OutsideWorkspace):
        ws.path(*parts)


def test_path_through_a_symlink_out_raises(root):
    os.symlink(root / "fy", root / "fx" / "link-to-fy")
    ws = workspace.open("fx")
    with pytest.raises(OutsideWorkspace):
        ws.path("link-to-fy", "customer.json")


def test_cwd_in_another_workspace_is_a_mismatch(root, monkeypatch):
    monkeypatch.chdir(root / "fx" / "data")
    with pytest.raises(WorkspaceMismatch, match="cwd is inside fx, --workspace is fy"):
        workspace.open("fy")


def test_cwd_in_the_same_workspace_is_fine(root, monkeypatch):
    monkeypatch.chdir(root / "fx" / "data")
    assert workspace.open("fx").slug == "fx"


def test_cwd_at_the_root_or_outside_is_fine(root, monkeypatch):
    monkeypatch.chdir(root)
    workspace.open("fx")


def test_explicit_cwd_argument(root):
    with pytest.raises(WorkspaceMismatch):
        workspace.open("fx", cwd=root / "fy")


def test_cwd_guard_runs_before_the_config_is_read(root, monkeypatch):
    (root / "fy" / "customer.json").write_text("{broken")
    monkeypatch.chdir(root / "fx")
    with pytest.raises(WorkspaceMismatch):
        workspace.open("fy")


def test_config_is_read_only(root):
    ws = workspace.open("fx")
    with pytest.raises(TypeError):
        ws.config["slug"] = "fy"
    with pytest.raises(TypeError):
        ws.config["business"]["name"] = "x"
    with pytest.raises((TypeError, AttributeError)):
        ws.config["locations"].append({})
    with pytest.raises(AttributeError):
        ws.slug = "fy"


def test_account(root):
    assert workspace.open("fx").account("google_ads") == FX["accounts"]["google_ads"]["id"]
    with pytest.raises(ConfigInvalid, match="no account"):
        workspace.open("fx").account("gmail")


def test_files_resolve_through_path(root):
    ws = workspace.open("fx")
    assert ws.file("facts") == (root / "fx" / "facts.json").resolve()
    assert ws.file("content_log") == (root / "fx" / "data" / "content_log.json").resolve()


def test_open_is_fast(root):
    times = []
    for _ in range(20):
        start = time.perf_counter()
        workspace.open("fy")
        times.append(time.perf_counter() - start)
    assert statistics.median(times) < 0.050
