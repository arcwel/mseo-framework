"""The variation gate and the internal-link rule. Axes are recomputed from the draft text and judged against the
content log; the proof at the end generates six drafts from six consecutive recipes, fills the markers with distinct
synthetic text that realises each recipe, and shows every one passes while consecutive recipes differ on >= 7 axes.
Customer values come from the fixture files (FX, facts.json, the research file), never literals."""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from mseo import cli, workspace
from mseo.content import check, places, recipes, variation
from mseo.exits import EXIT_OK
from tests.conftest import FX, FY, edit_config
from tests.fakes import FakeKeychain

TODAY = dt.date(2026, 11, 4)
SITE = FX["business"]["site"]
CITY = places.city_of_address(FX["locations"][0]["address"])
FACT_NUMBER = re.search(r"\d+", FX["locations"][0]["address"]).group()
STREET = " ".join(FX["locations"][0]["address"].split(",")[0].split()[:3])


@pytest.fixture
def fx(root, monkeypatch):
    monkeypatch.setattr(check, "today", lambda: TODAY)
    monkeypatch.setattr(check, "check_url", lambda u, timeout=20: {"url": u, "status": 200, "ok": True})
    monkeypatch.setattr(check, "deslop_version", lambda: "test")
    monkeypatch.setenv("MSEO_TODAY", TODAY.isoformat())
    return workspace.open("fx", keychain=FakeKeychain())


def article(ws):
    return ws.path("drafts", "good_article.md")


def gate(ws, name="variation", path=None, kind="article", slug="good-article"):
    d = check.Draft.load(ws, path or (article(ws) if kind == "article" else ws.path("drafts", "good_gbp.txt")), kind,
                         slug=slug if kind == "article" else "")
    return check.run(d, ws, only=name)[0]


def read_log(ws):
    return json.loads(ws.file("content_log").read_text())


def add_article(ws, **entry):
    log = read_log(ws)
    log["articles"].append({"type": "article", "status": "draft", **entry})
    ws.file("content_log").write_text(json.dumps(log))


def add_gbp(ws, **entry):
    log = read_log(ws)
    log["gbp"].append({"status": "draft", "claims": [], **entry})
    ws.file("content_log").write_text(json.dumps(log))


def own_entry(ws, slug="good-article"):
    return next(a for a in read_log(ws)["articles"] if a["slug"] == slug)


def axes(ws, path=None):
    d = check.Draft.load(ws, path or article(ws), "article", slug="good-article")
    places, used = variation.research_inputs(d, ws)
    return variation.article_axes(d.text, facts=variation.load_facts(ws), places=places, city=variation.city_of(ws), used=used,
                                  footers=variation.footers_of(ws))


def edit(path, old, new):
    text = path.read_text()
    assert old in text, old
    path.write_text(text.replace(old, new))


# ---------------------------------------------------------------- axes read from the fixture

def test_axes_of_the_fixture_article(fx):
    a = axes(fx)
    assert a["shape"] == own_entry(fx)["shape"] and a["blocks"] == own_entry(fx)["blocks"]
    assert a["title_pattern"] == "local-led" and a["intro"] == "bold-two-sentences"
    assert a["evidence"] == "worked-scenario" and a["faq_n"] == 2 and a["cta"] == "end"
    assert a["local_placement"] == "own-section" and a["closing"] == "one-line-summary"  # the NAP line closes the fixture
    assert len(a["opening"].split()) == recipes.article()["opening_unique_words"]
    assert {"fact-number", "local-place", "named-source", "table-or-list", "scenario"} <= set(a["proof_items"])
    assert a["word_band"] is None and a["words"] < 700  # the fixture is short on purpose


def test_city_and_places_come_from_the_workspace(fx):
    assert variation.city_of(fx) == CITY
    found, used = variation.research_inputs(check.Draft.load(fx, article(fx), "article", "good-article"), fx)
    assert CITY in found and any(" " in p for p in found) and used


def test_city_survives_a_suite_line_and_an_explicit_city_wins(root, monkeypatch):
    """"<street>, Suite 4, <city>, ST zip" is read with the schema's address rule; locations[].city overrides it."""
    street, rest = FX["locations"][0]["address"].split(",", 1)
    with_suite = f"{street}, Suite 4,{rest}"
    assert places.city_of_address(with_suite) == CITY
    edit_config(root, "fx", lambda c: c["locations"][0].update(address=with_suite))
    assert variation.city_of(workspace.open("fx", keychain=FakeKeychain())) == CITY
    edit_config(root, "fx", lambda c: c["locations"][0].update(city="Elsewhere Heights"))
    assert variation.city_of(workspace.open("fx", keychain=FakeKeychain())) == "Elsewhere Heights"


def test_title_patterns():
    places = {CITY}
    assert variation.title_pattern(f"How do I pick a shop in {CITY}?", places) == "how-what-when-question"
    assert variation.title_pattern("7 things to check first", places) == "number-plus-noun"
    assert variation.title_pattern("Option A vs option B", places) == "x-vs-y"
    assert variation.title_pattern("Should you wait a week?", places) == "should-you"
    assert variation.title_pattern(f"{CITY} drivers and the fair", places) == "local-led"
    assert variation.title_pattern("A plain statement", places) == "plain-statement"


def test_lead_words_come_from_the_lexicon_of_the_drafts_language():
    es = recipes.lexicon("es")
    assert variation.title_pattern("¿Cuándo conviene revisar los frenos?", set(), "es") == "how-what-when-question"
    assert variation.title_pattern("¿Debería esperar una semana?", set(), "es") == "should-you"
    assert variation.title_pattern("Opción A frente a opción B", set(), "es") == "x-vs-y"
    assert variation.title_pattern("¿Cuándo conviene revisar los frenos?", set(), "en") == "plain-statement"
    assert variation.gbp_axes("Imagine una mañana de lluvia.", places=set(), lang="es")["opening"] == "scenario"
    assert variation.gbp_axes("<!-- lang: es -->\nImagine una mañana de lluvia.", places=set())["opening"] == "scenario"
    assert variation.gbp_axes("Imagine una mañana de lluvia.", places=set())["opening"] == "scenario"  # en lexicon too
    assert variation.gbp_axes("Reserve una cita esta semana.", places=set())["opening"] == "fact"
    assert set(es["h2_lead_words"]) and set(es["h2_lead_words"]).isdisjoint(recipes.lexicon("en")["h2_lead_words"])
    assert recipes.lexicon("xx") == recipes.lexicon("en")
    assert variation.language_of("<!-- lang: es -->\n# T\n") == "es" and variation.language_of("# T\n") == "en"
    assert "h2_lead_words" not in recipes.article()  # moved under lexicon


def test_spanish_closing_is_a_next_step_in_its_own_lexicon():
    text = "<!-- lang: es -->\n# T\n\n**R.**\n\n<!-- block: cta -->\n## Siguiente paso\n\nReserve su cita hoy.\n"
    parsed = variation.parse(text)
    assert variation.closing_style(parsed, set(), "es") == "next-step"
    assert variation.closing_style(parsed, set(), "en") == "one-line-summary"


def test_intro_styles():
    assert variation.intro_style("**One. Two.**") == "bold-two-sentences"
    assert variation.intro_style("**Answer with 12 in it**") == "bold-one-line-plus-number"
    assert variation.intro_style("**Answer.**\n\nAnd the why.") == "bold-answer-then-why"
    assert variation.intro_style("A scene first.\n\n**Then the answer.**") == "scenario-then-bold-answer"
    assert variation.intro_style("no bold at all") is None


def test_fixture_gate_passes_and_sums_up(fx):
    g = gate(fx)
    assert (g.status, g.blocking) == ("pass", True) and "shape scenario-led" in g.detail


def test_variation_runs_for_both_kinds(fx):
    assert "variation" in check.gates_for("article") and "variation" in check.gates_for("gbp")
    assert gate(fx, kind="gbp").status == "pass"


# ---------------------------------------------------------------- the rules

def test_shape_equal_to_a_recent_post_fails(fx):
    add_article(fx, slug="older", title="Another piece", date="2026-10-20", shape="scenario-led", blocks=own_entry(fx)["blocks"])
    g = gate(fx)
    assert g.status == "fail" and g.detail.startswith("shape:") and "older" in g.detail


def test_shape_rule_looks_only_at_the_window(fx):
    for n in range(recipes.load()["window"]):
        add_article(fx, slug=f"filler-{n}", title=f"Filler {n}", date="2026-10-01", shape="steps-first",
                    blocks=recipes.shapes()["steps-first"])
    add_article(fx, slug="oldest", title="Oldest", date="2026-09-01", shape="scenario-led", blocks=own_entry(fx)["blocks"])
    log = read_log(fx)
    log["articles"] = [a for a in log["articles"] if a["slug"] != "oldest"] + []
    log["articles"].insert(0, {"type": "article", "status": "draft", "slug": "oldest", "title": "Oldest", "date": "2026-09-01",
                               "shape": "scenario-led", "blocks": own_entry(fx)["blocks"]})
    fx.file("content_log").write_text(json.dumps(log))
    assert gate(fx).status == "pass"


def test_too_many_axes_shared_with_the_previous_post_fails(fx):
    a = axes(fx)
    add_article(fx, slug="previous", title="Previous", date="2026-10-28", shape="steps-first", blocks=recipes.shapes()["steps-first"],
                **{k: a[k] for k in recipes.ARTICLE_AXES})
    g = gate(fx)
    assert g.status == "fail" and g.detail.startswith("shared-axes:")


def test_two_shared_axes_are_allowed(fx):
    a = axes(fx)
    add_article(fx, slug="previous", title="Previous", date="2026-10-28", shape="steps-first", blocks=recipes.shapes()["steps-first"],
                intro=a["intro"], cta=a["cta"], title_pattern="x-vs-y", evidence="table", faq_n=6, closing="question-back",
                local_placement="in-faq", word_band="900-1100")
    assert gate(fx).status == "pass"


def test_same_opening_as_a_logged_post_fails(fx):
    add_article(fx, slug="previous", title="Previous", date="2026-10-28", opening=axes(fx)["opening"] + " more words here")
    g = gate(fx)
    assert g.status == "fail" and g.detail.startswith("opening:")


def test_repeated_h2_within_60_days_fails_but_not_after(fx):
    h2 = axes(fx)["h2s"][1]
    add_article(fx, slug="previous", title="Previous", date="2026-10-20", h2s=[h2.upper()])
    g = gate(fx)
    assert g.status == "fail" and g.detail.startswith("h2-repeat:") and "previous" in g.detail
    log = read_log(fx)
    log["articles"][-1]["date"] = "2026-08-01"
    fx.file("content_log").write_text(json.dumps(log))
    assert gate(fx).status == "pass"


def test_h2_lead_word_share_over_half_fails(fx):
    edit(article(fx), "## When does the Harvest Fair change the plan?", "## What does the Harvest Fair change?")
    g = gate(fx)
    assert g.status == "fail" and g.detail.startswith("h2-lead:") and "'what'" in g.detail


def test_named_source_counts_only_urls_in_the_sources_block(fx):
    a = axes(fx)
    assert "named-source" in a["proof_items"] and a["sources"] and a["used_in_sources"]
    edit(article(fx), "(https://testville.example.gov/fair)", "")
    side = article(fx).with_name("good_article.md.research.json")
    data = json.loads(side.read_text())
    data["used"] = "Harvest Fair https://testville.example.gov/fair"
    side.write_text(json.dumps(data))
    a = axes(fx)
    assert "named-source" not in a["proof_items"] and a["sources"] == [] and not a["used_in_sources"]


def test_too_few_proof_items_fails(fx):
    thin = ("# A plain title\n\n**Answer without numbers or places.**\n\n<!-- block: why -->\n## Why does it matter?\n\n"
            "Because.\n\n<!-- block: steps -->\n## How is it done?\n\nCarefully.\n\n"
            "<!-- block: faq -->\n## Frequently asked questions\n\n### Why?\n\nBecause.\n")
    path = fx.path("drafts", "thin.md")
    path.write_text(thin)
    g = gate(fx, path=path, slug="thin")
    assert g.status == "fail" and g.detail.startswith("proof-items:")


def test_three_posts_in_a_row_in_one_word_band_fail_even_without_an_own_band(fx):
    a = axes(fx)
    bands = recipes.axis_values("article", "word_band")
    lo = int(bands[0].split("-")[0])
    mine = {**a, "words": lo + 10, "word_band": bands[0]}
    log = read_log(fx)
    assert "word_band" not in own_entry(fx)
    for n in range(2):
        log["articles"].append({"type": "article", "status": "published", "slug": f"band-{n}", "title": f"Band {n}",
                                "date": f"2026-10-2{n}", "word_band": bands[0]})
    ok, detail = variation.judge_article(mine, log, "good-article", TODAY)
    assert not ok and detail.startswith("word-band:") and "in a row" in detail
    log["articles"][-1]["word_band"] = bands[1]
    assert variation.judge_article(mine, log, "good-article", TODAY)[0]


def test_word_count_outside_the_logged_band_fails(fx):
    log = read_log(fx)
    own = next(a for a in log["articles"] if a["slug"] == "good-article")
    own["word_band"] = "700-900"
    fx.file("content_log").write_text(json.dumps(log))
    g = gate(fx)
    assert g.status == "fail" and g.detail.startswith("word-band:") and "700-900" in g.detail


def test_unreadable_log_is_an_error(fx):
    fx.file("content_log").write_text("{nope")
    assert gate(fx).status == "error"


def test_gbp_opening_repeated_twice_fails(fx):
    first = fx.path("drafts", "good_gbp.txt").read_text().splitlines()[0]
    assert variation.gbp_axes(first, places=set())["opening"] == "fact"
    add_gbp(fx, date="2026-10-20", theme="a", headline="an older post about brakes and wipers", opening="fact")
    add_gbp(fx, date="2026-10-27", theme="b", headline="another older post about lights", opening="fact")
    g = gate(fx, kind="gbp")
    assert g.status == "fail" and g.detail.startswith("gbp-opening:")


def test_gbp_type_among_the_last_three_fails(fx):
    log = read_log(fx)
    own = log["gbp"][0]
    own["gbp_type"] = "update"
    for n in range(3):
        log["gbp"].append({"date": f"2026-10-2{n}", "status": "published", "theme": f"t{n}", "headline": f"older post number {n} here",
                           "claims": [], "gbp_type": "update"})
    fx.file("content_log").write_text(json.dumps(log))
    g = gate(fx, kind="gbp")
    assert g.status == "fail" and g.detail.startswith("gbp-type:")


def test_gbp_length_band_three_in_a_row_fails():
    log = {"gbp": [{"status": "published", "headline": "x", "length_band": "60-120"},
                   {"status": "published", "headline": "y", "length_band": "60-120"}]}
    a = {"opening": "question", "words": 90, "length_band": "60-120", "first_line": "Why?"}
    ok, detail = variation.judge_gbp(a, log, "")
    assert not ok and detail.startswith("gbp-length-band:")
    assert variation.judge_gbp({**a, "words": 150, "length_band": "120-220"}, log, "")[0]


def test_gbp_opening_patterns():
    assert variation.gbp_axes("Is your car ready for rain?", places=set())["opening"] == "question"
    assert variation.gbp_axes("Imagine a wet morning commute.", places=set())["opening"] == "scenario"
    assert variation.gbp_axes("The Harvest Fair opens Saturday.", places={"Harvest Fair"})["opening"] == "local-hook"
    assert variation.gbp_axes("Our bay has a new frame rack.", places=set())["opening"] == "fact"


# ---------------------------------------------------------------- content log --draft

def test_log_draft_records_opening_h2s_and_words(fx):
    code = cli.main(["content", "log", "article", "--workspace", "fx", "--slug", "good-article", "--draft", str(article(fx))],
                    keychain=FakeKeychain())
    assert code == EXIT_OK
    entry = own_entry(fx)
    a = axes(fx)
    assert entry["opening"] == a["opening"] and entry["h2s"] == a["h2s"] and entry["words"] == a["words"]
    assert gate(fx).status == "pass"  # its own entry never counts against it


# ---------------------------------------------------------------- the internal-link rule (links gate)

def test_fewer_than_three_internal_links_fails(fx):
    edit(article(fx), f"See [what we repair](/services/) or read [how often to rotate your tires]({FX['business']['site']}/tire-rotation-basics/).", "")
    edit(article(fx), "[Request a complimentary estimate](/estimate/)", "Request a complimentary estimate")
    edit(article(fx), "[estimate form](/estimate/)", "estimate form")
    g = gate(fx, "links")
    assert g.status == "fail" and "needs 3" in g.detail


def test_missing_service_or_contact_path_fails(fx):
    edit(article(fx), "(/services/)", "(/stories/)")
    g = gate(fx, "links")
    assert g.status == "fail" and "service" in g.detail
    edit(article(fx), "(/stories/)", "(/services/)")
    edit(article(fx), "(/estimate/)", "(/estimates-and-more/)")  # still contact-like
    assert gate(fx, "links").status == "pass"


def test_repeated_anchor_text_fails(fx):
    edit(article(fx), "[estimate form](/estimate/)", "[Request a complimentary estimate](/estimate/)")
    g = gate(fx, "links")
    assert g.status == "fail" and "twice" in g.detail


def test_gbp_posts_are_not_held_to_the_internal_link_count(fx):
    assert gate(fx, "links", kind="gbp").status == "pass"


def test_link_paths_come_from_the_workspace_config(root, monkeypatch):
    """fy configures its service path; its article links there and passes, while fx's default would refuse it."""
    monkeypatch.setattr(check, "check_url", lambda u, timeout=20: {"url": u, "status": 200, "ok": True})
    fy = workspace.open("fy", keychain=FakeKeychain())
    service = FY["content"]["link_paths"]["service"][0]
    text = fy.path("drafts", "good_article.md").read_text()
    assert f"({service})" in text and service != "/services/"
    d = check.Draft.load(fy, fy.path("drafts", "good_article.md"), "article", slug="chipped-tooth")
    assert check.run(d, fy, only="links")[0].status == "pass"
    assert check.internal_link_problem(text, FY["business"]["site"], {"service": ["/services/"]}) is not None
    assert check.internal_link_problem(text, FY["business"]["site"], FY["content"]["link_paths"]) is None
    assert check.path_matches("/Estimates-and-more/", ["/estimate/"]) and not check.path_matches("/stories/", ["/services/"])


# ---------------------------------------------------------------- six drafts from six recipes

LEADS = recipes.lexicon("en")["h2_lead_words"]


class Filler:
    """Fills ###WRITE### markers with distinct synthetic text that realises a recipe's axes."""

    def __init__(self, place: str):
        self.place, self.n = place, 0

    def next(self) -> int:
        self.n += 1
        return self.n

    def title(self, pattern: str, k: int) -> str:
        return {"how-what-when-question": f"How does synthetic topic {k} work in {CITY}?",
                "number-plus-noun": f"{k + 3} synthetic facts about the service",
                "plain-statement": f"Synthetic statement {k} about the service",
                "x-vs-y": f"Synthetic option {k} vs option B",
                "should-you": f"Should you try synthetic option {k}?",
                "local-led": f"{CITY} synthetic guide {k}"}[pattern]

    def answer(self, intro: str, k: int, with_place: bool) -> str:
        where = f" near the {self.place}" if with_place else ""
        fact = f"{STREET}"
        return {"bold-two-sentences": f"**Synthetic answer number {k} for the service in {CITY}{where}. The one fact is {fact}.**",
                "bold-one-line-plus-number": f"**Synthetic answer number {k}: {fact} is the fact for the service in {CITY}{where}**",
                "bold-answer-then-why": f"**Synthetic answer number {k} for the service in {CITY}{where}.**\n\nThe why follows in plain text, with {fact} as the number.",
                "scenario-then-bold-answer": f"You are in {CITY}{where} with answer number {k} in mind.\n\n**The synthetic answer is {fact}.**"}[intro]

    def closing(self, style: str, k: int) -> str:
        return {"question-back": f"Which of these synthetic steps will you take first, number {k}?",
                "next-step": f"Book your synthetic visit this week, number {k}.",
                "local-fact-restated": f"The {self.place} fills early, so plan synthetic visit {k} around it.",
                "one-line-summary": f"The short version: synthetic summary number {k} of this article."}[style]

    def fill(self, text: str, rec: dict, k: int) -> str:
        lines = text.splitlines()
        out, lead_i, local_i, faq_i = [], 0, None, None
        in_block = None
        for line in lines:
            m = variation.MARKER_RE.match(line)
            if m:
                in_block = m.group(1)
            if line.startswith("# ###WRITE###"):
                line = "# " + self.title(rec["title_pattern"], k)
            elif line.startswith("## ###WRITE###"):
                line = f"## {LEADS[lead_i % len(LEADS)].capitalize()} does synthetic point {self.next()} matter in {CITY}?"
                lead_i += 1
            elif line.startswith("### ###WRITE###"):
                line = f"### Can synthetic question {self.next()} be settled?"
            elif line.strip() == "**###WRITE###**":
                line = self.answer(rec["intro"], k, rec["local_placement"] == "in-answer")
            elif "(/contact/)" in line and "###WRITE###" in line:
                n = self.next()
                where = f" near the {self.place}" if rec["local_placement"] == "in-close" else ""
                line = (f"Start with [synthetic anchor a{n}](/services/), then [synthetic anchor b{n}](/contact/) and "
                        f"[synthetic anchor c{n}]({SITE}/tire-rotation-basics/){where}.\n\n" + self.closing(rec["closing"], k))
            elif "(/services/)" in line and "###WRITE###" in line:
                line = f"Synthetic pointer {self.next()} to [synthetic anchor d{self.next()}](/services/)."
            elif line.startswith("- ###WRITE### ("):
                line = line.replace("###WRITE###", f"{CITY} fair listing") + " https://testville.example.gov/fair"  # the used item stays
            while "###WRITE###" in line:
                line = line.replace("###WRITE###", f"Synthetic sentence {self.next()} about the service", 1)
                if in_block == "local" and local_i is None and not line.startswith(("#", "<!--", "|", "-", "1")):
                    local_i = len(out)
                if in_block == "faq" and faq_i is None and not line.startswith("#"):
                    faq_i = len(out)
            out.append(line)
        assert local_i is not None and faq_i is not None
        if rec["local_placement"] == "own-section":
            out[local_i] += f" The {self.place} changes what to do."
        if rec["local_placement"] == "in-faq":
            out[faq_i] += f" Ask near the {self.place}."
        out.insert(local_i + 1, f"\nIllustration: synthetic diagram {self.next()}.")
        lo, hi = (int(x) for x in rec["word_band"].split("-"))
        text = "\n".join(out) + "\n"
        while variation.word_count(variation.parse(text, [FX["locations"][0]["footer"]])) < lo + 20:
            out[local_i] += " Padding words keep the count inside the band the recipe asked for."
            text = "\n".join(out) + "\n"
        return text


REALISED = ("title_pattern", "intro", "faq_n", "cta", "local_placement", "closing", "word_band")


def test_six_drafts_from_six_recipes_pass_variation_and_differ_on_seven_axes(fx, capsys):
    research = fx.path("research", "local", "2026-W45.md").read_text()
    place = sorted(p for p in variation.research_places(research) if " " in p and CITY not in p)[0]
    filler, picked = Filler(place), []
    for k in range(6):
        slug = f"gen-{k}"
        assert cli.main(["content", "new", "--workspace", "fx", "--kind", "article", "--slug", slug, "--service", "synthetic service",
                         "--area", CITY, "--json"], keychain=FakeKeychain()) == EXIT_OK
        rec = json.loads(capsys.readouterr().out)["recipe"]
        path = fx.path("drafts", f"{slug}.md")
        path.write_text(filler.fill(path.read_text(), rec, k))
        d = check.Draft.load(fx, path, "article", slug)
        by = {g.name: g for g in check.run(d, fx) if g.name in ("variation", "sections", "eeat", "links", "faq")}
        assert {n: (g.status, g.detail) for n, g in by.items() if g.status != "pass"} == {}, slug
        a = variation.article_axes(d.text, facts=variation.load_facts(fx), places=variation.research_inputs(d, fx)[0],
                                   city=CITY, used="", footers=variation.footers_of(fx))
        assert {x: a[x] for x in REALISED} == {x: rec[x] for x in REALISED}, slug
        assert a["shape"] == rec["shape"]
        assert cli.main(["content", "log", "article", "--workspace", "fx", "--slug", slug, "--draft", str(path)], keychain=FakeKeychain()) == EXIT_OK
        capsys.readouterr()
        picked.append(rec)
    assert len({r["shape"] for r in picked}) == 6
    for a, b in zip(picked, picked[1:], strict=False):
        assert len([x for x in recipes.COMPARED_ARTICLE_AXES if a[x] != b[x]]) >= 7
    entries = {e["slug"]: e for e in read_log(fx)["articles"]}
    assert len({entries[f"gen-{k}"]["opening"] for k in range(6)}) == 6
