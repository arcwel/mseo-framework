"""Regression tests for the independent review of M1 (2026-11-05). Each test names the finding it pins."""

from __future__ import annotations

import json
import subprocess
import urllib.request

import pytest

from mseo import cli, live_test, seal, workspace
from mseo.capture import account
from mseo.cms import wordpress
from mseo.content import check
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from mseo.secrets import CredentialError, Keychain, MissingCredential
from tests.conftest import FX
from tests.fakes import FakeKeychain, FakeWordPress

SITE = FX["cms"]["site"]


def run(*argv, kc=None):
    return cli.main(list(argv), keychain=kc or FakeKeychain())


# 1. credentials must never follow a redirect

def test_authenticated_requests_refuse_redirects():
    handler = wordpress.NoRedirect()
    req = urllib.request.Request(f"{SITE}/wp-json/wp/v2/users/me")
    with pytest.raises(wordpress.WPError, match="redirect"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://other.test/steal")


def test_the_opener_uses_the_no_redirect_handler():
    assert any(isinstance(h, wordpress.NoRedirect) for h in wordpress.OPENER.handlers)


# 2. sidecars and captures are read only inside the workspace

def test_a_symlinked_sidecar_into_another_workspace_is_an_error(root, monkeypatch):
    monkeypatch.setattr(check, "today", lambda: __import__("datetime").date(2026, 11, 4))
    ws = workspace.open("fx", keychain=FakeKeychain())
    receipt = ws.path("drafts", "good_article.md.deslop.json")
    stolen = root / "fy" / "stolen.json"
    stolen.write_text(receipt.read_text())
    receipt.unlink()
    receipt.symlink_to(stolen)
    g = check.run(check.Draft.load(ws, ws.path("drafts", "good_article.md"), "article", slug="good-article"), ws,
                  only="humanizer")[0]
    assert g.status == "error" and "outside" in g.detail


def test_a_symlinked_capture_into_another_workspace_is_not_read(root):
    run_dir = root / "fx" / "captures" / "linked"
    run_dir.mkdir()
    (run_dir / "google_ads-devices.tsv").symlink_to(root / "fx" / "captures" / "ok" / "google_ads-devices.tsv")
    (run_dir / "other.tsv").symlink_to(root / "fy" / "captures" / "ok" / "google_ads-devices.tsv")
    ws = workspace.open("fx", keychain=FakeKeychain())
    code, record = account.verify_dir(ws, run_dir, "google_ads")
    assert [c["file"] for c in record["captures"]] == ["google_ads-devices.tsv"]


# 3. anything unexpected is exit 3, never 1

def test_lint_on_a_missing_file_is_3(root):
    assert run("content", "lint", str(root / "fx" / "drafts" / "nope.md"), "--workspace", "fx") == EXIT_COULD_NOT_RUN


def test_lint_on_a_non_utf8_file_is_3(root):
    bad = root / "fx" / "drafts" / "bad.md"
    bad.write_bytes(b"\xff\xfe\xfa")
    assert run("content", "lint", str(bad), "--workspace", "fx") == EXIT_COULD_NOT_RUN


def test_dupes_with_a_bad_date_is_3(root):
    assert run("content", "dupes", "gbp", str(root / "fx" / "drafts" / "good_gbp.txt"), "--workspace", "fx",
               "--date", "2026-99-99") == EXIT_COULD_NOT_RUN


def test_a_non_json_wordpress_answer_is_3(root, monkeypatch):
    def html(*a, **k):
        raise ValueError("Expecting value: line 1 column 1")

    monkeypatch.setattr(wordpress, "request", html)
    kc = FakeKeychain({("mseo.fx.wordpress", "fx"): {"site": SITE, "user": "u", "app_password": "pppppp"}})
    assert run("wp", "verify", "--workspace", "fx", kc=kc) == EXIT_COULD_NOT_RUN


def test_errors_go_to_stderr_so_json_stays_clean(root, capsys):
    run("content", "lint", str(root / "fx" / "drafts" / "nope.md"), "--workspace", "fx", "--json")
    out = capsys.readouterr()
    assert out.out == "" and "error" in out.err


# 4. a fresh workspace's seal can pass: the live test brings its own sample draft

def test_live_test_plants_and_removes_its_own_sample_draft(root, monkeypatch):
    ws = workspace.open("fx", keychain=FakeKeychain())
    seen = {}

    def fake_run(argv, **kw):
        prompt = argv[2]
        seen["prompt"] = prompt
        seen["exists"] = ws.path(live_test.SAMPLE_DRAFT).exists()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(live_test.shutil, "which", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(live_test.subprocess, "run", fake_run)
    monkeypatch.setattr(live_test.Path, "home", classmethod(lambda cls: root.parent / "home"))
    live_test.run(ws, ["fy"])
    assert seen["exists"] and f"content check {live_test.SAMPLE_DRAFT}" in seen["prompt"]
    assert not ws.path(live_test.SAMPLE_DRAFT).exists()


# 5. only numeric IDs compare by digits

def test_text_ids_are_not_reduced_to_their_digits(root):
    ws = workspace.open("fx", keychain=FakeKeychain())
    expected = FX["accounts"]["search_console"]["id"]
    other = expected.replace("example", "another1") + "1"
    assert account.verify(ws, "search_console", {"account_id_seen": other})["status"] == "wrong_account"
    assert account.normalize("sc-domain:a1.test") != account.normalize("sc-domain:b1.test")
    assert account.normalize(FX["accounts"]["google_ads"]["id"]) == account.normalize(
        FX["accounts"]["google_ads"]["id"].replace("-", " "))


# 6. a failed read-back still reports the post id, so publish can log it and resume

def test_post_create_keeps_the_id_when_the_read_back_fails(root, monkeypatch):
    wp = FakeWordPress(SITE).install(monkeypatch, wordpress)
    real = wp.request

    def flaky(site, user, pw, method, path, payload=None, query=None):
        if method == "GET" and path.startswith("wp/v2/posts/"):
            raise wordpress.WPError("timed out")
        return real(site, user, pw, method, path, payload, query)

    monkeypatch.setattr(wordpress, "request", flaky)
    kc = FakeKeychain({("mseo.fx.wordpress", "fx"): {"site": SITE, "user": "u", "app_password": "pppppp"}})
    ws = workspace.open("fx", keychain=kc)
    code, res = wordpress.post_create(ws, ws.path("drafts", "good_article.md"), "good-article", publish=True)
    assert code == EXIT_FAIL and res["id"] == 42 and any("read-back" in p for p in res["problems"])


# 7. workspace new fails when a sibling could not be re-sealed

def test_new_reports_a_failed_reseal(root, monkeypatch):
    monkeypatch.setattr(seal, "reseal_all", lambda **kw: {"fx": {"code": 3, "error": "live test failed"}})
    assert run("workspace", "new", "acme-dental") == EXIT_COULD_NOT_RUN


def test_new_tolerates_unconfigured_siblings(root, monkeypatch):
    monkeypatch.setattr(seal, "reseal_all", lambda **kw: {"fx": {"code": 1, "live_test": "skipped"}})
    assert run("workspace", "new", "acme-dental") == EXIT_OK


# 8. a Keychain error is not "no item"

def test_keychain_distinguishes_not_found_from_other_errors(monkeypatch):
    kc = Keychain()
    monkeypatch.setattr(kc, "_run", lambda argv: subprocess.CompletedProcess(argv, 44, "", ""))
    assert kc.exists("mseo.fx.wordpress", "fx") is False
    with pytest.raises(MissingCredential):
        kc.get("mseo.fx.wordpress", "fx")
    monkeypatch.setattr(kc, "_run", lambda argv: subprocess.CompletedProcess(argv, 51, "", "locked"))
    with pytest.raises(CredentialError) as exc:
        kc.exists("mseo.fx.wordpress", "fx")
    assert not isinstance(exc.value, MissingCredential)
    with pytest.raises(CredentialError):
        kc.set("mseo.fx.wordpress", "fx", {"a": "b"})


# spec: exit 2 means "already exists"; dupes warnings are exit 0 with status warn

def test_dupes_warning_only_is_exit_0(root, capsys):
    post = root / "fx" / "drafts" / "cert.txt"
    post.write_text(f"We are {FX['business']['certifications'][0]}.\n")
    assert run("content", "dupes", "gbp", str(post), "--workspace", "fx", "--date", "2026-11-04", "--json") == EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "warn"


# spec: a stale or missing seal makes doctor exit 3

def test_doctor_without_a_seal_is_3(root):
    assert run("doctor", "--workspace", "fx") == EXIT_COULD_NOT_RUN


# 10. a redirected live URL must land on the site

def test_live_url_redirected_off_site_is_refused(monkeypatch):
    from mseo import publish

    monkeypatch.setattr(publish, "check_url", lambda url, timeout=20: {"url": url, "status": 200, "ok": True,
                                                                        "final": "https://other.test/good-article/"})
    assert publish.live_problem(f"{SITE}/good-article/", "article", "good-article", SITE)
