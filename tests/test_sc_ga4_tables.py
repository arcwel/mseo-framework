"""Extra report tables (plan 3.1 item 4, audit 25): Search Console countries, devices, search appearance and the
generative AI report; GA4 landing pages and key events. The week file is not changed by any of them."""

from __future__ import annotations

import csv
import io
import zipfile

import pytest

from mseo import week, workspace
from mseo.adapters import export, ga4, search_console
from mseo.browser import run as browser_run
from tests.conftest import FX, edit_config
from tests.fakes import FakeKeychain
from tests.test_export_pull import DAYS, sc_zip

GA4_HEAD = "# ----------------------------------------\n# Fixture\n# Landing page\n# {start}-{end}\n# ----------------------------------------\n\n"
META = "# Start date: 20261026\n# End date: 20261101\n"


def _zip_with(path, extra: dict, drop_metrics=False):
    sc_zip(path)
    buf = io.BytesIO()
    with zipfile.ZipFile(path) as old, zipfile.ZipFile(buf, "w") as new:
        for name in old.namelist():
            data = old.read(name).decode()
            if drop_metrics:
                rows = list(csv.reader(io.StringIO(data)))
                keep = [i for i, h in enumerate(rows[0]) if h not in ("Clicks", "CTR", "Position")]
                data = "".join(",".join(r[i] for i in keep) + "\n" for r in rows)
            new.writestr(name, data)
        for name, text in extra.items():
            new.writestr(name, text)
    path.write_bytes(buf.getvalue())
    return path


COUNTRIES = "Country,Clicks,Impressions,CTR,Position\nUnited States,30,900,3.33%,7.1\nCanada,2,40,5%,9\n"
DEVICES = "Device,Clicks,Impressions,CTR,Position\nMobile,20,600,3.33%,7\nDesktop,12,340,3.53%,7.3\n"
APPEARANCE = "Search Appearance,Clicks,Impressions,CTR,Position\nAI Overview,1,80,1.25%,2\n"


def test_countries_devices_and_search_appearance_are_parsed_when_present(tmp_path):
    parsed = search_console.parse_zip(_zip_with(tmp_path / "e.zip", {
        "Countries.csv": COUNTRIES, "Devices.csv": DEVICES, "Search appearance.csv": APPEARANCE}))
    assert parsed["tables"]["countries"][0] == {"country": "United States", "clicks": 30, "impressions": 900,
                                                "ctr": 0.0333, "position": 7.1}
    assert [r["device"] for r in parsed["tables"]["devices"]] == ["Mobile", "Desktop"]
    assert parsed["tables"]["search_appearance"][0]["search_appearance"] == "AI Overview"
    files = search_console.write(parsed, tmp_path)
    assert {f["path"] for f in files} >= {"countries.csv", "devices.csv", "search_appearance.csv", "dates.csv"}
    rows = list(csv.DictReader((tmp_path / "devices.csv").open()))
    assert rows[0]["device"] == "Mobile" and rows[0]["impressions"] == "600"


def test_a_zip_without_the_extra_members_still_parses_the_core_tables(tmp_path):
    parsed = search_console.parse_zip(sc_zip(tmp_path / "e.zip"))
    assert set(parsed["tables"]) == {"dates", "queries", "pages"}


def test_generative_ai_zip_accepts_impressions_only_and_prefixes_the_files(tmp_path):
    path = _zip_with(tmp_path / "g.zip", {"Countries.csv": COUNTRIES, "Devices.csv": DEVICES}, drop_metrics=True)
    with pytest.raises(export.LayoutChanged):
        search_console.parse_zip(path)  # the ordinary report needs every metric
    parsed = search_console.parse_generative_ai(path)
    assert parsed["dates"] == DAYS
    assert parsed["tables"]["dates"][0]["clicks"] is None and parsed["tables"]["dates"][0]["impressions"] == 100
    files = search_console.write_generative_ai(parsed, tmp_path)
    assert all(f["path"].startswith("generative_ai_") for f in files)
    assert (tmp_path / "generative_ai_pages.csv").is_file() and not (tmp_path / "pages.csv").exists()


def test_generative_ai_zip_without_impressions_is_layout_changed(tmp_path):
    p = tmp_path / "bad.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Dates.csv", "Date,Position\n2026-10-26,3\n")
    with pytest.raises(export.LayoutChanged):
        search_console.parse_generative_ai(p)


def test_search_console_extra_tables_come_from_config(root):
    ws = workspace.open("fx", keychain=FakeKeychain())
    assert search_console.extra_tables(ws) == []
    edit_config(root, "fx", lambda c: c["accounts"]["search_console"].update(tables=["generative_ai", "unknown_table"]))
    ws = workspace.open("fx", keychain=FakeKeychain())
    assert search_console.extra_tables(ws) == ["generative_ai"]
    assert search_console.extra_prompt("generative_ai") == "export_search_console_generative_ai"


# ---------------------------------------------------------------- GA4

LANDING = META + "\nLanding page,Sessions,Engaged sessions,Key events\n/,120,80,3\n/contact/,40,30,2\nGrand total,160,110,5\n"
EVENTS = META + "\nEvent name,Event count,Key events\npage_view,900,0\nphone_call,12,12\nform_submit,4,4\n"


def test_ga4_landing_pages_parse_and_write(tmp_path):
    path = tmp_path / "landing.csv"
    path.write_text(LANDING)
    parsed = ga4.parse_landing_pages(path)
    assert (parsed["start"], parsed["end"]) == ("2026-10-26", "2026-11-01")
    assert parsed["rows"] == [{"landingPage": "/", "sessions": 120, "engagedSessions": 80, "keyEvents": 3},
                              {"landingPage": "/contact/", "sessions": 40, "engagedSessions": 30, "keyEvents": 2}]
    files = ga4.write_landing_pages(parsed, tmp_path)
    assert files[0]["path"] == "landing_pages.csv" and files[0]["rows"] == 2


def test_ga4_key_events_parse_and_write(tmp_path):
    path = tmp_path / "events.csv"
    path.write_text(EVENTS)
    parsed = ga4.parse_key_events(path)
    assert [r["eventName"] for r in parsed["rows"]] == ["page_view", "phone_call", "form_submit"]
    assert parsed["rows"][1] == {"eventName": "phone_call", "eventCount": 12, "keyEvents": 12}
    assert ga4.write_key_events(parsed, tmp_path)[0]["path"] == "key_events.csv"


def test_ga4_extra_table_with_wrong_columns_is_layout_changed(tmp_path):
    path = tmp_path / "landing.csv"
    path.write_text(META + "\nPage path,Sessions\n/,1\n")
    with pytest.raises(export.LayoutChanged, match="Landing page"):
        ga4.parse_landing_pages(path)
    bare = tmp_path / "x.csv"
    bare.write_text("Event name,Event count,Key events\na,1,1\n")
    with pytest.raises(export.ExportError, match="Start date"):
        ga4.parse_key_events(bare)


def test_ga4_extra_tables_come_from_config_and_the_traffic_parser_is_unchanged(root):
    ws = workspace.open("fx", keychain=FakeKeychain())
    assert ga4.extra_tables(ws) == []
    edit_config(root, "fx", lambda c: c["accounts"]["ga4"].update(tables=["key_events", "landing_pages"]))
    ws = workspace.open("fx", keychain=FakeKeychain())
    assert ga4.extra_tables(ws) == ["landing_pages", "key_events"]
    parsed = ga4.parse_csv(ws.path("exports", "ga4-traffic-2026-W44.csv"))
    assert parsed["rows"] and set(parsed["rows"][0]) == {"date", "channel", "sessions", "engagedSessions", "keyEvents", "totalUsers"}


def test_week_file_reads_only_the_default_tables():
    assert week.FILES == {"search_console": "dates.csv", "ga4": "traffic.csv", "google_ads": "campaigns.csv"}


@pytest.mark.parametrize("name", ["export_ga4_landing_pages", "export_ga4_key_events", "export_search_console_generative_ai"])
def test_extra_prompts_render_with_the_pull_values(root, name):
    ws = workspace.open("fx", keychain=FakeKeychain())
    vals = {"url": "https://example.test/report", "email": FX["identity"]["email"], "snapshot": "snapshot-x.txt",
            "totals_snapshot": "totals-x.txt", "start_human": "Oct 26, 2026", "end_human": "Nov 1, 2026",
            "customer_id": FX["accounts"]["search_console"]["id"]}
    text = browser_run.prompt(name, ws, source="ga4", **vals)
    assert FX["identity"]["email"] in text and "{" not in text.split("\n\n", 2)[2].replace("{the page URL}", "")
    assert "data/inbox/snapshot-x.txt" in text and "data/inbox/totals-x.txt" in text


# ------------------------------------------------------------------ wiring in browser_pull (owner: integration)

def test_extra_specs_follow_the_configured_tables(root):
    from mseo import workspace
    from mseo.adapters import browser_pull
    from tests.conftest import edit_config
    from tests.fakes import FakeKeychain

    edit_config(root, "fx", lambda c: (c["accounts"]["ga4"].__setitem__("tables", ["key_events", "landing_pages"]),
                                       c["accounts"]["search_console"].__setitem__("tables", ["generative_ai"])))
    ws = workspace.open("fx", keychain=FakeKeychain())
    period = {"start": "2026-10-26", "end": "2026-11-01", "tz": ws.config["timezone"]}
    ga = browser_pull.extra_specs(ws, "ga4", browser_pull.values(ws, "ga4", period))
    assert [e["table"] for e in ga] == ["landing_pages", "key_events"]  # EXTRA order, not config order
    assert ga[0]["url"].endswith("&r=lifecycle-landing-page") and "explorer?params=" in ga[0]["url"]
    sc = browser_pull.extra_specs(ws, "search_console", browser_pull.values(ws, "search_console", period))
    assert [e["table"] for e in sc] == ["generative_ai"] and sc[0]["prompt"] == "export_search_console_generative_ai"
    assert browser_pull.extra_specs(ws, "google_ads", {"url": "x"}) == []
