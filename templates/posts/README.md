# Post templates and the variation rules

Framework files: no customer text here. `mseo content recipe` picks a recipe and `mseo content new --workspace S --kind article|gbp` 
copies the template, fills every `{{...}}` token from `customer.json`, `facts.json` and the week's research file,
and leaves `###WRITE###` where the writer works. `mseo publish` already refuses any draft that still holds a marker.

## Why templates and why they must not look alike

Answer engines (Google AI Overviews and AI Mode, ChatGPT, Perplexity) quote short, self-contained passages that
answer one question directly: about 100-120 words, answer first, written as a reader would ask it. Local answers
lean on the Business Profile first and the site second, so every post repeats the business entity (name, service
area, author) in text and in schema, identically to the profile. Google's scaled-content rule punishes many pages
built from one mould, and a reader notices it before Google does. So the core stays fixed and everything else
rotates.

## The fixed core (every article)

1. `<!-- Meta Description: ... -->` 150-160 characters, states the answer, names the service and the area.
2. One `# Title` written as the question people ask, in the pattern the recipe chose.
3. `<!-- block: answer -->` first: a **bold, direct answer in 40-80 words** that stands alone when quoted, names the
   service and the area once, and gives the one number or fact the reader came for (only facts in `facts.json`).
4. Every `##` is a question a reader would type. Lead words rotate (see axes). Each section answers in its first
   sentence, then explains.
5. One local block tied to this week's research file: a sourced local fact (road, event, weather, regulation) that
   changes what the reader should do. Never a sponsorship claim, never an event the research file does not list.
6. One evidence element: a table, numbered steps, a checklist or a worked scenario (the recipe chooses which).
7. `## Frequently asked questions` (or the configured language's heading) with 3-6 `###` questions. The FAQPage
   schema is built from this section by `md.py`; never paste schema by hand.
8. `<!-- block: author -->`: "Written by {{author.name}}, {{author.role}}. Reviewed {{today}}." The author must exist
   in `facts.json` (`author` block with a bio URL). This feeds the `Article` schema (planned in `md.py`).
9. `<!-- block: cta -->` with three internal links: the service page, the contact or estimate page, and one related
   article. Anchor text differs across posts. No phone number in the body; the NAP footer carries it.
10. `<!-- block: sources -->`: at least one primary source (government, manufacturer, standards body, or the business's
    own confirmed page when the research sidecar says `"source_kind": "own-confirmed"`). Only URLs in this block count
    as sources, and the research sidecar `used` item must appear here (by its URL, or by its words).
11. NAP footer `{{locations[0].footer}}` on its own line, matching the profile exactly.

## The variation axes (the recipe)

`recipes.json` lists the axes. `mseo content recipe` picks the value on every
axis that is most different from the last six logged posts and writes the choice into the content log. The
`variation` gate recomputes the axes from the draft and fails when:

- the shape (block order) equals any of the last 6 posts;
- more than 2 axes match the previous post;
- the first 8 words of the answer block match any logged post;
- any `##` question repeats a logged `##` within 60 days (the duplicates gate already covers near-matches);
- the lead word of more than half the `##` questions is the same word;
- fewer than 3 of the 6 "proof" items are present: a number from `facts.json`, a named local place, a named source,
  a table or list, a scenario, a first-hand photo or labelled illustration;
- the word count is outside the recipe's band, or three consecutive posts share a band.

| Axis | Values | Rule |
|---|---|---|
| shape | 8 block orders (see `recipes.json`) | not in the last 6 |
| title pattern | how/what/when question · number + noun · plain statement · X vs Y · should-you · local-led | not the last 2 |
| intro style | bold two sentences · bold one line + number · answer then why · scenario sentence then bold answer | not the last 2 |
| evidence element | table · numbered steps · checklist · worked scenario | not the last 2 |
| FAQ count | 3 · 4 · 5 · 6 | not the last 2 |
| CTA position | end · after answer + end · mid + end | not the last 1 |
| word band | 700-900 · 900-1100 · 1100-1400 | not 3 in a row |
| H2 lead words | how · what · when · why · can · should · which · where | at most half the H2s share one |
| local tie-in placement | in the answer · own section · in the FAQ · in the close | not the last 2 |
| closing style | one-line summary · next step · a question back to the reader · the local fact restated | not the last 2 |

Writers rewrite every template sentence. Templates carry instructions and tokens, not prose, so no two posts can
share boilerplate.

## Business Profile posts

Six templates under `business-profile/`. Core: 100-300 words (hard limit 1,500 characters), one idea, no phone
number (the Call button carries it), no prices or promises outside `facts.json`, the service named once, the area
named once, a plain "what to do next" line. Axes: opening pattern (question · fact · scenario · local hook), length
band (short 60-120 words · medium 120-220 · long 220-300), closing line (next step · question · reassurance), image
(yes with alt text · no). Rules: type not in the last 3 posts; opening pattern not the last 2; the same `facts.json`
claim at most once in 60 days (the duplicates gate's `claims` list); theme overlap under 0.5 with any post in the
window (existing gate). Posting slots come from `customer.json`.

## Configuration the templates and gates read

Four keys keep the framework free of one customer's or one country's assumptions. `content.link_paths` in
`customer.json` lists the site paths the links gate accepts for the service page (`service`, default `/services/`)
and the contact or estimate page (`contact`, default `/contact/`, `/estimate/`, `/book/`, `/appointment/`), matched
by path prefix; `content new` fills `{{links.service}}` and `{{links.contact}}` from the first entry of each, and
`related_default` stands in for `{{links.related}}` when no post is listed yet. `locations[].city` names a location's
city when the address rule (the token before `, ST zip`) cannot; `business.country` (ISO alpha-2, default `US`) is
the `addressCountry` of the business schema. The lead words the axis heuristics read (question, should, next-step
and scenario leads, the H2 lead words) live in `recipes.json` under `lexicon.<lang>` for `en` and `es`; a draft's
language is its `<!-- lang: xx -->` comment, else its FAQ heading, else `en`.

## Tokens

`{{business.name}}`, `{{business.site}}`, `{{service}}` (from the topic pick), `{{area}}` (the location's city or the
research file's town), `{{locations[0].footer}}`, `{{author.name}}`, `{{author.role}}`, `{{author.url}}`,
`{{today}}`, `{{research.file}}`, `{{research.used}}`, `{{links.service}}`, `{{links.contact}}`, `{{links.related}}`.
Unknown token or missing fact: `content new` exits 3 and names it.
