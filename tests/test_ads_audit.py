"""Plan v2 3.3 item 2: `mseo ads audit`, data-driven checks over the week's sheet tables."""

from __future__ import annotations

import csv
import json

import pytest

from mseo import cli, workspace
from mseo.ads import audit
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL
from tests.conftest import FX, edit_config
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


def test_every_check_has_the_four_fields(fx):
    seed_tables(fx, "fx")
    out = audit.audit(fx, W)
    assert out["checks"] and all(set(c) >= {"name", "status", "detail", "evidence"} for c in out["checks"])
    assert all(c["status"] in ("pass", "warn", "fail", "unchecked") for c in out["checks"])
    assert set(by_name(out)) == set(audit.CHECKS)


def test_fx_findings(fx):
    seed_tables(fx, "fx")
    checks = by_name(audit.audit(fx, W))
    waste = checks["wasted_spend"]
    assert waste["status"] == "fail"
    terms = sorted(r["search_term"] for r in waste["evidence"])
    assert terms == ["free bumper repair kit", "paint shop jobs"]  # cost >= 50 (the default threshold), 0 conversions
    assert all(r["cost"] >= FX.get("ads", {}).get("waste_threshold_usd", 50) for r in waste["evidence"])
    assert checks["broad_match_leakage"]["status"] == "warn"  # (62.5 + 55 + 12) / 269.5 of search-term cost
    assert checks["ai_max_share"]["status"] == "warn" and checks["ai_max_share"]["evidence"]
    assert checks["rsa_assets"]["status"] == "warn" and {r["text"] for r in checks["rsa_assets"]["evidence"]} == \
        {"Quality Work", "Paint work with a written estimate."}
    assert checks["rsa_pins"]["status"] == "warn" and checks["rsa_pins"]["evidence"][0]["ad_group"] == "Paint Jobs"
    dead = checks["ad_schedule"]
    assert dead["status"] == "fail" and [r["hour"] for r in dead["evidence"]] == [3]
    assert checks["locations"]["status"] == "fail" and checks["locations"]["evidence"][0]["state"] == "Nevada"
    assert checks["budget_pacing"]["status"] == "fail" and checks["budget_pacing"]["evidence"][0]["campaign"] == "Synthetic Repairs"
    assert checks["brand_exclusions"]["status"] == "unchecked"
    assert checks["call_asset"]["status"] == "pass"


def test_fy_findings(fy):
    seed_tables(fy, "fy")
    out = audit.audit(fy, W)
    checks = by_name(out)
    assert checks["wasted_spend"]["status"] == "pass" and checks["wasted_spend"]["evidence"] == []
    assert checks["broad_match_leakage"]["status"] == "pass"
    assert checks["locations"]["status"] == "pass"  # every row is in the configured state (OR)
    assert checks["budget_pacing"]["status"] == "pass"
    assert checks["call_asset"]["status"] == "fail"  # phone_calls expected, no calls reported
    assert out["status"] == "fail"


def test_thresholds_come_from_config(root, fx):
    seed_tables(fx, "fx")
    edit_config(root, "fx", lambda c: c.setdefault("ads", {}).update(waste_threshold_usd=60, brand_exclusions_expected=False))
    checks = by_name(audit.audit(workspace.open("fx", keychain=FakeKeychain()), W))
    assert [r["search_term"] for r in checks["wasted_spend"]["evidence"]] == ["free bumper repair kit"]
    assert checks["brand_exclusions"]["status"] == "pass"


def test_dead_hour_needs_spend_and_a_quarter_of_the_pause_clicks():
    rows = [{"campaign": "A", "day_of_week": "MONDAY", "hour": 3, "cost": 60.0, "clicks": 7, "impressions": 100, "conversions": 0},
            {"campaign": "A", "day_of_week": "TUESDAY", "hour": 4, "cost": 60.0, "clicks": 8, "impressions": 100, "conversions": 0},
            {"campaign": "A", "day_of_week": "TUESDAY", "hour": 5, "cost": 20.0, "clicks": 40, "impressions": 100, "conversions": 0},
            {"campaign": "A", "day_of_week": "TUESDAY", "hour": 6, "cost": 90.0, "clicks": 40, "impressions": 100, "conversions": 1}]
    assert [h["hour"] for h in audit.dead_hours(rows, 50, 32)] == [4]  # hour 3: 7 clicks < 32/4; 5: cheap; 6: converts
    assert audit.dead_hour_min_clicks(32) == 8 and audit.DEAD_HOUR_CLICKS_DIVISOR == 4


def test_ad_schedule_detail_and_thresholds_name_both_floors(fx):
    seed_tables(fx, "fx")
    out = audit.audit(fx, W)
    detail = by_name(out)["ad_schedule"]["detail"]
    assert ">= 50 spend" in detail and ">= 7.5 clicks" in detail and "min_clicks_before_pause / 4" in detail
    assert out["thresholds"]["dead_hour_min_clicks"] == 7.5 and out["thresholds"]["dead_hour_clicks_divisor"] == 4


def test_locations_without_a_readable_place_are_unchecked(fx):
    run_id = seed_tables(fx, "fx")
    path = fx.path("data", "runs", run_id, "locations.csv")
    rows = list(csv.DictReader(path.read_text().splitlines()))
    for r in rows:
        r["city"], r["state"] = "", ""  # an old script build: resource names only, nothing resolved
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    rec = json.loads((fx.path("data", "runs", run_id, "record.json")).read_text())
    from mseo.fsutil import sha256_file, write_json_atomic
    for f in rec["files"]:
        if f["path"] == "locations.csv":
            f["sha256"] = sha256_file(path)
    write_json_atomic(fx.path("data", "runs", run_id, "record.json"), rec)
    assert by_name(audit.audit(fx, W))["locations"]["status"] == "unchecked"


def test_cli_writes_json_and_markdown_and_exits_1_on_a_fail(fx, capsys):
    seed_tables(fx, "fx")
    code = cli.main(["ads", "audit", "--workspace", "fx", "--week", W], keychain=FakeKeychain())
    assert code == EXIT_FAIL
    text = capsys.readouterr().out
    assert "wasted_spend" in text and "| check | status | detail |" in text
    data = json.loads(fx.path("data", "weeks", f"{W}.ads-audit.json").read_text())
    assert data["status"] == "fail" and data["week"] == W and data["thresholds"]["waste_threshold_usd"] == 50
    assert fx.path("data", "weeks", f"{W}.ads-audit.md").is_file()
    assert cli.main(["ads", "audit", "--workspace", "fx", "--week", W, "--json"], keychain=FakeKeychain()) == EXIT_FAIL
    printed = json.loads(capsys.readouterr().out)
    assert printed["checks"] and "messages" not in printed


def test_audit_without_tables_is_could_not_run(fx, capsys):
    assert cli.main(["ads", "audit", "--workspace", "fx", "--week", W], keychain=FakeKeychain()) == EXIT_COULD_NOT_RUN
