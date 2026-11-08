# <Business name>: house rules for written content and images

Fill in each item with the business. Every item names who decided it and when. Everything that a check can enforce is
mirrored in `customer.json`; this file explains the why.

1. **Wording:** words or phrases to always use or never use. Mirror them in `customer.json` under `content.wording`.
2. **Business Profile posts:** length limit, links, phone numbers yes/no, framing to avoid, posting days and times
   (`posting.business_profile_slots`).
3. **Facts:** only claims on the live site or confirmed by the business in writing (`facts.json`). List the claims that
   are never allowed (`content.denylist_claims`) and the claim-like phrases that must always be verified
   (`content.claim_patterns`, for example a guarantee wording or a turnaround promise).
4. **Names and brands:** certifications, memberships and product brands that may be named, and how often
   (`business.certifications`, `content.subject_brands`).
5. **No repeats in 60 days:** same topic, same claims or near-identical text fails the duplicate check.
6. **Images:** how images are made and reviewed, alt text, and how AI images are labelled.
7. **Sensitive framing:** terms whose heavy use or lead position trips a platform classifier for this industry
   (financial, medical, legal), and the reason (`content.sensitive_terms`, `content.sensitive_reason`).
8. **How the copy names the premises:** shop, office, clinic, store, practice (`content.business_nouns`); the checks
   on phone, hours and address statements use these words.
9. **Local research:** radius around each address, the official sources to use, and any local topics to avoid.
