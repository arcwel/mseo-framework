"""mseo lint customer-strings: every x-identifying value of every workspace, searched for in the framework repo.

Planted strings are built at run time from the workspaces' own config, so this file holds no customer string itself.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mseo import cli, lint_strings
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from tests.conftest import FRAMEWORK, FX, SYNTHETIC

FX = json.loads((SYNTHETIC / "fx" / "customer.json").read_text())
FY = json.loads((SYNTHETIC / "fy" / "customer.json").read_text())


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, root) -> Path:
    """A throwaway framework-shaped repo with the synthetic workspaces under tests/workspaces/."""
    r = tmp_path / "framework"
    (r / "mseo").mkdir(parents=True)
    (r / "tests").mkdir()
    (r / "lint").mkdir()
    subprocess.run(["cp", "-R", str(SYNTHETIC), str(r / "tests" / "workspaces")], check=True)
    (r / "mseo" / "clean.py").write_text("PHONE_RE = r'\\(?\\d{3}\\)?[\\s.-]?\\d{3}[\\s.-]?\\d{4}'\n")
    (r / "lint" / "allow.txt").write_text("")
    git(r, "init", "-q")
    git(r, "add", "-A")
    git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return r


def plant(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(repo, "add", rel)


def lint(repo: Path, *extra: str) -> tuple[int, list[dict]]:
    report = lint_strings.run(repo, extra_roots=[], history="--history" in extra)
    return report.code, report.hits


def test_clean_repo_passes(repo):
    assert lint(repo) == (EXIT_OK, [])


def test_synthetic_workspaces_and_validation_folders_are_out_of_scope(repo):
    plant(repo, "validation/inputs.md", FX["business"]["name"])
    plant(repo, "anything/validation/x.txt", FX["business"]["name"])
    assert lint(repo)[0] == EXIT_OK


def test_business_name_is_found_case_insensitively_with_its_origin(repo):
    plant(repo, "mseo/bad.py", f'DEFAULT = "{FX["business"]["name"].upper()}"\n')
    code, hits = lint(repo)
    assert code == EXIT_FAIL
    hit = hits[0]
    assert (hit["file"], hit["line"], hit["workspace"], hit["field"]) == ("mseo/bad.py", 1, "fx", "business.name")


def test_word_boundaries(repo):
    name = FX["business"]["name"]
    plant(repo, "mseo/ok.py", f"x = 'pre{name.replace(' ', '')}post'\n")
    assert lint(repo)[0] == EXIT_OK


def test_phone_is_matched_by_digits_in_any_format(repo):
    digits = "".join(ch for ch in FY["business"]["phone"] if ch.isdigit())
    plant(repo, "mseo/p.py", f"CALL = '{digits[:3]}.{digits[3:6]}.{digits[6:]}'\n")
    code, hits = lint(repo)
    assert code == EXIT_FAIL
    assert any(h["workspace"] == "fy" and h["field"] == "business.phone" for h in hits)


def test_account_id_is_matched_by_digits(repo):
    digits = FX["accounts"]["google_ads"]["id"].replace("-", "")
    plant(repo, "mseo/a.py", f"ACCOUNT = {digits}\n")
    _, hits = lint(repo)
    assert any(h["field"] == "accounts.google_ads.id" for h in hits)


def test_short_values_and_short_digit_runs_are_skipped(repo, root):
    def change(c):
        c["accounts"]["ga4"]["id"] = "12345"
    from tests.conftest import edit_config
    edit_config(repo / "tests" / "workspaces", "fx", change)
    git(repo, "add", "-A")
    plant(repo, "mseo/n.py", "N = 12345\n")
    assert lint(repo)[0] == EXIT_OK


def test_site_and_its_host_are_found(repo):
    host = FY["business"]["site"].split("//")[1]
    plant(repo, "mseo/s.py", f"# see {host}/about\n")
    _, hits = lint(repo)
    assert any(h["field"] == "business.site" for h in hits)


def test_address_and_footer(repo):
    plant(repo, "docs/x.md", FX["locations"][1]["address"] + "\n")
    _, hits = lint(repo)
    assert any(h["field"] == "locations[1].address" for h in hits)


def test_brand_term_regexes_are_not_matched_as_literals(repo):
    plant(repo, "mseo/r.py", f"pat = r'{FY['business']['brand_terms'][1]}'\n")
    assert all(h["field"] != "business.brand_terms" for h in lint(repo)[1])


def test_fixed_patterns(repo):
    a, b, c, num = "555", "010", "2030", "4100"
    plant(repo, "mseo/f.py", f"x = '({a}) {b}-{c}'\ny = '{a}-{b}-{c}'\nz = '{num} Maple Avenue'\n")
    _, hits = lint(repo)
    kinds = {h["field"] for h in hits}
    assert {"pattern:us_phone", "pattern:street_address"} <= kinds
    assert len([h for h in hits if h["field"] == "pattern:us_phone"]) == 2


def test_real_workspaces_under_mseo_workspaces_are_searched_too(repo, root):
    """CUJ 5 shape: a value from a workspace that only exists under MSEO_WORKSPACES."""
    from tests.conftest import edit_config
    edit_config(root, "fy", lambda c: c["locations"][0].update(footer="Quietwater Dental Studio footer"))
    plant(repo, "mseo/q.py", "FOOTER = 'Quietwater Dental Studio footer'\n")
    report = lint_strings.run(repo, extra_roots=[root])
    assert report.code == EXIT_FAIL and report.hits[0]["field"] == "locations[0].footer"


def test_allow_list(repo):
    plant(repo, "mseo/bad.py", f'X = "{FX["business"]["name"]}"\n')
    (repo / "lint" / "allow.txt").write_text(f"# comment\n\nmseo/bad.py:1 {FX['business']['name']} # fixture name in a doc example\n")
    assert lint(repo)[0] == EXIT_OK
    (repo / "lint" / "allow.txt").write_text(f"mseo/bad.py:* {FX['business']['name']} # whole file\n")
    assert lint(repo)[0] == EXIT_OK
    (repo / "lint" / "allow.txt").write_text(f"mseo/bad.py:2 {FX['business']['name']} # wrong line\n")
    assert lint(repo)[0] == EXIT_FAIL


def test_allow_list_needs_a_reason(repo):
    (repo / "lint" / "allow.txt").write_text(f"mseo/bad.py:1 {FX['business']['name']}\n")
    with pytest.raises(lint_strings.LintError, match="reason"):
        lint(repo)


def test_history_scan_finds_a_value_that_was_committed_and_removed(repo):
    plant(repo, "mseo/old.py", f'X = "{FY["business"]["name"]}"\n')
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "oops")
    (repo / "mseo" / "old.py").unlink()
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "remove")
    assert lint(repo)[0] == EXIT_OK
    code, hits = lint(repo, "--history")
    assert code == EXIT_FAIL and hits[0]["file"].startswith("history:")


def test_cli_output_format(repo, capsys):
    plant(repo, "mseo/bad.py", f'X = "{FX["business"]["name"]}"\n')
    assert cli.main(["lint", "customer-strings", "--repo", str(repo)]) == EXIT_FAIL
    out = capsys.readouterr().out
    assert f'mseo/bad.py:1: {FX["business"]["name"]} (workspace fx, field business.name)' in out


def test_cli_json(repo, capsys):
    assert cli.main(["lint", "customer-strings", "--repo", str(repo), "--json"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["hits"] == []


def test_not_a_git_repo_could_not_run(tmp_path, root):
    assert cli.main(["lint", "customer-strings", "--repo", str(tmp_path)]) == EXIT_COULD_NOT_RUN


def test_the_framework_repo_itself_is_clean():
    """The real check: this repo, with the synthetic workspaces (and any real ones on this Mac)."""
    report = lint_strings.run(lint_strings.framework_root(), extra_roots=[lint_strings.real_root()])
    assert report.hits == [], report.hits


def test_a_workspace_without_customer_json_is_reported_as_unprotected(repo, root, capsys):
    """A legacy folder (partner.json only) has no identifying fields to search for: say so, never skip it silently."""
    (root / "legacy").mkdir()
    (root / "legacy" / "partner.json").write_text("{}")
    assert lint_strings.run(repo, extra_roots=[root]).unprotected == ["legacy"]
    cli.main(["lint", "customer-strings", "--repo", str(repo)])
    assert "legacy" in capsys.readouterr().err


# ---------------------------------------------------------------- industry vocabulary (plan v2 R8)

def test_vocabulary_lint_finds_a_customers_industry_phrase_in_the_core(repo):
    phrase = FX["content"]["denylist_claims"][0]
    assert lint_strings.run_vocabulary(repo).hits == []
    (repo / "mseo" / "leak.py").write_text(f'BAD = "we offer a {phrase} to everyone"\n')
    (repo / "tests" / "note.py").write_text(f'# {phrase}\n')  # tests are out of scope for the vocabulary lint
    git(repo, "add", "-A")
    hits = lint_strings.run_vocabulary(repo).hits
    assert [(h["file"], h["value"].lower()) for h in hits] == [("mseo/leak.py", phrase.lower())]


def test_vocabulary_lint_cli_and_the_framework_itself(repo, capsys):
    assert cli.main(["lint", "vocabulary", "--repo", str(repo)]) == 0
    assert "vocabulary: clean" in capsys.readouterr().out
    assert lint_strings.run_vocabulary(FRAMEWORK, extra_roots=[]).hits == []
