"""mseo capture verify: the account a capture saw is compared with the workspace's account, never copied from config.

Synthetic captures in tests/workspaces/<slug>/captures/{ok,wrong,unverified}/: the page's account_id_seen matches,
differs, or is absent (only the legacy expected_id_seen boolean).
"""

from __future__ import annotations

import json

import pytest

from mseo import cli, workspace
from mseo.capture import account
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from tests.conftest import FX, FY
from tests.fakes import FakeKeychain


def verify(root, slug, case, *extra):
    return cli.main(["capture", "verify", str(root / slug / "captures" / case), "--workspace", slug,
                     "--source", "google_ads", "--json", *extra], keychain=FakeKeychain())


def record(root, slug, case):
    return json.loads((root / slug / "captures" / case / "google_ads.run.json").read_text())


@pytest.mark.parametrize("slug,cfg", [("fx", FX), ("fy", FY)])
def test_matching_account_is_ok_and_keeps_rows(root, slug, cfg):
    assert verify(root, slug, "ok") == EXIT_OK
    r = record(root, slug, "ok")
    assert r["status"] == "ok" and r["account_id_expected"] == cfg["accounts"]["google_ads"]["id"]
    assert r["captures"][0]["rows"]


@pytest.mark.parametrize("slug", ["fx", "fy"])
def test_wrong_account_records_what_the_page_showed_and_saves_no_rows(root, slug):
    assert verify(root, slug, "wrong") == EXIT_FAIL
    r = record(root, slug, "wrong")
    seen = json.loads((root / slug / "captures" / "wrong" / "google_ads-devices.tsv").read_text()
                      .splitlines()[0].split(" ", 1)[1])["account_id_seen"]
    assert (r["status"], r["account_id_seen"]) == ("wrong_account", seen)
    assert r["account_id_seen"] != r["account_id_expected"]
    assert all(c["rows"] is None for c in r["captures"])


@pytest.mark.parametrize("slug", ["fx", "fy"])
def test_only_the_legacy_boolean_is_unverified(root, slug):
    assert verify(root, slug, "unverified") == EXIT_FAIL
    r = record(root, slug, "unverified")
    assert (r["status"], r["account_id_seen"]) == ("unverified_account", None)
    assert all(c["rows"] is None for c in r["captures"])


def test_another_workspaces_capture_is_wrong_account(root):
    """fy's good capture, copied into fx, is fy's account: fx rejects it."""
    src = root / "fy" / "captures" / "ok" / "google_ads-devices.tsv"
    dest = root / "fx" / "captures" / "copied"
    dest.mkdir()
    (dest / src.name).write_text(src.read_text())
    assert verify(root, "fx", "copied") == EXIT_FAIL
    assert record(root, "fx", "copied")["status"] == "wrong_account"


def test_ids_compare_digits_only(root):
    ws = workspace.open("fx", keychain=FakeKeychain())
    expected = FX["accounts"]["google_ads"]["id"]
    assert account.verify(ws, "google_ads", {"account_id_seen": expected.replace("-", "")})["status"] == "ok"
    sc = FX["accounts"]["search_console"]["id"]
    assert account.verify(ws, "search_console", {"account_id_seen": sc.upper()})["status"] == "ok"


def test_no_capture_for_the_source_is_3(root):
    assert cli.main(["capture", "verify", str(root / "fx" / "captures" / "ok"), "--workspace", "fx",
                     "--source", "ga4"], keychain=FakeKeychain()) == EXIT_COULD_NOT_RUN


def test_run_dir_in_another_workspace_is_refused(root):
    assert cli.main(["capture", "verify", str(root / "fy" / "captures" / "ok"), "--workspace", "fx",
                     "--source", "google_ads"], keychain=FakeKeychain()) == EXIT_COULD_NOT_RUN
    assert not (root / "fy" / "captures" / "ok" / "google_ads.run.json").exists()


def test_a_non_ascii_id_never_matches_an_ascii_one():
    # Sonnet pentest low 9: the Kelvin sign lower-cased to k, so a look-alike property compared equal
    from mseo.capture import account as acct

    assert acct.normalize("sc-domain:Kmart.test") != acct.normalize("sc-domain:kmart.test")
    assert acct.normalize("sc-domain:ｋmart.test") != acct.normalize("sc-domain:kmart.test")  # fullwidth k
    assert acct.normalize("sc-domain:Kmart.test") != acct.normalize("sc-domain:Kmart.test".lower())
    assert acct.normalize("SC-Domain:KMart.test") == acct.normalize("sc-domain:kmart.test")
