"""Business Profile location list: names, addresses and verification state.

The page shape is UNVERIFIED (built on synthetic fixtures until an identity has Business Profile access). Only the customer's own location row is stored. Other locations on the page are counted, never stored, and the
snapshot is then discarded, because it names them.
"""

from __future__ import annotations

import datetime as dt

from mseo import identity
from mseo.adapters import export
from mseo.browser import attest, extract
from mseo.browser import run as browser_run
from mseo.exits import EXIT_FAIL, EXIT_OK
from mseo.run import record as run_record

SOURCE = "business_profile"
EXIT_EXISTS = 2
SNAPSHOT = "snapshot-business_profile.txt"
FIELDS = ["id", "name", "address", "verification"]
DEFAULT_RUNNER = browser_run.subprocess_runner


def pull(ws, period: dict, *, runner=None, force: bool = False, job_id: str | None = None,
         now: dt.datetime | None = None, signed_in: bool = False, require_seal: bool = True):
    """(exit code, record or None, one line)."""
    runner = runner or DEFAULT_RUNNER
    started = now or dt.datetime.now(dt.timezone.utc)
    dup = export.existing(ws, SOURCE, period)
    if dup and not force:
        return EXIT_EXISTS, None, f"{SOURCE} {period['start']}..{period['end']} already recorded ({', '.join(dup)})"
    ident = ws.identity_for(SOURCE)
    expected = ws.account(SOURCE)
    run_id = run_record.new_run_id(SOURCE, now=started)
    run_dir = ws.path("data", "runs", run_id)
    base = dict(run_id=run_id, job_id=job_id, workspace=ws.slug, source=SOURCE, adapter="browser",
                identity=ident["email"], identity_shared=bool(ident["allow_shared"]), period=period,
                account_id_expected=expected, seen_via="page:bp_locations", started=started)

    def finish(status, error, *, seen=None, files=(), usage=None, note=None):
        rec = run_record.build(**base, account_id_seen=seen, status=status, error=error, files=list(files),
                               usage=usage, complete={"value": status == "ok", "reason": note if status == "ok"
                                                      else status},
                               finished=now or dt.datetime.now(dt.timezone.utc))
        run_record.write(run_dir, rec)
        code = EXIT_OK if status in ("ok", "empty") else EXIT_FAIL
        return code, rec, f"{status}: {SOURCE} -> {run_dir}"

    if not signed_in:
        sign = identity.sign_in_check(ws, SOURCE, period=period, runner=runner, now=now, job_id=job_id,
                                      require_seal=require_seal)
        if sign["status"] != "ok":
            return finish(sign["status"], f"sign-in check: {sign['error']}")
    inbox = export.inbox(ws)
    inbox.mkdir(parents=True, exist_ok=True)
    snap = inbox / SNAPSHOT
    snap.unlink(missing_ok=True)
    res = browser_run.run_agent(ws, browser_run.prompt("list_business_profile", ws, source=SOURCE, snapshot=SNAPSHOT,
                                                       email=ident["email"]), runner=runner, require_seal=require_seal)
    if not res.ok:
        snap.unlink(missing_ok=True)
        return finish("auth_failed" if res.status == "auth_failed" else "error", res.error, usage=res.usage)
    if res.reply.lower().startswith("failed"):
        snap.unlink(missing_ok=True)
        return finish("error", f"the agent replied: {res.reply[:300]}", usage=res.usage)
    if not snap.is_file():
        return finish("unverified_account", f"the agent saved no {SNAPSHOT}", usage=res.usage)
    rows, reason = attest.attested(extract.bp_locations, snap.read_text(encoding="utf-8", errors="replace"), res)
    if reason in attest.REASONS:
        snap.unlink(missing_ok=True)
        return finish("unverified_account", attest.why(reason, "location list"), usage=res.usage)
    if reason:
        snap.unlink(missing_ok=True)
        status = "auth_failed" if reason == "signed_out" else "layout_changed"
        return finish(status, f"location list not recognised ({reason})", usage=res.usage)
    mine = [r for r in rows if r["id"] == expected]
    others = len(rows) - len(mine)
    if not mine:
        snap.unlink(missing_ok=True)
        return finish("wrong_account", f"location {expected} is not in the list ({others} other location(s), not "
                                       "stored)", usage=res.usage)
    run_dir.mkdir(parents=True, exist_ok=True)
    files = []
    if others:
        snap.unlink()
        note = f"{others} other location(s) on the page were not stored; the snapshot was discarded"
    else:
        files.append(export.ingest(ws, snap, run_dir))
        note = None
    files.append(export.write_table(run_dir, "locations.csv", FIELDS, mine[:1]))
    return finish("ok", None, seen=expected, files=files, usage=res.usage, note=note)
