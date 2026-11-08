"""The seal: a workspace's generated Claude Code settings plus the record of what was checked.

`seal(ws)` builds the framework venv, lists the sibling workspaces, writes <ws>/.claude/settings.json (permission
rules and sandbox that deny every sibling, transcripts and earlier credential files), checks the Keychain items,
Chrome profiles and origin remote, runs the live agent test and writes <ws>/.mseo/seal.json.
`check_seal(ws)` recomputes the settings hash and the sibling set. Never edit either file by hand: re-seal.

Claude Code permission rules have no "everything except", so each sibling is named; `workspace new` re-seals all.
Paths are written in both spellings (as configured and symlink-resolved), since ~/Projects may be a link.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mseo import workspace as workspace_mod
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_FAIL, EXIT_OK
from mseo.fsutil import sha256_file, write_json_atomic

SEAL_SCHEMA = "mseo.seal/1"
CHROME_LOCAL_STATE = Path("~/Library/Application Support/Google/Chrome/Local State").expanduser()
NEEDS_KEYCHAIN = {"wordpress": "cms"}  # credential source -> the config section that makes it required
# Rules every sealed workspace carries, beyond its path denies:
# - mcp__*: MCP tools (desktop-commander, shell servers, Drive, Chrome, the app's browser) run outside the sandbox and
#   outside the Read/Edit rules, so a sealed agent gets none of them. Deny wins over any allow in any scope.
# - Keychain: the `security` CLI anywhere in a command, not only as its first word. The rules are a
#   tripwire; the sandbox is the boundary.
# - --approved: the operator's approval flag for publishing. The agent must never supply it itself.
AGENT_DENY_RULES = ["mcp__*", "Bash(security *)", "Bash(/usr/bin/security *)", "Bash(*security *-generic-password*)",
                    "Bash(*security *-internet-password*)", "Bash(*security dump-keychain*)", "Bash(*security export*)",
                    "Bash(*--approved*)",
                    # the Ads approval and apply steps are the operator's; dry runs too
                    "Bash(*actions approve*)", "Bash(*actions apply*)", "Bash(*MSEO_APPROVALS_DIR*)"]

# Folders an agent may read but never write: every record is written by Python after the agent finishes.
# Agents save snapshots and exports to data/inbox/, which Python moves into the run folder.
RECORD_DIRS = ("data/runs", "data/outcomes", "data/weeks")

# The pull settings (.claude/pull-settings.json) are the workspace settings with the blanket mcp__* deny swapped for
# these named Chrome tools. A pull loads only that file (--setting-sources "") and no MCP server but Chrome
# (--strict-mcp-config --chrome), so no other server's tools exist in the session. Tools that move local files to a
# page, broadcast a browser switch, or run page scripts stay denied. Selecting a browser by device ID is allowed,
# because with several connected the extension refuses every action until one is chosen; the sign-in check then
# proves which account that browser is signed in to.
CHROME_PREFIX = "mcp__claude-in-chrome__"
CHROME_ALLOW = tuple(CHROME_PREFIX + t for t in (
    "tabs_context_mcp", "tabs_create_mcp", "tabs_close_mcp", "navigate", "computer", "read_page", "get_page_text",
    "find", "list_connected_browsers", "select_browser"))
CHROME_DENY = tuple(CHROME_PREFIX + t for t in (
    "file_upload", "upload_image", "switch_browser", "javascript_tool", "gif_creator"))
PULL_SETTINGS = "pull-settings.json"
# An empty folder beside the workspaces, denied like any sibling, so a lone workspace still has somewhere for the
# live test to plant its canary. Hidden, so lint and the workspace resolver ignore it.
CANARY_DIR = ".mseo-canary"

# Cloud-synced folders are denied by default: a workspace's data lives inside the workspace. Any other folder
# outside the workspaces root that holds customer data (earlier repos, old exports, credential dotfiles of a retired
# tool) belongs in the operator deny list below, never in framework code.
FRAMEWORK_DENY_READ = ["~/Library/CloudStorage/**"]   # Claude Code permission-rule globs (home-relative)
FRAMEWORK_SANDBOX_DENY_READ = ["~/Library/CloudStorage"]  # sandbox denyRead entries (expanded at seal time)

# The operator's list of folders outside the workspaces root that hold customer data (earlier repos, old exports).
# It lives outside the framework so no customer path is ever in framework code. One path per line, # for comments.
OPERATOR_DENY_DEFAULT = Path("~/.config/mseo/deny-read.txt")


class StaleSeal(workspace_mod.WorkspaceError):
    """The seal is missing or no longer matches the disk. Maps to exit 3."""


def framework_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _framework_raw() -> Path:
    """The framework path as configured (MSEO_HOME, else this checkout), without resolving symlinks."""
    return Path(os.environ.get("MSEO_HOME") or framework_root()).expanduser()


def _raw_root() -> Path:
    return Path(os.environ.get("MSEO_WORKSPACES") or workspace_mod.DEFAULT_ROOT).expanduser()


def operator_deny_file() -> Path:
    return Path(os.environ.get("MSEO_OPERATOR_DENY") or OPERATOR_DENY_DEFAULT).expanduser()


def operator_deny_paths(path: Path | None = None) -> list[Path]:
    """The operator's extra denied folders. A missing file yields [] (the seal records that as a failed check)."""
    try:
        lines = (path or operator_deny_file()).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [Path(line.strip()).expanduser() for line in lines if line.strip() and not line.strip().startswith("#")]


def siblings(slug: str, root: Path | None = None) -> list[str]:
    """Every folder under the workspaces root except this one (valid workspace or not: deny them all)."""
    root = root or workspace_mod.workspaces_root()
    return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name != slug)


def _spellings(*paths: Path) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        for q in (Path(os.path.abspath(p)), Path(p).resolve()):
            if q not in out:
                out.append(q)
    return out


def generate_settings(slug: str, sibs: list[str], root: Path, *, raw_root: Path | None = None,
                      framework: Path | None = None, home: Path | None = None,
                      extra_deny: list[Path] | None = None) -> dict:
    home = home or Path.home()
    roots = _spellings(*([raw_root] if raw_root else []), root)
    frameworks = _spellings(framework or _framework_raw(), *([] if framework else [framework_root()]))
    deny: list[str] = []
    deny_read: list[str] = []
    for sib in sibs:
        for r in roots:
            deny += [f"Read(/{r / sib}/**)", f"Edit(/{r / sib}/**)"]
            deny_read.append(str(r / sib))
    for extra in _spellings(*(extra_deny or [])):
        deny += [f"Read(/{extra}/**)", f"Edit(/{extra}/**)"]
        deny_read.append(str(extra))
    deny += [f"Edit(/{f}/**)" for f in frameworks]
    deny += ["Read(~/.claude/projects/**)", *[f"Read({p})" for p in FRAMEWORK_DENY_READ],
             "Edit(.claude/**)", "Edit(.mseo/**)", *AGENT_DENY_RULES]
    deny += [f"Edit(/{r / slug / d}/**)" for r in roots for d in RECORD_DIRS]
    deny += [f"Edit(/{r / slug / 'customer.json'})" for r in roots]  # expected account IDs
    deny_read += [str(home / ".claude" / "projects"),
                  *[str(Path(p).expanduser()) for p in FRAMEWORK_SANDBOX_DENY_READ]]
    return {
        "permissions": {"defaultMode": "dontAsk", "deny": deny},
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {
                "denyRead": deny_read,
                "allowWrite": [str(r / slug) for r in roots],
                "denyWrite": [str(f) for f in frameworks] + [str(r / slug / d) for r in roots
                                                               for d in (".claude", ".mseo", *RECORD_DIRS)]
                             + [str(r / slug / "customer.json") for r in roots],
            },
        },
    }


def generate_pull_settings(base: dict) -> dict:
    """The workspace settings for an agent pull: the same denies and sandbox, with the one narrow Chrome allow."""
    pull = json.loads(json.dumps(base))
    perms = pull["permissions"]
    perms["deny"] = [r for r in perms["deny"] if r != "mcp__*"] + list(CHROME_DENY)
    # dontAsk refuses a Write that no rule allows, so the agent gets one place to save snapshots: the inbox.
    inboxes = [f"Edit(/{Path(w) / 'data' / 'inbox'}/**)" for w in pull["sandbox"]["filesystem"]["allowWrite"]]
    perms["allow"] = list(CHROME_ALLOW) + inboxes
    return pull


def pull_flags(ws) -> list[str]:
    """The claude -p flags that make a pull load only the sealed pull file and only the Chrome tools."""
    return ["--setting-sources", "", "--settings", str(ws.path(".claude", PULL_SETTINGS)), "--strict-mcp-config",
            "--chrome", "--permission-mode", "dontAsk"]


# ---------------------------------------------------------------- checks

def chrome_profile_exists(name: str, local_state: Path = CHROME_LOCAL_STATE) -> bool:
    """True when Chrome's Local State lists a profile whose folder or display name is `name`."""
    try:
        cache = json.loads(Path(local_state).read_text(encoding="utf-8"))["profile"]["info_cache"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return any(name == folder or name == (info or {}).get("name") for folder, info in cache.items())


def keychain_checks(ws) -> list[dict]:
    out = []
    for source, section in NEEDS_KEYCHAIN.items():
        if not ws.config.get(section):
            continue
        service = f"mseo.{ws.slug}.{source}"
        try:
            ok = ws.keychain.exists(service, ws.slug)
        except Exception as exc:  # noqa: BLE001 - a broken keychain is a failed check, not a crash
            ok, service = False, f"{service} ({type(exc).__name__})"
        out.append({"check": f"Keychain item mseo.{ws.slug}.{source}", "ok": bool(ok), "detail": service})
    return out


def chrome_checks(ws, local_state: Path) -> list[dict]:
    profiles = sorted({ws.identity_for(k)["chrome_profile"] for k, a in ws.config["accounts"].items()
                       if not k.startswith("_") and a.get("enabled", True) and a.get("adapter", "browser") != "api"})
    return [{"check": f"Chrome profile {p}", "ok": chrome_profile_exists(p, local_state), "detail": str(local_state)}
            for p in profiles]


def _origin(folder: Path) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(folder), "remote", "get-url", "origin"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


IDENTIFIER_FIELDS = (("business", "site"), ("cms", "site"))


def _identifiers(config: dict) -> set[str]:
    """Values that must belong to exactly one workspace: account IDs, sites and browser profiles."""
    from mseo.capture.account import normalize

    out: set[str] = set()
    for key, account in (config.get("accounts") or {}).items():
        if key.startswith("_") or not isinstance(account, Mapping):
            continue
        if account.get("id"):
            out.add(f"{key} id {normalize(str(account['id']))}")
        if account.get("scripts_sheet_id"):  # two customers must never read the same report Sheet (audit v2 L4)
            out.add(f"{key} sheet {account['scripts_sheet_id']}")

    ident = config.get("identity") or {}
    if isinstance(ident, Mapping) and not ident.get("allow_shared"):
        if ident.get("email"):
            out.add(f"identity {str(ident['email']).lower()}")
        if ident.get("chrome_profile"):
            out.add(f"chrome profile {ident['chrome_profile']}")
    for section, name in IDENTIFIER_FIELDS:
        value = (config.get(section) or {}).get(name)
        if value:
            out.add(f"site {str(value).rstrip('/').lower()}")
    return out


def _sibling_configs(ws, sibs: list[str]):
    """[(slug, config)] for every sibling with a readable customer.json, plus the ones that could not be read."""
    configs, unread = [], []
    for sib in sibs:
        try:
            configs.append((sib, json.loads((ws.root.parent / sib / "customer.json").read_text(encoding="utf-8"))))
        except FileNotFoundError:
            continue  # not a workspace (lint customer-strings reports it as unprotected)
        except (OSError, ValueError) as exc:
            unread.append(f"{sib} ({type(exc).__name__})")
    return configs, unread


def identity_clashes(ws, sibs: list[str] | None = None) -> list[str]:
    """Siblings whose identity shares this workspace's Chrome profile or email . A shared Chrome profile is
    always a clash (one browser session for two customers); a shared email only when this identity is not marked
    allow_shared. Read from the siblings' customer.json files, which doctor never writes."""
    sibs = siblings(ws.slug, ws.root.parent) if sibs is None else sibs
    mine = ws.config.get("identity") or {}
    out = []
    for sib, other in _sibling_configs(ws, sibs)[0]:
        ident = other.get("identity") or {}
        if not isinstance(ident, Mapping):
            continue
        if mine.get("chrome_profile") and ident.get("chrome_profile") == mine["chrome_profile"]:
            out.append(f"{sib}: chrome profile {mine['chrome_profile']}")
        if (mine.get("email") and not mine.get("allow_shared")
                and str(ident.get("email", "")).lower() == str(mine["email"]).lower()):
            out.append(f"{sib}: identity email {str(mine['email']).lower()}")
    return out


def identifiers_check(ws, sibs: list[str]) -> dict:
    mine = _identifiers(ws.config)
    configs, unread = _sibling_configs(ws, sibs)
    clashes = [f"{sib}: {v}" for sib, other in configs for v in sorted(mine & _identifiers(other))]
    detail = "; ".join(clashes + [f"could not read {u}" for u in unread]) or f"{len(mine)} identifier(s), none shared"
    return {"check": "identifiers unique across workspaces", "ok": not clashes and not unread, "detail": detail}


def operator_deny_check() -> dict:
    path = operator_deny_file()
    if not path.exists():
        return {"check": "operator deny list", "ok": False,
                "detail": f"{path} missing: list every folder outside the workspaces root that holds customer data"}
    return {"check": "operator deny list", "ok": True, "detail": f"{len(operator_deny_paths(path))} path(s) from {path}"}


def framework_origin() -> str | None:
    return _origin(framework_root())


def origin_check(ws, sibs: list[str]) -> dict:
    mine = _origin(ws.root)
    if mine is None:
        return {"check": "origin remote unique", "ok": True, "detail": "no origin yet (nothing pushed)"}
    clashes = [s for s in sibs if _origin(ws.root.parent / s) == mine]
    if mine == framework_origin():
        clashes.append("the framework repo")
    return {"check": "origin remote unique", "ok": not clashes,
            "detail": f"{mine} also used by {', '.join(clashes)}" if clashes else mine}


def uv_sync() -> None:
    """Build the framework venv before the settings make the framework read-only to the agent."""
    home = _framework_raw()
    if not (home / "pyproject.toml").is_file():
        raise StaleSeal(f"no pyproject.toml at {home}: seal runs from the framework checkout (bin/mseo workspace seal ...) "
                        "or with MSEO_HOME set to it; the installed tool cannot build the venv")
    proc = subprocess.run(["uv", "sync", "--locked", "--project", str(home)], capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise StaleSeal(f"uv sync --locked failed: {proc.stderr.strip()[-300:]}")


# ---------------------------------------------------------------- seal

@dataclass
class SealResult:
    code: int
    record: dict
    messages: list[str] = field(default_factory=list)


def seal(ws, *, live_test: Callable | None = None, pull_test: Callable | None = None,
         sync: Callable | None = uv_sync, chrome_state: Path = CHROME_LOCAL_STATE) -> SealResult:
    if sync:
        sync()
    root = ws.root.parent
    (root / CANARY_DIR).mkdir(exist_ok=True)
    sibs = siblings(ws.slug, root)
    raw = _raw_root()
    settings = generate_settings(ws.slug, sibs, root, raw_root=raw if raw.exists() else None,
                                 extra_deny=operator_deny_paths())
    settings_path = ws.path(".claude", "settings.json")
    write_json_atomic(settings_path, settings)
    pull_path = ws.path(".claude", PULL_SETTINGS)
    write_json_atomic(pull_path, generate_pull_settings(settings))
    checks = keychain_checks(ws) + chrome_checks(ws, chrome_state) + [origin_check(ws, sibs), identifiers_check(ws, sibs),
                                                                      operator_deny_check()]
    live = live_test(ws, sibs) if live_test else {"status": "skipped", "rows": []}
    pull = pull_test(ws, sibs) if pull_test else {"status": "skipped", "rows": []}
    record = {"schema": SEAL_SCHEMA, "slug": ws.slug, "sealed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
              "siblings": sibs, "settings_sha256": sha256_file(settings_path),
              "pull_settings_sha256": sha256_file(pull_path),
              "operator_deny_sha256": sha256_file(operator_deny_file()), **trust_hashes(ws), "checks": checks,
              "live_test": live, "pull_test": pull}
    if live["status"] != "inconclusive":  # a skipped attempt proves nothing: keep the previous seal on disk
        write_json_atomic(ws.path(".mseo", "seal.json"), record)
    msgs = [f"{'ok ' if c['ok'] else 'NO '} {c['check']} ({c['detail']})" for c in checks]
    msgs.append(f"live agent test: {live['status']}")
    msgs += [f"  row {r['row']}: {'ok' if r.get('ok') else 'FAILED'} {r.get('why', '')}".rstrip() for r in live.get("rows", [])]
    msgs.append(f"pull agent test: {pull['status']}")
    msgs += [f"  row {r['row']}: {'ok' if r.get('ok') else 'FAILED'} {r.get('why', '')}".rstrip() for r in pull.get("rows", [])]
    if pull["status"] == "fail":
        msgs.append("the pull settings let an agent write a record or load a non-Chrome MCP tool: fix before any pull")
        return SealResult(EXIT_COULD_NOT_RUN, record, msgs)
    if live["status"] == "inconclusive":
        msgs.append("the agent skipped a denied-action row, so nothing was proved either way: the previous seal is kept, "
                    "rerun the seal")
        return SealResult(EXIT_COULD_NOT_RUN, record, msgs)
    if live["status"] == "fail":
        msgs.append("a denied action succeeded: the agent layer must move to Alternative H (one macOS user per workspace) "
                    "before another customer onboards")
        return SealResult(EXIT_COULD_NOT_RUN, record, msgs)
    if live["status"] not in ("pass", "skipped", "incomplete"):
        msgs.append(f"live agent test could not run: {live.get('why')}")
        return SealResult(EXIT_COULD_NOT_RUN, record, msgs)
    code = EXIT_OK if all(c["ok"] for c in checks) and live["status"] == "pass" else EXIT_FAIL
    msgs.append(f"sealed {ws.slug}: {len(sibs)} sibling(s) denied")
    return SealResult(code, record, msgs)


SECURITY_CHECKS = ("identifiers unique across workspaces", "operator deny list")  # a missing credential is not one
# The trust anchors an agent could edit: the seal records their hashes, so a changed customer.json (expected account
# IDs, site, identity) or facts.json (claims, hours) makes the seal stale until the operator re-seals.


def trust_hashes(ws) -> dict:
    return {"customer_sha256": sha256_file(ws.path("customer.json")), "facts_sha256": sha256_file(ws.file("facts"))}


def check_seal(ws) -> list[str]:
    """Problems with the seal, or [] when it is current."""
    try:
        record = json.loads(ws.path(".mseo", "seal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [f"no seal: run mseo workspace seal --workspace {ws.slug}"]
    problems = []
    if record.get("settings_sha256") != sha256_file(ws.path(".claude", "settings.json")):
        problems.append(".claude/settings.json changed since the seal (or is missing): re-seal")
    if record.get("pull_settings_sha256") != sha256_file(ws.path(".claude", PULL_SETTINGS)):
        problems.append(f".claude/{PULL_SETTINGS} changed since the seal (or is missing): re-seal")
    if not operator_deny_file().is_file():
        problems.append(f"operator deny list {operator_deny_file()} is missing: restore it, then re-seal")
    elif record.get("operator_deny_sha256") != sha256_file(operator_deny_file()):
        problems.append(f"operator deny list {operator_deny_file()} changed since the seal: re-seal")
    for key, value in trust_hashes(ws).items():
        name = "customer.json" if key == "customer_sha256" else ws.config["files"]["facts"]
        if key not in record:
            problems.append(f"the seal predates the {name} hash: re-seal")
        elif record[key] != value:
            problems.append(f"{name} changed since the seal (or is missing): review the change, then re-seal")
    for c in record.get("checks") or []:
        if c.get("check") in SECURITY_CHECKS and not c.get("ok"):
            problems.append(f"the seal's check failed: {c['check']} ({c.get('detail', '')}): fix it, then re-seal")
    current = siblings(ws.slug, ws.root.parent)
    if record.get("siblings") != current:
        problems.append(f"sibling set changed ({record.get('siblings')} -> {current}): re-seal")
    if (record.get("live_test") or {}).get("status") != "pass":
        problems.append(f"live agent test did not pass ({(record.get('live_test') or {}).get('status')})")
    return problems


def require_current(ws) -> None:
    problems = check_seal(ws)
    if problems:
        raise StaleSeal("stale seal: " + "; ".join(problems))


def require_pull_ready(ws) -> None:
    """A pull needs a current seal whose pull test passed (seal --pull-test), not only the live test."""
    require_current(ws)
    record = json.loads(ws.path(".mseo", "seal.json").read_text(encoding="utf-8"))
    status = (record.get("pull_test") or {}).get("status")
    if status != "pass":
        raise StaleSeal(f"pull test did not pass ({status}): run mseo workspace seal --workspace {ws.slug} --pull-test")


def reseal_all(*, exclude: str | None = None, keychain=None, **kw) -> dict:
    """Re-seal every workspace except `exclude`. A workspace that cannot open is reported, not skipped silently."""
    out = {}
    root = workspace_mod.workspaces_root()
    for slug in siblings(exclude or "", root):
        if slug == CANARY_DIR:
            continue
        try:
            ws = workspace_mod.open(slug, cwd=root, keychain=keychain)
            res = seal(ws, **kw)
            out[slug] = {"code": res.code, "live_test": res.record["live_test"]["status"]}
        except workspace_mod.WorkspaceError as exc:
            out[slug] = {"code": EXIT_COULD_NOT_RUN, "error": str(exc)}
    return out
