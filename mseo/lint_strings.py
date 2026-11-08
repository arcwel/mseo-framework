"""mseo lint customer-strings: keep customer facts out of framework code.

Collects every x-identifying value from every workspace under MSEO_WORKSPACES plus the repo's tests/workspaces/,
and searches the repo's tracked files for them (and, with --history, every commit's patch).

Matching: literal, case-insensitive, on word boundaries; values under 4 characters are skipped. Phone numbers and
account IDs are compared as digits, only at 7 or more digits. URL values also match their bare host. Regex fields
(business.brand_terms) are not matched. Fixed patterns are flagged too: US phone shapes and street-address shapes.
Out of scope: tests/workspaces/, any validation/ folder, and lint/allow.txt.

lint/allow.txt: one entry per line, `<path>:<line or *> <value> # <reason>`; the reason is required.
Exit 0 clean, 1 hits found, 3 the lint could not run.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from mseo import workspace as workspace_mod
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK

MIN_CHARS = 4
MIN_DIGITS = 7
DIGIT_FIELDS = re.compile(r"^(business\.phone|accounts\.[^.]+\.id)$")
SKIP_FIELDS = re.compile(r"^business\.brand_terms")
ALLOW_FILE = "lint/allow.txt"
FIXED_PATTERNS = {
    "pattern:us_phone": re.compile(r"(?<![\w-])(?:\(\d{3}\)\s?|\d{3}[-.])\d{3}[-.]\d{4}(?![\w-])"),
    "pattern:street_address": re.compile(
        r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+"
        r"(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Highway|Hwy|Court|Ct|Place|Pl|Parkway|Pkwy)\b"),
}
DIGIT_RUN = re.compile(r"\+?\d[\d\s().-]{5,}\d")


class LintError(Exception):
    """The lint cannot run (not a git repo, bad allow.txt)."""


@dataclass
class Needle:
    value: str
    workspace: str
    field: str
    digits: str = ""
    regex: re.Pattern | None = None


@dataclass
class Report:
    hits: list[dict] = field(default_factory=list)
    unprotected: list[str] = field(default_factory=list)  # workspace folders with no customer.json to search for

    @property
    def code(self) -> int:
        return EXIT_FAIL if self.hits else EXIT_OK


def framework_root() -> Path:
    return Path(__file__).resolve().parents[1]


def real_root() -> Path:
    return workspace_mod.workspaces_root()


def unprotected_folders(roots: list[Path]) -> list[str]:
    """Folders under a workspaces root with no customer.json (an older config format, say): their values are not linted."""
    return sorted(p.name for root in roots if root.is_dir() for p in root.iterdir()
                  if p.is_dir() and not p.name.startswith(".") and not (p / workspace_mod.CONFIG_FILE).is_file())


def _workspace_configs(roots: list[Path]) -> list[tuple[str, dict]]:
    out = []
    for root in roots:
        if not root.is_dir():
            continue
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            cfg = folder / workspace_mod.CONFIG_FILE
            if not cfg.is_file():
                continue
            try:
                out.append((folder.name, json.loads(cfg.read_text(encoding="utf-8"))))
            except (OSError, ValueError) as exc:
                raise LintError(f"{cfg} could not be read ({exc})") from exc
    return out


def needles(roots: list[Path]) -> list[Needle]:
    schema = workspace_mod.load_schema(2)  # identifying markers of the current schema (v1 is retired)
    out: list[Needle] = []
    for slug, config in _workspace_configs(roots):
        for path, value in workspace_mod.identifying_values(config, schema):
            generic = re.sub(r"\[\d+\]", "", path)
            if SKIP_FIELDS.match(generic):
                continue
            values = [value]
            if value.startswith("https://"):
                host = urlparse(value).hostname or ""
                values.append(host[4:] if host.startswith("www.") else host)
            for v in values:
                digits = re.sub(r"\D", "", v) if DIGIT_FIELDS.match(generic) else ""
                if digits and len(digits) >= MIN_DIGITS:
                    out.append(Needle(v, slug, path, digits=digits))
                    continue
                if DIGIT_FIELDS.match(generic) and re.fullmatch(r"[\d\s().+-]+", v):
                    continue  # a short numeric ID: below the digit threshold
                if len(v) < MIN_CHARS:
                    continue
                out.append(Needle(v, slug, path, regex=re.compile(r"(?<!\w)" + re.escape(v) + r"(?!\w)", re.I)))
    return out


def _in_scope(rel: str) -> bool:
    parts = Path(rel).parts
    return not (rel.startswith("tests/workspaces/") or "validation" in parts[:-1] or rel == ALLOW_FILE)


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LintError(f"git could not run ({exc})") from exc
    if proc.returncode != 0:
        raise LintError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout


def load_allow(repo: Path) -> list[tuple[str, str, str]]:
    path = repo / ALLOW_FILE
    if not path.exists():
        return []
    out = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(\S+):(\d+|\*)\s+(.+?)\s+#\s*(\S.*)$", line)
        if not m:
            raise LintError(f"{ALLOW_FILE}:{n}: expected '<path>:<line or *> <value> # <reason>' (a reason is required)")
        out.append((m.group(1), m.group(2), m.group(3).lower()))
    return out


def _allowed(allow, rel: str, line: int, value: str) -> bool:
    return any(p == rel and (ln == "*" or int(ln) == line) and v == value.lower() for p, ln, v in allow)


def scan_text(text: str, rel: str, items: list[Needle], allow) -> list[dict]:
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        runs = [re.sub(r"\D", "", m.group(0)) for m in DIGIT_RUN.finditer(line)] + re.findall(r"\d{7,}", line)
        for nd in items:
            if nd.digits:
                if any(nd.digits in r for r in runs):
                    hits.append({"file": rel, "line": n, "value": nd.value, "workspace": nd.workspace, "field": nd.field})
            elif nd.regex.search(line):
                hits.append({"file": rel, "line": n, "value": nd.value, "workspace": nd.workspace, "field": nd.field})
        for name, pat in FIXED_PATTERNS.items():
            for m in pat.finditer(line):
                hits.append({"file": rel, "line": n, "value": m.group(0), "workspace": None, "field": name})
    seen, out = set(), []
    for h in hits:
        key = (h["file"], h["line"], h["value"].lower(), h["field"])
        if key not in seen and not _allowed(allow, rel, h["line"], h["value"]):
            seen.add(key)
            out.append(h)
    return out


def _history(repo: Path, items, allow) -> list[dict]:
    """Added lines in every commit's patch, outside the excluded paths."""
    hits, current, commit = [], None, ""
    for line in _git(repo, "log", "-p", "--all", "--no-color", "--format=commit %H").splitlines():
        if line.startswith("commit "):
            commit = line.split()[1][:12]
        elif line.startswith("+++ "):
            name = line[4:]
            current = name[2:] if name.startswith("b/") else None
        elif line.startswith("+") and current and _in_scope(current):
            for h in scan_text(line[1:], current, items, allow):
                hits.append({**h, "file": f"history:{commit}:{current}"})
    return hits


def run(repo: Path, extra_roots: list[Path] | None = None, history: bool = False) -> Report:
    repo = Path(repo).resolve()
    _git(repo, "rev-parse", "--show-toplevel")
    roots = [repo / "tests" / "workspaces"] + list(extra_roots or [])
    items = needles(roots)
    allow = load_allow(repo)
    report = Report(unprotected=unprotected_folders(list(extra_roots or [])))
    for rel in _git(repo, "ls-files", "-z").split("\0"):
        if not rel or not _in_scope(rel):
            continue
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except (OSError, IsADirectoryError):
            continue
        report.hits += scan_text(text, rel, items, allow)
    if history:
        report.hits += _history(repo, items, allow)
    return report


# ---------------------------------------------------------------- industry vocabulary

VOCAB_FIELDS = (("content", "denylist_claims"), ("content", "subject_brands"), ("content", "topic_bank"),
                ("business", "certifications"), ("ads", "offer_terms"))
VOCAB_SCOPE = ("mseo/", "templates/", "workspace-template/")
VOCAB_MIN_CHARS = 4


def vocabulary_needles(roots: list[Path]) -> list[Needle]:
    """Every customer's industry phrases (denied claims, brands, topics, certifications, offers) as whole-phrase
    needles. The framework and its templates must not carry any of them: that is how the second customer runs on
    the same code."""
    out, seen = [], set()
    for slug, config in _workspace_configs(roots):
        for section, key in VOCAB_FIELDS:
            for value in ((config.get(section) or {}).get(key) or []):
                phrase = str(value).strip()
                if len(phrase) < VOCAB_MIN_CHARS or phrase.lower() in seen:
                    continue
                seen.add(phrase.lower())
                out.append(Needle(phrase, slug, f"{section}.{key}",
                                  regex=re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", re.I)))
    return out


def run_vocabulary(repo: Path, extra_roots: list[Path] | None = None) -> Report:
    repo = Path(repo).resolve()
    _git(repo, "rev-parse", "--show-toplevel")
    items = vocabulary_needles([repo / "tests" / "workspaces"] + list(extra_roots or []))
    allow = load_allow(repo)
    report = Report()
    for rel in _git(repo, "ls-files", "-z").split("\0"):
        if not rel or not rel.startswith(VOCAB_SCOPE) or rel == ALLOW_FILE:
            continue
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except (OSError, IsADirectoryError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for nd in items:
                if nd.regex.search(line) and not _allowed(allow, rel, n, nd.value):
                    report.hits.append({"file": rel, "line": n, "value": nd.value, "workspace": nd.workspace, "field": nd.field})
    return report


def cmd_vocabulary(args, ctx) -> int:
    repo = Path(args.repo) if args.repo else framework_root()
    try:
        report = run_vocabulary(repo, extra_roots=[real_root()])
    except LintError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    msgs = [f"{h['file']}:{h['line']}: {h['value']} (workspace {h['workspace']}, field {h['field']})" for h in report.hits]
    ctx.emit({"status": "fail" if report.hits else "ok", "hits": report.hits, "messages": msgs or ["vocabulary: clean"]})
    return report.code


# ---------------------------------------------------------------- CLI

def cmd_lint(args, ctx) -> int:
    repo = Path(args.repo) if args.repo else framework_root()
    try:
        report = run(repo, extra_roots=[real_root()], history=args.history)
    except LintError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    msgs = [f"{h['file']}:{h['line']}: {h['value']} "
            + (f"(workspace {h['workspace']}, field {h['field']})" if h["workspace"] else f"({h['field']})")
            for h in report.hits]
    for slug in report.unprotected:
        print(f"warning: workspace folder {slug!r} has no customer.json, so its values are not searched for; "
              "convert it before committing anything about it", file=sys.stderr)
    ctx.emit({"status": "fail" if report.hits else "ok", "hits": report.hits, "unprotected": report.unprotected,
              "messages": msgs or ["customer-strings: clean"]})
    return report.code


def register(sub, parents) -> None:
    p = sub.add_parser("lint", help="Static checks on the framework repo")
    lsub = p.add_subparsers(dest="lint_command", required=True, parser_class=type(p))
    c = lsub.add_parser("customer-strings", parents=[parents.json],
                        help="Find any workspace's identifying values in framework files (no --workspace: reads all)")
    c.add_argument("--repo", help="Repo to scan (default: this framework)")
    c.add_argument("--history", action="store_true", help="Also scan every commit's patch (git log -p)")
    c.set_defaults(func=cmd_lint, command_id="lint customer-strings")
    v = lsub.add_parser("vocabulary", parents=[parents.json],
                        help="Find any workspace's industry phrases (denied claims, brands, topics, certifications, offers) in mseo/ and templates/")
    v.add_argument("--repo", help="Repo to scan (default: this framework)")
    v.set_defaults(func=cmd_vocabulary, command_id="lint vocabulary")
