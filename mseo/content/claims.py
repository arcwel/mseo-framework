"""content/claims: claim-like phrases that facts.json does not cover, and statements that contradict the business
facts (phone, street address, opening hours). Nothing here knows an industry: the generic patterns (numbers, prices,
years, superlatives) are code; every industry phrase comes from customer.json (`content.claim_patterns`,
`content.sensitive_terms`, `content.business_nouns`, `business.brand_terms`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path


class FactsUnavailable(Exception):
    pass


PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
GBP_CHAR_LIMIT = 1500
DEFAULT_BUSINESS_NOUNS = ("shop", "office", "clinic", "store", "location", "practice")

# Claims that must be backed by facts.json whatever the industry (an invented claim is a trust and policy risk).
# A customer adds its own under content.claim_patterns (an industry promise, a turnaround wording).
GENERIC_CLAIM_PATTERNS = [
    re.compile(r"\b\d+\s*(?:-|–|—|to|or)\s*\d+\s*(?:business\s+|working\s+)?(?:days?|hours?|weeks?)\b", re.I),
    re.compile(r"\b\d+\s*(?:business\s+|working\s+)?(?:days?|hours?)\b", re.I),
    re.compile(r"\b\d{2,}\+?\s+years?\b", re.I),
    re.compile(r"\bsince\s+(?:19|20)\d{2}\b", re.I),
    re.compile(r"\b\d+(?:\.\d+)?\s?%"),
    re.compile(r"\$\s?\d[\d,]*(?:\.\d\d)?"),
    re.compile(r"\b(?:best|#1|number one|guarantee[ds]?|always|never)\b", re.I),
]


def claim_patterns(extra=()) -> list:
    """The generic patterns plus the customer's own (content.claim_patterns), each case-insensitive."""
    return GENERIC_CLAIM_PATTERNS + [re.compile(pat, re.I) for pat in extra]


def sensitive_hits(text: str, terms) -> tuple[int, int, bool]:
    """(matching words, total words, first line matches) for the customer's content.sensitive_terms regexes."""
    patterns = [re.compile(t, re.I) for t in terms]
    words = re.findall(r"[\w\u00c0-\u024f]+", text)
    hits = sum(1 for w in words if any(p.search(w) for p in patterns)) if patterns else 0
    first = text.strip().splitlines()[0] if text.strip() else ""
    return hits, len(words), bool(patterns) and any(p.search(first) for p in patterns)


def load_facts(path):
    """Verified claims the copy may use: {"claims": ["5-10 business days", ...]}. Raises FactsUnavailable when the file is missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FactsUnavailable(f"facts file {path} could not be read ({exc}); the claims check cannot run") from exc
    if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
        raise FactsUnavailable(f'facts file {path} must be {{"claims": [...]}}; the claims check cannot run')
    # an entry is either a string or {"claim": "...", "source": "where it was confirmed"}
    return [str(c.get("claim", "") if isinstance(c, dict) else c).lower().strip() for c in data.get("claims", [])]


def _dashes(t: str) -> str:
    """One spelling for numeric ranges: "3-5", "3 - 5", "3—5" and "3 to 5" all become "3–5"."""
    return re.sub(r"(\d)\s*(?:[-–—]|\bto\b)\s*(\d)", "\\1–\\2", t)


def _covers(short: str, long: str) -> bool:
    """True when `short` appears in `long` as whole tokens: "2 days" is in "2 days or less" but not in "12 days",
    and "30 days" does not cover the range "7–30 days"."""
    short, long = _dashes(short), _dashes(long)
    # "30 days" is not a claim inside "3 or 30 days" either: that phrase states two numbers
    return re.search(r"(?<!\w)(?<!\d[.,])(?<![-–—])(?<!\d or )" + re.escape(short) + r"(?!\w)(?![.,]\d)(?![-–—]\d)(?! or \d)", long) is not None


def unverified_claims(text: str, facts, extra_patterns=()):
    """Claim-like phrases in `text` that no verified fact covers (a fact covers a phrase if it contains it)."""
    found, seen, spans = [], set(), []
    for pattern in claim_patterns(extra_patterns):
        for m in pattern.finditer(text):
            if any(a <= m.start() and m.end() <= b for a, b in spans):
                continue  # "5 days" inside "3-5 days" is part of the range, not a claim of its own
            spans.append(m.span())
            phrase = re.sub(r"\s+", " ", m.group(0).lower().strip())
            if phrase in seen:
                continue
            seen.add(phrase)
            if not any(_covers(phrase, fact) or _covers(fact, phrase) for fact in facts):
                found.append(m.group(0).strip())
    return found


STREET_FOLD = {"road": "rd", "avenue": "ave", "street": "st", "boulevard": "blvd", "drive": "dr", "lane": "ln",
               "highway": "hwy", "court": "ct", "place": "pl", "parkway": "pkwy"}
DIRECTIONS = {"north": "n", "south": "s", "east": "e", "west": "w"}
_SUFFIXES = "|".join(sorted(set(STREET_FOLD) | set(STREET_FOLD.values()) | {"way"}, key=len, reverse=True))
# a street number, one to three words (directionals and numbered streets like 4th included), a street suffix
ADDRESS_RE = re.compile(r"\b(\d{1,6})\s+((?:(?:[A-Za-z]+|\d+(?:st|nd|rd|th))\.?\s+){1,3}?)(" + _SUFFIXES + r")\b\.?", re.I)
TIME_RANGE_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?m?\.?\s*(?:-|–|—|to|until)\s*(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b\.?", re.I)
OPEN_DAYS_RE = re.compile(r"\bopen\s+(?:on\s+)?(saturdays?|sundays?|weekends?|7 days|seven days)\b", re.I)
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
NEGATION_RE = re.compile(r"\b(?:not|no|never|don't|doesn't|cannot|can't|isn't|won't|without)\b", re.I)
SENTENCE_END_RE = re.compile(r"[!?\n]|\.\s")
# Only clear statements about the business are compared (false positives block real drafts). The business's own
# names come from customer.json business.brand_terms, the noun for its premises from content.business_nouns.


def context_res(brand_terms, nouns=DEFAULT_BUSINESS_NOUNS) -> tuple[re.Pattern, re.Pattern, re.Pattern]:
    """(named, hours, address) context regexes: the business's brand terms OR the generic phrases."""
    noun = "|".join(re.escape(n) for n in nouns) or "location"
    named = rf"\b(?:call us|text us|our (?:{noun})|our number|reach us)\b"            # phones
    hours = rf"\b(?:open|opens|hours|closed|we're|call|{noun})\b"                     # time ranges
    address = rf"\b(?:our (?:{noun})|visit us|visit our|find us|located at|come see us)\b"
    brand = "|".join(f"(?:{t})" for t in brand_terms)
    return tuple(re.compile((brand + "|" if brand else "") + g, re.I) for g in (named, hours, address))


def business_facts(facts) -> dict | None:
    """The `business` block of facts.json (phone, locations, hours); `shop` is accepted as the older name."""
    if not isinstance(facts, dict):
        return None
    block = facts.get("business")
    return block if isinstance(block, dict) else None


def _line_at(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _sentence(text: str, start: int, end: int) -> tuple[str, str]:
    """(the sentence up to the match, the whole sentence) around text[start:end]."""
    left = max((m.end() for m in SENTENCE_END_RE.finditer(text, 0, start)), default=0)
    right = next((m.start() for m in SENTENCE_END_RE.finditer(text, end)), len(text))
    return text[left:start], text[left:right]


def _street(m: re.Match) -> str:
    """'<n> <Name> Road', '<n> <name> rd.' and '<n> <Name> Rd Suite A' fold to one key; 'West 4th' and 'W 4th' too."""
    name = " ".join(DIRECTIONS.get(w.strip(".").lower(), w.strip(".").lower()) for w in m.group(2).split())
    suffix = m.group(3).lower()
    return f"{m.group(1)} {name} {STREET_FOLD.get(suffix, suffix)}"


def _clock(hour: str, minute: str | None, half: str) -> str:
    h = int(hour) % 12 + (12 if half.lower() == "p" else 0)
    return f"{h:02d}:{minute or '00'}"


def _expand_days(spec: str) -> set:
    """'Mon-Thu' -> {mon, tue, wed, thu}; 'Fri' -> {fri}."""
    parts = [p.strip().lower()[:3] for p in re.split(r"\s*[-–]\s*", spec)]
    if len(parts) == 2 and all(p in DAYS for p in parts):
        a, b = DAYS.index(parts[0]), DAYS.index(parts[1])
        return set(DAYS[a:b + 1])
    return {p for p in parts if p in DAYS}


def _place_re(shop: dict) -> re.Pattern:
    """City, state and ZIP from the business's addresses (content/places, so a suite line is not a place), or any
    ZIP: what makes '<number> <name> <suffix>' an address."""
    from mseo.content import places

    words = set().union(*[places.place_words(loc) for loc in shop["locations"] if hasattr(loc, "get")] or [set()])
    return re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(words, key=len, reverse=True)) or ["(?!)"]) + r"|\d{5})\b", re.I)


def business_mismatches(text: str, facts: dict, brand_terms=(), nouns=DEFAULT_BUSINESS_NOUNS):
    """Phone numbers, street addresses and opening hours in `text` that differ from facts.json's `business` block.
    Returns [(line, found, expected)]. A missing or malformed block raises FactsUnavailable.
    Conservative: a negated sentence is skipped; a phone counts when it has the business's area code or the sentence
    names the business; a time range only in a sentence about opening hours; an address only with a city/state/ZIP
    after it or a sentence that points at the business."""
    shop = business_facts(facts)
    if shop is None or not all(k in shop for k in ("phone", "locations", "hours")):
        raise FactsUnavailable("facts file has no business block (phone, locations, hours); the business-facts check cannot run")
    out = []
    SHOP_NAMED_RE, HOURS_CONTEXT_RE, ADDRESS_CONTEXT_RE = context_res(brand_terms, nouns)

    def statements(regex, context=None, after=None):
        for m in regex.finditer(text):
            before, sentence = _sentence(text, m.start(), m.end())
            if NEGATION_RE.search(before):
                continue
            if context is None or context.search(sentence) or (after is not None and after.search(text, m.end(), m.end() + 40)):
                yield m, sentence
    phone = re.sub(r"\D", "", str(shop["phone"]))[-10:]
    for m, sentence in statements(PHONE_RE):
        digits = re.sub(r"\D", "", m.group(0))[-10:]
        if digits != phone and (digits[:3] == phone[:3] or SHOP_NAMED_RE.search(sentence)):
            out.append((_line_at(text, m.start()), m.group(0).strip(), shop["phone"]))
    streets = {_street(m) for loc in shop["locations"] for m in [ADDRESS_RE.search(loc.get("address", ""))] if m}
    for m, _ in statements(ADDRESS_RE, ADDRESS_CONTEXT_RE, _place_re(shop)):
        if _street(m) not in streets:
            out.append((_line_at(text, m.start()), m.group(0).strip(), " or ".join(l["address"] for l in shop["locations"])))
    open_hours = {(h["opens"], h["closes"]) for h in shop["hours"] if not h.get("closed")}
    wanted = "; ".join(f'{h["days"]} closed' if h.get("closed") else f'{h["days"]} {h["opens"]}-{h["closes"]}' for h in shop["hours"])
    for m, _ in statements(TIME_RANGE_RE, HOURS_CONTEXT_RE):
        closes = _clock(m.group(4), m.group(5), m.group(6))
        opens_half = m.group(3) or ("a" if 5 <= int(m.group(1)) <= 11 else m.group(6))
        if (_clock(m.group(1), m.group(2), opens_half), closes) not in open_hours:
            out.append((_line_at(text, m.start()), m.group(0).strip(), wanted))
    closed = set().union(*[_expand_days(h["days"]) for h in shop["hours"] if h.get("closed")] or [set()])
    for m, _ in statements(OPEN_DAYS_RE):
        word = m.group(1).lower()
        days = {"sat"} if word.startswith("sat") else {"sun"} if word.startswith("sun") else {"sat", "sun"} if word.startswith("week") else set(DAYS)
        if days & closed:
            out.append((_line_at(text, m.start()), m.group(0).strip(), wanted))
    return sorted(out)


