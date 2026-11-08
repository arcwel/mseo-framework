"""Keychain access, scoped to one workspace.

Items are named mseo.<slug>.<source> with account <slug>, and hold a JSON payload. There are no credential dotfiles.
`security` is always called with a list argv, never through a shell.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mseo.workspace import Workspace

SECURITY = "/usr/bin/security"
SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
TIMEOUT = 30
NOT_FOUND = 44  # security's exit code for "item could not be found"

# What each source's payload must hold.
PAYLOAD_KEYS = {"wordpress": ("site", "user", "app_password")}


class CredentialError(Exception):
    """A credential cannot be used. Maps to exit 3. The message never carries the secret."""


class MissingCredential(CredentialError):
    pass


class SecretExists(Exception):
    """secret set without --replace on an existing item. Maps to exit 2."""


def service_name(slug: str, source: str) -> str:
    if not SOURCE_RE.match(source or ""):
        raise CredentialError(f"bad credential source {source!r}: must match {SOURCE_RE.pattern}")
    return f"mseo.{slug}.{source}"


def quote_interactive(value: str) -> str:
    """A double-quoted argument for a `security -i` command line (backslash escapes " and \\)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Keychain:
    """The macOS login Keychain through /usr/bin/security."""

    def _run(self, argv: list[str], input: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(argv, input=input, capture_output=True, text=True, timeout=TIMEOUT)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CredentialError(f"could not run {argv[0]} ({type(exc).__name__})") from exc

    def exists(self, service: str, account: str) -> bool:
        proc = self._run([SECURITY, "find-generic-password", "-s", service, "-a", account])
        if proc.returncode == NOT_FOUND:
            return False
        if proc.returncode != 0:  # locked Keychain, denied access: not the same as "no item"
            raise CredentialError(f"security could not read {service} (exit {proc.returncode})")
        return True

    def get(self, service: str, account: str) -> dict:
        proc = self._run([SECURITY, "find-generic-password", "-s", service, "-a", account, "-w"])
        if proc.returncode not in (0, NOT_FOUND):
            raise CredentialError(f"security could not read {service} (exit {proc.returncode})")
        if proc.returncode == NOT_FOUND:
            raise MissingCredential(f"no Keychain item {service} (account {account}); "
                                    f"store it with: mseo secret set --workspace {account} --source {service.split('.')[-1]}")
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            raise CredentialError(f"Keychain item {service} is not JSON; re-store it with mseo secret set --replace") from None
        if not isinstance(payload, dict):
            raise CredentialError(f"Keychain item {service} must hold a JSON object")
        return payload

    def set(self, service: str, account: str, payload: dict, replace: bool = False) -> None:
        if self.exists(service, account) and not replace:
            raise SecretExists(f"Keychain item {service} exists; pass --replace to overwrite it")
        # The secret never goes in argv (visible in the process list). `security -i` reads the whole command from
        # stdin; plain `-w` on stdin truncates at 128 characters. `-i` exits 0 even when the command fails, so the
        # item is read back and compared.
        text = json.dumps(payload)
        command = f"add-generic-password -s {service} -a {account} -U -w {quote_interactive(text)}\n"
        proc = self._run([SECURITY, "-i"], input=command)
        if proc.returncode != 0:
            raise CredentialError(f"security could not write {service} (exit {proc.returncode})")
        try:
            stored = self.get(service, account)
        except CredentialError as exc:
            raise CredentialError(f"read-back of {service} failed after writing it: {exc}") from None
        if stored != payload:
            raise CredentialError(f"read-back of {service} does not match what was written")


def workspace_secret(ws: "Workspace", source: str) -> dict:
    if ws.keychain is None:
        raise CredentialError("this workspace was opened without a keychain")
    return ws.keychain.get(service_name(ws.slug, source), ws.slug)


def validate_payload(ws: "Workspace", source: str, payload) -> dict:
    """The payload checked against its source's keys; WordPress's site must equal cms.site."""
    keys = PAYLOAD_KEYS.get(source)
    if keys is None:
        raise CredentialError(f"no credential format for source {source!r} (known: {', '.join(sorted(PAYLOAD_KEYS))})")
    if not isinstance(payload, dict):
        raise CredentialError("the payload must be a JSON object")
    if set(payload) != set(keys):
        raise CredentialError(f"the {source} payload must have exactly the keys {', '.join(keys)}")
    empty = [k for k in keys if not isinstance(payload[k], str) or not payload[k].strip()]
    if empty:
        raise CredentialError(f"empty or non-string value for {', '.join(empty)}")
    if source == "wordpress" and payload["site"].rstrip("/") != ws.config["cms"]["site"].rstrip("/"):
        raise CredentialError(f"the credential's site {payload['site']} differs from cms.site {ws.config['cms']['site']}")
    return payload


# ---------------------------------------------------------------- CLI

def cmd_secret_set(args, ctx) -> int:
    from mseo.exits import EXIT_EXISTS, EXIT_OK

    ws = ctx.open(args.workspace)
    raw = ctx.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        raise CredentialError("stdin is not JSON") from None
    validate_payload(ws, args.source, payload)
    service = service_name(ws.slug, args.source)
    try:
        ws.keychain.set(service, ws.slug, payload, replace=args.replace)
    except SecretExists as exc:
        ctx.emit({"status": "exists", "service": service, "messages": [str(exc)]})
        return EXIT_EXISTS
    ctx.emit({"status": "ok", "service": service, "messages": [f"stored {service}"]})
    return EXIT_OK


def register(sub, parents) -> None:
    p = sub.add_parser("secret", help="Store a workspace credential in the Keychain")
    ssub = p.add_subparsers(dest="secret_command", required=True, parser_class=type(p))
    s = ssub.add_parser("set", parents=[parents.workspace, parents.json],
                        help="Read a JSON payload from stdin and store it as mseo.<slug>.<source>")
    s.add_argument("--source", required=True, help="Credential source, e.g. wordpress")
    s.add_argument("--replace", action="store_true", help="Overwrite an existing item")
    s.set_defaults(func=cmd_secret_set, command_id="secret set")
