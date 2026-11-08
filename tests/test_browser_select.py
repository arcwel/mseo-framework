"""M2 step 8 follow-up: with several Chrome browsers connected, each pull selects its identity's browser by device ID
before any page opens (found live on 2026-11-07: the agent refused to guess)."""

import json

import pytest

from mseo import seal, workspace
from mseo.browser import run as browser_run
from tests.conftest import FX
from tests.fakes import FakeKeychain

DEVICE = "0f0e0d0c-0b0a-4009-8807-060504030201"
OTHER = "11111111-2222-4333-8444-555555555555"


def _ws(root, mutate):
    path = root / "fx" / "customer.json"
    cfg = json.loads(path.read_text())
    mutate(cfg)
    path.write_text(json.dumps(cfg))
    return workspace.open("fx", keychain=FakeKeychain())


def test_chrome_device_is_optional_and_must_look_like_a_device_id(root):
    assert "chrome_device" not in workspace.open("fx", keychain=FakeKeychain()).config["identity"]
    w = _ws(root, lambda c: c["identity"].update(chrome_device=DEVICE))
    assert w.identity_for("ga4")["chrome_device"] == DEVICE
    with pytest.raises(workspace.ConfigInvalid):
        _ws(root, lambda c: c["identity"].update(chrome_device="Browser 2"))


def test_override_carries_its_own_device(root):
    over = {"email": "operator@agency.example", "chrome_profile": "Default", "chrome_device": OTHER,
            "allow_shared": True, "temporary": True, "until": "grant lands"}
    w = _ws(root, lambda c: (c["identity"].update(chrome_device=DEVICE),
                             c["accounts"]["google_ads"].update(identity_override=over)))
    assert w.identity_for("google_ads")["chrome_device"] == OTHER
    assert w.identity_for("ga4")["chrome_device"] == DEVICE


def test_prompt_starts_by_selecting_the_device(root):
    w = _ws(root, lambda c: c["identity"].update(chrome_device=DEVICE))
    text = browser_run.prompt("sign_in", w, source="ga4", snapshot="snapshot-account.txt")
    first = text.split("\n\n", 1)[0]
    assert "list_connected_browsers" in first and "select_browser" in first and DEVICE in first
    assert "failed: browser not connected" in first


def test_prompt_without_a_device_refuses_when_several_are_connected(root):
    text = browser_run.prompt("sign_in", workspace.open("fx", keychain=FakeKeychain()), source="ga4",
                              snapshot="snapshot-account.txt")
    assert "failed: no browser configured" in text.split("\n\n", 1)[0]


def test_pull_settings_allow_selecting_but_not_broadcasting(root):
    pull = seal.generate_pull_settings(seal.generate_settings("fx", ["fy"], root))
    for tool in ("select_browser", "list_connected_browsers"):
        assert seal.CHROME_PREFIX + tool in pull["permissions"]["allow"]
        assert seal.CHROME_PREFIX + tool not in pull["permissions"]["deny"]
    assert seal.CHROME_PREFIX + "switch_browser" in pull["permissions"]["deny"]


def test_device_ids_are_not_identifiers(root):
    w = _ws(root, lambda c: c["identity"].update(chrome_device=DEVICE))
    assert not any(DEVICE in i for i in seal._identifiers(w.config))
    assert FX["identity"]["email"]  # the identity email still is (test_customer_v2)
