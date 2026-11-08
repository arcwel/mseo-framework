"""customer.json v1: what the schema accepts and rejects, and the defaults the loader fills."""

import json

import pytest

from mseo import workspace
from mseo.workspace import ConfigInvalid
from tests.conftest import FX, SYNTHETIC, edit_config


@pytest.mark.parametrize("slug", ["fx", "fy"])
def test_synthetic_workspaces_validate(root, slug):
    ws = workspace.open(slug)
    assert ws.config["slug"] == slug


def _rejects(root, change, where):
    edit_config(root, "fx", change)
    with pytest.raises(ConfigInvalid) as exc:
        workspace.open("fx")
    assert where in str(exc.value), str(exc.value)
    return str(exc.value)


def test_unknown_top_level_field(root):
    _rejects(root, lambda c: c.update(drive={"data_folder": "x"}), "drive")


def test_unknown_nested_field(root):
    _rejects(root, lambda c: c["business"].update(fax="1"), "$.business")


def test_missing_required_field_names_its_path(root):
    _rejects(root, lambda c: c["business"].pop("phone"), "$.business")


def test_wrong_type(root):
    _rejects(root, lambda c: c["local_research"].update(radius_miles="ten"), "$.local_research.radius_miles")


def test_comment_keys_are_allowed_at_every_level(root):
    def change(c):
        c["_paths"] = "anything"
        c["business"]["_confirmed"] = "2026-11-02"
        c["content"]["_denylist_note"] = {"free": "form"}
    edit_config(root, "fx", change)
    workspace.open("fx")


def test_placeholder_values_are_rejected(root):
    _rejects(root, lambda c: c["business"].update(name="<Business name>"), "$.business.name")


def test_phone_shape(root):
    digits = "".join(ch for ch in FX["business"]["phone"] if ch.isdigit())
    _rejects(root, lambda c: c["business"].update(phone=".".join([digits[:3], digits[3:6], digits[6:]])), "$.business.phone")
    edit_config(root, "fx", lambda c: c["business"].update(phone="+1" + digits))
    workspace.open("fx")


def test_site_must_be_https(root):
    _rejects(root, lambda c: c["business"].update(site=FX["business"]["site"].replace("https", "http")), "$.business.site")


def test_brand_terms_need_at_least_one_valid_regex(root):
    _rejects(root, lambda c: c["business"].update(brand_terms=[]), "$.business.brand_terms")
    _rejects(root, lambda c: c["business"].update(brand_terms=["(unclosed"]), "$.business.brand_terms")


def test_identity_needs_a_chrome_profile(root):
    _rejects(root, lambda c: c["identity"].pop("chrome_profile"), "$.identity")


def test_cms_site_must_equal_business_site_unless_allowed(root):
    _rejects(root, lambda c: c["cms"].update(site="https://elsewhere.test"), "cms.site")
    edit_config(root, "fx", lambda c: c["cms"].update(allow_other_site=True))
    workspace.open("fx")


def test_slug_must_equal_folder_name(root):
    _rejects(root, lambda c: c.update(slug="fy"), "slug")


def test_unknown_schema_version(root):
    _rejects(root, lambda c: c.update(schema_version="customer/9"), "customer/9")
    _rejects(root, lambda c: c.update(schema_version="partner/1"), "schema_version")


def test_bad_timezone(root):
    _rejects(root, lambda c: c.update(timezone="Mars/Olympus"), "$.timezone")


def test_posting_slot_shape(root):
    _rejects(root, lambda c: c["posting"].update(business_profile_slots=["10:00"]), "$.posting.business_profile_slots")


def test_workspace_relative_file_paths_cannot_escape(root):
    _rejects(root, lambda c: c["files"].update(facts="../fy/facts.json"), "files.facts")
    _rejects(root, lambda c: c["files"].update(facts="/etc/passwd"), "files.facts")


def test_unreadable_json(root):
    (root / "fx" / "customer.json").write_text("{not json")
    with pytest.raises(ConfigInvalid):
        workspace.open("fx")


def test_defaults_are_filled(root):
    ws = workspace.open("fy")
    assert ws.config["files"]["facts"] == "facts.json"
    assert ws.config["files"]["wp_posts"] == "data/wp_posts.json"
    assert ws.config["local_research"]["sources"] == "local_sources.json"
    assert ws.config["cms"]["allow_other_site"] is False
    assert ws.config["accounts"]["google_ads"]["adapter"] == "browser"
    assert ws.config["accounts"]["google_ads"]["enabled"] is True
    assert ws.config["content"]["denylist_claims"]


def test_enabled_sources(root):
    assert workspace.open("fy").enabled_sources() == ["ga4", "google_ads"]


def test_identifying_fields_are_marked():
    schema = json.loads((SYNTHETIC.parent.parent / "mseo" / "schema" / "customer.v2.json").read_text())
    marked = workspace.identifying_paths(schema)
    for path in ("business.name", "business.phone", "business.site", "business.brand_terms[]",
                 "locations[].address", "locations[].footer", "accounts.*.id", "cms.site"):
        assert path in marked, marked
    assert "content.topic_bank[]" not in marked


def test_every_unfilled_placeholder_is_listed_in_file_order(root):
    def many(c):
        c["business"].update(name="<Business name>", phone="<(NNN) NNN-NNNN>")
        c["cms"].update(site="<https://site>")
    edit_config(root, "fx", many)
    with pytest.raises(workspace.ConfigInvalid) as e:
        workspace.open("fx")
    msg = str(e.value)
    assert msg.startswith("customer.json: 3 value(s) still need filling in")
    lines = msg.splitlines()[1:]
    assert lines == ["  $.business.name: replace the <...> placeholder",
                     "  $.business.phone: replace the <...> placeholder",
                     "  $.cms.site: replace the <...> placeholder"], lines
    assert "should not be valid" not in msg
