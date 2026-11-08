"""The job process the wrapper starts (`python -m mseo.runner.child`): opens one workspace, runs the weekly job,
which writes the outcome record. Exit 0 when the outcome is ok, 1 otherwise."""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from mseo import workspace
from mseo.runner import weekly


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mseo.runner.child", allow_abbrev=False)
    p.add_argument("--workspace", required=True)
    p.add_argument("--job-id", required=True)
    p.add_argument("--week")
    p.add_argument("--scheduled-for")
    args = p.parse_args(argv)
    ws = workspace.open(args.workspace)
    scheduled = dt.datetime.fromisoformat(args.scheduled_for) if args.scheduled_for else None
    out = weekly.run_job(ws, week=args.week, job_id=args.job_id, scheduled_for=scheduled,
                         after=weekly.assemble_and_compare)
    print(f"{out['job_id']} {out['status']}" + (f": {out['error']}" if out.get("error") else ""))
    return 0 if out["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
