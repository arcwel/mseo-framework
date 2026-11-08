"""Plan v2 3.3 item 1: `mseo ads tracking`, the conversion-tracking gate over the sheet's conversion_actions table."""

from __future__ import annotations

import json

import pytest

from mseo import cli, workspace
from mseo.ads import tracking
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from tests.conftest import FY, edit_config
from tests.fakes import FakeKeychain
from tests.test_ads_sheet import W, seed_tables, with_sheet


@pytest.fixture
def fx(root):
    with_sheet(root, "fx")
    return workspace.open("fx", keychain=FakeKeychain())


@pytest.fixture
def fy(root):
    with_sheet(root, "fy")
    return workspace.open("fy", keychain=FakeKeychain())


def by_name(out):
    return {c["name"]: c for c in out["checks"]}


def test_fx_passes_the_gate(fx):
    seed_tables(fx, "fx")
    out = tracking.gate(fx, W)
    checks = by_name(out)
    assert out["status"] == "pass", checks
    assert checks["phone_call_primary"]["status"] == "pass"
    assert checks["ga4_imports_secondary"]["status"] == "pass"
    assert checks["one_primary_per_category"]["status"] == "pass"
    assert checks["enhanced_conversions"]["status"] == "unchecked" and "Settings" in checks["enhanced_conversions"]["detail"]
    assert fx.path("data", "weeks", f"{W}.tracking.json").is_file()


def test_fy_fails_on_the_ga4_import_and_the_missing_call_action(fy):
    seed_tables(fy, "fy")
    out = tracking.gate(fy, W)
    checks = by_name(out)
    assert out["status"] == "fail"
    assert checks["phone_call_primary"]["status"] == "fail"
    assert checks["ga4_imports_secondary"]["status"] == "fail" and checks["ga4_imports_secondary"]["evidence"]
    assert checks["one_primary_per_category"]["status"] == "fail"  # two primary SUBMIT_LEAD_FORM actions count twice


def test_two_primary_call_actions_warn_other_shared_categories_fail():
    """A call asset and a website call both primary count calls twice at worst: warn. Two primary lead forms fail."""
    calls = [{"name": "Calls from ads", "type": "AD_CALL", "category": "PHONE_CALL_LEAD", "status": "ENABLED", "primary": "true",
              "origin": "GOOGLE_ADS"},
             {"name": "Website calls", "type": "WEBSITE_CALL", "category": "PHONE_CALL_LEAD", "status": "ENABLED", "primary": "true",
              "origin": "GOOGLE_ADS"}]
    cfg = {"phone_calls": True, "ga4_import": False, "enhanced_conversions": False, "primary_actions": []}
    check = {c["name"]: c for c in tracking.checks(calls, [{"calls": 3}], cfg)}["one_primary_per_category"]
    assert check["status"] == "warn" and "phone-call" in check["detail"] and len(check["evidence"]) == 2
    forms = [{**a, "type": "WEBPAGE", "category": "SUBMIT_LEAD_FORM"} for a in calls]
    check = {c["name"]: c for c in tracking.checks(calls + forms, [], cfg)}["one_primary_per_category"]
    assert check["status"] == "fail" and "SUBMIT_LEAD_FORM" in check["detail"] and "PHONE_CALL_LEAD" in check["detail"]


def test_configured_primary_actions_must_be_primary(root, fx):
    seed_tables(fx, "fx")
    edit_config(root, "fx", lambda c: c.setdefault("ads", {}).setdefault("tracking", {})
                .__setitem__("primary_actions", ["GA4 form_submit", "Not configured"]))
    out = tracking.gate(workspace.open("fx", keychain=FakeKeychain()), W)
    checks = by_name(out)
    assert checks["primary_actions"]["status"] == "fail"
    detail = checks["primary_actions"]["detail"]
    assert "GA4 form_submit" in detail and "Not configured" in detail


def test_phone_calls_not_required_skips_the_call_checks(root, fy):
    seed_tables(fy, "fy")
    edit_config(root, "fy", lambda c: c.setdefault("ads", {}).setdefault("tracking", {})
                .update(phone_calls=False, ga4_import=False))
    out = tracking.gate(workspace.open("fy", keychain=FakeKeychain()), W)
    checks = by_name(out)
    assert checks["phone_call_primary"]["status"] == "pass" and checks["ga4_imports_secondary"]["status"] == "pass"
    assert checks["call_volume"]["status"] == "pass"


def test_cli_exit_codes_and_json(root, fx, fy, capsys):
    seed_tables(fx, "fx")
    seed_tables(fy, "fy")
    assert cli.main(["ads", "tracking", "--workspace", "fx", "--week", W], keychain=FakeKeychain()) == EXIT_OK
    text_out = capsys.readouterr().out
    assert "phone_call_primary" in text_out
    assert cli.main(["ads", "tracking", "--workspace", "fy", "--week", W, "--json"], keychain=FakeKeychain()) == EXIT_FAIL
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "fail" and "messages" not in printed
    data = json.loads(fy.path("data", "weeks", f"{W}.tracking.json").read_text())
    assert data["status"] == "fail" and data["workspace"] == "fy" and data["account"] == FY["accounts"]["google_ads"]["id"]


def test_no_tables_for_the_week_is_could_not_run(fx, capsys):
    assert cli.main(["ads", "tracking", "--workspace", "fx", "--week", W], keychain=FakeKeychain()) == EXIT_COULD_NOT_RUN
    assert "pull" in capsys.readouterr().err
