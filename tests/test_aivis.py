"""`mseo ai-visibility`: the prompt set and the citation scoring (plan 3.2 item 6). No network."""

from __future__ import annotations

import json
import shlex
import sys

import pytest

from mseo import aivis, cli, workspace
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_OK
from tests.conftest import FX, SYNTHETIC, edit_config
from tests.fakes import FakeKeychain

RIVAL = "https://rival-shop.example"
PROMPTS = ["who fixes things near me", "how much does a repair cost in town", "best shop open on saturday"]
POSTS = json.loads((SYNTHETIC / "fx" / "data" / "wp_posts.json").read_text())
SITE_HOST = FX["business"]["site"].split("//", 1)[1]


@pytest.fixture
def fx(root):
    edit_config(root, "fx", lambda c: c.setdefault("ai_visibility", {}).update(prompts=PROMPTS, competitors=[RIVAL]))
    return workspace.open("fx", keychain=FakeKeychain())


def answers(name=FX["business"]["name"]):
    return [
        {"prompt": PROMPTS[0], "engine": "chatgpt", "answer_text": f"Try {name} on the main road.",
         "cited_urls": [f"https://www.{SITE_HOST}/about/", "https://www.google.com/maps/place/x"]},
        {"prompt": PROMPTS[1], "engine": "chatgpt", "answer_text": "Costs vary.", "cited_urls": [RIVAL + "/prices"]},
        {"prompt": PROMPTS[0], "engine": "perplexity", "answer_text": "Several shops.", "cited_urls": ["https://g.co/kgs/abc", RIVAL]},
        {"prompt": PROMPTS[2], "engine": "perplexity", "answer_text": "No idea.", "cited_urls": []},
    ]


# ---------------------------------------------------------------- prompt set

def test_prompt_set_lists_config_prompts_and_tops_up_from_article_headings(fx):
    ps = aivis.prompt_set(fx)
    texts = [p["text"] for p in ps["prompts"]]
    assert texts[:3] == PROMPTS and all(p["source"] == "config" for p in ps["prompts"][:3])
    generated = [p for p in ps["prompts"] if p["source"] == "article"]
    assert len(generated) == min(len(POSTS), aivis.GENERATED_MAX) and generated[0]["text"] == POSTS[0]["title"] + "?"
    assert ps["engines"] == FX.get("ai_visibility", {}).get("engines", ["google_ai_mode", "chatgpt", "perplexity"])
    assert [p["id"] for p in ps["prompts"]] == [f"p{i:02d}" for i in range(1, len(ps["prompts"]) + 1)]


def test_a_long_prompt_list_is_not_topped_up(root):
    edit_config(root, "fx", lambda c: c.setdefault("ai_visibility", {}).update(prompts=[f"question {n}" for n in range(12)]))
    ps = aivis.prompt_set(workspace.open("fx", keychain=FakeKeychain()))
    assert len(ps["prompts"]) == 12 and all(p["source"] == "config" for p in ps["prompts"])


def test_prompts_cli_prints_json(fx, capsys):
    assert cli.main(["ai-visibility", "prompts", "--workspace", "fx"], keychain=FakeKeychain()) == EXIT_OK
    assert json.loads(capsys.readouterr().out)["prompts"][0]["text"] == PROMPTS[0]


# ---------------------------------------------------------------- scoring

def test_scores_per_engine_and_overall(fx):
    s = aivis.score(fx, aivis.validate_answers(answers()))
    o = s["overall"]
    assert (o["answers"], o["cited_site"], o["cited_profile"], o["cited_competitor"], o["mentioned_name"]) == (4, 1, 2, 2, 1)
    assert o["cited_site_share"] == 0.25 and o["cited_profile_share"] == 0.5
    assert s["engines"]["chatgpt"]["cited_site"] == 1 and s["engines"]["perplexity"]["cited_site"] == 0
    assert s["engines"]["perplexity"]["cited_profile"] == 1  # g.co counts as the profile
    assert s["cited_hosts"][RIVAL.split("//")[1]] == 2 and SITE_HOST in s["cited_hosts"]  # www. folded
    assert s["rows"][0]["mentioned_name"] and not s["rows"][1]["mentioned_name"]


def test_name_match_is_case_insensitive(fx):
    rows = aivis.score(fx, aivis.validate_answers(answers(name=FX["business"]["name"].upper())))["rows"]
    assert rows[0]["mentioned_name"]


def test_invalid_answers_are_refused():
    with pytest.raises(aivis.AnswersInvalid):
        aivis.validate_answers({"prompt": "x"})
    with pytest.raises(aivis.AnswersInvalid):
        aivis.validate_answers([{"prompt": "x", "engine": "e", "cited_urls": "https://a.test"}])
    with pytest.raises(aivis.AnswersInvalid):
        aivis.validate_answers([{"prompt": "x"}])


# ---------------------------------------------------------------- files and trend

def test_run_writes_month_files_and_keeps_a_trend(fx, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", "2026-10-15")
    aivis.run(fx, aivis.validate_answers(answers()[:2]))
    monkeypatch.setenv("MSEO_TODAY", "2026-11-07")
    report = aivis.run(fx, aivis.validate_answers(answers()))
    folder = fx.path("data", "ai-visibility")
    assert sorted(p.name for p in folder.iterdir()) == ["2026-10.json", "2026-10.md", "2026-11.json", "2026-11.md"]
    assert report["trend"] == [{"month": "2026-10", "answers": 2, "cited_site_share": 0.5, "cited_profile_share": 0.5,
                                "cited_competitor_share": 0.5, "mentioned_name_share": 0.5}]
    md = (folder / "2026-11.md").read_text()
    assert "## Trend" in md and "| 2026-10 |" in md and "| 2026-11 |" in md and "| chatgpt |" in md
    assert json.loads((folder / "2026-11.json").read_text())["scores"]["overall"]["answers"] == 4


def test_run_cli_with_an_answers_file(fx, root, monkeypatch, capsys):
    monkeypatch.setenv("MSEO_TODAY", "2026-11-07")
    path = root / "fx" / "data" / "answers.json"
    path.write_text(json.dumps(answers()))
    assert cli.main(["ai-visibility", "run", "--workspace", "fx", "--answers", str(path), "--json"], keychain=FakeKeychain()) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["scores"]["overall"]["answers"] == 4 and out["path"].endswith("2026-11.json")
    assert cli.main(["ai-visibility", "run", "--workspace", "fx", "--answers", str(root / "fy" / "customer.json")],
                    keychain=FakeKeychain()) == EXIT_COULD_NOT_RUN  # outside the workspace
    path.write_text("{not json")
    assert cli.main(["ai-visibility", "run", "--workspace", "fx", "--answers", str(path)], keychain=FakeKeychain()) == EXIT_COULD_NOT_RUN


def test_run_without_answers_or_command_is_3(fx, monkeypatch, capsys):
    monkeypatch.delenv(aivis.CMD_ENV, raising=False)
    assert cli.main(["ai-visibility", "run", "--workspace", "fx"], keychain=FakeKeychain()) == EXIT_COULD_NOT_RUN
    assert aivis.CMD_ENV in capsys.readouterr().out


FAKE_CMD = """
import json, sys
ps = json.load(sys.stdin)
assert ps["schema"] == "mseo.ai-visibility.prompts/1"
print(json.dumps([{"prompt": p["text"], "engine": "fake", "answer_text": "An answer", "cited_urls": []} for p in ps["prompts"]]))
"""


def test_run_with_the_check_command_feeds_the_prompt_set_on_stdin(fx, tmp_path, monkeypatch, capsys):
    script = tmp_path / "fake_check.py"
    script.write_text(FAKE_CMD)
    monkeypatch.setenv(aivis.CMD_ENV, f"{shlex.quote(sys.executable)} -I {shlex.quote(str(script))}")
    monkeypatch.setenv("MSEO_TODAY", "2026-11-07")
    assert cli.main(["ai-visibility", "run", "--workspace", "fx", "--json"], keychain=FakeKeychain()) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["scores"]["overall"]["answers"] == len(aivis.prompt_set(fx)["prompts"])
    assert out["scores"]["engines"]["fake"]["cited_site"] == 0


def test_a_failing_check_command_is_3(fx, tmp_path, monkeypatch):
    script = tmp_path / "bad.py"
    script.write_text("import sys; sys.exit(2)")
    monkeypatch.setenv(aivis.CMD_ENV, f"{shlex.quote(sys.executable)} -I {shlex.quote(str(script))}")
    assert cli.main(["ai-visibility", "run", "--workspace", "fx"], keychain=FakeKeychain()) == EXIT_COULD_NOT_RUN
    script.write_text("print('not json')")
    assert cli.main(["ai-visibility", "run", "--workspace", "fx"], keychain=FakeKeychain()) == EXIT_COULD_NOT_RUN
