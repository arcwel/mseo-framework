"""content/lint_copy: `mseo content lint FILE`, the house copy rules.

Business Profile posts: no phone numbers, the 1,500-character limit, and the customer's sensitive-framing rule
(content.sensitive_terms: terms whose density or lead position trips a platform classifier; empty means no rule).
All copy: allegation phrasing and claims facts.json does not cover. Exit 1 when an ERROR is found, 0 otherwise
(warnings never block here), 3 when facts.json cannot be read.
"""

from __future__ import annotations

import sys

from mseo.content.claims import (
    GBP_CHAR_LIMIT,
    PHONE_RE,
    FactsUnavailable,
    load_facts,
    sensitive_hits,
    unverified_claims,
)
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK

ALLEGATIONS = ("cheap parts", "will cheat", "rip you off", "scam")
DEFAULT_REASON = "sensitive framing (classifier risk)"


def content_rules(ws) -> dict:
    """The customer's copy rules from customer.json content.*, with code defaults for a v1 config."""
    content = ws.config["content"]
    return {"sensitive": tuple(content.get("sensitive_terms") or ()),
            "density": float(content.get("sensitive_density", 0.05)),
            "reason": content.get("sensitive_reason") or DEFAULT_REASON,
            "claim_patterns": tuple(content.get("claim_patterns") or ())}


def lint_text(text: str, gbp: bool = True, facts=None, *, sensitive=(), density: float = 0.05,
              reason: str = DEFAULT_REASON, claim_patterns=()):
    """Check post copy against the house content rules. Returns [(level, message)]."""
    findings = []
    phones = PHONE_RE.findall(text)
    if gbp and phones:  # the NAP footer phone is fine on the site; Google rejects posts that carry one
        findings.append(("ERROR", f"phone number in text ({phones[0].strip()}) — Google auto-rejects Business Profile posts with phone numbers; use the Call now button"))
    if gbp and len(text) > GBP_CHAR_LIMIT:
        findings.append(("ERROR", f"{len(text)} chars exceeds the Business Profile limit of {GBP_CHAR_LIMIT}"))
    if gbp and sensitive:
        hits, total, leads = sensitive_hits(text, sensitive)
        share = hits / max(total, 1)
        if share > density or leads:
            where = "headline leads with a sensitive term" if leads else f"sensitive-term density {share:.0%}"
            findings.append(("WARN", f"{where} — {reason}; lead with the service instead"))
    for phrase in ALLEGATIONS:
        if phrase in text.lower():
            findings.append(("WARN", f'"{phrase}" — reads as allegation about others; state what we do instead'))
    if facts is not None:  # no invented claims: every number, guarantee or superlative must be verified
        for claim in unverified_claims(text, facts, claim_patterns):
            findings.append(("WARN", f'unverified claim "{claim}" — add it to the workspace facts.json if it is true, otherwise remove it'))
    return findings


def cmd_lint(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    path = ws.contains(args.file)
    text = path.read_text(encoding="utf-8")
    try:
        facts = load_facts(ws.file("facts"))
    except FactsUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    findings = lint_text(text, gbp=not args.blog, facts=facts, **content_rules(ws))
    status = "fail" if any(lv == "ERROR" for lv, _ in findings) else ("warn" if findings else "ok")
    ctx.emit({"status": status, "file": str(path), "chars": len(text),
              "findings": [{"level": lv, "message": msg} for lv, msg in findings],
              "messages": [f"{lv}: {msg}" for lv, msg in findings] or [f"OK — {len(text)} chars, no rule violations"]})
    return EXIT_FAIL if status == "fail" else EXIT_OK


def register(csub, parents) -> None:
    p = csub.add_parser("lint", parents=[parents.workspace, parents.json], help="Check copy against the house rules")
    p.add_argument("file")
    p.add_argument("--blog", action="store_true", help="Lint as blog content (skips Business Profile-only rules)")
    p.set_defaults(func=cmd_lint, command_id="content lint")
