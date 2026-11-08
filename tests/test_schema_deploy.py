"""`mseo wp schema-deploy`: the site-wide business JSON-LD rendered from customer.json and facts.json, deployed as the
head snippet "mseo business schema" with read-back (plan 3.2 item 2). WordPress is faked."""

from __future__ import annotations

import json

import pytest

from mseo import cli, workspace
from mseo.cms import wordpress
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from tests.conftest import FX, FY, edit_config, seal_for_tests
from tests.fakes import FakeKeychain, FakeWordPress

CRED = {"site": FX["cms"]["site"], "user": "editor", "app_password": "abcd efgh ijkl"}
FX_FACTS = json.loads((__import__("tests.conftest", fromlist=["SYNTHETIC"]).SYNTHETIC / "fx" / "facts.json").read_text())


@pytest.fixture
def env(root, monkeypatch):
    wp = FakeWordPress(FX["cms"]["site"]).install(monkeypatch, wordpress)
    kc = FakeKeychain({("mseo.fx.wordpress", "fx"): CRED})
    seal_for_tests(root, ("fx",), keychain=kc)
    return root, wp, kc


def _ws(kc):
    return workspace.open("fx", keychain=kc)


# ---------------------------------------------------------------- rendering

def test_postal_address_is_split_into_parts():
    loc = FX["locations"][0]["address"]
    parts = wordpress.postal_address(loc)
    assert parts["@type"] == "PostalAddress" and parts["postalCode"] == loc.rsplit(" ", 1)[1]
    assert parts["addressLocality"] == loc.split(", ")[1] and parts["addressRegion"] == loc.split(", ")[2].split()[0]
    assert wordpress.postal_address("Unit 4, somewhere unusual") == {"@type": "PostalAddress", "streetAddress": "Unit 4, somewhere unusual"}


def test_days_of_week_expands_ranges_and_lists():
    assert wordpress.days_of_week("Mon-Thu") == ["Monday", "Tuesday", "Wednesday", "Thursday"]
    assert wordpress.days_of_week("Sat-Sun") == ["Saturday", "Sunday"]
    assert wordpress.days_of_week("Fri") == ["Friday"]
    assert wordpress.days_of_week("Mon, Wed") == ["Monday", "Wednesday"]
    assert wordpress.days_of_week("someday") == []


def test_business_schema_from_config_and_facts(env):
    root, wp, kc = env
    data = wordpress.business_schema(_ws(kc), FX_FACTS)
    assert data["@type"] == "LocalBusiness" and data["name"] == FX["business"]["name"]
    assert data["telephone"] == FX["business"]["phone"] and data["url"] == FX["business"]["site"] + "/"
    assert [a["streetAddress"] for a in data["address"]] == [l["address"].split(",")[0] for l in FX["locations"]]
    cities = {FX["locations"][i]["address"].split(", ")[1] for i in range(len(FX["locations"]))}
    assert {c["name"] for c in data["areaServed"]} == cities
    open_rules = [h for h in FX_FACTS["business"]["hours"] if not h.get("closed")]
    assert len(data["openingHoursSpecification"]) == len(open_rules)
    assert data["openingHoursSpecification"][0]["opens"] == open_rules[0]["opens"]
    assert "sameAs" not in data  # nothing invented: no social profiles are configured


def test_schema_type_comes_from_config(root, monkeypatch):
    edit_config(root, "fx", lambda c: c["business"].update(schema_type="ProfessionalService"))
    data = wordpress.business_schema(workspace.open("fx", keychain=FakeKeychain()), {})
    assert data["@type"] == "ProfessionalService" and "openingHoursSpecification" not in data


def test_fy_renders_its_own_single_address(root):
    ws = workspace.open("fy", keychain=FakeKeychain())
    data = wordpress.business_schema(ws, {})
    assert data["name"] == FY["business"]["name"] and isinstance(data["address"], dict)
    assert data["address"]["streetAddress"] == FY["locations"][0]["address"].split(",")[0]


# ---------------------------------------------------------------- deploy

def test_schema_deploy_posts_a_published_head_snippet_and_reads_it_back(env):
    root, wp, kc = env
    assert cli.main(["wp", "schema-deploy", "--workspace", "fx"], keychain=kc) == EXIT_OK
    snippet = next(iter(wp.snippets.values()))
    assert snippet["title"]["raw"] == wordpress.SCHEMA_SNIPPET_TITLE and snippet["status"] == "publish"
    assert snippet["meta"]["_elementor_location"] == wordpress.ELEMENTOR_LOCATIONS["head"]
    code = snippet["meta"]["_elementor_code"]
    assert code.startswith('<script type="application/ld+json">')
    body = json.loads(code.split(">", 1)[1].rsplit("</script>", 1)[0])
    assert body["@type"] == "LocalBusiness" and body["name"] == FX["business"]["name"]
    assert ("GET", "wp/v2/elementor_snippet/9") in wp.calls  # read-back
    assert set(wp.sites) == {FX["cms"]["site"]}


def test_schema_deploy_read_back_mismatch_is_a_failure(env):
    root, wp, kc = env
    wp.snippet_mangle = True
    assert cli.main(["wp", "schema-deploy", "--workspace", "fx"], keychain=kc) == EXIT_FAIL


def test_schema_deploy_dry_run_sends_nothing_and_writes_nothing(env, capsys):
    root, wp, kc = env
    before = sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())
    assert cli.main(["wp", "schema-deploy", "--workspace", "fx", "--dry-run", "--json"], keychain=kc) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "dry-run" and "application/ld+json" in out["markup"] and out["schema_type"] == "LocalBusiness"
    assert wp.snippets == {} and not any(m == "POST" for m, _ in wp.calls)
    assert sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file()) == before


def test_schema_deploy_needs_a_current_seal_unless_dry_run(root, monkeypatch):
    wp = FakeWordPress(FX["cms"]["site"]).install(monkeypatch, wordpress)
    kc = FakeKeychain({("mseo.fx.wordpress", "fx"): CRED})
    assert cli.main(["wp", "schema-deploy", "--workspace", "fx"], keychain=kc) == EXIT_COULD_NOT_RUN
    assert wp.calls == []
    assert cli.main(["wp", "schema-deploy", "--workspace", "fx", "--dry-run"], keychain=kc) == EXIT_OK


def test_snippet_from_content_needs_a_title(env):
    root, wp, kc = env
    with pytest.raises(wordpress.WPError):
        wordpress.snippet_deploy(_ws(kc), None, content="<script></script>")
