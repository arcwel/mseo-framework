"""capture/account: `mseo capture verify RUN_DIR`, compare the account a capture saw with the workspace's account.

A yes/no flag (`expected_id_seen`) would accept a capture made with the wrong expected ID, so the ID the page showed (`account_id_seen` in the capture's metadata)
is compared, digits-normalized, with accounts.<source>.id:

    equal                          -> status ok, rows kept
    present and different          -> status wrong_account, account_id_seen recorded, rows discarded
    absent (only expected_id_seen) -> status unverified_account, rows discarded

A capture file is a "#<name>.capture/<n> {json meta}" line followed by tab-separated rows. RUN_DIR (inside the
workspace) holds the captures; every one whose meta.source is --source is checked, and the run record is written to
RUN_DIR/<source>.run.json. Exit 0 ok, 1 wrong or unverified account, 3 no readable capture for that source.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from mseo.fsutil import sha256_file, write_json_atomic
from mseo.run import record as run_record

HEADER_RE = re.compile(r"^#[\w.-]+\.capture/\d+ (\{.*\})\s*$")
OK, WRONG, UNVERIFIED = "ok", "wrong_account", "unverified_account"
SEVERITY = {OK: 0, UNVERIFIED: 1, WRONG: 2}


class CaptureError(Exception):
    """The file is not a capture (no header line, bad JSON)."""


def digits(value) -> str:
    return re.sub(r"\D", "", str(value) if value is not None else "")


NUMERIC_ID_RE = re.compile(r"^[\d\s-]+$")


def normalize(value) -> str:
    """A numeric-style ID (digits, dashes, spaces) compares by its digits; any other ID (sc-domain:..., a property
    URL) compares as lower-cased text, so two text IDs that share digits never match. A non-ASCII ID matches nothing,
    not even itself in another form: a look-alike (the Kelvin sign for k) must never pass for the real property."""
    text = (str(value) if value is not None else "").strip()
    if not text.isascii():
        return "\x00refused-non-ascii:" + text
    return digits(text) if NUMERIC_ID_RE.match(text) else text.lower()


def verify(ws, source: str, meta: dict, rows: list | None = None) -> dict:
    """The run record for one capture: status, the account expected and the account seen; rows only when ok."""
    expected = ws.account(source)
    seen = meta.get("account_id_seen")
    record = {"workspace": ws.slug, "source": source, "account_id_expected": expected, "account_id_seen": seen}
    if seen in (None, ""):
        return {**record, "status": UNVERIFIED, "rows": None,
                "problems": ["the capture does not record which account the page showed (account_id_seen); "
                             "nothing was saved"]}
    if normalize(seen) != normalize(expected):
        return {**record, "status": WRONG, "rows": None,
                "problems": [f"the page showed account {seen}, but {ws.slug} expects {expected}; nothing was saved"]}
    return {**record, "status": OK, "rows": rows if rows is not None else [], "problems": []}


def _text(value) -> str | None:
    return None if value in (None, "") else str(value)


def read_capture(path: Path) -> tuple[dict, list]:
    text = path.read_text(encoding="utf-8")
    first, _, rest = text.partition("\n")
    m = HEADER_RE.match(first)
    if not m:
        raise CaptureError(f"{path.name}: first line is not a #<name>.capture/<n> header")
    try:
        meta = json.loads(m.group(1))
    except ValueError as exc:
        raise CaptureError(f"{path.name}: capture header is not valid JSON ({exc})") from exc
    return meta, [line.split("\t") for line in rest.split("\n") if line.strip()]


def verify_dir(ws, run_dir: Path, source: str) -> tuple[int, dict]:
    captures, problems = [], []
    for path in sorted(p for p in run_dir.iterdir() if p.is_file() and not p.name.endswith(".run.json")):
        try:
            ws.contains(path)  # a symlink out of the workspace is never read
        except Exception as exc:  # noqa: BLE001 - OutsideWorkspace
            problems.append(f"{path.name}: {exc}; skipped")
            continue
        try:
            meta, rows = read_capture(path)
        except (CaptureError, OSError, UnicodeDecodeError) as exc:
            problems.append(str(exc))
            continue
        if meta.get("source") != source:
            continue
        captures.append({"file": path.name, "table": meta.get("table"), **verify(ws, source, meta, rows)})
    if not captures:
        return EXIT_COULD_NOT_RUN, {"status": "error", "source": source, "problems": problems or
                                    [f"no {source} capture in {run_dir.name}"]}
    worst = max(captures, key=lambda c: SEVERITY[c["status"]])
    status = worst["status"]
    now = dt.datetime.now(dt.timezone.utc)
    files = [{"path": c["file"], "sha256": sha256_file(run_dir / c["file"]), "rows": len(c["rows"] or []),
              "truncated": False} for c in captures] if status == OK else []
    seen = worst["account_id_seen"]
    record = run_record.build(
        run_id=run_record.new_run_id(source, now=now), workspace=ws.slug, source=source, adapter="capture",
        identity=None, period=None, account_id_expected=_text(ws.account(source)), account_id_seen=_text(seen),
        seen_via="page:capture_meta", status=status, complete={"value": False, "reason": "capture check only"},
        files=files, started=now, finished=now,
        error=None if status == OK else "; ".join(p for c in captures for p in c["problems"]),
        captures=[{k: v for k, v in c.items() if k not in ("workspace", "source")} for c in captures])
    if worst["status"] != OK:  # one wrong or unverified table discards them all
        for c in record["captures"]:
            c["rows"] = None
    return (EXIT_OK if worst["status"] == OK else EXIT_FAIL), record


def write_record(path: Path, record: dict) -> None:
    write_json_atomic(path, record, indent=1)


def cmd_verify(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    run_dir = ws.contains(args.run_dir)
    if not run_dir.is_dir():
        print(f"error: {run_dir} is not a folder", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    if args.source not in ws.enabled_sources():
        print(f"error: workspace {ws.slug} has no enabled account for {args.source!r}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    code, record = verify_dir(ws, run_dir, args.source)
    out = run_dir / f"{args.source}.run.json"
    if code != EXIT_COULD_NOT_RUN:
        write_record(out, record)
    msgs = [f"{record['status']}: {args.source} expected {record.get('account_id_expected')}, "
            f"saw {record.get('account_id_seen')}"] + [f"  {p}" for c in record.get("captures", []) for p in c["problems"]]
    msgs += [f"  {p}" for p in record.get("problems", [])]
    if code != EXIT_COULD_NOT_RUN:
        msgs.append(f"  -> {out}")
    ctx.emit({**record, "messages": msgs})
    return code


def register(sub, parents) -> None:
    p = sub.add_parser("capture", help="Checks on page captures")
    csub = p.add_subparsers(dest="capture_command", required=True, parser_class=type(p))
    v = csub.add_parser("verify", parents=[parents.workspace, parents.json],
                        help="Compare the account each capture saw with the workspace's account; write <source>.run.json")
    v.add_argument("run_dir", help="Folder of captures, inside the workspace")
    v.add_argument("--source", required=True, help="Account source, e.g. google_ads")
    v.set_defaults(func=cmd_verify, command_id="capture verify")
