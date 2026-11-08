"""M2b strix Medium (a detached process escapes the 12-hour kill): a process that calls setsid and double-forks leaves
the job's process group, so the group kill misses it. It cannot leave the job's macOS coalition: launchd gives each
job its own, and every descendant inherits it. Under launchd the wrapper kills every other member of its coalition
at the deadline and when the job ends."""

import json
import os
import plistlib
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from mseo.runner import coalition, wrap

PY = sys.executable
REPO = Path(__file__).resolve().parents[1]

ESCAPER = """
import os, sys, time
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        for fd in range(0, 256):
            try: os.close(fd)
            except OSError: pass
        open(sys.argv[1], "w").write(str(os.getpid()))
        time.sleep(600)
    os._exit(0)
time.sleep(600)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ------------------------------------------------------------------ the sweep, with fake members

class FakeProcs:
    def __init__(self, pids, stubborn=()):
        self.pids, self.stubborn, self.sent = set(pids), set(stubborn), []

    def members(self):
        return sorted(self.pids)

    def kill(self, pid, sig):
        self.sent.append((pid, sig))
        if sig == 9 or pid not in self.stubborn:
            self.pids.discard(pid)


def test_sweep_terms_then_kills_what_is_left():
    procs = FakeProcs([11, 12, 13], stubborn=[13])
    n = wrap.sweep(procs.members, kill=procs.kill, grace_s=0.2)
    assert n == 3 and not procs.pids
    assert (13, 9) in procs.sent and (11, 9) not in procs.sent


def test_sweep_with_nothing_left_sends_nothing():
    procs = FakeProcs([])
    assert wrap.sweep(procs.members, kill=procs.kill, grace_s=0.1) == 0 and procs.sent == []


def test_members_never_include_the_wrapper_itself():
    me = coalition.coalition_id(os.getpid())
    if me is None:
        pytest.skip("no coalition support on this platform")
    assert os.getpid() not in coalition.members(me, exclude={os.getpid()})


LAUNCHD_ENV = {"XPC_SERVICE_NAME": "com.mseo.fx.weekly"}


def test_an_interactive_run_does_not_own_its_coalition():
    # a terminal's coalition holds the terminal itself: sweeping it would kill the operator's shell
    assert wrap.owns_coalition(ppid=4242, env=LAUNCHD_ENV, others=lambda: []) is False


def test_an_orphan_from_a_terminal_does_not_own_its_coalition():
    # `nohup mseo ... &` after the shell exits: ppid 1, but the coalition is still the terminal's
    assert wrap.owns_coalition(ppid=1, env={}, others=lambda: []) is False
    assert wrap.owns_coalition(ppid=1, env={"XPC_SERVICE_NAME": "com.apple.Terminal"}, others=lambda: []) is False
    assert wrap.owns_coalition(ppid=1, env=LAUNCHD_ENV, others=lambda: [501, 502]) is False


def test_a_launchd_job_alone_in_its_coalition_owns_it():
    assert wrap.owns_coalition(ppid=1, env=LAUNCHD_ENV, others=lambda: []) is True


def test_sweep_rechecks_each_pid_before_signalling():
    procs = FakeProcs([21, 22])
    wrap.sweep(procs.members, kill=procs.kill, grace_s=0.1, still=lambda pid: pid != 22)
    assert all(pid != 22 for pid, _ in procs.sent)


def test_the_wrapper_sweeps_after_a_normal_exit_too(tmp_path):
    procs = FakeProcs([77])
    code, sent, swept = wrap.supervise([PY, "-c", "pass"], log=tmp_path / "log", timeout_s=30, grace_s=0.2,
                                       env=dict(os.environ), sweeper=lambda g: wrap.sweep(procs.members,
                                                                                          kill=procs.kill, grace_s=g))
    assert code == 0 and sent is None and swept == 1


# ------------------------------------------------------------------ the real thing, under launchd

@pytest.mark.skipif(sys.platform != "darwin", reason="launchd coalitions are macOS only")
def test_a_setsid_double_fork_child_is_dead_after_the_kill(tmp_path):
    label = f"com.mseo.test-sweep-{uuid.uuid4().hex[:8]}"
    escaper, pidfile, result = tmp_path / "esc.py", tmp_path / "gc.pid", tmp_path / "result.json"
    escaper.write_text(ESCAPER)
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import json, os, sys\nfrom pathlib import Path\nfrom mseo.runner import wrap\n"
        f"own = wrap.owns_coalition()\n"
        f"code, sent, swept = wrap.supervise([sys.executable, {str(escaper)!r}, {str(pidfile)!r}], "
        f"log=Path({str(tmp_path / 'job.log')!r}), timeout_s=3, grace_s=1, env=dict(os.environ), "
        "sweeper=wrap.coalition_sweeper() if own else None)\n"
        f"Path({str(result)!r}).write_text(json.dumps(dict(own=own, code=code, sent=sent, swept=swept)))\n")
    plist = tmp_path / f"{label}.plist"
    plist.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": [PY, "-P", str(driver)],
                                      "EnvironmentVariables": {"PYTHONPATH": str(REPO)},
                                      "WorkingDirectory": str(tmp_path), "RunAtLoad": True}))
    domain = f"gui/{os.getuid()}"
    boot = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
    if boot.returncode:
        pytest.skip(f"cannot bootstrap a launchd job here: {boot.stderr.strip()}")
    gc = None
    try:
        deadline = time.monotonic() + 30
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert result.exists(), "the driver never finished"
        out = json.loads(result.read_text())
        gc = int(pidfile.read_text())
        assert out["own"] is True
        assert out["sent"] and out["swept"] >= 1
        time.sleep(0.5)
        assert not _alive(gc), "the detached grandchild survived the kill"
    finally:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
        if gc and _alive(gc):
            os.kill(gc, 9)
