<!-- Template: questions-led. Recipe axes are set by `mseo content recipe`; rewrite every sentence, keep the block markers. See templates/posts/README.md. -->
<!-- Meta Description: ###WRITE### -->
<!-- Title pattern: plain statement, e.g. What {{service}} really involves -->
# ###WRITE###

<!-- block: answer -->
<!-- 40-80 words, bold, stands alone when quoted. Name {{service}} and {{area}} once. One fact from facts.json. Intro style: {{intro}}. -->
**###WRITE###**

<!-- block: questions -->
## ###WRITE### (a "what" question)
###WRITE###

<!-- block: table -->
## ###WRITE### (a "which" or "how much" question)
<!-- A pipe table with 3-6 rows. Numbers only from facts.json or a named source. -->
| ###WRITE### | ###WRITE### | ###WRITE### |
|---|---|---|
| ###WRITE### | ###WRITE### | ###WRITE### |
| ###WRITE### | ###WRITE### | ###WRITE### |

<!-- block: right -->
## ###WRITE### (a question about the reader's rights or choices)
###WRITE###

<!-- block: local -->
## ###WRITE### (names {{area}} or a local place)
###WRITE###

<!-- block: read -->
## ###WRITE### (a "how do I read / understand" question)
###WRITE###

<!-- block: faq -->
## Frequently asked questions
<!-- {{faq_n}} questions, each a real query from Search Console or the research file. Answer in 1-3 sentences, first sentence is the answer. The FAQ schema is generated from this section. -->
### ###WRITE###
###WRITE###

### ###WRITE###
###WRITE###

### ###WRITE###
###WRITE###

<!-- block: cta -->
## What to do next
<!-- Three internal links, anchor text different from the last posts: service page, contact or estimate page, one related article. No phone number here. -->
###WRITE### [###WRITE###]({{links.service}}) ###WRITE### [###WRITE###]({{links.contact}}) ###WRITE### [###WRITE###]({{links.related}})

<!-- block: author -->
## Who wrote this
Written by {{author.name}}, {{author.role}} at {{business.name}}. Reviewed {{today}}. <!-- author must be in facts.json; link the bio: -->[About {{author.name}}]({{author.url}})

<!-- block: sources -->
## Sources
<!-- At least one primary source. The research sidecar `used` item goes here with its link. -->
- ###WRITE### ({{research.used}})

{{locations[0].footer}}
