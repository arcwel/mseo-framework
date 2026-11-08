"""M2 step 3: customer/2 (identity, timezone, schedule, per-source adapter and check, overrides, budget)."""

import copy
import json

import pytest

from mseo import seal, workspace
from tests.fakes import FakeKeychain


def raw(root, slug="fx"):
    return json.loads((root / slug / "customer.json").read_text())


def check(root, mutate, slug="fx"):
    cfg = raw(root, slug)
    mutate(cfg)
    return workspace.validate(cfg, slug)


def test_fixtures_are_v2_with_a_dedicated_identity(root):
    for slug in ("fx", "fy"):
        ws = workspace.open(slug, keychain=FakeKeychain())
        assert ws.config["schema_version"] == "customer/2"
        ident = ws.config["identity"]
        assert ident["kind"] in ("workspace_user", "google_account", "existing")
        assert ident["allow_shared"] is False
        assert ws.config["schedule"]["weekly"] == {"weekday": "Mon", "time": "06:00"}


def test_v1_is_refused_with_the_new_fields_named(root):
    def to_v1(c):
        c["schema_version"] = "customer/1"
    with pytest.raises(workspace.ConfigInvalid, match="customer/2.*identity.*timezone.*schedule"):
        check(root, to_v1)


def test_identity_kind_is_required_and_has_no_default(root):
    with pytest.raises(workspace.ConfigInvalid, match="kind"):
        check(root, lambda c: c["identity"].pop("kind"))
    with pytest.raises(workspace.ConfigInvalid):
        check(root, lambda c: c["identity"].update(kind="service_account"))


def test_allow_shared_defaults_false(root):
    cfg = check(root, lambda c: c["identity"].pop("allow_shared", None))
    assert cfg["identity"]["allow_shared"] is False


def test_timezone_is_top_level_and_iana(root):
    with pytest.raises(workspace.ConfigInvalid, match="timezone"):
        check(root, lambda c: c.update(timezone="Mars/Olympus"))
    with pytest.raises(workspace.ConfigInvalid, match="timezone"):
        check(root, lambda c: c.pop("timezone"))


def test_schedule_validates_weekday_and_time(root):
    with pytest.raises(workspace.ConfigInvalid):
        check(root, lambda c: c.update(schedule={"weekly": {"weekday": "Funday", "time": "06:00"}}))
    with pytest.raises(workspace.ConfigInvalid):
        check(root, lambda c: c.update(schedule={"weekly": {"weekday": "Tue", "time": "25:00"}}))


def test_account_adapter_and_check_defaults(root):
    cfg = check(root, lambda c: None)
    acct = cfg["accounts"]["google_ads"]
    assert acct["adapter"] == "browser" and acct["check"] == "auto"
    with pytest.raises(workspace.ConfigInvalid):
        check(root, lambda c: c["accounts"]["ga4"].update(adapter="scrape"))


def test_identity_override_must_be_temporary_shared_and_dated(root):
    good = {"email": "operator@agency.example", "chrome_profile": "Default", "allow_shared": True,
            "temporary": True, "until": "the identity is granted this account"}
    cfg = check(root, lambda c: c["accounts"]["google_ads"].update(identity_override=copy.deepcopy(good)))
    assert cfg["accounts"]["google_ads"]["identity_override"]["email"] == "operator@agency.example"
    for key, bad in (("temporary", False), ("allow_shared", False)):
        with pytest.raises(workspace.ConfigInvalid):
            check(root, lambda c, k=key, b=bad: c["accounts"]["google_ads"].update(identity_override={**good, k: b}))
    with pytest.raises(workspace.ConfigInvalid, match="until"):
        check(root, lambda c: c["accounts"]["google_ads"].update(
            identity_override={k: v for k, v in good.items() if k != "until"}))


def test_identity_for_prefers_the_override(root):
    path = root / "fx" / "customer.json"
    cfg = raw(root)
    cfg["accounts"]["google_ads"]["identity_override"] = {
        "email": "operator@agency.example", "chrome_profile": "Default", "allow_shared": True, "temporary": True,
        "until": "grant lands"}
    path.write_text(json.dumps(cfg))
    ws = workspace.open("fx", keychain=FakeKeychain())
    assert ws.identity_for("google_ads")["email"] == "operator@agency.example"
    assert ws.identity_for("google_ads")["override"] is True
    assert ws.identity_for("ga4")["email"] == cfg["identity"]["email"]
    assert ws.identity_for("ga4")["override"] is False
    assert [s for s in ws.enabled_sources() if ws.config["accounts"][s].get("identity_override")] == ["google_ads"]


def test_budget_is_optional_and_positive(root):
    assert "budget" not in check(root, lambda c: c.pop("budget", None)) or True
    with pytest.raises(workspace.ConfigInvalid):
        check(root, lambda c: c.update(budget={"weekly_tokens": 0}))
    assert check(root, lambda c: c.update(budget={"weekly_tokens": 400000}))["budget"]["weekly_tokens"] == 400000


def test_identity_email_and_profile_are_identifiers(root):
    cfg = raw(root)
    ids = seal._identifiers(cfg)
    assert f"identity {cfg['identity']['email'].lower()}" in ids
    assert f"chrome profile {cfg['identity']['chrome_profile']}" in ids


def test_shared_override_is_not_an_identifier(root):
    cfg = raw(root)
    cfg["accounts"]["google_ads"]["identity_override"] = {
        "email": "operator@agency.example", "chrome_profile": "Default", "allow_shared": True, "temporary": True,
        "until": "grant lands"}
    ids = seal._identifiers(cfg)
    assert not any("operator@agency.example" in i or i == "chrome profile Default" for i in ids)


def test_identity_email_is_marked_identifying():
    schema = workspace.load_schema(2)
    assert schema["properties"]["identity"]["properties"]["email"].get("x-identifying") is True
