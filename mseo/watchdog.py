"""`mseo watchdog`: plain Python, twice a day, reads only each workspace's customer.json,
`data/outcomes/` and `data/runs/*/record.json`, and emails the operator over SMTP.

A finding is {workspace, condition, source, detail, alert}. Its key is (workspace, condition, source): emailed the
first time, then at most once a day while it stays; when a pass no longer finds it, a "cleared" line goes into the
next daily summary. The summary goes once a day, from the first pass at or after 09:00 operator time, even when
nothing is wrong: its absence is the Mac-off signal.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import smtplib
import ssl
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from mseo import workspace as workspace_mod
from mseo.fsutil import write_json_atomic
from mseo.run import outcome as outcome_mod
from mseo.run import record as record_mod
from mseo.runner import lock, schedule, weekly

NOT_RUN_AFTER = dt.timedelta(hours=6)
LATE_AFTER = dt.timedelta(hours=1)
STALE_AFTER = dt.timedelta(days=8)
RESEND_AFTER = dt.timedelta(days=1)
SUMMARY_AT = dt.time(9, 0)
BAD_OUTCOMES = ("refused", "killed", "failed", "partial")
BAD_RECORDS = ("auth_failed", "wrong_account", "unverified_account", "layout_changed", "budget_exceeded", "error")
SUMMARY_ONLY = ("late", "shared_identity")
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
KEYCHAIN_SERVICE = "mseo.alerts.smtp"
HEARTBEAT_TIMEOUT_S = 10
MEASURED_WITHIN = dt.timedelta(days=7)


class AlertsNotConfigured(Exception):
    pass


class _NoKeychain:
    """The watchdog opens workspaces without any credential store: a pull credential is never in reach."""

    def __getattr__(self, name):
        raise AttributeError("the watchdog holds no customer credentials")


# ------------------------------------------------------------------ config and state

def config_path() -> Path:
    return Path(os.environ.get("MSEO_ALERTS_CONFIG") or "~/.config/mseo/alerts.toml").expanduser()


def load_config() -> dict:
    path = config_path()
    try:
        cfg = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AlertsNotConfigured(f"{path} is missing; it needs `to` and `from` addresses") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AlertsNotConfigured(f"{path} is not valid TOML ({exc})") from exc
    for key in ("to", "from"):
        if not cfg.get(key):
            raise AlertsNotConfigured(f"{path} has no `{key}` address")
    return cfg


def operator_tz() -> dt.tzinfo:
    try:
        name = load_config().get("timezone")
    except AlertsNotConfigured:
        name = None
    return ZoneInfo(name) if name else dt.datetime.now().astimezone().tzinfo


def _state_path() -> Path:
    return lock.state_dir() / "alerts.json"


def load_state() -> dict:
    empty = {"keys": {}, "cleared": [], "summary_sent": None}
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not (isinstance(state, dict) and isinstance(state.get("keys", {}), dict)
            and isinstance(state.get("cleared", []), list)):
        return empty  # a damaged state file must never stop the alerts
    return {**empty, **state}


def save_state(state: dict) -> None:
    _state_path().parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(_state_path(), state)


# ------------------------------------------------------------------ the table

def scheduled_sources(ws) -> list[str]:
    return [s for s in weekly.ORDER if s in ws.enabled_sources()]


def _is_scheduled(slug: str) -> bool:
    return (schedule.agents_dir() / f"{schedule.label(slug)}.plist").is_file()


def _ts(value) -> dt.datetime | None:
    """An aware time, or None for anything else (an impossible date, a number, a naive time): the watchdog must
    keep alerting whatever a file holds."""
    if not isinstance(value, str) or not value:
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def _outcomes(ws) -> list[dict]:
    folder = ws.path("data", "outcomes")
    out = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            out.append(outcome_mod.load(path))
        except (OSError, ValueError, record_mod.RecordInvalid):
            continue
    return sorted(out, key=lambda o: o["started"])


def _records(ws) -> list[dict]:
    folder = ws.path("data", "runs")
    out = []
    for path in sorted(folder.glob("*/record.json")) if folder.is_dir() else []:
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        try:  # a damaged record is skipped, never fatal: the watchdog must keep alerting
            record_mod.validate(rec)
        except (record_mod.RecordInvalid, AttributeError, TypeError):
            continue
        out.append(rec)
    return sorted(out, key=lambda r: r["started"])


def _finding(ws, condition: str, source: str | None, detail: str) -> dict:
    return {"workspace": ws.slug, "condition": condition, "source": source, "detail": detail,
            "alert": condition not in SUMMARY_ONLY}


def scan_workspace(ws, now: dt.datetime, *, scheduled: bool) -> list[dict]:
    found: list[dict] = []
    outs = _outcomes(ws)
    latest = outs[-1] if outs else None
    if scheduled:
        slot = schedule.last_slot(ws, now)
        ran = [o for o in outs if (_ts(o["started"]) or now) >= slot - LATE_AFTER]
        if not ran and now - slot >= NOT_RUN_AFTER:
            found.append(_finding(ws, "not_run", None, f"no outcome for the job scheduled {slot.isoformat()}"))
    if latest:
        status = latest["status"]
        if status == "not_run":
            found.append(_finding(ws, "not_run", None, latest.get("error") or "the job did not run"))
        elif status in BAD_OUTCOMES:
            found.append(_finding(ws, status, None, f"{latest['job_id']}: {latest.get('error') or status}"))
        sched, started = _ts(latest.get("scheduled_for")), _ts(latest["started"])
        if sched and started and started - sched > LATE_AFTER:
            found.append(_finding(ws, "late", None, f"{latest['job_id']} started {started - sched} after its slot"))
        if latest.get("identity_shared"):
            found.append(_finding(ws, "shared_identity", None, f"{latest['job_id']} ran on a shared identity"))
    recs = _records(ws)
    by_source: dict[str, dict] = {}
    for rec in recs:
        by_source[rec["source"]] = rec
    for source, rec in sorted(by_source.items()):
        if rec["status"] in BAD_RECORDS:
            found.append(_finding(ws, rec["status"], source, f"{rec['run_id']}: {rec.get('error') or rec['status']}"))
    if scheduled:
        cutoff = (now - STALE_AFTER).date().isoformat()
        for source in scheduled_sources(ws):
            ok = [r for r in recs if r["source"] == source and r["status"] == "ok"
                  and (r.get("period") or {}).get("end", "") >= cutoff]
            if not ok:
                found.append(_finding(ws, "stale", source, f"no ok record ending on or after {cutoff}"))
    return found


def scan(now: dt.datetime) -> list[dict]:
    root = workspace_mod.workspaces_root()
    found: list[dict] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        try:
            ws = workspace_mod.open(folder.name, cwd=root, keychain=_NoKeychain())
        except workspace_mod.WorkspaceError as exc:
            found.append({"workspace": folder.name, "condition": "unreadable", "source": None,
                          "detail": str(exc), "alert": True})
            continue
        try:  # one workspace's bad files must never silence the others' alerts
            found.extend(scan_workspace(ws, now, scheduled=_is_scheduled(ws.slug)))
        except Exception as exc:  # noqa: BLE001 - reported as a finding, not swallowed
            found.append({"workspace": folder.name, "condition": "unreadable", "source": None,
                          "detail": f"the scan failed: {type(exc).__name__}: {exc}", "alert": True})
    return found


# ------------------------------------------------------------------ alert keys, summary, email

def key(f: dict) -> str:
    return f"{f['workspace']}|{f['condition']}|{f['source'] or '-'}"


def _line(f: dict) -> str:
    return f"{f['workspace']}: {f['condition']}" + (f" ({f['source']})" if f["source"] else "") + f": {f['detail']}"


def plan(findings: list[dict], state: dict, now: dt.datetime, tz: dt.tzinfo) -> dict:
    keys = dict(state.get("keys") or {})
    alerting = {key(f): f for f in findings if f["alert"]}
    send = []
    for k, f in alerting.items():
        prior = keys.get(k) if isinstance(keys.get(k), dict) else {}
        last = _ts(prior.get("last_sent"))
        if last is None or now - last >= RESEND_AFTER:
            send.append(f)
            keys[k] = {"first": prior.get("first") or now.isoformat(), "last_sent": now.isoformat()}
    cleared = list(state.get("cleared") or []) + sorted(k for k in keys if k not in alerting)
    keys = {k: v for k, v in keys.items() if k in alerting}
    local = now.astimezone(tz)
    summary = local.time() >= SUMMARY_AT and state.get("summary_sent") != local.date().isoformat()
    new_state = {"keys": keys, "cleared": [] if summary else cleared,
                 "summary_sent": local.date().isoformat() if summary else state.get("summary_sent")}
    return {"send": send, "cleared": cleared, "summary": summary, "state": new_state}


def _message(cfg: dict, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["from"], cfg["to"], " ".join(subject.split())
    msg.set_content(body)
    return msg


def compose_alert(cfg: dict, findings: list[dict]) -> EmailMessage:
    slugs = sorted({f["workspace"] for f in findings})
    return _message(cfg, f"mseo ALERT: {', '.join(slugs)}", "\n".join(_line(f) for f in findings) + "\n")


def compose_summary(cfg: dict, findings: list[dict], cleared: list[str], day: dt.date) -> EmailMessage:
    parts = [f"mseo watchdog, {day.isoformat()}"]
    open_ = [f for f in findings if f["alert"]]
    parts.append("Open:\n" + ("\n".join(_line(f) for f in open_) if open_ else "nothing wrong"))
    notes = [f for f in findings if not f["alert"]]
    if notes:
        parts.append("Notes:\n" + "\n".join(_line(f) for f in notes))
    if cleared:
        parts.append("cleared since the last summary:\n" + "\n".join(k.replace("|", " ") for k in cleared))
    parts += weekly_sections(day)
    return _message(cfg, f"mseo daily summary {day.isoformat()}", "\n\n".join(parts) + "\n")


# ------------------------------------------------------------------ weekly sections of the summary

def last_complete_week(day: dt.date) -> str:
    """The ISO week before the one holding `day`: the last week whose data can be complete."""
    year, week, _ = (day - dt.timedelta(days=7)).isocalendar()
    return f"{year}-W{week:02d}"


def _read_json(path: Path):
    """The file's JSON, or None: the summary tolerates every missing or damaged file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _statuses(data) -> list[str]:
    """The status values of a check list, whatever the file calls the list (checks, findings, or the file itself)."""
    items = data
    if isinstance(data, dict):
        items = next((data[k] for k in ("checks", "findings", "items", "rows") if isinstance(data.get(k), list)), [])
    return [str(i.get("status", "")) for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _within(value, day: dt.date, span: dt.timedelta) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        when = dt.date.fromisoformat(value[:10])
    except ValueError:
        return False
    return day - span <= when <= day


def _measured_at(entry: dict):
    m = entry.get("measured_28d")
    if not isinstance(m, dict):
        return None
    return next((m[k] for k in ("updated", "at", "measured_at", "date") if isinstance(m.get(k), str)), None)


def _draft_slugs_passing(ws) -> set[str]:
    """Slugs of drafts whose <draft>.check.json result is pass."""
    out = set()
    for path in sorted(ws.root.glob("drafts/*.check.json")):
        data = _read_json(path)
        if isinstance(data, dict) and data.get("result") == "pass" and data.get("slug"):
            out.add(str(data["slug"]))
    return out


def workspace_sections(ws, day: dt.date) -> list[str]:
    """One line per section for one workspace, from files that exist for the last complete ISO week. Read-only."""
    week = last_complete_week(day)
    lines: list[str] = []
    shadow = _read_json(ws.path("data", "weeks", f"{week}.shadow.json"))
    if isinstance(shadow, dict):
        lines.append(f"shadow verdict {week}: {shadow.get('status', 'unknown')}")
    wk = _read_json(ws.path("data", "weeks", f"{week}.json"))
    if isinstance(wk, dict) and isinstance(wk.get("sources"), dict):
        lines.append(f"week metrics {week}:")
        for source, entry in sorted(wk["sources"].items()):
            entry = entry if isinstance(entry, dict) else {}
            if entry.get("status") == "ok" and isinstance(entry.get("metrics"), dict):
                metrics = ", ".join(f"{k} {v}" for k, v in entry["metrics"].items() if v is not None)
                lines.append(f"  {source}: {metrics or 'no metrics'}")
            else:
                lines.append(f"  {source}: {entry.get('status', 'not available')}")
    audit = _read_json(ws.path("data", "weeks", f"{week}.ads-audit.json"))
    if audit is not None:
        st = _statuses(audit)
        lines.append(f"Ads audit {week}: {st.count('fail')} fail, {st.count('warn')} warn ({len(st)} checks)")
    batches = ws.path("data", "batches")
    if batches.is_dir():
        open_items = 0
        for path in sorted(batches.glob("*.json")):
            data = _read_json(path)
            items = data.get("items") if isinstance(data, dict) else data
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and (item.get("status") == "proposed"
                                               or ("status" not in item and not item.get("approved"))):
                    open_items += 1
        if open_items:
            lines.append(f"open action batch: {open_items} item(s) proposed and not approved")
    log = _read_json(ws.file("content_log")) if ws.config["files"].get("content_log") else None
    if isinstance(log, dict):
        entries = [e for k in ("articles", "gbp") for e in (log.get(k) or []) if isinstance(e, dict)]
        passing = _draft_slugs_passing(ws)
        drafts = [e for e in entries if e.get("status") == "draft" and e.get("slug") in passing]
        if drafts:
            lines.append("drafts awaiting approval: " + ", ".join(str(e["slug"]) for e in drafts))
        measured = [e for e in entries if _within(_measured_at(e), day, MEASURED_WITHIN)]
        if measured:
            lines.append(f"measured items (last 7 days): {len(measured)}")
    audits = sorted(ws.path("data", "audits").glob("*-site.json")) if ws.path("data", "audits").is_dir() else []
    if audits:
        latest = _read_json(audits[-1])
        counts = (latest or {}).get("counts") if isinstance(latest, dict) else None
        if isinstance(counts, dict):
            lines.append(f"site audit {audits[-1].name[:10]}: {counts.get('fail', 0)} fail, {counts.get('warn', 0)} warn")
    return lines


def weekly_sections(day: dt.date) -> list[str]:
    """The weekly sections for every workspace under the root, each led by its slug. Nothing here can raise."""
    try:
        root = workspace_mod.workspaces_root()
        folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
    parts: list[str] = []
    for folder in folders:
        try:
            ws = workspace_mod.open(folder.name, cwd=root, keychain=_NoKeychain())
            lines = workspace_sections(ws, day)
        except Exception:  # noqa: BLE001 - an unreadable workspace is already a finding; the summary still goes
            continue
        if lines:
            parts.append(f"{ws.slug}, week {last_complete_week(day)}:\n" + "\n".join(lines))
    return parts


# ------------------------------------------------------------------ heartbeat

def urllib_fetch(url: str, timeout: int = HEARTBEAT_TIMEOUT_S) -> int:
    """GET `url`; the HTTP status. Any failure raises (the caller turns it into a logged line)."""
    req = urllib.request.Request(url, headers={"User-Agent": "mseo watchdog heartbeat"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
        return resp.status


def heartbeat_host(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or "(no host)"


def heartbeat(cfg: dict, *, fetcher=urllib_fetch, dry_run: bool = False) -> dict:
    """Ping the dead-man's switch named by `heartbeat_url` in the alerts config. Never raises; the full URL (often a
    token) is kept out of the status line: only its host is named."""
    url = cfg.get("heartbeat_url")
    if not url:
        return {"status": "not configured", "host": None}
    host = heartbeat_host(str(url))
    if dry_run:
        return {"status": "dry-run", "host": host}
    try:
        code = fetcher(str(url), HEARTBEAT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - a failed ping is logged, never fatal
        detail = str(exc).replace(str(url), "<heartbeat url>")  # a malformed URL error would echo the token
        return {"status": "failed", "host": host, "error": f"{type(exc).__name__}: {detail}"[:200]}
    ok = isinstance(code, int) and 200 <= code < 300
    return {"status": "ok" if ok else "failed", "host": host, "http": code}


def heartbeat_line(hb: dict) -> str:
    return f"heartbeat {hb['status']}" + (f" ({hb['host']})" if hb.get("host") else "") + \
        (f": {hb['error']}" if hb.get("error") else "")


def smtp_sender(*, keychain=None, cfg: dict | None = None):
    """A callable that sends one EmailMessage over SMTP SSL as the mseo-alerts user."""
    cfg = cfg or load_config()
    if keychain is None:
        from mseo.secrets import Keychain

        keychain = Keychain()
    try:
        password = keychain.get(KEYCHAIN_SERVICE, cfg["from"])["app_password"]
    except Exception as exc:  # noqa: BLE001 - any missing or malformed item means alerts are not set up
        raise AlertsNotConfigured(f"no Keychain item {KEYCHAIN_SERVICE} for {cfg['from']} ({exc}); "
                                  "store the alert user's app password first") from exc

    def send(msg: EmailMessage) -> None:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60, context=ssl.create_default_context()) as smtp:
            smtp.login(cfg["from"], password)
            smtp.send_message(msg)

    return send


def run(now: dt.datetime, *, sender, dry_run: bool = False, fetcher=urllib_fetch) -> dict:
    findings = scan(now)
    tz = operator_tz()
    state = load_state()
    p = plan(findings, state, now, tz)
    result = {"findings": findings, "alerts": p["send"], "summary": p["summary"], "cleared": p["cleared"]}
    if dry_run:
        return result
    cfg = load_config()
    if p["send"]:
        sender(compose_alert(cfg, p["send"]))
    if p["summary"]:
        sender(compose_summary(cfg, findings, p["cleared"], now.astimezone(tz).date()))
    save_state(p["state"])
    result["heartbeat"] = heartbeat(cfg, fetcher=fetcher)  # after a successful run: the switch stays quiet otherwise
    return result


# ------------------------------------------------------------------ CLI

def cmd_watchdog(args, ctx) -> int:
    fixed = os.environ.get("MSEO_NOW")
    now = dt.datetime.fromisoformat(fixed) if fixed else dt.datetime.now(dt.timezone.utc)
    sender = None if args.dry_run else smtp_sender(keychain=ctx.keychain)
    result = run(now, sender=sender, dry_run=args.dry_run)
    lines = [_line(f) for f in result["findings"]] or ["nothing wrong"]
    if result.get("heartbeat"):
        lines.append(heartbeat_line(result["heartbeat"]))
    ctx.emit({**result, "messages": lines})
    return 1 if any(f["alert"] for f in result["findings"]) else 0


def cmd_heartbeat(args, ctx) -> int:
    """Ping the dead-man's switch by hand, or show (--dry-run) which host would be pinged. Text output never shows
    the full URL; --json may, since the operator asked for the machine form."""
    cfg = load_config()
    hb = heartbeat(cfg, dry_run=args.dry_run)
    if hb["status"] == "not configured":
        ctx.emit({**hb, "messages": [f"heartbeat not configured: set heartbeat_url in {config_path()}"]})
        return 3
    payload = {**hb, "messages": [heartbeat_line(hb)]}
    if getattr(args, "json", False):
        payload["url"] = cfg["heartbeat_url"]
    ctx.emit(payload)
    return 0 if hb["status"] in ("ok", "dry-run") else 1


def cmd_alerts_password(args, ctx) -> int:
    """Store the alert user's app password (read from stdin, never argv) as the Keychain item mseo.alerts.smtp."""
    from mseo.secrets import Keychain

    cfg = load_config()
    password = ctx.stdin.read().strip()
    if not password:
        print("error: pipe the app password on stdin", file=__import__("sys").stderr)
        return 3
    (ctx.keychain or Keychain()).set(KEYCHAIN_SERVICE, cfg["from"], {"app_password": password}, replace=args.replace)
    ctx.emit({"status": "ok", "messages": [f"stored {KEYCHAIN_SERVICE} for {cfg['from']}"]})
    return 0


def register(sub, parents) -> None:
    p = sub.add_parser("watchdog", parents=[parents.json],
                       help="Check every workspace's outcomes and records; email alerts and the daily summary")
    p.add_argument("--dry-run", action="store_true", help="Print findings; send nothing, keep the alert state")
    p.set_defaults(func=cmd_watchdog, command_id="watchdog")
    hb = sub.add_parser("heartbeat", parents=[parents.json],
                        help="Ping the dead-man's switch from the alerts config (heartbeat_url)")
    hb.add_argument("--dry-run", action="store_true", help="Show the host that would be pinged; send nothing")
    hb.set_defaults(func=cmd_heartbeat, command_id="heartbeat")
    a = sub.add_parser("alerts", help="The alert email account")
    asub = a.add_subparsers(dest="alerts_command", required=True, parser_class=type(a))
    pw = asub.add_parser("password", parents=[parents.json],
                         help="Store the mseo-alerts app password (stdin) in the Keychain")
    pw.add_argument("--replace", action="store_true")
    pw.set_defaults(func=cmd_alerts_password, command_id="alerts password")
