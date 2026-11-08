"""content/recipes: the axes come from templates/posts/recipes.json and the picker follows the README rules.
Runs on fresh copies of fx and fy; every customer value comes from the fixture files."""

from __future__ import annotations

import json

import pytest

from mseo import cli, workspace
from mseo.content import recipes, shapes
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_OK
from tests.fakes import FakeKeychain

TODAY = "2026-11-04"


@pytest.fixture
def fx(root, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", TODAY)
    return workspace.open("fx", keychain=FakeKeychain())


def read_log(ws):
    return json.loads(ws.file("content_log").read_text())


def run(*argv):
    return cli.main(list(argv), keychain=FakeKeychain())


# ---------------------------------------------------------------- the file

def test_recipes_json_drives_shapes_and_keeps_the_old_names():
    data = recipes.load()
    assert len(data["article"]["shapes"]) == 8
    assert {"steps-first", "questions-led", "myth-vs-fact", "compare-table", "scenario-led"} <= set(shapes.SHAPES)
    assert shapes.SHAPES == recipes.shapes()
    assert shapes.INTROS == data["article"]["axes"]["intro"]["values"]
    assert shapes.FAQ_N == data["article"]["axes"]["faq_n"]["values"]
    assert set(data["gbp"]["types"]) == {p.stem for p in (recipes.TEMPLATES / "business-profile").glob("*.txt")}
    assert set(data["article"]["shapes"]) == {p.stem for p in (recipes.TEMPLATES / "articles").glob("*.md")}


def test_every_shape_block_is_a_known_block():
    data = recipes.load()
    for name, blocks in data["article"]["shapes"].items():
        assert set(blocks) <= set(data["blocks"]), name
        assert blocks[0] == "answer" and {"local", "faq", "author", "cta", "sources"} <= set(blocks), name


def test_template_path_points_at_the_framework_not_the_workspace():
    assert recipes.template_path("article", "steps-first").is_file()
    assert recipes.template_path("gbp", "update").is_file()
    assert recipes.FRAMEWORK_ROOT in recipes.template_path("article", "steps-first").parents


# ---------------------------------------------------------------- pick_axis

def test_avoid_last_skips_the_recent_values_and_ties_go_to_the_smallest_name():
    value, _ = recipes.pick_axis(["c", "a", "b", "d"], ["a", "b"], avoid_last=2)
    assert value == "c"  # a and b are the last two; c and d tie on use, c sorts first


def test_least_used_in_the_window_wins():
    value, _ = recipes.pick_axis(["a", "b", "c"], ["a", "b", "a", "b", "c"], avoid_last=1)
    assert value == "a"  # c is banned; a and b used twice each -> a by name


def test_max_run_bans_only_a_value_about_to_make_a_longer_run():
    assert recipes.pick_axis(["x", "y"], ["x", "x"], max_run=2)[0] == "y"
    assert recipes.pick_axis(["x", "y"], ["x", "y"], max_run=2)[0] == "x"  # no run to break: a tie, by name


def test_every_value_recent_falls_back_to_the_least_used():
    assert recipes.pick_axis([3, 4], [3, 4], avoid_last=2)[0] == 3


# ---------------------------------------------------------------- recipe()

def test_article_recipe_avoids_the_logged_shape_and_axes(fx):
    logged = read_log(fx)["articles"][-1]
    rec = recipes.recipe(fx.file("content_log"), "article")
    assert rec["shape"] != logged["shape"] and rec["blocks"] == recipes.shapes()[rec["shape"]]
    assert rec["intro"] != logged["intro"] and rec["faq_n"] != logged["faq_n"] and rec["cta"] != logged["cta"]
    for axis in recipes.ARTICLE_AXES:
        assert rec[axis] in recipes.axis_values("article", axis)
    assert rec == recipes.recipe(fx.file("content_log"), "article")  # deterministic


def test_shapes_recipe_still_answers_the_old_call(fx):
    assert shapes.recipe(fx.file("content_log"))["shape"] == recipes.recipe(fx.file("content_log"))["shape"]


def test_six_consecutive_recipes_rotate_every_axis(fx):
    picked = []
    for n in range(6):
        rec = recipes.recipe(fx.file("content_log"), "article")
        recipes.log_recipe(fx, rec, slug=f"post-{n}")
        picked.append(rec)
    assert len({r["shape"] for r in picked}) == 6
    for a, b in zip(picked, picked[1:], strict=False):
        differ = [k for k in recipes.COMPARED_ARTICLE_AXES if a[k] != b[k]]
        assert len(differ) >= 7, (a, b)
    bands = [r["word_band"] for r in picked]
    assert not any(bands[i] == bands[i + 1] == bands[i + 2] for i in range(len(bands) - 2))
    entries = {a["slug"]: a for a in read_log(fx)["articles"]}
    assert entries["post-0"]["blocks"] == picked[0]["blocks"] and entries["post-0"]["title"] == "Post 0"
    assert entries["post-0"]["list"] in shapes.LISTS  # the old field stays


def test_gbp_recipe_rotates_type_opening_and_band(fx):
    picked = []
    for n in range(4):
        rec = recipes.recipe(fx.file("content_log"), "gbp")
        recipes.log_recipe(fx, rec, headline=f"synthetic post {n}", date=f"2026-11-{10 + n}")
        picked.append(rec)
    types = [r["gbp_type"] for r in picked]
    assert len(set(types)) == 4 and all(t in recipes.load()["gbp"]["types"] for t in types)
    assert all(a["opening"] != b["opening"] for a, b in zip(picked, picked[1:], strict=False))
    assert all(a["closing"] != b["closing"] for a, b in zip(picked, picked[1:], strict=False))
    entry = next(g for g in read_log(fx)["gbp"] if g["headline"] == "synthetic post 0")
    assert entry["gbp_type"] == types[0] and entry["theme"] == types[0] and entry["image"] in ("yes", "no")


def test_recipe_can_be_forced_to_one_shape(fx):
    assert recipes.recipe(fx.file("content_log"), "article", allowed=["steps-first"])["shape"] == "steps-first"
    with pytest.raises(recipes.RecipeError):
        recipes.recipe(fx.file("content_log"), "article", allowed=["nope"])


# ---------------------------------------------------------------- the command

def test_recipe_command_logs_the_axes_on_the_slug(fx, capsys):
    assert run("content", "recipe", "--workspace", "fx", "--kind", "article", "--slug", "next-post", "--json") == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    entry = next(a for a in read_log(fx)["articles"] if a["slug"] == "next-post")
    for key in ("shape", "blocks", "title_pattern", "intro", "evidence", "faq_n", "cta", "word_band", "local_placement", "closing", "list"):
        assert entry[key] == out[key], key
    assert entry["status"] == "draft" and entry["date"] == TODAY and out["logged"] == "logged"


def test_recipe_command_without_a_key_prints_and_logs_nothing(fx, capsys):
    before = read_log(fx)
    assert run("content", "recipe", "--workspace", "fx", "--kind", "article") == EXIT_OK
    assert "not logged" in capsys.readouterr().out and read_log(fx) == before


def test_gbp_recipe_command_needs_date_and_headline(fx):
    assert run("content", "recipe", "--workspace", "fx", "--kind", "gbp", "--date", "2026-11-12", "--headline", "A fresh post") == EXIT_OK
    entry = read_log(fx)["gbp"][-1]
    assert entry["headline"] == "a fresh post" and entry["gbp_type"] in recipes.load()["gbp"]["types"]


def test_recipe_command_refuses_a_bad_slug_before_logging(fx, capsys):
    before = read_log(fx)
    assert run("content", "recipe", "--workspace", "fx", "--kind", "article", "--slug", "../x") == EXIT_COULD_NOT_RUN
    assert "slug" in capsys.readouterr().err and read_log(fx) == before
    assert recipes.check_slug("a-fine-slug-2") == "a-fine-slug-2"
    for bad in ("x", "-lead", "Caps", "a" * 82):
        with pytest.raises(recipes.RecipeError):
            recipes.check_slug(bad)


def test_unreadable_log_is_3(fx):
    fx.file("content_log").write_text("{nope")
    assert run("content", "recipe", "--workspace", "fx", "--kind", "article") == EXIT_COULD_NOT_RUN


def test_fy_recipe_reads_fys_log(root, monkeypatch):
    monkeypatch.setenv("MSEO_TODAY", TODAY)
    fy = workspace.open("fy", keychain=FakeKeychain())
    logged = read_log(fy)["articles"][-1]
    rec = recipes.recipe(fy.file("content_log"), "article")
    assert rec["shape"] != logged["shape"] and rec["intro"] != logged["intro"]
