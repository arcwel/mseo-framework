"""The isolation suite: every CLI command, run in a subprocess on a tree holding fx and fy.

For each command in COMMANDS:
  (a) with --workspace fx, nothing under fy/ is opened, listed or written, and only registered binaries are run;
      fy is chmod 000 during the case, so a non-Python reader fails loudly;
  (b) with cwd inside fy and --workspace fx, the command exits 3 and writes nothing in either workspace;
  (c) without --workspace, it exits 3.
Commands exempt from --workspace (workspace new, lint customer-strings) have their own cases.

An audit hook in tests/isolation/sitecustomize.py logs every path and argv. A new subcommand that is not in COMMANDS
fails test_every_command_is_registered.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mseo import cli
from tests.conftest import FX, SYNTHETIC, TESTS, seal_for_tests

HOOK_DIR = TESTS / "isolation"
ALLOWED_BINARIES = {"security", "git"}
WP_PAYLOAD = {"site": FX["cms"]["site"], "user": "editor", "app_password": "abcd efgh"}

# id -> case. argv uses {ws} for the workspace slug and {draft} for fx's sample article. "exempt": no --workspace.
COMMANDS: dict[str, dict] = {
    "secret set": {"argv": ["secret", "set", "--workspace", "{ws}", "--source", "wordpress"],
                   "stdin": json.dumps(WP_PAYLOAD)},
    "lint customer-strings": {"argv": ["lint", "customer-strings"], "exempt": True},
    "lint vocabulary": {"argv": ["lint", "vocabulary"], "exempt": True},
    "workspace new": {"argv": ["workspace", "new", "fz", "--skip-live-test"], "exempt": True},
    "workspace seal": {"argv": ["workspace", "seal", "--workspace", "{ws}", "--skip-live-test"],
                       "binaries": {"uv", "git"}, "codes": (0, 1),
                       # the seal lists the siblings, asks git for each one's origin remote and reads each one's
                       # customer.json to check no identifier is shared (audit M4), by design
                       "fy_allowed": (r"^(Popen \['git', '-C', '[^']*/fy', 'remote', 'get-url', 'origin'\]"
                                      r"|open .*/fy/customer\.json)$")},
    # 3: unsealed fixture. The identity-uniqueness check reads each sibling's customer.json by design (v2 3.1 item 2).
    "doctor": {"argv": ["doctor", "--workspace", "{ws}"], "codes": (0, 1, 3), "fy_allowed": r"^open .*/fy/customer\.json$"},
    "content check": {"argv": ["content", "check", "{draft}", "--workspace", "{ws}", "--kind", "article",
                               "--slug", "good-article"], "codes": (0,), "binaries": {"deslop"}},
    "content lint": {"argv": ["content", "lint", "{draft}", "--workspace", "{ws}", "--blog"], "codes": (0,)},
    "content wording": {"argv": ["content", "wording", "{draft}", "--workspace", "{ws}"], "codes": (0,)},
    "content dupes": {"argv": ["content", "dupes", "article", "{draft}", "--workspace", "{ws}", "--slug", "good-article"],
                      "codes": (0,)},
    "content log article": {"argv": ["content", "log", "article", "--workspace", "{ws}", "--slug", "good-article",
                                     "--status", "draft"], "codes": (0,)},
    "content log gbp": {"argv": ["content", "log", "gbp", "--workspace", "{ws}", "--date", "2026-11-12",
                                 "--headline", "a new post", "--theme", "t"], "codes": (0,)},
    # Recipes and templates (v2 3.2): the templates live in the framework, read-only; topics has no records: 3.
    "content recipe": {"argv": ["content", "recipe", "--workspace", "{ws}", "--kind", "article"], "codes": (0,)},
    "content new": {"argv": ["content", "new", "--workspace", "{ws}", "--kind", "article", "--slug", "iso-post",
                             "--service", "s", "--area", "a"], "codes": (0,)},
    "content topics": {"argv": ["content", "topics", "--workspace", "{ws}", "--week", "2026-W45"], "codes": (3,)},
    "loop verify-live": {"argv": ["loop", "verify-live", "--workspace", "{ws}"], "codes": (0,)},
    "loop measure": {"argv": ["loop", "measure", "--workspace", "{ws}", "--week", "2026-W45"], "codes": (0,)},
    "wp verify": {"argv": ["wp", "verify", "--workspace", "{ws}"], "codes": (0,)},
    "wp post-create": {"argv": ["wp", "post-create", "{draft}", "--workspace", "{ws}", "--slug", "good-article",
                                "--dry-run"], "codes": (0,)},
    "wp page-create": {"argv": ["wp", "page-create", "RULES.md", "--workspace", "{ws}", "--slug", "p", "--title", "P",
                                "--dry-run"], "codes": (0,)},
    "wp snippet-deploy": {"argv": ["wp", "snippet-deploy", "RULES.md", "--workspace", "{ws}", "--dry-run"], "codes": (0,)},
    "wp posts-refresh": {"argv": ["wp", "posts-refresh", "--workspace", "{ws}"], "codes": (0,)},
    "publish": {"argv": ["publish", "{draft}", "--workspace", "{ws}", "--kind", "article", "--slug", "good-article",
                         "--approved", "Operator 2026-11-05", "--dates-confirmed"], "codes": (0,), "binaries": {"deslop"}},
    "capture verify": {"argv": ["capture", "verify", "captures/ok", "--workspace", "{ws}", "--source", "google_ads"],
                       "codes": (0,)},
    "pull": {"argv": ["pull", "--workspace", "{ws}", "--source", "ga4", "--week", "2026-W44",
                      "--file", "exports/ga4-traffic-2026-W44.csv"], "codes": (0,)},
    # Reads Chrome's Local State; the synthetic profile is absent on most machines, so 3 (profile missing) is fine.
    "identity check": {"argv": ["identity", "check", "--workspace", "{ws}"], "codes": (1, 3)},  # no seal: 3
    "identity profile": {"argv": ["identity", "profile", "--workspace", "{ws}", "--dry-run"], "codes": (0, 3)},
    # The wrapper starts the job as a child Python (unsealed fixture: the identity check refuses, outcome not ok: 1).
    "run weekly": {"argv": ["run", "weekly", "--workspace", "{ws}", "--unscheduled"], "codes": (1,),
                   "binaries": {Path(sys.executable).name}},
    # Lists the folder names under the root for the stagger order; opens nothing inside fy.
    "schedule generate": {"argv": ["schedule", "generate", "--workspace", "{ws}", "--dry-run"], "codes": (0,)},
    "week assemble": {"argv": ["week", "assemble", "--workspace", "{ws}", "--week", "2026-W44"], "codes": (0,)},
    # No week file in the fixture: could not run (3).
    "week report": {"argv": ["week", "report", "--workspace", "{ws}", "--week", "2026-W44"], "codes": (3,)},
    # No week file in the fixture: could not run (3).
    "shadow compare": {"argv": ["shadow", "compare", "--workspace", "{ws}", "--week", "2026-W44"], "codes": (3,)},
    "lock run": {"argv": ["lock", "run", "--browser", "--", sys.executable, "-c", "pass"], "exempt": True},
    "schedule watchdog": {"argv": ["schedule", "watchdog", "--dry-run"], "exempt": True},
    # Operator-level and read-only by design: lists the root and reads every workspace's config, outcomes and records.
    "watchdog": {"argv": ["watchdog", "--dry-run"], "exempt": True, "codes": (0, 1), "reads_all": True},
    # No alerts.toml in the harness: 3 before any Keychain write.
    "alerts password": {"argv": ["alerts", "password"], "exempt": True, "codes": (3,), "stdin": "x"},
    # Ads (v2 3.3): the fixture has no Sheet ID and no sheet tables for the week, so every command stops at 3.
    "ads script install": {"argv": ["ads", "script", "install", "--workspace", "{ws}", "--dry-run"], "codes": (3,)},
    "ads tracking": {"argv": ["ads", "tracking", "--workspace", "{ws}", "--week", "2026-W44"], "codes": (3,)},
    "ads audit": {"argv": ["ads", "audit", "--workspace", "{ws}", "--week", "2026-W44"], "codes": (3,)},
    "actions propose": {"argv": ["actions", "propose", "--workspace", "{ws}", "--week", "2026-W44"], "codes": (3,)},
    "actions approve": {"argv": ["actions", "approve", "--workspace", "{ws}", "--batch", "20260101T000000Z-fx-dead",
                                 "--items", "1", "--by", "Op"], "codes": (3,)},
    "actions apply": {"argv": ["actions", "apply", "--workspace", "{ws}", "--batch", "20260101T000000Z-fx-dead",
                               "--dry-run"], "codes": (3,)},
    "actions status": {"argv": ["actions", "status", "--workspace", "{ws}", "--week", "2026-W44"], "codes": (3,)},
    # Site and AI visibility (v2 3.2): the fixture host does not resolve under the fake network, so the audit fails (1).
    "site audit": {"argv": ["site", "audit", "--workspace", "{ws}"], "codes": (0, 1)},
    "site indexnow": {"argv": ["site", "indexnow", "--workspace", "{ws}", "https://example.invalid/x/"], "codes": (3,)},
    "wp schema-deploy": {"argv": ["wp", "schema-deploy", "--workspace", "{ws}", "--dry-run"], "codes": (0,)},
    "ai-visibility prompts": {"argv": ["ai-visibility", "prompts", "--workspace", "{ws}"], "codes": (0,)},
    "ai-visibility run": {"argv": ["ai-visibility", "run", "--workspace", "{ws}"], "codes": (3,)},
    # No alerts.toml in the harness: not configured (3).
    "heartbeat": {"argv": ["heartbeat", "--dry-run"], "exempt": True, "codes": (3,)},
    # No week file in the fixture: could not run (3). Nothing is written before the check.
    "report monthly": {"argv": ["report", "monthly", "--workspace", "{ws}", "--month", "2026-11"], "codes": (3,)},
}


def test_every_command_is_registered():
    assert set(COMMANDS) == cli.command_ids()


# ---------------------------------------------------------------- harness

class Tree:
    """A temp workspaces root with fresh copies of fx and fy."""

    def __init__(self, base: Path):
        self.base = base
        self.root = base / "mseo-workspaces"
        self.root.mkdir()
        for slug in ("fx", "fy"):
            shutil.copytree(SYNTHETIC / slug, self.root / slug)
        self.log = base / "audit.log"
        self.keychain = base / "keychain.json"
        self.keychain.write_text(json.dumps({"mseo.fx.wordpress|fx": WP_PAYLOAD}))
        self.deny = base / "operator-deny-read.txt"
        self.deny.write_text("# synthetic\n" + str(base / "legacy-data") + "\n")

    def run(self, argv: list[str], cwd: Path, stdin: str = "") -> subprocess.CompletedProcess:
        self.log.write_text("")
        env = {k: v for k, v in os.environ.items() if not k.startswith("MSEO_")}
        env.update(PYTHONPATH=str(HOOK_DIR), MSEO_WORKSPACES=str(self.root), MSEO_AUDIT_LOG=str(self.log),
                   MSEO_FAKE_KEYCHAIN=str(self.keychain), MSEO_FAKE_NET="1", MSEO_TODAY="2026-11-04", PYTHONDONTWRITEBYTECODE="1",
                   MSEO_STATE_DIR=str(self.base / "state"), MSEO_LAUNCH_AGENTS=str(self.base / "LaunchAgents"),
                   MSEO_OPERATOR_DENY=str(self.deny),
                   MSEO_ALERTS_CONFIG=str(self.base / "alerts.toml"))
        return subprocess.run([sys.executable, "-m", "mseo", *argv], cwd=cwd, env=env, input=stdin,
                              capture_output=True, text=True, timeout=120)

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def touched(self, slug: str) -> list[str]:
        """Logged paths (resolved against the process cwd) that fall inside workspace `slug`."""
        target = (self.root / slug).resolve()
        out = []
        for e in self.events():
            for key in ("path", "dest"):
                if key in e:
                    p = Path(e[key])
                    p = (Path(e["cwd"]) / p) if not p.is_absolute() else p
                    p = Path(os.path.normpath(p))
                    if p == target or target in p.parents or self.root / slug == p or (self.root / slug) in p.parents:
                        out.append(f"{e['event']} {p}")
            if e["event"] == "subprocess.Popen" and any(str(target) in a or str(self.root / slug) in a for a in e["argv"]):
                out.append(f"Popen {e['argv']}")
        return out

    def binaries(self) -> set[str]:
        return {Path(e["executable"]).name for e in self.events() if e["event"] == "subprocess.Popen"}

    def snapshot(self) -> dict:
        out = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return out


@pytest.fixture
def tree(tmp_path, monkeypatch):
    t = Tree(tmp_path)
    # fx carries a current seal: publish and the wp writes refuse to run without one (audit L1)
    monkeypatch.setenv("MSEO_WORKSPACES", str(t.root))
    monkeypatch.setenv("MSEO_OPERATOR_DENY", str(t.deny))
    seal_for_tests(t.root, ("fx",))
    yield t
    for slug in ("fx", "fy"):
        (t.root / slug).chmod(0o755)


def argv_for(case: dict, slug: str) -> list[str]:
    return [a.format(ws=slug, draft="drafts/good_article.md") for a in case["argv"]]


def without_workspace(argv: list[str]) -> list[str]:
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == "--workspace":
            skip = True
            continue
        out.append(a)
    return out


WORKSPACE_CASES = sorted(k for k, v in COMMANDS.items() if not v.get("exempt"))


@pytest.mark.parametrize("command", WORKSPACE_CASES)
def test_a_fx_run_never_touches_fy(tree, command):
    case = COMMANDS[command]
    (tree.root / "fy").chmod(0)
    try:
        proc = tree.run(argv_for(case, "fx"), cwd=tree.root / "fx", stdin=case.get("stdin", ""))
    finally:
        (tree.root / "fy").chmod(0o755)
    assert proc.returncode in case.get("codes", (0, 1, 2)), proc.stderr
    assert tree.events(), "the audit hook logged nothing; the harness is broken"
    allowed = re.compile(case["fy_allowed"]) if case.get("fy_allowed") else None
    assert [t for t in tree.touched("fy") if not (allowed and allowed.match(t))] == []
    assert tree.binaries() <= ALLOWED_BINARIES | case.get("binaries", set())


@pytest.mark.parametrize("command", WORKSPACE_CASES)
def test_b_cwd_in_fy_with_workspace_fx_exits_3_and_writes_nothing(tree, command):
    case = COMMANDS[command]
    before = tree.snapshot()
    proc = tree.run(argv_for(case, "fx"), cwd=tree.root / "fy" / "data", stdin=case.get("stdin", ""))
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "workspace_mismatch" in proc.stderr
    assert tree.snapshot() == before


@pytest.mark.parametrize("command", WORKSPACE_CASES)
def test_c_no_workspace_exits_3(tree, command):
    case = COMMANDS[command]
    before = tree.snapshot()
    proc = tree.run(without_workspace(argv_for(case, "fx")), cwd=tree.base, stdin=case.get("stdin", ""))
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert tree.snapshot() == before


def test_harness_sees_a_read_of_fy(tree):
    """Proves the hook would catch a leak: a deliberate read of fy shows up in touched()."""
    tree.log.write_text("")
    env = {**os.environ, "PYTHONPATH": str(HOOK_DIR), "MSEO_AUDIT_LOG": str(tree.log)}
    subprocess.run([sys.executable, "-c", f"open({str(tree.root / 'fy' / 'customer.json')!r}).read()"], env=env, check=True)
    assert tree.touched("fy")


def test_lint_reads_both_workspaces_but_writes_nothing(tree):
    """Exempt from --workspace by design: it reads every workspace's identifying values and writes nothing."""
    before = tree.snapshot()
    proc = tree.run(["lint", "customer-strings"], cwd=tree.base)
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    assert tree.snapshot() == before
    assert tree.binaries() <= ALLOWED_BINARIES
    writes = [e for e in tree.events() if e["event"] in ("os.rename", "os.replace", "os.remove", "os.mkdir")]
    assert writes == []


def test_workspace_new_creates_only_its_folder_and_reseals_the_others(tree):
    """Exempt from --workspace: it creates the workspace. In the others it may write only .claude/ and .mseo/."""
    before = tree.snapshot()
    proc = tree.run(["workspace", "new", "fz", "--skip-live-test"], cwd=tree.base)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    after = tree.snapshot()
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    for path in changed:
        rel = Path(path).relative_to(tree.root)
        assert rel.parts[0] == "fz" or rel.parts[1] in (".claude", ".mseo"), rel
    assert (tree.root / "fz" / ".git").is_dir()
    assert tree.binaries() <= ALLOWED_BINARIES | {"uv"}


@pytest.mark.parametrize("command", ["lock run", "schedule watchdog", "watchdog", "alerts password"])
def test_operator_level_commands_write_nothing_in_any_workspace(tree, command):
    """Exempt from --workspace: the browser lock, the watchdog and its email account belong to the operator."""
    case = COMMANDS[command]
    before = tree.snapshot()
    proc = tree.run(case["argv"], cwd=tree.base, stdin=case.get("stdin", ""))
    assert proc.returncode in case.get("codes", (0,)), proc.stdout + proc.stderr
    assert tree.snapshot() == before
    if not case.get("reads_all"):
        assert tree.touched("fx") == [] and tree.touched("fy") == []
    writes = [e for e in tree.events() if e["event"] in ("os.rename", "os.replace", "os.remove", "os.mkdir")
              and str(tree.root) in e.get("path", "") + e.get("dest", "")]
    assert writes == []
    assert not (tree.base / "LaunchAgents").exists()
