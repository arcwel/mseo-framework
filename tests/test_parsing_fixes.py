"""Tests verifying parsing fixes, schema validation, and edge cases for customer configuration, drafts, captures, and legacy paths."""

from __future__ import annotations

import copy

import pytest

from mseo.capture import account
from mseo.content import md
from mseo.seal import FRAMEWORK_DENY_READ, FRAMEWORK_SANDBOX_DENY_READ
from mseo.workspace import ConfigInvalid, validate
from tests.conftest import FX


def test_md_link_single_escaping_query_params():
    converted = md.convert("# Title\n\nSee [our guide](https://example.com/path?a=1&b=2).\n")
    assert '<a href="https://example.com/path?a=1&amp;b=2">our guide</a>' in converted.html
    assert "&amp;amp;" not in converted.html


def test_md_link_obfuscated_javascript_scheme_blocked():
    converted = md.convert("# Title\n\n[click](javascript&colon;alert(1))\n")
    assert "javascript:" not in converted.html
    assert "<a " not in converted.html


def test_account_digits_and_normalize_falsy_zero():
    assert account.digits(0) == "0"
    assert account.digits(None) == ""
    assert account.normalize(0) == "0"
    assert account.normalize("0") == "0"
    assert account.normalize(0) == account.normalize("0")
    assert account.normalize(None) == ""


def test_account_verify_with_numeric_zero_id(root):
    from mseo import workspace
    from tests.fakes import FakeKeychain
    ws = workspace.open("fx", keychain=FakeKeychain())
    res = account.verify(ws, "google_ads", {"account_id_seen": 0})
    assert res["status"] in ("wrong_account", "ok")
    assert res["account_id_seen"] == 0


def test_customer_schema_relpath_traversal_refused():
    raw = copy.deepcopy(FX)
    raw["files"] = {**raw["files"], "rules": "../RULES.md"}
    with pytest.raises(ConfigInvalid, match=r"\$\.files\.rules"):
        validate(raw, FX["slug"])


def test_framework_deny_defaults_name_no_customer_or_tool():
    """R7 (plan v2): per-machine legacy files live in the operator deny list, so the framework defaults are generic."""
    assert FRAMEWORK_DENY_READ and FRAMEWORK_SANDBOX_DENY_READ
    assert all("~" in p for p in FRAMEWORK_DENY_READ)
    assert all(p.startswith("~/Library") for p in FRAMEWORK_DENY_READ + FRAMEWORK_SANDBOX_DENY_READ)  # no customer path