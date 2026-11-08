"""mseo workspace new | seal."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

from mseo import seal as seal_mod
from mseo import workspace as workspace_mod
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_EXISTS, EXIT_OK

TEMPLATE = seal_mod.framework_root() / "workspace-template"


def _live_test(args):
    if getattr(args, "skip_live_test", False):
        return None
    from mseo import live_test

    return live_test.run


def _pull_test(args):
    """The pull test needs Chrome connected in the workspace's profile, so it runs only when asked (--pull-test)."""
    if not getattr(args, "pull_test", False):
        return None
    from mseo import live_test

    return live_test.run_pull


def cmd_new(args, ctx) -> int:
    """Create <root>/<slug> from the template as a fresh git repo, then re-seal every other workspace."""
    slug = args.slug
    if not workspace_mod.SLUG_RE.match(slug):
        raise workspace_mod.ConfigInvalid(f"bad workspace slug {slug!r}: must match {workspace_mod.SLUG_RE.pattern}")
    workspace_mod.cwd_guard(slug)
    root = workspace_mod.workspaces_root()
    target = root / slug
    if target.exists() or target.is_symlink():
        ctx.emit({"status": "exists", "path": str(target), "messages": [f"{target} already exists"]})
        return EXIT_EXISTS
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE, target)
    cfg = target / workspace_mod.CONFIG_FILE
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["slug"] = slug
    cfg.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    proc = subprocess.run(["git", "init", "-q", "-b", "main", str(target)], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        print(f"error: git init failed: {proc.stderr.strip()}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    resealed = seal_mod.reseal_all(exclude=slug, keychain=ctx.keychain, live_test=_live_test(args))
    msgs = [f"created {target} (fill customer.json, then: mseo workspace seal --workspace {slug})"]
    msgs += [f"  re-sealed {s}: exit {r['code']}" + (f" ({r['error']})" if r.get("error") else "") for s, r in resealed.items()]
    # code 1 (a sibling not configured yet, or skipped) is tolerated; code 3 means a sealed sibling is now unsealed
    failed = sorted(s for s, r in resealed.items() if r.get("code") == EXIT_COULD_NOT_RUN)
    if failed:
        msgs.append(f"  could not re-seal {', '.join(failed)}: run mseo workspace seal --workspace <slug> for each")
    ctx.emit({"status": "failed" if failed else "ok", "path": str(target), "resealed": resealed, "messages": msgs})
    return EXIT_COULD_NOT_RUN if failed else EXIT_OK


def cmd_seal(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    result = seal_mod.seal(ws, live_test=_live_test(args), pull_test=_pull_test(args))
    ctx.emit({"status": "ok" if result.code == EXIT_OK else "failed", **result.record, "messages": result.messages})
    return result.code


def register(sub, parents) -> None:
    p = sub.add_parser("workspace", help="Create and seal customer workspaces")
    wsub = p.add_subparsers(dest="workspace_command", required=True, parser_class=type(p))
    n = wsub.add_parser("new", parents=[parents.json], help="Create a workspace repo from the template (no --workspace)")
    n.add_argument("slug")
    n.add_argument("--skip-live-test", action="store_true", help="Re-seal the others without the live agent test (seal stays not current)")
    n.set_defaults(func=cmd_new, command_id="workspace new")
    s = wsub.add_parser("seal", parents=[parents.workspace, parents.json],
                        help="Write the agent settings, run the checks and the live agent test, record the seal")
    s.add_argument("--skip-live-test", action="store_true", help="Record the live test as skipped (doctor will fail)")
    s.add_argument("--pull-test", action="store_true",
                   help="Also run the pull test with the pull settings (needs Chrome connected in this workspace's profile)")
    s.set_defaults(func=cmd_seal, command_id="workspace seal")
