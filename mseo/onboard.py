"""`mseo workspace init SLUG`: the guided onboarding (docs/designs/workspace-init.md).

Every `<...>` placeholder in customer.json is a question, asked in file order and validated against the schema rule
for its path. Credentials are asked with hidden input and written to the Keychain through the same path as
`secret set`. The config is written after every accepted answer, so quitting (Ctrl-D or Ctrl-C) loses nothing and a
re-run asks only what is still a placeholder. `--set PATH=VALUE` and `--answers FILE` fill fields without prompting;
secrets always prompt. Without a terminal and with placeholders left, exit 3 lists them.
"""

from __future__ import annotations

import copy
import getpass
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from mseo import secrets as secrets_mod
from mseo import workspace as workspace_mod
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_OK
from mseo.fsutil import write_json_atomic
from mseo.workspace import CONFIG_FILE, WorkspaceError

PLACEHOLDER = re.compile(r"^<(.*)>$", re.S)
TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]|\['((?:[^'\\]|\\.)*)'\]")
RETRIES = 3
# Credential fields shown while typing. Anything not listed here is hidden, so a new secret field is never echoed.
PLAIN_FIELDS = {"site", "user", "username", "email"}

# Keys the runbook adds later. Offered once per run when absent; Enter skips.
OPTIONAL = (
    ("$.accounts.google_ads.scripts_sheet_id",
     "the ID of the Google Ads report Sheet, from its URL between /d/ and /edit (runbook step 1)"),
    ("$.site_audit.indexnow_key",
     "the IndexNow key you host at https://<domain>/<key>.txt, 8 to 128 characters (runbook step 7)"),
    ("$.site_audit.psi_key_env",
     "the name of the shell variable that will hold the PageSpeed Insights API key, usually MSEO_PSI_KEY (runbook step 6)"),
)
BANNER = ("Answer each question and press Enter. Ctrl-D saves what you have typed so far and stops; run the same "
          "command again to continue.")


class OnboardError(WorkspaceError):
    """Maps to exit 3."""


@dataclass
class Question:
    path: str                  # JSON path of the value, or of the dict entry whose key is the placeholder
    hint: str                  # the text inside the angle brackets
    key_of: str | None = None  # for a placeholder key: the path of the dict that holds it

    @property
    def label(self) -> str:
        return self.path[2:] if self.path.startswith("$.") else self.path


# ---------------------------------------------------------------- paths

def _tokens(path: str) -> list[str | int]:
    if not isinstance(path, str) or not path.startswith("$"):
        raise OnboardError(f"bad path {path!r}: must start with $")
    out: list[str | int] = []
    pos = 1
    for m in TOKEN.finditer(path):
        if m.start() != pos:
            raise OnboardError(f"bad path {path!r}")
        if m.group(1) is not None:
            out.append(m.group(1))
        elif m.group(2) is not None:
            out.append(int(m.group(2)))
        else:
            out.append(re.sub(r"\\(.)", r"\1", m.group(3)))
        pos = m.end()
    if pos != len(path) or not out:
        raise OnboardError(f"bad path {path!r}")
    return out


def _quote(key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f".{key}"
    return "['" + key.replace("\\", "\\\\").replace("'", "\\'") + "']"


def get_at(raw: Any, path: str) -> Any:
    node = raw
    for t in _tokens(path):
        node = node[t]
    return node


def has_path(raw: Any, path: str) -> bool:
    try:
        get_at(raw, path)
        return True
    except (KeyError, IndexError, TypeError):
        return False


def set_at(raw: dict, path: str, value: Any, *, create: bool = False) -> None:
    """Set a value at an existing path. With `create`, missing objects along the way are made (optional keys)."""
    toks = _tokens(path)
    node = raw
    for i, t in enumerate(toks):
        last = i == len(toks) - 1
        if isinstance(node, dict):
            if t not in node:
                if not create:
                    raise OnboardError(f"{path}: no such field in {CONFIG_FILE}")
                if not last:
                    node[t] = {}
            if last:
                node[t] = value
            else:
                node = node[t]
        elif isinstance(node, list):
            if not isinstance(t, int) or t >= len(node):
                raise OnboardError(f"{path}: no such list item in {CONFIG_FILE}")
            if last:
                node[t] = value
            else:
                node = node[t]
        else:
            raise OnboardError(f"{path}: {'.'.join(map(str, toks[:i]))} is a value, not an object")


def _parent_and_key(path: str):
    toks = _tokens(path)
    return "$" + "".join(_quote(t) if isinstance(t, str) else f"[{t}]" for t in toks[:-1]), toks[-1]


# ---------------------------------------------------------------- questions

def questions(raw: dict) -> list[Question]:
    """Every placeholder in file order. Comment keys (leading underscore) are skipped."""
    out: list[Question] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k.startswith("_"):
                    continue
                m = PLACEHOLDER.match(k)
                if m:
                    out.append(Question(f"{path}{_quote(k)}", m.group(1), key_of=path))
                    continue
                walk(v, f"{path}{_quote(k)}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            m = PLACEHOLDER.match(node)
            if m:
                out.append(Question(path, m.group(1)))

    walk(raw, "$")
    return out


def _errors(raw: dict) -> set[tuple[str, str]]:
    m = workspace_mod.SCHEMA_VERSION_RE.match(str(raw.get("schema_version", "")))
    if not m:
        raise OnboardError(f"{CONFIG_FILE} $.schema_version must look like customer/N")
    return {(e.json_path, e.message) for e in workspace_mod._validator(int(m.group(1))).iter_errors(raw)}


def try_change(raw: dict, change: Callable[[dict], None]) -> tuple[dict, str | None]:
    """Apply `change` to a copy. Returns (new_raw, None) when no new schema error appeared, else (raw, message).
    Comparing the error sets before and after catches an error reported at a parent (an unknown key is reported
    at `$`) and never confuses `$.business` with `$.business_profile`."""
    before = _errors(raw)
    candidate = copy.deepcopy(raw)
    change(candidate)
    new = sorted(_errors(candidate) - before)
    if new:
        path, message = new[0]
        return raw, f"{path}: {message}" if path != "$" else message
    return candidate, None


# ---------------------------------------------------------------- prompters

class TerminalPrompter:
    """Prompts on stderr (so --json stdout stays pure), answers from the terminal, hidden ones through getpass."""

    def ask(self, prompt: str, *, hidden: bool = False) -> str:
        if hidden:
            return getpass.getpass(prompt + ": ", stream=sys.stderr)
        print(prompt + ": ", end="", file=sys.stderr, flush=True)
        return input()


class NoPrompter:
    """--no-prompt, or no TTY: every question is unanswered."""

    def ask(self, prompt: str, *, hidden: bool = False) -> str:
        raise EOFError


# ---------------------------------------------------------------- the run

class Session:
    """One onboarding run: the config, where it is written, how to ask and how to report."""

    def __init__(self, slug: str, folder: Path, raw: dict, prompter, out: Callable[[str], None]):
        self.slug, self.folder, self.raw, self.prompter, self.out = slug, folder, raw, prompter, out
        self.cfg = folder / CONFIG_FILE
        self.notes: list[str] = []  # things left undone, for the summary

    def save(self) -> None:
        write_json_atomic(self.cfg, self.raw, ensure_ascii=False)

    def note(self, text: str) -> None:
        self.notes.append(text)
        self.out(f"  {text}")

    def ask(self, prompt: str, *, hidden: bool = False) -> str:
        answer = self.prompter.ask(prompt, hidden=hidden)
        return answer if hidden else answer.strip()

    # -- one question

    def answer_value(self, q: Question, given: Any) -> bool:
        """Fill one value. Returns False only when the operator stopped (EOF)."""
        hint = q.hint
        for attempt in range(RETRIES):
            if given is not None:
                answer, given, from_terminal = str(given).strip(), None, False
            else:
                try:
                    answer, from_terminal = self.ask(f"{q.label} ({hint})"), True
                except EOFError:
                    return False
            if PLACEHOLDER.match(answer):
                problem = "still a <...> placeholder"
            else:
                self.raw, problem = try_change(self.raw, lambda r, a=answer: set_at(r, q.path, a))
            if problem is None:
                self.save()
                return True
            if not from_terminal:
                self.note(f"{q.label}: not applied ({problem})")
                return True
            if attempt == RETRIES - 1:
                self.note(f"{q.label}: left unfilled ({problem})")
                return True
            hint = f"{q.hint}; {problem}"
        return True

    def answer_key(self, q: Question, given: Any) -> bool:
        """Fill a placeholder key and its value (the wording.always entry)."""
        d = get_at(self.raw, q.key_of)
        old_key = _tokens(q.path)[-1]
        value_hint = str(d[old_key]).strip("<>")
        hint = q.hint
        for attempt in range(RETRIES):
            if isinstance(given, dict) and "key" in given and "value" in given:
                key, value, given, from_terminal = str(given["key"]).strip(), str(given["value"]).strip(), None, False
            elif given is not None:
                self.note(f"{q.label}: not applied (an entry needs {{\"key\": ..., \"value\": ...}})")
                return True
            else:
                try:
                    key = self.ask(f"{q.label} (the key: {hint})")
                    value = self.ask(f"{q.key_of[2:]}[{key!r}] (its value: {value_hint})")
                    from_terminal = True
                except EOFError:
                    return False
            if not key or PLACEHOLDER.match(key):
                problem = "the key is empty or still a placeholder"
            elif key in d:
                problem = f"an entry {key!r} already exists"
            else:
                def rename(r, key=key, value=value):
                    target = get_at(r, q.key_of)
                    items = [(key, value) if k == old_key else (k, v) for k, v in target.items()]
                    target.clear()
                    target.update(items)
                self.raw, problem = try_change(self.raw, rename)
            if problem is None:
                self.save()
                return True
            if not from_terminal:
                self.note(f"{q.label}: not applied ({problem})")
                return True
            if attempt == RETRIES - 1:
                self.note(f"{q.label}: left unfilled ({problem})")
                return True
            hint = f"{q.hint}; {problem}"
        return True

    # -- the other steps

    def updates(self, given: dict) -> None:
        """--set / --answers entries that were not placeholders: plain field updates."""
        for path, value in given.items():
            try:
                self.raw, problem = try_change(self.raw, lambda r, p=path, v=value: set_at(r, p, v))
            except OnboardError as exc:
                problem = str(exc)
            if problem:
                self.note(f"{path}: not applied ({problem})")
            else:
                self.save()

    def credentials(self, ws, keychain) -> tuple[list[str], list[str]]:
        """Ask for each missing credential. Returns (stored services, missing services)."""
        stored, missing = [], []
        for source, keys in secrets_mod.PAYLOAD_KEYS.items():
            service = secrets_mod.service_name(ws.slug, source)
            if keychain.exists(service, ws.slug):
                continue
            self.out(f"credential {source}: {secrets_mod.CREDENTIAL_HELP.get(source, '')}".rstrip())
            payload = {}
            try:
                for key in keys:
                    hidden = key not in PLAIN_FIELDS
                    default = ws.config["cms"]["site"] if (source == "wordpress" and key == "site") else None
                    label = f"{source} {key}" + (f" (Enter for {default})" if default else "")
                    answer = self.ask(label, hidden=hidden)
                    payload[key] = (answer if hidden else answer) or default or ""
            except EOFError:
                missing.append(service)
                self.note(f"credential {service}: not stored; run: mseo secret set --workspace {ws.slug} --source {source}")
                continue
            try:
                secrets_mod.validate_payload(ws, source, payload)
                keychain.set(service, ws.slug, payload)
            except (secrets_mod.CredentialError, secrets_mod.SecretExists) as exc:
                missing.append(service)
                self.note(f"credential {service}: not stored ({exc})")
                continue
            stored.append(service)
            self.out(f"  stored {service}")
        return stored, missing

    def optional(self) -> list[str]:
        written = []
        for path, label in OPTIONAL:
            if has_path(self.raw, path):
                continue
            try:
                answer = self.ask(f"{path[2:]} (optional: {label}; Enter to skip)")
            except EOFError:
                break
            if not answer:
                continue
            self.raw, problem = try_change(self.raw, lambda r, p=path, a=answer: set_at(r, p, a, create=True))
            if problem:
                self.note(f"{path[2:]}: skipped ({problem})")
            else:
                self.save()
                written.append(path)
        return written


def _load_answers(path: Path | None) -> dict:
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OnboardError(f"answers file {path}: {exc}") from None
    if not isinstance(data, dict):
        raise OnboardError(f"answers file {path}: must be a JSON object of path: value")
    return data


def _parse_sets(sets) -> dict:
    out = {}
    for item in sets or ():
        if "=" not in item:
            raise OnboardError(f"--set needs PATH=VALUE, got {item!r}")
        path, value = item.split("=", 1)
        _tokens(path)  # a malformed path is a usage error, before anything is read
        out[path] = value
    return out


def run(slug: str, prompter, keychain, *, answers_file: Path | None = None, sets=(), out: Callable = print) -> dict:
    """The whole onboarding for one workspace. Returns the summary the CLI emits."""
    workspace_mod.cwd_guard(slug)
    folder = workspace_mod.workspace_dir(slug)  # validates the slug and refuses links before any read or write
    cfg = folder / CONFIG_FILE
    if not cfg.is_file():
        raise OnboardError(f"no {CONFIG_FILE} in {folder}: run mseo workspace new {slug} first")
    given = {**_load_answers(answers_file), **_parse_sets(sets)}
    try:
        raw = json.loads(cfg.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise OnboardError(f"{cfg}: not valid JSON ({exc})") from None
    s = Session(slug, folder, raw, prompter, out)

    todo = questions(raw)
    out(f"{slug}: {len(todo)} value(s) to fill in {CONFIG_FILE}")
    if todo and not isinstance(prompter, NoPrompter):
        out(BANNER)
    for_questions = {q.path: given.pop(q.path) for q in todo if q.path in given}
    s.updates(given)  # plain field updates first: they need no terminal
    for q in todo:
        handler = s.answer_key if q.key_of else s.answer_value
        if not handler(q, for_questions.get(q.path)):
            break

    missing = [q.path for q in questions(s.raw)]
    summary = {"slug": slug, "missing": missing, "stored": [], "credentials_missing": [], "optional": [], "notes": s.notes}
    if missing:
        out(f"{CONFIG_FILE}: {len(missing)} value(s) still need filling in")
        out("\n".join(f"  {p[2:]}" for p in missing))
        out(f"run again: mseo workspace init {slug}")
        return {"status": "incomplete", **summary}

    ws = workspace_mod.open(slug, keychain=keychain)
    stored, cred_missing = s.credentials(ws, keychain)
    optional = s.optional()
    summary.update(stored=stored, credentials_missing=cred_missing, optional=optional)

    from mseo import doctor

    report = doctor.run(workspace_mod.open(slug, keychain=keychain))
    for c in report.checks:
        out(f"{'ok ' if c['ok'] else 'NO '} {c['check']} ({c['detail']})")
    summary["doctor"] = report.checks
    if cred_missing:
        out(f"not finished: {', '.join(cred_missing)}; run again: mseo workspace init {slug}")
        return {"status": "incomplete", **summary}
    from mseo import seal as seal_mod

    failed = [c["check"] for c in report.checks if not c["ok"] and c["check"] != "seal current"]
    if failed:
        out(f"doctor: {len(failed)} check(s) failed (the NO lines); fix them, then: cd {seal_mod.framework_root()} && "
            f"bin/mseo workspace seal --workspace {slug} --pull-test")
        return {"status": "checks_failed", **summary}
    out(f"next: cd {seal_mod.framework_root()} && bin/mseo workspace seal --workspace {slug} --pull-test")
    return {"status": "ok", **summary}


# ---------------------------------------------------------------- CLI

def cmd_init(args, ctx) -> int:
    interactive = not args.no_prompt and sys.stdin.isatty()
    prompter = TerminalPrompter() if interactive else NoPrompter()
    if ctx.keychain is None:
        ctx.keychain = secrets_mod.Keychain()
    lines: list[str] = []
    want_json = getattr(args, "json", False)

    def out(line: str) -> None:
        lines.append(line)
        if interactive or not want_json:
            print(line, file=sys.stderr, flush=True)  # the operator sees progress as it happens

    try:
        result = run(args.slug, prompter, ctx.keychain, answers_file=Path(args.answers) if args.answers else None,
                     sets=args.set, out=out)
    except KeyboardInterrupt:
        print(f"\nstopped; what you typed is saved. Run again: mseo workspace init {args.slug}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    if want_json:
        print(json.dumps({k: v for k, v in result.items()}, indent=2, default=str))
    from mseo.exits import EXIT_FAIL

    return {"ok": EXIT_OK, "checks_failed": EXIT_FAIL}.get(result["status"], EXIT_COULD_NOT_RUN)


def register_init(wsub, parents) -> None:
    i = wsub.add_parser("init", parents=[parents.json],
                        help="Guided onboarding: fill customer.json, store the credentials, run doctor (no --workspace)")
    i.add_argument("slug")
    i.add_argument("--answers", metavar="FILE", help="JSON object of path: value for the non-secret fields")
    i.add_argument("--set", action="append", default=[], metavar="PATH=VALUE", help="Write one field without prompting")
    i.add_argument("--no-prompt", action="store_true", help="Never ask: fill from --answers and --set, then report what is missing")
    i.set_defaults(func=cmd_init, command_id="workspace init")
