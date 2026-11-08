"""The live agent test on this Mac: a real sealed `claude -p` run in fx against sibling fy (pytest -m mac_live).

If any of rows 1-5 succeeds, the agent layer moves to Alternative H before another customer onboards.
"""

import shutil

import pytest

from mseo import live_test, seal, workspace
from tests.conftest import SYNTHETIC
from tests.fakes import FakeKeychain

pytestmark = pytest.mark.mac_live


@pytest.fixture
def sealed_fx(tmp_path, monkeypatch):
    if not shutil.which("claude"):
        pytest.skip("claude is not on PATH")
    root = tmp_path / "mseo-workspaces"
    for slug in ("fx", "fy"):
        shutil.copytree(SYNTHETIC / slug, root / slug)
    monkeypatch.setenv("MSEO_WORKSPACES", str(root))
    return workspace.open("fx", cwd=root, keychain=FakeKeychain())


def test_live_agent_rows(sealed_fx):
    result = seal.seal(sealed_fx, live_test=live_test.run, sync=None)
    live = result.record["live_test"]
    rows = {r["row"]: r for r in live["rows"]}
    for n in (1, 2, 3, 4, 5):
        assert rows[n]["ok"] is True, f"row {n} was not denied: {rows[n]['why']} -> Alternative H"
    assert rows[6]["ok"] is True, rows[6]["why"]
    if rows[7]["ok"] is not None:
        assert rows[7]["ok"] is True, rows[7]["why"]
    assert live["status"] in ("pass", "incomplete")


def test_live_pull_rows(sealed_fx):
    """Rows 8 and 10 must hold under the pull settings; rows 9 and 11 need Chrome connected and a writable inbox."""
    result = seal.seal(sealed_fx, live_test=None, pull_test=live_test.run_pull, sync=None)
    pull = result.record["pull_test"]
    rows = {r["row"]: r for r in pull["rows"]}
    assert rows[8]["ok"] is True, rows[8]["why"]
    assert rows[10]["ok"] is True, rows[10]["why"]
    assert rows[11]["ok"] is True, rows[11]["why"]
    assert pull["status"] in ("pass", "incomplete"), pull
