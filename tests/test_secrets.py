"""Credentials: Keychain items mseo.<slug>.<source>, read only through the workspace, set from stdin."""

import io
import json
import subprocess

import pytest

from mseo import cli, secrets, workspace
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_EXISTS, EXIT_OK
from tests.conftest import FX, FY
from tests.fakes import FakeKeychain

WP = {"site": FX["cms"]["site"], "user": "editor", "app_password": "abcd efgh ijkl mnop"}


def unquote_interactive(text: str) -> str:
    if not (len(text) >= 2 and text[0] == text[-1] == '"'):
        raise ValueError("not a quoted argument")
    out, chars = [], iter(text[1:-1])
    for ch in chars:
        out.append(next(chars, "") if ch == "\\" else ch)
    return "".join(out)  # the inverse of secrets.quote_interactive, kept with the tests that need it


def test_secret_reads_only_its_own_slugs_item(root):
    kc = FakeKeychain({("mseo.fx.wordpress", "fx"): WP, ("mseo.fy.wordpress", "fy"): {**WP, "site": FY["cms"]["site"]}})
    ws = workspace.open("fx", keychain=kc)
    assert ws.secret("wordpress")["user"] == "editor"
    assert {(s, a) for _, s, a in kc.calls} == {("mseo.fx.wordpress", "fx")}


def test_secret_source_name_is_checked(root):
    ws = workspace.open("fx", keychain=FakeKeychain())
    for bad in ("../fy", "fy.wordpress", "", "Word Press"):
        with pytest.raises(secrets.CredentialError):
            ws.secret(bad)


def test_missing_secret(root):
    ws = workspace.open("fx", keychain=FakeKeychain())
    with pytest.raises(secrets.MissingCredential, match="mseo.fx.wordpress"):
        ws.secret("wordpress")


def test_service_name():
    assert secrets.service_name("fx", "wordpress") == "mseo.fx.wordpress"


# ---------------------------------------------------------------- the real Keychain wrapper, with subprocess faked

class Recorder:
    def __init__(self, results):
        self.results, self.argv = list(results), []

    def __call__(self, argv, **kw):
        self.argv.append((argv, kw))
        code, out = self.results.pop(0)
        return subprocess.CompletedProcess(argv, code, out, "")


def test_keychain_get_uses_list_argv_never_shell(monkeypatch):
    rec = Recorder([(0, json.dumps(WP) + "\n")])
    monkeypatch.setattr(secrets.subprocess, "run", rec)
    assert secrets.Keychain().get("mseo.fx.wordpress", "fx") == WP
    argv, kw = rec.argv[0]
    assert argv == [secrets.SECURITY, "find-generic-password", "-s", "mseo.fx.wordpress", "-a", "fx", "-w"]
    assert not kw.get("shell")


def test_keychain_get_missing(monkeypatch):
    monkeypatch.setattr(secrets.subprocess, "run", Recorder([(44, "")]))
    with pytest.raises(secrets.MissingCredential):
        secrets.Keychain().get("mseo.fx.wordpress", "fx")


def test_keychain_get_non_json_payload(monkeypatch):
    monkeypatch.setattr(secrets.subprocess, "run", Recorder([(0, "plain-password\n")]))
    with pytest.raises(secrets.CredentialError, match="not JSON"):
        secrets.Keychain().get("mseo.fx.wordpress", "fx")


def test_keychain_set_refuses_existing_unless_replace(monkeypatch):
    rec = Recorder([(0, "x")])  # exists check finds it
    monkeypatch.setattr(secrets.subprocess, "run", rec)
    with pytest.raises(secrets.SecretExists):
        secrets.Keychain().set("mseo.fx.wordpress", "fx", WP)
    rec = Recorder([(0, "x"), (0, ""), (0, json.dumps(WP) + "\n")])
    monkeypatch.setattr(secrets.subprocess, "run", rec)
    secrets.Keychain().set("mseo.fx.wordpress", "fx", WP, replace=True)
    argv, kw = rec.argv[1]
    assert argv == [secrets.SECURITY, "-i"]  # the command, secret included, goes on stdin, never in argv
    assert WP["app_password"] not in " ".join(argv)
    assert kw["input"].startswith("add-generic-password -s mseo.fx.wordpress -a fx -U -w ")
    assert json.loads(unquote_interactive(kw["input"].split(" -w ", 1)[1].strip())) == WP


def test_keychain_set_fails_when_the_read_back_differs(monkeypatch):
    # security -i exits 0 even when a command fails, and plain -w stdin truncates at 128 characters: read back.
    rec = Recorder([(44, ""), (0, ""), (0, '{"site": "trunc')])
    monkeypatch.setattr(secrets.subprocess, "run", rec)
    with pytest.raises(secrets.CredentialError, match="read-back"):
        secrets.Keychain().set("mseo.fx.wordpress", "fx", WP)


def test_interactive_quoting_round_trips_quotes_and_backslashes():
    value = json.dumps({"p": 'a"b\\c d$e\'f'})
    assert unquote_interactive(secrets.quote_interactive(value)) == value


# ---------------------------------------------------------------- mseo secret set

def run(argv, kc, stdin=""):
    return cli.main(argv, keychain=kc, stdin=io.StringIO(stdin))


def test_secret_set_writes_the_item(root):
    kc = FakeKeychain()
    assert run(["secret", "set", "--workspace", "fx", "--source", "wordpress"], kc, json.dumps(WP)) == EXIT_OK
    assert ("mseo.fx.wordpress", "fx") in kc.items


def test_secret_set_exists_is_exit_2_unless_replace(root):
    kc = FakeKeychain({("mseo.fx.wordpress", "fx"): WP})
    assert run(["secret", "set", "--workspace", "fx", "--source", "wordpress"], kc, json.dumps(WP)) == EXIT_EXISTS
    assert run(["secret", "set", "--workspace", "fx", "--source", "wordpress", "--replace"], kc, json.dumps(WP)) == EXIT_OK


def test_secret_set_site_must_match_cms_site(root, capsys):
    kc = FakeKeychain()
    bad = {**WP, "site": FY["cms"]["site"]}
    assert run(["secret", "set", "--workspace", "fx", "--source", "wordpress"], kc, json.dumps(bad)) == EXIT_COULD_NOT_RUN
    assert "cms.site" in capsys.readouterr().err
    assert not kc.items


@pytest.mark.parametrize("payload", ["not json", json.dumps({"site": WP["site"], "user": "u"}),
                                     json.dumps({**WP, "extra": 1}), json.dumps([WP]), json.dumps({**WP, "user": ""})])
def test_secret_set_validates_keys(root, payload):
    kc = FakeKeychain()
    assert run(["secret", "set", "--workspace", "fx", "--source", "wordpress"], kc, payload) == EXIT_COULD_NOT_RUN
    assert not kc.items


def test_secret_set_unknown_source(root):
    assert run(["secret", "set", "--workspace", "fx", "--source", "gmail"], FakeKeychain(), "{}") == EXIT_COULD_NOT_RUN


def test_secret_set_needs_workspace(root):
    assert run(["secret", "set", "--source", "wordpress"], FakeKeychain(), json.dumps(WP)) == EXIT_COULD_NOT_RUN


def test_secret_set_never_prints_the_password(root, capsys):
    run(["secret", "set", "--workspace", "fx", "--source", "wordpress"], FakeKeychain(), json.dumps(WP))
    out = capsys.readouterr()
    assert "abcd" not in out.out + out.err
