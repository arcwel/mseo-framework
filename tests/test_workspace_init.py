"""`mseo workspace init`: the guided onboarding (docs/designs/workspace-init.md). Answers come from the fx fixture."""

from __future__ import annotations

import json

import pytest

from mseo import cli, onboard, workspace
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_OK
from tests.conftest import FRAMEWORK, FX
from tests.fakes import FakeKeychain


class Scripted:
    """A prompter fed from a list; records every prompt and whether it was hidden; EOF when the list runs out."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked: list[tuple[str, bool]] = []

    def ask(self, prompt: str, *, hidden: bool = False) -> str:
        self.asked.append((prompt, hidden))
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


@pytest.fixture
def fresh(root, monkeypatch):
    """A workspace made from the template, as `workspace new fz` leaves it."""
    code = cli.main(["workspace", "new", "fz", "--skip-live-test"], keychain=FakeKeychain())
    assert code == EXIT_OK
    return root / "fz"


def _raw(path):
    return json.loads((path / "customer.json").read_text())


TEMPLATE = json.loads((FRAMEWORK / "workspace-template" / "customer.json").read_text())
NEVER = FX["content"]["wording"]["never"][0]


def fx_answers():
    """The non-secret answers for every template placeholder, in file order, taken from fx (never literals).
    The wording.always entry is a key question: it takes two answers (the key, then its value)."""
    out = []
    for q in onboard.questions(TEMPLATE):
        if q.path == "$.slug":
            continue
        if q.key_of:
            out += [NEVER, FX["content"]["wording"]["always"][NEVER]]
        else:
            out.append(onboard.get_at(FX, q.path))
    return out


def fx_answer_map():
    """The same answers keyed by path, for --answers files."""
    out = {}
    for q in onboard.questions(TEMPLATE):
        if q.path == "$.slug":
            continue
        out[q.path] = {"key": NEVER, "value": FX["content"]["wording"]["always"][NEVER]} if q.key_of else onboard.get_at(FX, q.path)
    return out


def test_questions_follow_the_file_and_include_the_dict_key(fresh):
    qs = onboard.questions(_raw(fresh))
    paths = [q.path for q in qs]
    assert paths[0] == "$.business.name" and paths[-1] == "$.cms.site"
    assert "$.content.wording.always['<the same regex>']" in paths
    assert "$.slug" not in paths  # workspace new filled it
    assert not any(p.split(".")[-1].startswith("_") for p in paths)
    assert len(paths) == 19


def test_scripted_run_fills_the_config_and_stores_the_credential(fresh, root):
    kc = FakeKeychain()
    answers = fx_answers() + ["", "editor", "app-pass-word", "", "", ""]  # site defaults to cms.site; three optional keys skipped
    p = Scripted(answers)
    result = onboard.run("fz", p, kc, out=lambda *_: None)
    assert result["status"] == "checks_failed" and result["missing"] == [], result  # config and credential done; doctor has NO lines in the fixture
    ws = workspace.open("fz", keychain=kc)
    assert ws.config["business"]["name"] == FX["business"]["name"]
    assert ws.config["content"]["wording"]["always"] == FX["content"]["wording"]["always"]
    assert "<the same regex>" not in json.dumps(_raw(fresh))
    item = kc.items[("mseo.fz.wordpress", "fz")]
    assert json.loads(item) == {"site": FX["cms"]["site"], "user": "editor", "app_password": "app-pass-word"}
    hidden = [prompt for prompt, h in p.asked if h]
    assert len(hidden) == 1 and "password" in hidden[0].lower()
    assert not p.answers  # every scripted answer was consumed


def test_a_bad_answer_is_asked_again_with_the_rule(fresh):
    answers = fx_answers()
    answers.insert(1, "555-1234")  # a phone that fails the schema pattern, before the good one
    p = Scripted(answers + ["", "editor", "pw", "", "", ""])
    onboard.run("fz", p, FakeKeychain(), out=lambda *_: None)
    phone_prompts = [a for a, _ in p.asked if "business.phone" in a]
    assert len(phone_prompts) == 2 and "does not match" in phone_prompts[1]


def test_quitting_midway_keeps_the_answers_and_a_rerun_asks_only_the_rest(fresh):
    first = Scripted(fx_answers()[:5])
    result = onboard.run("fz", first, FakeKeychain(), out=lambda *_: None)
    assert result["status"] == "incomplete" and len(result["missing"]) == 14
    assert _raw(fresh)["business"]["name"] == FX["business"]["name"]
    second = Scripted(fx_answers()[5:] + ["", "editor", "pw", "", "", ""])
    result = onboard.run("fz", second, FakeKeychain(), out=lambda *_: None)
    assert result["status"] == "checks_failed" and result["missing"] == []
    assert len(second.asked) == 15 + 3 + 3  # 14 questions (the key one asks twice), 3 credential fields, 3 optional keys


def test_without_a_terminal_and_without_answers_it_exits_3_listing_the_missing_fields(fresh, capsys):
    code = cli.main(["workspace", "init", "fz", "--no-prompt"], keychain=FakeKeychain())
    assert code == EXIT_COULD_NOT_RUN
    err = capsys.readouterr().err
    assert "business.name" in err and "19" in err


def test_set_writes_one_field_without_prompting(fresh, capsys):
    code = cli.main(["workspace", "init", "fz", "--no-prompt", "--set", f"$.business.name={FX['business']['name']}"], keychain=FakeKeychain())
    assert code == EXIT_COULD_NOT_RUN  # still incomplete, but the field is written
    assert _raw(fresh)["business"]["name"] == FX["business"]["name"]
    assert "18" in capsys.readouterr().err


def test_answers_file_fills_everything_but_secrets(fresh, tmp_path):
    f = tmp_path / "answers.json"
    f.write_text(json.dumps(fx_answer_map()))
    p = Scripted(["", "editor", "pw", "", "", ""])
    result = onboard.run("fz", p, FakeKeychain(), answers_file=f, out=lambda *_: None)
    assert result["status"] == "checks_failed" and result["missing"] == [] and result["stored"] == ["mseo.fz.wordpress"]
    assert not any("(" in a and "optional" not in a and "wordpress" not in a for a, _ in p.asked)


def test_optional_keys_are_written_when_given(fresh):
    p = Scripted(fx_answers() + ["", "editor", "pw", "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdef", "", ""])
    onboard.run("fz", p, FakeKeychain(), out=lambda *_: None)
    assert _raw(fresh)["accounts"]["google_ads"]["scripts_sheet_id"] == "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdef"


def test_placeholder_errors_point_at_init(fresh):
    with pytest.raises(workspace.ConfigInvalid) as e:
        workspace.open("fz")
    assert str(e.value).rstrip().endswith("run: mseo workspace init fz")


def test_existing_credential_is_not_asked_again(fresh):
    kc = FakeKeychain({("mseo.fz.wordpress", "fz"): {"site": FX["cms"]["site"], "user": "u", "app_password": "p"}})
    p = Scripted(fx_answers() + ["", "", ""])
    result = onboard.run("fz", p, kc, out=lambda *_: None)
    assert result["status"] == "checks_failed" and result["missing"] == [] and result["credentials_missing"] == []
    assert not any(h for _, h in p.asked)


# ---------------------------------------------------------------- review findings (2026-11-08)

CREDS = ["", "editor", "pw"]
SKIPS = ["", "", ""]


def test_a_bad_slug_is_refused_before_anything_is_read_or_written(root):
    for bad in ("../evil", "fy/../fy", "FX", "x" * 40):
        with pytest.raises(workspace.WorkspaceError):
            onboard.run(bad, Scripted([]), FakeKeychain(), out=lambda *_: None)
    assert not (root.parent / "evil").exists()


def test_set_on_an_unknown_or_malformed_path_is_refused_with_a_message(fresh):
    before = _raw(fresh)
    result = onboard.run("fz", Scripted([]), FakeKeychain(), sets=["$.foo.bar=1"], out=lambda *_: None)
    assert any("$.foo.bar" in n and "not applied" in n for n in result["notes"])
    assert _raw(fresh) == before
    for bad in ("$=1", "$.a[9]=1", "$.business.name.x=1", "noequals"):
        try:
            result = onboard.run("fz", Scripted([]), FakeKeychain(), sets=[bad], out=lambda *_: None)
        except onboard.OnboardError:
            continue
        assert any("not applied" in n for n in result["notes"]), bad
    assert _raw(fresh) == before


def test_a_bad_given_value_is_reported_and_the_rest_still_runs(fresh):
    answers = fx_answers()
    answers.pop(1)  # the phone comes from --set instead, and is wrong
    p = Scripted(answers + CREDS + SKIPS)
    result = onboard.run("fz", p, FakeKeychain(), sets=["$.business.phone=555"], out=lambda *_: None)
    assert result["status"] == "incomplete" and result["missing"] == ["$.business.phone"]
    assert any("business.phone: not applied" in n for n in result["notes"])
    assert _raw(fresh)["business"]["name"] == FX["business"]["name"]  # the others were asked and kept


def test_three_bad_answers_leave_the_placeholder_and_move_on(fresh):
    answers = fx_answers()
    answers[1:1] = ["bad", "worse", "<still>"]
    answers.pop(4)  # the good phone is never reached: three tries used
    p = Scripted(answers + CREDS + SKIPS)
    result = onboard.run("fz", p, FakeKeychain(), out=lambda *_: None)
    assert result["missing"] == ["$.business.phone"]
    assert any("business.phone: left unfilled" in n for n in result["notes"])
    assert _raw(fresh)["business"]["site"] == FX["business"]["site"]  # asked after the phone gave up


def test_key_question_refuses_empty_and_duplicate_keys_and_keeps_the_placeholder_on_give_up(fresh):
    answers = fx_answers()
    i = answers.index(NEVER)  # the never[0] value; the key question follows it
    answers[i + 1:i + 3] = ["", "x", "\\bother\\b", "v", "\\bother\\b", "v2"]  # empty key, then a duplicate of the never? no: three tries
    p = Scripted(answers + CREDS + SKIPS)
    result = onboard.run("fz", p, FakeKeychain(), out=lambda *_: None)
    always = _raw(fresh)["content"]["wording"]["always"]
    assert set(always) == {"\\bother\\b"} and always["\\bother\\b"] == "v"
    assert result["status"] in ("ok", "checks_failed")  # no placeholder left


def test_tokens_and_quote_round_trip():
    for key in ("plain", "with.dot", "it's", "back\\slash", "[br]", "a-b"):
        path = "$" + onboard._quote(key)
        assert onboard._tokens(path) == [key], path
    assert onboard._tokens("$.a[2]['k']") == ["a", 2, "k"]


def test_try_change_reports_an_unknown_key_and_does_not_confuse_prefixes(fresh):
    raw = _raw(fresh)
    _, problem = onboard.try_change(raw, lambda r: onboard.set_at(r, "$.nope", "x", create=True))
    assert problem and "nope" in problem
    new, problem = onboard.try_change(raw, lambda r: onboard.set_at(r, "$.business.name", FX["business"]["name"]))
    assert problem is None and new["business"]["name"] == FX["business"]["name"]


def test_a_rejected_optional_value_leaves_no_empty_parent(fresh):
    p = Scripted(fx_answers() + CREDS + ["bad id with spaces", "", ""])
    result = onboard.run("fz", p, FakeKeychain(), out=lambda *_: None)
    raw = _raw(fresh)
    assert "scripts_sheet_id" not in raw["accounts"]["google_ads"] and "site_audit" not in raw
    assert any("scripts_sheet_id: skipped" in n for n in result["notes"])


def test_no_credential_stored_means_incomplete_and_exit_3(fresh, capsys):
    f = fresh.parent / "answers.json"
    f.write_text(json.dumps(fx_answer_map()))
    code = cli.main(["workspace", "init", "fz", "--no-prompt", "--answers", str(f), "--json"], keychain=FakeKeychain())
    assert code == EXIT_COULD_NOT_RUN
    out = capsys.readouterr().out
    data = json.loads(out)  # --json stdout stays pure JSON
    assert data["status"] == "incomplete" and data["credentials_missing"] == ["mseo.fz.wordpress"]


def test_the_secret_never_appears_in_any_output(fresh, capsys):
    lines = []
    p = Scripted(fx_answers() + ["", "editor", "s3cr3t-app-pw"] + SKIPS)
    result = onboard.run("fz", p, FakeKeychain(), out=lines.append)
    text = "\n".join(lines) + json.dumps(result) + capsys.readouterr().out + capsys.readouterr().err
    assert "s3cr3t-app-pw" not in text
    assert all(h for prompt, h in p.asked if "app_password" in prompt)


def test_ctrl_c_keeps_the_answers_typed_so_far(fresh):
    class Interrupting(Scripted):
        def ask(self, prompt, *, hidden=False):
            if not self.answers:
                raise KeyboardInterrupt
            return super().ask(prompt, hidden=hidden)

    with pytest.raises(KeyboardInterrupt):
        onboard.run("fz", Interrupting(fx_answers()[:3]), FakeKeychain(), out=lambda *_: None)
    raw = _raw(fresh)
    assert raw["business"]["name"] == FX["business"]["name"] and raw["business"]["site"] == FX["business"]["site"]


def test_all_string_answers_from_a_terminal(fresh):
    p = Scripted([str(a) for a in fx_answers()] + CREDS + SKIPS)
    result = onboard.run("fz", p, FakeKeychain(), out=lambda *_: None)
    assert result["status"] in ("ok", "checks_failed") and result["missing"] == []
