"""Recon fixtures (build plan, "recon before code"): real page snapshots and exports saved by a read-only live run.

They hold customer data, so they live in the workspace (`<ws>/recon/<source>/`), never in this repository. Point
MSEO_LIVE_FIXTURES at a workspace's recon folder to run these; without it they are skipped.
"""

import os
from pathlib import Path

import pytest

from mseo.adapters import ga4, google_ads, search_console
from mseo.browser import extract

ROOT = Path(os.environ["MSEO_LIVE_FIXTURES"]) if os.environ.get("MSEO_LIVE_FIXTURES") else None
pytestmark = pytest.mark.skipif(ROOT is None or not ROOT.is_dir(), reason="MSEO_LIVE_FIXTURES not set")


def _one(source: str, pattern: str) -> Path:
    found = sorted((ROOT / source).glob(pattern))
    if not found:
        pytest.skip(f"no {source}/{pattern} recon file")
    return found[0]


def test_search_console_export_and_snapshot():
    parsed = search_console.parse(_one("search_console", "*.zip"))
    assert len(parsed["dates"]) == 7 and parsed["tables"]["queries"]
    prop, reason = extract.sc_selected_property(_one("search_console", "snapshot-*.txt").read_text())
    assert reason is None and prop.startswith("https://")


def test_ga4_export_and_snapshot():
    parsed = ga4.parse(_one("ga4", "*.csv"))
    assert parsed["rows"] and {r["date"] for r in parsed["rows"]} <= {parsed["start"], parsed["end"]} | {
        r["date"] for r in parsed["rows"]}
    pid, reason = extract.ga4_selected_property(_one("ga4", "snapshot-*.txt").read_text())
    assert reason is None and pid.isdigit()


def test_google_ads_export_and_header():
    parsed = google_ads.parse(_one("google_ads", "*.csv"))
    assert parsed["rows"] and all(r["cost"] is not None for r in parsed["rows"])
    cid, reason = extract.ads_header_customer_id(_one("google_ads", "header-*.txt").read_text())
    assert reason is None and len(cid) == 12
