"""Regression tests for the first security audit. Each test names the finding it pins."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mseo import seal, workspace
from tests.conftest import FRAMEWORK, FX, FY, edit_config
from tests.fakes import FakeKeychain
from tests.test_seal import WP_ITEM, passing_live_test


@pytest.fixture
def chrome_state(tmp_path):
    path = tmp_path / "Local State"
    path.write_text(json.dumps({"profile": {"info_cache": {"Profile 3": {"name": FX["identity"]["chrome_profile"]}}}}))
    return path


def _seal(root, chrome_state):
    ws = workspace.open("fx", keychain=FakeKeychain({WP_ITEM: {"site": FX["cms"]["site"], "user": "u", "app_password": "p"}}))
    return ws, seal.seal(ws, live_test=passing_live_test, sync=lambda: None, chrome_state=chrome_state)


# H2. customer data outside the workspaces root is denied through the operator's deny list

def test_operator_deny_list_is_read_with_comments_and_home(tmp_path, monkeypatch):
    listing = tmp_path / "deny-read.txt"
    listing.write_text("# legacy data\n~/legacy-a\n\n/opt/legacy-b  \n")
    monkeypatch.setenv("MSEO_OPERATOR_DENY", str(listing))
    paths = seal.operator_deny_paths()
    assert paths == [Path("~/legacy-a").expanduser(), Path("/opt/legacy-b")]


def test_operator_paths_are_denied_to_read_tools_and_the_sandbox(root, tmp_path):
    extra = tmp_path / "legacy-data"
    s = seal.generate_settings("fx", ["fy"], root, extra_deny=[extra])
    assert f"Read(/{extra}/**)" in s["permissions"]["deny"] and f"Edit(/{extra}/**)" in s["permissions"]["deny"]
    assert str(extra) in s["sandbox"]["filesystem"]["denyRead"]


def test_seal_fails_its_check_when_the_operator_deny_list_is_missing(root, chrome_state, monkeypatch, tmp_path):
    monkeypatch.setenv("MSEO_OPERATOR_DENY", str(tmp_path / "absent.txt"))
    _, result = _seal(root, chrome_state)
    check = next(c for c in result.record["checks"] if c["check"] == "operator deny list")
    assert not check["ok"] and result.code == 1


def test_a_changed_operator_deny_list_makes_the_seal_stale(root, chrome_state):
    ws, result = _seal(root, chrome_state)
    assert result.code == 0, result.messages
    listing = Path(__import__("os").environ["MSEO_OPERATOR_DENY"])
    listing.write_text(listing.read_text() + "/opt/another-legacy-folder\n")
    assert any("operator deny list" in p for p in seal.check_seal(ws))


# M2. the framework repo's own settings turn the sandbox on, so denyRead applies to Bash

def test_framework_settings_enable_the_sandbox():
    settings = json.loads((FRAMEWORK / ".claude" / "settings.json").read_text())
    assert settings["sandbox"]["enabled"] is True
    assert "~/Projects/mseo-workspaces" in settings["sandbox"]["filesystem"]["denyRead"]


# M3. generated files that name sibling customers are never committed to a customer repo

def test_template_ignores_generated_seal_files():
    ignored = (FRAMEWORK / "workspace-template" / ".gitignore").read_text().split()
    assert ".claude/settings.json" in ignored and ".mseo/" in ignored


# M4. two workspaces may not share an account ID, a site or a Chrome profile

def test_seal_flags_an_account_id_shared_with_a_sibling(root, chrome_state):
    edit_config(root, "fy", lambda c: c["accounts"]["ga4"].update(id=FX["accounts"]["ga4"]["id"]))
    _, result = _seal(root, chrome_state)
    check = next(c for c in result.record["checks"] if c["check"] == "identifiers unique across workspaces")
    assert not check["ok"] and "fy" in check["detail"] and result.code == 1


def test_seal_flags_a_shared_site(root, chrome_state):
    edit_config(root, "fy", lambda c: c["business"].update(site=FX["business"]["site"]))
    _, result = _seal(root, chrome_state)
    check = next(c for c in result.record["checks"] if c["check"] == "identifiers unique across workspaces")
    assert not check["ok"]


def test_unique_identifiers_pass(root, chrome_state):
    assert FX["accounts"]["ga4"]["id"] != FY["accounts"]["ga4"]["id"]
    _, result = _seal(root, chrome_state)
    check = next(c for c in result.record["checks"] if c["check"] == "identifiers unique across workspaces")
    assert check["ok"], check


# H1. MCP tools (shell servers, file servers, Drive, browsers) bypass Read rules and the sandbox: all denied

def test_sealed_settings_deny_every_mcp_tool(root):
    assert "mcp__*" in seal.generate_settings("fx", ["fy"], root)["permissions"]["deny"]


def test_sealed_workspace_on_disk_denies_mcp(root, chrome_state):
    _seal(root, chrome_state)
    settings = json.loads((root / "fx" / ".claude" / "settings.json").read_text())
    assert "mcp__*" in settings["permissions"]["deny"]


# M1 / M5. the Keychain CLI anywhere in a command, and the publish approval flag, are denied to the agent

def test_keychain_and_approval_rules_match_anywhere(root):
    deny = seal.generate_settings("fx", ["fy"], root)["permissions"]["deny"]
    assert "Bash(*security *-generic-password*)" in deny and "Bash(*--approved*)" in deny
