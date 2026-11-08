<!-- Template: myth-vs-fact. Recipe axes are set by `mseo content recipe`; rewrite every sentence, keep the block markers. See templates/posts/README.md. -->
<!-- Meta Description: ###WRITE### -->
<!-- Title pattern: number + noun, e.g. 5 things people get wrong about {{service}} -->
# ###WRITE###

<!-- block: answer -->
<!-- 40-80 words, bold, stands alone when quoted. Name {{service}} and {{area}} once. One fact from facts.json. Intro style: {{intro}}. -->
**###WRITE###**

<!-- block: myths -->
## ###WRITE### (a "what" question about common beliefs)
<!-- Bullets: **Myth:** ... **Fact:** ... Each fact sourced or in facts.json. 3-5 pairs. -->
- **Myth:** ###WRITE### **Fact:** ###WRITE###
- **Myth:** ###WRITE### **Fact:** ###WRITE###
- **Myth:** ###WRITE### **Fact:** ###WRITE###

<!-- block: what-it-means -->
## ###WRITE### (a "what does this mean for me" question)
###WRITE###

<!-- block: scenario -->
## ###WRITE### (a "what happens if" question)
<!-- One worked scenario, 120-180 words, a named local road or place from {{research.file}}. -->
###WRITE###

<!-- block: local -->
## ###WRITE### (names {{area}})
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

<!-- block: author -->
## Who wrote this
Written by {{author.name}}, {{author.role}} at {{business.name}}. Reviewed {{today}}. <!-- author must be in facts.json; link the bio: -->[About {{author.name}}]({{author.url}})

<!-- block: cta -->
## What to do next
<!-- Three internal links, anchor text different from the last posts: service page, contact or estimate page, one related article. No phone number here. -->
###WRITE### [###WRITE###]({{links.service}}) ###WRITE### [###WRITE###]({{links.contact}}) ###WRITE### [###WRITE###]({{links.related}})

<!-- block: sources -->
## Sources
<!-- At least one primary source. The research sidecar `used` item goes here with its link. -->
- ###WRITE### ({{research.used}})

{{locations[0].footer}}
