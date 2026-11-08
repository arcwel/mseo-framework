"""The agent identity in the browser: the sign-in check and `mseo identity profile`.

Sign-in check: an agent opens the Google Account page and saves its text; Python reads the signed-in email. Anything
other than the identity's email is `wrong_account` and the snapshot is deleted, so a foreign account's page is never
kept. `identity profile` points the workspace's Chrome profile's downloads at <ws>/data/inbox/.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from mseo import seal
from mseo.browser import attest, extract
from mseo.browser import run as browser_run
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from mseo.fsutil import sha256_file, write_json_atomic
from mseo.run import record

SIGN_IN_SNAPSHOT = "snapshot-account.txt"
CHROME_DIR = Path("~/Library/Application Support/Google/Chrome").expanduser()


class ProfileMissing(Exception):
    """The workspace's Chrome profile is not in Chrome's Local State."""


def _now(now: dt.datetime | None) -> dt.datetime:
    return now or dt.datetime.now(dt.UTC)


def sign_in_check(ws, source: str, *, period: dict, runner: Callable = browser_run.subprocess_runner,
                  now: dt.datetime | None = None, job_id: str | None = None, require_seal: bool = True) -> dict:
    """Run the sign-in check for the identity `source` runs as. Writes a `sign_in` run record; returns its summary."""
    ident = ws.identity_for(source)
    started = _now(now)
    run_id = record.new_run_id("sign_in", now=started)
    run_dir = ws.path("data", "runs", run_id)
    inbox = ws.path("data", "inbox")
    inbox.mkdir(parents=True, exist_ok=True)
    snap = inbox / SIGN_IN_SNAPSHOT
    snap.unlink(missing_ok=True)
    res = browser_run.run_agent(ws, browser_run.prompt("sign_in", ws, source=source, snapshot=SIGN_IN_SNAPSHOT), runner=runner,
                                require_seal=require_seal)
    seen, files, error = None, [], res.error
    if not res.ok:
        status = res.status
    elif not snap.is_file():
        status, error = "error", f"the agent saved no {SIGN_IN_SNAPSHOT}"
    else:
        page = snap.read_text(encoding="utf-8", errors="replace")
        seen, reason = attest.attested(extract.signed_in_email, page, res)
        expected = ident["email"].lower()
        if reason in attest.REASONS:
            status, error = "unverified_account", attest.why(reason, "signed-in account")
        elif seen == expected:
            status, error = "ok", None
        elif reason == "signed_out":
            status, error = "auth_failed", "the profile is signed out of Google"
        elif reason:
            status, error = "layout_changed", f"account page not recognised ({reason}; {extract.page_header(page)})"
        else:
            status, error = "wrong_account", f"signed in as {seen}, expected {expected}"
    if status == "ok":
        run_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(snap), run_dir / SIGN_IN_SNAPSHOT)
        files = [{"path": SIGN_IN_SNAPSHOT, "sha256": sha256_file(run_dir / SIGN_IN_SNAPSHOT), "rows": None,
                  "truncated": False}]
    snap.unlink(missing_ok=True)  # a foreign account's page is never kept
    rec = record.build(run_id=run_id, job_id=job_id, workspace=ws.slug, source="sign_in", adapter="browser",
                       identity=ident["email"], identity_shared=bool(ident["allow_shared"]), period=period,
                       account_id_expected=ident["email"].lower(), account_id_seen=seen,
                       seen_via="page:google_account", status=status,
                       complete={"value": status == "ok", "reason": None if status == "ok" else status},
                       files=files, started=started, finished=_now(None) if now is None else now, usage=res.usage,
                       error=error)
    record.write(run_dir, rec)
    return {"status": status, "run_id": run_id, "identity": ident["email"], "seen": seen, "error": error}


# ---------------------------------------------------------------- mseo identity check

def _ga4_properties(text: str):
    ids, reason = extract.ga4_ids(text)
    return (ids["properties"] if ids else None), reason


# source -> (prompt name, extractor returning (ids, reason)). Sources without a lister are `unchecked`.
LISTERS = {"search_console": ("list_search_console", extract.sc_properties),
           "ga4": ("list_ga4", _ga4_properties),
           "google_ads": ("list_google_ads", extract.ads_customer_ids),
           "business_profile": ("list_business_profile", extract.bp_location_ids)}
DEFAULT_RUNNER = browser_run.subprocess_runner
REFUSING = {"missing", "auth_failed", "error", "layout_changed", "unchecked"}


def _list_one(ws, source: str, email: str, runner: Callable, run_dir: Path, require_seal: bool) -> tuple[str, str, dict | None, dict | None]:
    """(result, detail, file entry or None, usage) for one lister run. A list with foreign IDs is never kept."""
    from mseo.capture.account import normalize

    prompt_name, extractor = LISTERS[source]
    snap_name = f"snapshot-{source}-list.txt"
    snap = ws.path("data", "inbox", snap_name)
    snap.unlink(missing_ok=True)
    res = browser_run.run_agent(ws, browser_run.prompt(prompt_name, ws, source=source, snapshot=snap_name,
                                                   email=email),
                                runner=runner, require_seal=require_seal)
    try:
        if not res.ok:
            return ("auth_failed" if res.status == "auth_failed" else "error"), res.error or "", None, res.usage
        if not snap.is_file():
            return "error", f"the agent saved no {snap_name}", None, res.usage
        ids, reason = attest.attested(extractor, snap.read_text(encoding="utf-8", errors="replace"), res)
        if reason in attest.REASONS:
            return "error", attest.why(reason, "account list"), None, res.usage
        if reason == "signed_out":
            return "auth_failed", "the list page asked to sign in", None, res.usage
        if reason:
            return "layout_changed", f"account list not recognised ({reason})", None, res.usage
        expected = normalize(ws.account(source))
        seen = {normalize(i) for i in ids}
        others = len(seen - {expected})
        if expected not in seen:
            return "missing", f"{ws.account(source)} not in the list ({others} other account(s))", None, res.usage
        if others:
            return "foreign", f"{others} other account(s) visible besides {ws.account(source)}", None, res.usage
        run_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(snap), run_dir / snap_name)
        entry = {"path": snap_name, "sha256": sha256_file(run_dir / snap_name), "rows": len(ids), "truncated": False}
        return "ok", f"only {ws.account(source)}", entry, res.usage
    finally:
        snap.unlink(missing_ok=True)


def _clear_snapshots(ws, since: float) -> None:
    """Agents write only snapshot-*.txt to the inbox; any left behind from this check (an extra page, a foreign
    list) is deleted rather than kept. Export files are not snapshot-* and are left for their pull."""
    folder = ws.path("data", "inbox")
    for p in folder.glob("snapshot-*.txt") if folder.exists() else []:
        if p.stat().st_mtime >= since - 1:
            p.unlink()


def _sum_usage(usages: list[dict | None]) -> dict | None:
    got = [u for u in usages if u]
    if not got:
        return None
    total = {k: sum(u[k] for u in got) for k in ("tokens_in", "tokens_out", "cache_read", "cache_write", "turns")}
    return {**total, "minutes": round(sum(u["minutes"] for u in got), 2), "model": got[-1]["model"]}


def identity_check(ws, *, sources: list[str] | None = None, runner: Callable | None = None,
                   now: dt.datetime | None = None, period: dict | None = None, require_seal: bool = True,
                   job_id: str | None = None) -> dict:
    """Sign in, then list what the identity can see per source. Status: ok, shared (foreign but allow_shared), refused."""
    runner = runner or DEFAULT_RUNNER
    wall_start = time.time()
    started = _now(now)
    period = period or record.week_period(started.astimezone().strftime("%G-W%V"), ws.config["timezone"])
    run_id = record.new_run_id("identity_check", now=started)
    run_dir = ws.path("data", "runs", run_id)
    srcs = sources or ws.enabled_sources()
    groups: dict[str, list[str]] = {}
    for src in srcs:
        groups.setdefault(ws.identity_for(src)["email"], []).append(src)
    results, details, files, usages, sign_ins, shared_ok = {}, {}, [], [], {}, False
    for email, group in groups.items():
        ident = ws.identity_for(group[0])
        manual_srcs = {s for s in group if ws.config["accounts"][s].get("check") == "manual"}
        listed = [s for s in group if s in LISTERS and s not in manual_srcs]
        for s in group:
            if s not in listed:
                manual = s in manual_srcs
                results[s], details[s] = ("manual", "checked by hand (check: manual)") if manual else (
                    "unchecked", "no lister for this source; set check: manual if you verify it by hand")
        if not listed:
            continue
        sign = sign_in_check(ws, listed[0], period=period, runner=runner, now=now, job_id=job_id,
                             require_seal=require_seal)
        sign_ins[email] = sign["status"]
        if sign["status"] != "ok":
            for s in listed:
                results[s], details[s] = "auth_failed", f"sign-in check: {sign['status']} ({sign['error']})"
            continue
        for s in listed:
            results[s], details[s], entry, usage = _list_one(ws, s, email, runner, run_dir, require_seal)
            usages.append(usage)
            if entry:
                files.append(entry)
            if results[s] == "foreign" and ident["allow_shared"]:
                shared_ok = True
    _clear_snapshots(ws, wall_start)
    vals = set(results.values())
    refused_foreign = any(r == "foreign" and not ws.identity_for(s)["allow_shared"] for s, r in results.items())
    if vals & REFUSING or refused_foreign:
        status = "refused"
    else:
        status = "shared" if shared_ok else "ok"
    if status == "refused":
        rec_status = ("wrong_account" if (refused_foreign or "missing" in vals) else
                      "auth_failed" if "auth_failed" in vals else "error")
    else:
        rec_status = "ok"
    if rec_status != "ok":
        files = []  # only ok keeps files
        for p in (run_dir.glob("snapshot-*-list.txt") if run_dir.exists() else []):
            p.unlink()
    checks = [{"check": s, "ok": results[s] in ("ok", "manual") or (results[s] == "foreign" and
                                                                    ws.identity_for(s)["allow_shared"]),
               "detail": f"{results[s]}: {details[s]}"} for s in srcs]
    identity_shared = any(ws.identity_for(s)["allow_shared"] for s in srcs)
    rec = record.build(run_id=run_id, job_id=job_id, workspace=ws.slug, source="identity_check", adapter="browser",
                       identity=ws.config["identity"]["email"], identity_shared=identity_shared, period=period,
                       account_id_expected=None, account_id_seen=None, seen_via="page:account_lists",
                       status=rec_status, complete={"value": False, "reason": "an identity check is not a data pull"},
                       files=files, started=started, finished=_now(None) if now is None else now,
                       usage=_sum_usage(usages), checks=checks,
                       error=None if rec_status == "ok" else "; ".join(c["detail"] for c in checks if not c["ok"]))
    record.write(run_dir, rec)
    return {"status": status, "run_id": run_id, "results": results, "details": details,
            "sign_in": next(iter(sign_ins.values()), None) if len(sign_ins) == 1 else sign_ins}


def cmd_check(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    sources = [args.source] if args.source else None
    try:
        out = identity_check(ws, sources=sources)
    except seal.StaleSeal as exc:
        ctx.emit({"status": "error", "messages": [f"error: {exc}"]})
        return EXIT_COULD_NOT_RUN
    msgs = [f"{s}: {r} ({out['details'][s]})" for s, r in out["results"].items()]
    msgs.append(f"identity check {ws.slug}: {out['status']}")
    ctx.emit({**out, "messages": msgs})
    return EXIT_OK if out["status"] in ("ok", "shared") else EXIT_FAIL


# ---------------------------------------------------------------- mseo identity profile

def chrome_running() -> bool:
    proc = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True, text=True, check=False)
    return proc.returncode == 0


def _profile_folder(name: str, chrome_dir: Path) -> str:
    try:
        cache = json.loads((chrome_dir / "Local State").read_text(encoding="utf-8"))["profile"]["info_cache"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProfileMissing(f"cannot read Chrome's Local State in {chrome_dir}: {type(exc).__name__}") from exc
    for folder, info in cache.items():
        if name in (folder, (info or {}).get("name")):
            return folder
    raise ProfileMissing(f"Chrome has no profile named {name!r}: create it in Chrome, then run this again")


def set_profile_downloads(ws, *, chrome_dir: Path | None = None, chrome_running: Callable[[], bool] | None = None,
                          dry_run: bool = False) -> dict:
    chrome_dir = chrome_dir or CHROME_DIR
    running = chrome_running or globals()["chrome_running"]
    name = ws.config["identity"]["chrome_profile"]
    folder = _profile_folder(name, chrome_dir)
    inbox = ws.path("data", "inbox")
    prefs_path = chrome_dir / folder / "Preferences"
    out = {"profile": name, "profile_folder": folder, "download_folder": str(inbox), "changed": False}
    manual = (f"in the {name} profile: Settings > Downloads > Location = {inbox}, and turn off "
              "'Ask where to save each file'")
    if dry_run:
        return {**out, "message": f"would set {name}'s download folder to {inbox}"}
    if running():
        return {**out, "message": f"quit Chrome and run this again, or set it by hand {manual}"}
    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    download = prefs.setdefault("download", {})
    download.update(default_directory=str(inbox), prompt_for_download=False)
    inbox.mkdir(parents=True, exist_ok=True)
    write_json_atomic(prefs_path, prefs, indent=None)
    return {**out, "changed": True, "message": f"set {name}'s download folder to {inbox}"}


def cmd_profile(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    try:
        out = set_profile_downloads(ws, dry_run=args.dry_run)
    except ProfileMissing as exc:
        ctx.emit({"status": "error", "messages": [f"error: {exc}"]})
        return EXIT_COULD_NOT_RUN
    once = [f"once by hand: sign the {out['profile']} profile in to {ws.config['identity']['email']}",
            f"once by hand: install Claude in Chrome in the {out['profile']} profile and connect it"]
    ok = out["changed"] or args.dry_run
    ctx.emit({"status": "ok" if ok else "pending", **out, "messages": [out["message"], *once]})
    return EXIT_OK if ok else EXIT_FAIL


def register(sub, parents) -> None:
    p = sub.add_parser("identity", help="The workspace's agent identity in Chrome")
    isub = p.add_subparsers(dest="identity_command", required=True, parser_class=type(p))
    prof = isub.add_parser("profile", parents=[parents.workspace, parents.json],
                           help="Point the identity's Chrome profile downloads at <ws>/data/inbox/")
    prof.add_argument("--dry-run", action="store_true", help="Print what would change")
    prof.set_defaults(func=cmd_profile, command_id="identity profile")
    chk = isub.add_parser("check", parents=[parents.workspace, parents.json],
                          help="Sign in, then list what the identity can see per source (ok, foreign, missing, ...)")
    chk.add_argument("--source", help="Check one source only")
    chk.set_defaults(func=cmd_check, command_id="identity check")
