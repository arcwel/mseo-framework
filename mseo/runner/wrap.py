"""The runner wrapper: holds the workspace lock, starts the job in its own process group,
kills the group at 12 hours, and writes the job's one outcome record whatever happened.

The wrapper is the parent and is not in the job's process group, so it survives the group kill and can still write
the `killed` outcome. The job runs as `python -m mseo.runner.child`, which writes the outcome itself when it finishes.

A process that calls setsid() leaves the group. Under launchd (the scheduled path) the wrapper also owns the job's
coalition, which such a process cannot leave, so at the deadline and when the job ends it kills every other member of
its coalition. Run from a terminal, the coalition is the terminal's, so only the group is killed.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from mseo.run import outcome
from mseo.runner import coalition, lock

KILL_AFTER_S = 12 * 60 * 60
GRACE_S = 30.0
STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")  # subscription only: never per-token API billing
LOG_TAIL = 400


def child_env(env: dict | None = None) -> dict:
    out = {k: v for k, v in (os.environ if env is None else env).items() if k not in STRIP_ENV}
    out["PYTHONSAFEPATH"] = "1"  # never put the (agent-writable) cwd on sys.path
    return out


def child_argv(ws, *, job_id: str, week: str | None, scheduled_for: dt.datetime | None) -> list[str]:
    argv = [sys.executable, "-P", "-m", "mseo.runner.child", "--workspace", ws.slug, "--job-id", job_id]
    if week:
        argv += ["--week", week]
    if scheduled_for is not None:
        argv += ["--scheduled-for", scheduled_for.isoformat()]
    return argv


def _job_run_ids(ws, job_id: str) -> list[str]:
    runs = ws.path("data", "runs")
    out = []
    for path in sorted(runs.glob("*/record.json")) if runs.is_dir() else []:
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("job_id") == job_id:
                out.append(path.parent.name)
        except (OSError, ValueError):
            continue
    return out


def _tail(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text[-LOG_TAIL:]


def _write(ws, job_id: str, status: str, error: str, started, scheduled_for) -> dict:
    out = outcome.build(job_id=job_id, workspace=ws.slug, status=status, scheduled_for=scheduled_for,
                        started=started, finished=dt.datetime.now(dt.timezone.utc),
                        run_ids=_job_run_ids(ws, job_id), identity_check={}, identity_shared=False, usage=None,
                        error=error)
    outcome.write(ws.path("data", "outcomes"), out)
    return out


def _group_alive(proc: subprocess.Popen) -> bool:
    proc.poll()  # reap the leader so it does not count as a member
    try:
        os.killpg(proc.pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _kill_group(proc: subprocess.Popen, grace_s: float) -> str:
    """SIGTERM to the job's group, then SIGKILL after grace_s. Returns what it took."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):  # macOS: EPERM for a group of zombies
        return "SIGTERM"
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and _group_alive(proc):  # the whole group gets the grace, not just the leader
        time.sleep(0.05)
    sent = "SIGTERM, then SIGKILL" if _group_alive(proc) else "SIGTERM"
    try:  # the job may be gone while its children (claude -p, a browser helper) are not
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # macOS: EPERM for a group of zombies
        pass
    proc.wait()
    return sent


JOB_PREFIX = "com.mseo."


def owns_coalition(*, ppid: int | None = None, env: dict | None = None, others=None) -> bool:
    """True only when launchd started this process as an mseo job and nothing else is in its coalition yet. Checked
    before the child starts. ppid 1 alone is not enough: a terminal run orphaned by `nohup` also has ppid 1 but keeps
    the terminal's coalition, which holds the operator's shells and editors. A second process already in the
    coalition (another wrapper in the same job) also means it is not ours to sweep."""
    env = os.environ if env is None else env
    if (os.getppid() if ppid is None else ppid) != 1:
        return False
    if not str(env.get("XPC_SERVICE_NAME") or "").startswith(JOB_PREFIX):
        return False
    if others is None:
        cid = coalition.coalition_id(os.getpid())
        if cid is None:
            return False
        others = lambda: coalition.members(cid, exclude={os.getpid()})  # noqa: E731
    return not others()


def _signal(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def sweep(members, *, kill=_signal, grace_s: float = GRACE_S, still=lambda pid: True) -> int:
    """SIGTERM every process members() lists, SIGKILL whatever is left after grace_s. still(pid) is checked right
    before each signal, so a pid that left (and may have been reused) is skipped. Returns how many there were."""
    found = members()
    for pid in found:
        if still(pid):
            kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while found and time.monotonic() < deadline and members():
        time.sleep(0.05)
    for pid in members():
        if still(pid):
            kill(pid, signal.SIGKILL)
    return len(found)


def coalition_sweeper():
    """Kills every other process in this launchd job's coalition (see `sweep`).

    Known gap (accepted): a process that launchd starts on request (`launchctl
    submit`, `open`, `at`) gets its own job and coalition, so it survives both the deadline kill and this sweep. Only an
    unsandboxed process can ask launchd for that; the agent's Bash runs in the sandbox with unsandboxed commands off,
    so keep that setting and the sandbox's deny on launchd access."""
    cid, me = coalition.coalition_id(os.getpid()), os.getpid()
    return lambda grace_s: sweep(lambda: coalition.members(cid, exclude={me}), grace_s=grace_s,
                                 still=lambda pid: coalition.coalition_id(pid) == cid)


def supervise(argv: list[str], *, log: Path, timeout_s: float, grace_s: float, env: dict | None,
              sweeper=None) -> tuple[int | None, str | None, int]:
    """Run argv in its own session; (exit code or None when killed, what was sent to the group, processes swept)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as fh:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                env=child_env(env), start_new_session=True)
        try:
            code, sent = proc.wait(timeout=timeout_s), None
        except subprocess.TimeoutExpired:
            code, sent = None, _kill_group(proc, grace_s)
    swept = sweeper(grace_s) if sweeper else 0  # a detached leftover dies with the job, killed or not
    return code, sent, swept


def run_wrapped(ws, *, job_id: str, argv: list[str], scheduled_for: dt.datetime | None = None,
                timeout_s: float = KILL_AFTER_S, grace_s: float = GRACE_S, env: dict | None = None,
                sweeper=None) -> dict:
    started = dt.datetime.now(dt.timezone.utc)
    with lock.workspace_lock(ws.slug) as held:
        if not held:
            return _write(ws, job_id, "not_run", f"another job holds the workspace lock (pid {lock.holder(f'ws-{ws.slug}')})",
                          started, scheduled_for)
        log = ws.path("data", "logs", f"{job_id}.log")
        own = owns_coalition() if sweeper is None else bool(sweeper)
        code, sent, swept = supervise(argv, log=log, timeout_s=timeout_s, grace_s=grace_s, env=env,
                                      sweeper=(coalition_sweeper() if sweeper is None else sweeper) if own else None)
        if code is None:
            extra = (f" and {swept} detached process(es) in its coalition" if own and swept else
                     "" if own else "; not under launchd, so detached processes were not swept")
            return _write(ws, job_id, "killed", f"the job ran past {timeout_s:.0f} s; sent {sent} to its group{extra}",
                          started, scheduled_for)
        path = ws.path("data", "outcomes", f"{job_id}.json")
        if path.is_file():
            return outcome.load(path)
        tail = _tail(log)
        return _write(ws, job_id, "failed", f"the job exited with code {code} and wrote no outcome"
                      + (f": {tail}" if tail else ""), started, scheduled_for)
