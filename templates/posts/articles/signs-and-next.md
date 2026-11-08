<!-- Template: signs-and-next. Recipe axes are set by `mseo content recipe`; rewrite every sentence, keep the block markers. See templates/posts/README.md. -->
<!-- Meta Description: ###WRITE### -->
<!-- Title pattern: how / what question, e.g. How do I know if ...? -->
# ###WRITE###

<!-- block: answer -->
<!-- 40-80 words, bold, stands alone when quoted. Name {{service}} and {{area}} once. One fact from facts.json. Intro style: {{intro}}. -->
**###WRITE###**

<!-- block: signs -->
## ###WRITE### (a "how do I know" question)
<!-- 3-6 signs as bullets, each one sentence, each observable by the reader. -->
- ###WRITE###
- ###WRITE###
- ###WRITE###

<!-- block: decisions -->
## ###WRITE### (a "can I wait" or "is it safe" question)
###WRITE###

<!-- block: local -->
## ###WRITE### (names {{area}})
###WRITE###

<!-- block: steps -->
## ###WRITE### (a "what do I do next" question)
1. ###WRITE###
2. ###WRITE###
3. ###WRITE###

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
