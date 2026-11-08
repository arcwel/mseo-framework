"""The `mseo content ...` command group: one subcommand per content module."""

from __future__ import annotations

CONTENT_MODULES = ["mseo.content.check", "mseo.content.lint_copy", "mseo.content.wording", "mseo.content.dupes",
                   "mseo.content.log", "mseo.content.recipes", "mseo.content.new", "mseo.content.topics"]


def register(sub, parents) -> None:
    p = sub.add_parser("content", help="Pre-publish checks and the content log for articles and Business Profile posts")
    csub = p.add_subparsers(dest="content_command", required=True, parser_class=type(p))
    for name in CONTENT_MODULES:
        __import__(name, fromlist=["register"]).register(csub, parents)
