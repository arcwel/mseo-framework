"""A lone workspace still gets a live isolation test: the seal keeps an empty .mseo-canary folder beside the
workspaces, denied like any sibling, for the live test to plant its canary in."""

import shutil

from mseo import live_test, seal, workspace
from tests.fakes import FakeKeychain


def test_seal_creates_the_canary_folder_and_denies_it(root):
    shutil.rmtree(root / "fy")
    w = workspace.open("fx", keychain=FakeKeychain())
    res = seal.seal(w, sync=None)
    assert (root / seal.CANARY_DIR).is_dir()
    assert res.record["siblings"] == [seal.CANARY_DIR]
    deny = (root / "fx" / ".claude" / "settings.json").read_text()
    assert seal.CANARY_DIR in deny


def test_live_test_prefers_a_real_workspace_then_the_canary(root):
    (root / seal.CANARY_DIR).mkdir()
    assert live_test.target_sibling(root, ["fy", seal.CANARY_DIR]) == "fy"
    shutil.rmtree(root / "fy")
    assert live_test.target_sibling(root, [seal.CANARY_DIR]) == seal.CANARY_DIR


def test_lint_does_not_report_the_canary_as_unprotected(root):
    from mseo import lint_strings

    (root / seal.CANARY_DIR).mkdir()
    assert seal.CANARY_DIR not in lint_strings.unprotected_folders([root])


def test_seal_check_stays_current_with_the_canary(root):
    w = workspace.open("fx", keychain=FakeKeychain())
    seal.seal(w, live_test=lambda *_: {"status": "pass", "rows": []}, sync=None)
    assert seal.check_seal(w) == []
