"""`mseo actions propose | approve | apply | status`: numbered action batches.

propose  runs the tracking gate first (a fail refuses with exit 3; --ignore-tracking overrides and is recorded in
         the batch as tracking_overridden), then reads the week's sheet tables and writes
         data/batches/<batch_id>.json (mseo.batch/1): every item carries its evidence rows, the before state, the
         after state and item_hash = sha256 of the canonical JSON of {type, target, before, after}. A capped campaign
         whose budget is shared (budgets.shared) gets no budget item: raising it would raise every campaign on it.
approve  writes the approval record OUTSIDE the workspace (MSEO_APPROVALS_DIR, default ~/.config/mseo/approvals/
         <slug>/<batch_id>.json, mseo.approval/1) with the item hashes and the batch's content hash. A sealed agent
         cannot write there; that is the point: a directory inside the workspaces root is refused whatever the
         variable says, and the command needs a terminal on stdin (recorded as `tty`). It refuses when an item's hash
         no longer matches its content.
apply    needs a current seal, verifies the approval against the batch as it is now, and writes the
         approved_changes CSV the Ads Script reads (negatives, keyword and ad-group pauses, budgets). Nothing here
         talks to Google: the operator pastes the CSV into the `approved_changes` tab and confirms with
         --confirm-pasted, which records pasted_at and leaves the items approved. A CSV cell that starts with
         = + - or @ is written with a leading single quote so the Sheet never evaluates a pasted search term as a
         formula (the script strips the quote). Other types stay approved for the browser agent (D3).
status   reads the applied_log table from the week's sheet run (what the script did with the pasted rows) and marks
         each item applied, with applied_at from its first `ok:` row, or failed with the script's reason.

The batch content hash covers everything but each item's `status`, `note`, `pasted_at`, `applied_at` and
`measured`, so approving, confirming the paste, status and loop measure never invalidate the record while any other
edit to the file does."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sys
from pathlib import Path

from mseo import seal, workspace
from mseo.ads import TEMPLATES, tables
from mseo.ads import audit as audit_mod
from mseo.ads import tracking as tracking_mod
from mseo.fsutil import write_json_atomic
from mseo.run import record as record_mod

BATCH_SCHEMA, APPROVAL_SCHEMA = "mseo.batch/1", "mseo.approval/1"
BATCH_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-z0-9-]+-[0-9a-f]{4}$")
SCRIPT_TYPES = ("negative_keyword", "pause_keyword", "pause_ad_group", "budget")  # applyOne_ in the Ads Script
CSV_COLUMNS = ["batch_id", "n", "item_hash", "type", "campaign", "ad_group", "text", "match_type", "value"]
SHEET_TAB = "approved_changes"
ALL_CAMPAIGNS = "*"
DEFERRED_NOTE = "browser agent, D3"
SEEDS_NOTE = "existing negatives cannot be read from the sheet, so this seed may already be present; the script logs a duplicate"
NEGATIVES = TEMPLATES / "negatives"
MATCH_WRAP = {"exact": "[{}]", "phrase": '"{}"', "broad": "{}"}
ENABLED = "ENABLED"
HASHED = ("type", "target", "before", "after")
UNHASHED_ITEM_KEYS = ("status", "note", "pasted_at", "applied_at", "measured")
FORMULA_LEADS = ("=", "+", "-", "@")  # a pasted cell starting with one of these is a formula to Sheets
TEXT_PREFIX = "'"
OK_RESULT, FAILED_RESULT = "ok:", "failed:"  # how the script's applied_log results start


class BatchError(Exception):
    """The batch or the approval cannot be used as asked. Maps to exit 3."""


class ApprovalExists(BatchError):
    """An approval record for this batch already exists. Maps to exit 2."""


# ---------------------------------------------------------------- hashes and paths

def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def item_hash(item: dict) -> str:
    return hashlib.sha256(_canonical({k: item.get(k) for k in HASHED})).hexdigest()


def batch_content_sha256(batch: dict) -> str:
    """The batch without each item's status and note: stable across approve and apply, changed by anything else."""
    body = copy.deepcopy(batch)
    body["items"] = [{k: v for k, v in item.items() if k not in UNHASHED_ITEM_KEYS} for item in body["items"]]
    return hashlib.sha256(_canonical(body)).hexdigest()


def approvals_dir() -> Path:
    """Where approval records live: never inside the workspaces root, whatever MSEO_APPROVALS_DIR says (a sealed
    agent can set the variable in its own command; it cannot write outside the root)."""
    env = os.environ.get("MSEO_APPROVALS_DIR")
    folder = Path(env).expanduser() if env else Path.home() / ".config" / "mseo" / "approvals"
    root = workspace.workspaces_root()
    resolved = folder.resolve()
    if resolved == root or root in resolved.parents:
        raise BatchError(f"MSEO_APPROVALS_DIR {folder} is inside the workspaces root {root}; approvals live outside it")
    return folder


def approval_path(ws, batch_id: str) -> Path:
    return approvals_dir() / ws.slug / f"{check_id(batch_id)}.json"


def check_id(batch_id: str) -> str:
    if not BATCH_ID.match(batch_id or ""):
        raise BatchError(f"{batch_id!r} is not a batch id (<UTC stamp>-<slug>-<4 hex>)")
    return batch_id


def batch_path(ws, batch_id: str) -> Path:
    return ws.path("data", "batches", f"{check_id(batch_id)}.json")


def load_batch(ws, batch_id: str) -> dict:
    path = batch_path(ws, batch_id)
    try:
        batch = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise BatchError(f"no batch {batch_id} in {ws.slug} ({path})") from None
    except ValueError as exc:
        raise BatchError(f"batch {batch_id} is not JSON ({exc})") from None
    record_mod.check_schema("batch.v1.json", batch)
    if batch["workspace"] != ws.slug:
        raise BatchError(f"batch {batch_id} belongs to {batch['workspace']}, not {ws.slug}")
    return batch


def save_batch(ws, batch: dict) -> None:
    record_mod.check_schema("batch.v1.json", batch)
    write_json_atomic(batch_path(ws, batch["batch_id"]), batch)


MAX_ITEMS = 1000  # a batch never holds more; a huge --items range is a mistake, not a request


def parse_items(text: str) -> list[int]:
    """'1,3-5,9' -> [1, 3, 4, 5, 9]."""
    out: list[int] = []
    for part in (p.strip() for p in (text or "").split(",")):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            lo, hi = int(a), int(b)
            if hi < lo:
                raise ValueError(f"{part}: the range ends before it starts")
            if hi - lo >= MAX_ITEMS:
                raise ValueError(f"{part}: more than {MAX_ITEMS} items in one range")
            out += list(range(lo, hi + 1))
        else:
            out.append(int(part))
    if not out:
        raise ValueError("no item numbers given")
    return sorted(set(out))


# ---------------------------------------------------------------- seed negatives

def seed_list(name: str, _seen: tuple[str, ...] = ()) -> list[dict]:
    """The negatives of templates/ads/negatives/<name>.json, with the lists it extends first."""
    if name in _seen:
        raise BatchError(f"negative seed lists extend each other in a loop: {' -> '.join((*_seen, name))}")
    path = NEGATIVES / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise BatchError(f"no negative seed list {name!r} under {NEGATIVES}") from None
    except ValueError as exc:
        raise BatchError(f"seed list {name} is not JSON ({exc})") from None
    base = seed_list(data["extends"], (*_seen, name)) if data.get("extends") else []
    own = [{"text": n["text"], "match": n.get("match", "broad"), "why": n.get("why", ""), "list": name}
           for n in data.get("negatives", [])]
    seen = {(n["text"].lower(), n["match"]) for n in base}
    return base + [n for n in own if (n["text"].lower(), n["match"]) not in seen]


def _negative(text: str, match: str) -> str:
    return MATCH_WRAP.get(match, "{}").format(text)


# ---------------------------------------------------------------- propose

def _item(n: int, type_: str, target: dict, before, after, evidence: list[dict], expected: str, *, note: str | None = None,
          reversible: bool = True) -> dict:
    item = {"n": n, "type": type_, "target": target, "before": before, "after": after, "evidence": evidence,
            "expected_metric": expected, "reversible": reversible, "status": "proposed"}
    if note:
        item["note"] = note
    item["item_hash"] = item_hash(item)
    return item


def build_items(ws, data: dict) -> list[dict]:
    t = data["tables"]
    cfg = ws.config["ads"]
    threshold = float(cfg["waste_threshold_usd"])
    min_clicks = int(cfg["min_clicks_before_pause"])
    items: list[dict] = []

    def add(*args, **kw):
        items.append(_item(len(items) + 1, *args, **kw))

    wasted = audit_mod.wasted_terms(t["search_terms"], threshold)
    for r in sorted(wasted, key=lambda r: -float(r["cost"] or 0)):
        add("negative_keyword", {"campaign": r["campaign"], "text": r["search_term"], "match_type": "exact"},
            {"cost": r["cost"], "clicks": r["clicks"], "conversions": r["conversions"], "match_type": r["match_type"]},
            {"negative": _negative(r["search_term"], "exact")}, [r],
            f"cost on this term to 0 (was {r['cost']} over the search-term window)")
    for k in t["keywords"]:
        if (k["status"] or "").upper() == ENABLED and float(k["clicks"] or 0) >= min_clicks and float(k["conversions"] or 0) == 0:
            add("pause_keyword", {"campaign": k["campaign"], "ad_group": k["ad_group"], "text": k["keyword"],
                                  "match_type": (k["match_type"] or "").lower()},
                {"status": k["status"], "clicks": k["clicks"], "cost": k["cost"], "conversions": k["conversions"]},
                {"status": "PAUSED"}, [k], f"cost on this keyword to 0 (was {k['cost']} for {int(float(k['clicks']))} clicks)")
    shared = {b["campaign"] for b in t["budgets"] if str(b.get("shared") or "").strip().lower() in tracking_mod.TRUE_WORDS}
    for c in audit_mod.capped_campaigns(t["campaigns"]):
        if c["campaign"] in shared:
            continue  # a shared budget raises every campaign on it; not proposed (audit L8)
        if float(c["conversions"] or 0) > 0 and c["budget"] is not None:
            before, after = float(c["budget"]), round(float(c["budget"]) * audit_mod.BUDGET_RAISE, 2)
            add("budget", {"campaign": c["campaign"]},
                {"budget": before, "lost_is_budget": c["lost_is_budget"], "conversions": c["conversions"]},
                {"budget": after}, [c], f"lost impression share (budget) below {audit_mod.LOST_IS_BUDGET:.0%}; conversions up")
    for h in audit_mod.dead_hours(t["ad_schedule"], threshold, min_clicks):
        add("ad_schedule", {"hour": h["hour"], "campaigns": h["campaigns"]},
            {"cost": h["cost"], "clicks": h["clicks"], "conversions": 0}, {"exclude_hour": h["hour"]}, [h],
            f"cost in hour {h['hour']} to 0 (was {h['cost']} over the week)")
    enabled = sorted(c["campaign"] for c in t["campaigns"] if (c["status"] or "").upper() == ENABLED)
    proposed = {i["target"]["text"].lower() for i in items if i["type"] == "negative_keyword"}
    for s in seed_list(cfg["negative_seed_list"]):
        if s["text"].lower() in proposed or not enabled:
            continue
        add("negative_keyword", {"campaign": ALL_CAMPAIGNS, "campaigns": enabled, "text": s["text"], "match_type": s["match"]},
            {"present": "unknown"}, {"negative": _negative(s["text"], s["match"])},
            [{"list": s["list"], "why": s["why"]}], "fewer off-intent clicks", note=SEEDS_NOTE)
    return items


def new_batch_id(slug: str, now: dt.datetime | None = None, rand: str | None = None) -> str:
    stamp = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{slug}-{rand or secrets.token_hex(2)}"


def propose(ws, week: str, *, now: dt.datetime | None = None, ignore_tracking: bool = False) -> dict:
    """The tracking gate first: optimizing on bad conversion data is worse than not optimizing."""
    gate = tracking_mod.gate(ws, week, now=now)
    if gate["status"] == "fail" and not ignore_tracking:
        failed = ", ".join(c["name"] for c in gate["checks"] if c["status"] == "fail")
        raise BatchError(f"the tracking gate fails for {week} ({failed}); fix the conversion setup (`mseo ads tracking`) or "
                         "run propose with --ignore-tracking to record the override in the batch")
    data = tables.load_week(ws, week)
    now = now or dt.datetime.now(dt.timezone.utc)
    batch = {"schema_version": BATCH_SCHEMA, "batch_id": new_batch_id(ws.slug, now), "workspace": ws.slug, "week": week,
             "created": record_mod.utc(now), "source_run_ids": [data["run_id"]], "items": build_items(ws, data)}
    if gate["status"] == "fail":
        batch["tracking_overridden"] = True
    save_batch(ws, batch)
    return batch


def _target_text(item: dict) -> str:
    t = item["target"]
    parts = [t.get("campaign"), t.get("ad_group"), t.get("text"), f"hour {t['hour']}" if "hour" in t else None]
    return " / ".join(str(p) for p in parts if p)


def item_lines(batch: dict) -> list[str]:
    out = []
    for i in batch["items"]:
        out.append(f"{i['n']}. [{i['status']}] {i['type']} {_target_text(i)}: {json.dumps(i['before'], default=str)} -> "
                   f"{json.dumps(i['after'], default=str)}" + (f" ({i['note']})" if i.get("note") else ""))
    return out


# ---------------------------------------------------------------- approve

def approve(ws, batch_id: str, items: list[int], *, by: str, now: dt.datetime | None = None, require_tty: bool = True,
            stdin=None) -> dict:
    """require_tty: the approval is a person's act, so stdin must be a terminal (the CLI default); tests pass False."""
    if not (by or "").strip():
        raise BatchError("--by needs a name")
    tty = bool((stdin or sys.stdin).isatty())
    if require_tty and not tty:
        raise BatchError("actions approve needs a terminal on stdin: an approval is typed by a person, not piped or scripted")
    batch = load_batch(ws, batch_id)
    path = approval_path(ws, batch_id)
    if path.exists():
        raise ApprovalExists(f"an approval record for {batch_id} exists ({path}); approvals are written once")
    by_n = {i["n"]: i for i in batch["items"]}
    approved = []
    for n in items:
        item = by_n.get(n)
        if item is None:
            raise BatchError(f"no item {n} in batch {batch_id} (items 1..{len(batch['items'])})")
        if item_hash(item) != item["item_hash"]:
            raise BatchError(f"item {n} hash differs from its content; the batch file was edited, nothing approved")
        approved.append({"n": n, "item_hash": item["item_hash"]})
    chosen = [by_n[n] for n in items]
    rec = {"schema_version": APPROVAL_SCHEMA, "batch_id": batch_id, "workspace": ws.slug, "approved_by": by.strip(),
           "approved_at": record_mod.utc(now or dt.datetime.now(dt.timezone.utc)), "items": approved,
           "rejected": [], "batch_sha256": batch_content_sha256(batch), "tty": tty,
           "rows_sha256": rows_sha256(chosen), "rows": script_rows(chosen)}
    record_mod.check_schema("approval.v1.json", rec)
    write_json_atomic(path, rec)
    for n in items:
        by_n[n]["status"] = "approved"
    save_batch(ws, batch)
    return rec


def script_rows(items: list[dict]) -> list[dict]:
    """The exact approved_changes rows the Ads Script will receive for these items: what the approver signs."""
    return [r for item in items if item["type"] in SCRIPT_TYPES for r in _rows(item)]


def rows_sha256(items: list[dict]) -> str:
    return hashlib.sha256(json.dumps(script_rows(items), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def approve_lines(batch: dict, items: list[int]) -> list[str]:
    """What the approver is shown before saying yes: the rows as the script will apply them, one line each."""
    by_n = {i["n"]: i for i in batch["items"]}
    out = []
    for n in items:
        item = by_n.get(n)
        if item is None:
            continue
        rows = _rows(item) if item["type"] in SCRIPT_TYPES else []
        if not rows:
            out.append(f"  {n}. {item['type']}: not applied by the script (browser step)")
        for r in rows:
            out.append(f"  {n}. {r['type']}: campaign {r['campaign']!r}" + (f", ad group {r['ad_group']!r}" if r.get("ad_group") else "")
                       + (f", {r['match_type']} {r['text']!r}" if r.get("text") else "") + (f", value {r['value']}" if r.get("value") not in (None, "") else ""))
    return out


def log_campaign(target) -> str:
    """The campaign named by an applied_log target cell: JSON ({campaign, ad_group, text}) from the current script,
    or the older 'campaign / ad group / text' text."""
    text = str(target or "")
    if text.startswith("{"):
        try:
            data = json.loads(text)
            return str(data.get("campaign", "")) if isinstance(data, dict) else text
        except ValueError:
            pass
    return text.split(" / ")[0]


def load_approval(ws, batch_id: str) -> dict:
    path = approval_path(ws, batch_id)
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise BatchError(f"no approval record for {batch_id} ({path}); run `mseo actions approve` first") from None
    except ValueError as exc:
        raise BatchError(f"approval record {path} is not JSON ({exc})") from None
    record_mod.check_schema("approval.v1.json", rec)
    if rec["batch_id"] != batch_id or rec["workspace"] != ws.slug:
        raise BatchError(f"approval record {path} is for {rec['workspace']}/{rec['batch_id']}, not {ws.slug}/{batch_id}")
    return rec


# ---------------------------------------------------------------- apply

def _rows(item: dict) -> list[dict]:
    """The approved_changes rows for one script-applied item (a seed negative expands to every enabled campaign)."""
    t = item["target"]
    base = {"n": item["n"], "item_hash": item["item_hash"], "type": item["type"], "campaign": t.get("campaign", ""),
            "ad_group": t.get("ad_group", ""), "text": t.get("text", ""), "match_type": t.get("match_type", ""), "value": ""}
    if item["type"] == "budget":
        base["value"] = str(item["after"]["budget"])
    if item["type"] == "negative_keyword" and t.get("campaign") == ALL_CAMPAIGNS:
        return [{**base, "campaign": c} for c in t.get("campaigns", [])]
    return [base]


def guard_cell(value) -> str:
    """A CSV cell for the Sheet: text starting with = + - or @ gets a leading single quote (Sheets evaluates pasted
    text; the script strips the quote, and Sheets itself usually eats it as a text marker)."""
    text = "" if value is None else str(value)
    return TEXT_PREFIX + text if text.startswith(FORMULA_LEADS) else text


def apply(ws, batch_id: str, *, dry_run: bool = False, confirm_pasted: bool = False, now: dt.datetime | None = None) -> dict:
    if not dry_run:
        seal.require_current(ws)
    batch = load_batch(ws, batch_id)
    approval = load_approval(ws, batch_id)
    if batch_content_sha256(batch) != approval["batch_sha256"]:
        raise BatchError(f"batch {batch_id} changed after its approval; nothing applied")
    by_n = {i["n"]: i for i in batch["items"]}
    chosen = []
    for a in approval["items"]:
        item = by_n.get(a["n"])
        if item is None or item["item_hash"] != a["item_hash"] or item_hash(item) != a["item_hash"]:
            raise BatchError(f"item {a['n']} differs from its approval record; nothing applied")
        if item["status"] in ("approved", "applied"):
            chosen.append(item)
    approved_items = [by_n[a["n"]] for a in approval["items"]]
    if approval.get("rows_sha256") and rows_sha256(approved_items) != approval["rows_sha256"]:
        raise BatchError(f"the rows for {batch_id} differ from what was approved; nothing applied")
    rows = [{"batch_id": batch_id, **r} for item in chosen if item["type"] in SCRIPT_TYPES for r in _rows(item)]
    applied = [i["n"] for i in chosen if i["type"] in SCRIPT_TYPES]
    deferred = [i["n"] for i in chosen if i["type"] not in SCRIPT_TYPES]
    csv_path = ws.path("data", "batches", f"{batch_id}.{SHEET_TAB}.csv")
    if not dry_run:
        import csv

        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            w.writeheader()
            w.writerows([{k: guard_cell(v) for k, v in r.items()} for r in rows])
        if confirm_pasted:
            today = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).date().isoformat()
            for item in chosen:
                if item["type"] in SCRIPT_TYPES:
                    item["pasted_at"] = today  # still approved: the script's applied_log says when it was applied
                else:
                    item["note"] = DEFERRED_NOTE if not item.get("note") else f"{item['note']}; {DEFERRED_NOTE}"
            save_batch(ws, batch)
    return {"batch_id": batch_id, "tab": SHEET_TAB, "csv": None if dry_run else str(csv_path), "rows": rows,
            "applied": applied, "deferred": deferred, "dry_run": dry_run, "confirmed": bool(confirm_pasted and not dry_run),
            "approved_by": approval["approved_by"]}


def apply_lines(out: dict) -> list[str]:
    where = "dry run: nothing written" if out["dry_run"] else f"wrote {out['csv']}"
    lines = [f"batch {out['batch_id']} ({len(out['rows'])} row(s) for the script; {where})",
             f"paste the CSV rows into the Sheet tab `{out['tab']}`, then run `mseo actions apply --confirm-pasted` "
             f"to record the paste for items {', '.join(map(str, out['applied'])) or '-'}; `mseo actions status` marks "
             "them applied from the script's applied_log"]
    if out["deferred"]:
        lines.append(f"items {', '.join(map(str, out['deferred']))} stay approved for the browser agent ({DEFERRED_NOTE})")
    if out["confirmed"]:
        lines.append("confirmed: pasted_at recorded; the items stay approved until `mseo actions status` reads the applied_log")
    return lines


# ---------------------------------------------------------------- status (from the script's applied_log)

def _log_index(rows: list[dict]) -> dict[tuple[str, int], list[dict]]:
    out: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        try:
            key = (str(r.get("batch_id") or "").strip(), int(float(r.get("n"))))
        except (TypeError, ValueError):
            continue
        if key[0]:
            out.setdefault(key, []).append(r)
    return out


def _batches(ws) -> list[dict]:
    folder = ws.path("data", "batches")
    out = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            batch = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if batch.get("schema_version") == BATCH_SCHEMA and batch.get("workspace") == ws.slug:
            out.append(batch)
    return out


def status(ws, week: str) -> dict:
    """Mark pasted script items from the week's applied_log: `applied` (applied_at = the date of the first ok row) or
    `failed` (note = the script's last reason). Items with no row yet stay approved."""
    data = tables.load_week(ws, week)
    log = data["tables"].get(tables.sheet.APPLIED_LOG)
    if log is None:
        raise BatchError(f"the {week} sheet run {data['run_id']} holds no applied_log tab; install the current script "
                         f"(`mseo ads script install`) and pull the week again")
    index = _log_index(log)
    applied, failed, pending = [], [], []
    for batch in _batches(ws):
        changed = False
        for item in batch["items"]:
            if item["type"] not in SCRIPT_TYPES or item.get("status") not in ("approved", "failed", "applied"):
                continue
            if not item.get("pasted_at") and item.get("status") == "approved":
                continue
            rows = index.get((batch["batch_id"], item["n"]), [])
            oks = [r for r in rows if (r.get("result") or "").startswith(OK_RESULT)]
            fails = [r for r in rows if (r.get("result") or "").startswith(FAILED_RESULT)]
            ref = {"batch_id": batch["batch_id"], "n": item["n"], "type": item["type"]}
            # a seed negative expands to one row per campaign: applied only when the latest row of EVERY campaign is ok
            latest = {}
            for r in rows:
                latest[log_campaign(r.get("target"))] = r.get("result") or ""
            wanted = set(item["target"].get("campaigns") or [])  # a seed negative names every campaign it expands to
            all_ok = not wanted or (wanted <= set(latest) and all(latest[c].startswith(OK_RESULT) for c in wanted))
            if oks and not all_ok:
                fails = fails or [{"result": "failed: not every campaign has an ok row yet"}]
                oks = []
            if oks:
                when = str(oks[0].get("applied_at") or "")[:10]
                if item.get("status") != "applied" or item.get("applied_at") != when:
                    item["status"], item["applied_at"] = "applied", when
                    changed = True
                applied.append({**ref, "applied_at": when})
            elif fails and item.get("status") != "applied":
                why = str(fails[-1].get("result") or "")
                if item.get("status") != "failed" or item.get("note") != why:
                    item["status"], item["note"] = "failed", why
                    changed = True
                failed.append({**ref, "result": why})
            elif item.get("status") != "applied":
                pending.append(ref)
        if changed:
            save_batch(ws, batch)
    return {"week": week, "run_id": data["run_id"], "log_rows": len(log), "applied": applied, "failed": failed,
            "pending": pending}


def status_lines(out: dict) -> list[str]:
    lines = [f"applied_log of {out['week']} (run {out['run_id']}): {out['log_rows']} row(s)"]
    lines += [f"applied  {a['batch_id']} item {a['n']} ({a['type']}) on {a['applied_at']}" for a in out["applied"]]
    lines += [f"FAILED   {f['batch_id']} item {f['n']} ({f['type']}): {f['result']}" for f in out["failed"]]
    lines += [f"pending  {p['batch_id']} item {p['n']} ({p['type']}): pasted, no log row yet" for p in out["pending"]]
    lines.append(f"{len(out['applied'])} applied, {len(out['failed'])} failed, {len(out['pending'])} pending")
    return lines
