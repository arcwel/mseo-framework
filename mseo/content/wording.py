"""content/wording: `mseo content wording FILE`, the house wording rule (ported from the earlier house script).

The rule now comes from customer.json: each entry of content.wording.never is a case-insensitive regex, and
content.wording.always maps an entry to the phrase to use instead.
Exit 0 none found, 1 banned wording found, 3 the file cannot be read.
"""

from __future__ import annotations

import re
import sys

from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK

NO_REPLACEMENT = "different wording"


def rules(ws) -> list[tuple[str, str]]:
    wording = ws.config["content"]["wording"]
    return [(pat, wording["always"].get(pat, NO_REPLACEMENT)) for pat in wording["never"]]


def check(text: str, banned) -> list[tuple[int, str, str]]:
    """[(line, found, use instead)] for every match of every banned pattern."""
    out = []
    for pat, fix in banned:
        for m in re.finditer(pat, text, re.I):
            out.append((text.count("\n", 0, m.start()) + 1, m.group(0), fix))
    return out


def cmd_wording(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    path = ws.contains(args.file)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"error: cannot read {path} ({exc}); wording check did not run", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    found = check(text, rules(ws))
    msgs = [f"{path.name}:{line}: '{hit}' -> use '{fix}'" for line, hit, fix in found]
    ctx.emit({"status": "fail" if found else "ok", "findings": [{"line": l, "found": h, "use": f} for l, h, f in found],
              "messages": msgs + ["OK: no banned wording" if not found else f"{len(found)} problem(s)"]})
    return EXIT_FAIL if found else EXIT_OK


def register(csub, parents) -> None:
    p = csub.add_parser("wording", parents=[parents.workspace, parents.json], help="Find the workspace's banned wording")
    p.add_argument("file")
    p.set_defaults(func=cmd_wording, command_id="content wording")
