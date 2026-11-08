"""mseo command line: the command table, shared flags and exit codes.

Every command that touches customer data takes --workspace (required, no default).
Exit codes: 0 pass, 1 check failed, 2 already exists, 3 could not run (including usage errors).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, TextIO

from mseo import workspace as workspace_mod
from mseo.exits import EXIT_COULD_NOT_RUN
from mseo.secrets import CredentialError
from mseo.workspace import WorkspaceError


class UsageError(Exception):
    """argparse rejected the command line."""


class Parser(argparse.ArgumentParser):
    """An ArgumentParser whose usage errors exit 3: argparse's own 2 means "already exists" here."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)  # `--approve` must never pass for `--approved`
        super().__init__(*args, **kwargs)

    def error(self, message):  # noqa: D401 - argparse API
        raise UsageError(f"{self.prog}: error: {message}")


class CouldNotRun(Exception):
    """A command cannot start or finish its check. Maps to exit 3."""


@dataclass
class Context:
    """What a command handler gets besides its args: injected keychain, stdin and output."""

    args: Any
    keychain: Any = None
    stdin: TextIO = field(default_factory=lambda: sys.stdin)
    opened: Any = None

    def open(self, slug: str):
        self.opened = workspace_mod.open(slug, keychain=self.keychain)
        return self.opened

    def emit(self, payload: dict) -> None:
        if getattr(self.args, "json", False):
            print(json.dumps({k: v for k, v in payload.items() if k != "messages"}, indent=2, default=str))
        else:
            for line in payload.get("messages", []):
                print(line)


def _parents() -> SimpleNamespace:
    ws = Parser(add_help=False)
    ws.add_argument("--workspace", required=True, metavar="SLUG", help="The one workspace this run may touch")
    js = Parser(add_help=False)
    js.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    dry = Parser(add_help=False)
    dry.add_argument("--dry-run", action="store_true", help="Show what would be written, write nothing")
    return SimpleNamespace(workspace=ws, json=js, dry_run=dry)


# Modules that add subcommands, in help order. Each has register(sub, parents).
COMMAND_MODULES = ["mseo.workspace_cli", "mseo.doctor", "mseo.secrets", "mseo.lint_strings", "mseo.content_cli",
                   "mseo.cms.wordpress", "mseo.publish", "mseo.capture.account", "mseo.pull",
                   "mseo.identity", "mseo.runner.cli", "mseo.watchdog", "mseo.week", "mseo.shadow",
                   "mseo.ads.cli", "mseo.loop", "mseo.site", "mseo.aivis", "mseo.report"]


def build_parser() -> Parser:
    parser = Parser(prog="mseo", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    parents = _parents()
    for name in COMMAND_MODULES:
        __import__(name, fromlist=["register"]).register(sub, parents)
    return parser


def command_ids(parser: Parser | None = None) -> set[str]:
    """Every leaf subcommand, e.g. {'secret set', 'content check'}: what the isolation suite must cover."""
    parser = parser or build_parser()
    out: set[str] = set()

    def walk(p: argparse.ArgumentParser, prefix: str) -> None:
        subs = [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs:
            out.add(prefix.strip())
            return
        for name, child in subs[0].choices.items():
            walk(child, f"{prefix} {name}")

    walk(parser, "")
    return out


def main(argv=None, *, keychain=None, stdin: TextIO | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    ctx = Context(args, keychain=keychain, stdin=stdin or sys.stdin)
    try:
        return args.func(args, ctx)
    except (WorkspaceError, CredentialError, CouldNotRun) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    except Exception as exc:  # noqa: BLE001 - anything unexpected is "could not run" (3), never "check failed" (1)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN


def main_exit() -> None:
    sys.exit(main())
