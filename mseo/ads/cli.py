"""`mseo ads ...` and `mseo actions ...`: the Ads Scripts data layer, the tracking gate, the audit and the numbered
action batches. Every command reads the week's sheet tables from the run records; none talks to
Google."""

from __future__ import annotations

from mseo.ads import audit as audit_mod
from mseo.ads import batch as batch_mod
from mseo.ads import script as script_mod
from mseo.ads import tracking as tracking_mod
from mseo.ads.tables import NoTables
from mseo.cli import CouldNotRun
from mseo.exits import EXIT_EXISTS, EXIT_FAIL, EXIT_OK


def cmd_script_install(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        out = script_mod.render(ws)
    except script_mod.ScriptError as exc:
        raise CouldNotRun(str(exc)) from None
    steps = [f"{i}. {s}" for i, s in enumerate(out["steps"], 1)]
    ctx.emit({**out, "messages": [out["script"], "", f"Install steps (Sheet {out['sheet_id']}):", *steps]})
    return EXIT_OK


def cmd_tracking(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        out = tracking_mod.gate(ws, args.week)
    except NoTables as exc:
        raise CouldNotRun(str(exc)) from None
    ctx.emit({**out, "messages": tracking_mod.lines(out)})
    return EXIT_OK if out["status"] == "pass" else EXIT_FAIL


def cmd_audit(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        out = audit_mod.audit(ws, args.week)
    except NoTables as exc:
        raise CouldNotRun(str(exc)) from None
    ctx.emit({**out, "messages": audit_mod.markdown(out)})
    return EXIT_FAIL if out["status"] == "fail" else EXIT_OK


def cmd_propose(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        batch = batch_mod.propose(ws, args.week, ignore_tracking=args.ignore_tracking)
    except (NoTables, batch_mod.BatchError) as exc:
        raise CouldNotRun(str(exc)) from None
    head = f"batch {batch['batch_id']}: {len(batch['items'])} item(s) for {args.week} -> data/batches/{batch['batch_id']}.json"
    if batch.get("tracking_overridden"):
        head += " (tracking gate FAILED; overridden with --ignore-tracking)"
    ctx.emit({**batch, "messages": [head, *batch_mod.item_lines(batch),
                                    f"approve with: mseo actions approve --workspace {ws.slug} --batch {batch['batch_id']} "
                                    "--items N,M --by NAME"]})
    return EXIT_OK


def cmd_approve(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        items = batch_mod.parse_items(args.items)
        rec = batch_mod.approve(ws, args.batch, items, by=args.by, require_tty=True, stdin=ctx.stdin)
    except batch_mod.ApprovalExists as exc:
        ctx.emit({"status": "exists", "messages": [f"exists: {exc}"]})
        return EXIT_EXISTS
    except (ValueError, batch_mod.BatchError) as exc:
        raise CouldNotRun(str(exc)) from None
    path = batch_mod.approval_path(ws, args.batch)
    shown = batch_mod.approve_lines(batch_mod.load_batch(ws, args.batch), items)
    ctx.emit({**rec, "path": str(path),
              "messages": [f"approved items {', '.join(str(i['n']) for i in rec['items'])} of {args.batch} by {rec['approved_by']}",
                           "the script will apply exactly these rows (signed in the record):", *shown, f"record: {path}"]})
    return EXIT_OK


def cmd_apply(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        out = batch_mod.apply(ws, args.batch, dry_run=args.dry_run, confirm_pasted=args.confirm_pasted)
    except batch_mod.BatchError as exc:
        raise CouldNotRun(str(exc)) from None
    ctx.emit({**out, "messages": batch_mod.apply_lines(out)})
    return EXIT_OK


def cmd_status(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        out = batch_mod.status(ws, args.week)
    except (NoTables, batch_mod.BatchError) as exc:
        raise CouldNotRun(str(exc)) from None
    ctx.emit({**out, "messages": batch_mod.status_lines(out)})
    return EXIT_FAIL if out["failed"] else EXIT_OK


def register(sub, parents) -> None:
    ads = sub.add_parser("ads", help="Google Ads: the Scripts Sheet data layer, the tracking gate and the audit")
    asub = ads.add_subparsers(dest="ads_command", required=True, parser_class=type(ads))
    script = asub.add_parser("script", help="The Ads Script that writes the report Sheet")
    ssub = script.add_subparsers(dest="script_command", required=True, parser_class=type(ads))
    inst = ssub.add_parser("install", parents=[parents.workspace, parents.json, parents.dry_run],
                           help="Print the script with the workspace's Sheet ID and the install steps (no network)")
    inst.set_defaults(func=cmd_script_install, command_id="ads script install")
    tr = asub.add_parser("tracking", parents=[parents.workspace, parents.json],
                         help="The conversion-tracking gate over the week's conversion_actions table")
    tr.add_argument("--week", required=True, help="ISO week, e.g. 2026-W44")
    tr.set_defaults(func=cmd_tracking, command_id="ads tracking")
    au = asub.add_parser("audit", parents=[parents.workspace, parents.json],
                         help="Data-driven checks over the week's sheet tables; writes data/weeks/<W>.ads-audit.json")
    au.add_argument("--week", required=True, help="ISO week, e.g. 2026-W44")
    au.set_defaults(func=cmd_audit, command_id="ads audit")

    actions = sub.add_parser("actions", help="Numbered action batches: propose, approve by number, apply")
    xsub = actions.add_subparsers(dest="actions_command", required=True, parser_class=type(actions))
    pr = xsub.add_parser("propose", parents=[parents.workspace, parents.json],
                         help="Write data/batches/<batch_id>.json from the week's sheet tables")
    pr.add_argument("--week", required=True, help="ISO week, e.g. 2026-W44")
    pr.add_argument("--ignore-tracking", action="store_true",
                    help="Propose although the tracking gate fails; recorded in the batch as tracking_overridden")
    pr.set_defaults(func=cmd_propose, command_id="actions propose")
    ap = xsub.add_parser("approve", parents=[parents.workspace, parents.json],
                         help="Write the approval record outside the workspace for the numbered items")
    ap.add_argument("--batch", required=True, help="Batch id, e.g. 20261109T150000Z-fx-1a2b")
    ap.add_argument("--items", required=True, help="Item numbers, e.g. 1,3-5")
    ap.add_argument("--by", required=True, help="Who approves (recorded as approved_by)")
    ap.set_defaults(func=cmd_approve, command_id="actions approve")
    al = xsub.add_parser("apply", parents=[parents.workspace, parents.json, parents.dry_run],
                         help="Verify the approval and write the approved_changes CSV for the Ads Script")
    al.add_argument("--batch", required=True, help="Batch id")
    al.add_argument("--confirm-pasted", action="store_true",
                    help="The CSV rows are in the Sheet's approved_changes tab: record pasted_at on the script items")
    al.set_defaults(func=cmd_apply, command_id="actions apply")
    st = xsub.add_parser("status", parents=[parents.workspace, parents.json],
                         help="Read the week's applied_log tab and mark pasted items applied (or failed)")
    st.add_argument("--week", required=True, help="ISO week whose sheet run holds the applied_log, e.g. 2026-W45")
    st.set_defaults(func=cmd_status, command_id="actions status")
