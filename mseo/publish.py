"""publish: `mseo publish DRAFT`, the only publish path for articles and Business Profile posts (ported from the earlier house publish script).

    mseo publish drafts/post.md --workspace S --kind article --slug my-post --approved "NAME 2026-01-15" [--dates-confirmed]
    mseo publish drafts/gbp_A.txt --workspace S --kind gbp --approved "NAME 2026-01-15"                    # text + slot
    mseo publish drafts/gbp_A.txt --workspace S --kind gbp --approved "NAME 2026-01-15" --confirm-live URL  # after scheduling

Steps: (1) re-run `content check` and refuse anything but pass, or a missing --approved; (2) show each event date
from the research sidecar with its source link, stop unless --dates-confirmed, then write `rechecked: DATE` into the
research file; (3) log the piece as `publishing` with the checked hash; (4) article: post it to cms.site with the
workspace's Keychain credential, Business Profile: print the text and next slot and exit with status awaiting-live;
(5) fetch the live URL and only then log `published` with it; (6) when `site_audit.indexnow_key` is set, ping IndexNow
with the article URL and record `indexnow: {status, at}` on the entry (a failed ping is a message, never a failure).
A re-run resumes: `publishing` skips to step 4, `published` does nothing.
WordPress gets exactly the bytes that were checked. An existing post with the slug is updated only when its id is the
one this command logged (wp_id). The live URL must be on cms.site and carry the slug (articles), or be a Google URL
(Business Profile, --confirm-live). --dry-run runs steps 1 and 2 (writing only check.json) and prints what 3-5 would do.

Exit codes: 0 done (or awaiting-live, or nothing to do) · 1 refused or a step failed · 3 the check could not run
(including a missing or mismatched WordPress credential).
"""

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

from mseo.cms import wordpress
from mseo.content import check as check_mod
from mseo.content import dupes
from mseo.content import log as log_mod
from mseo.content.links import URL_RE, check_url
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_EXISTS, EXIT_FAIL, EXIT_OK
from mseo.fsutil import write_atomic
from mseo.site_checks import indexnow

GOOGLE_HOSTS = ("business.google.com", "g.page", "maps.app.goo.gl", "google.com")
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def today() -> dt.date:
    return check_mod.today()


def done(code: int, status: str, messages, **extra) -> dict:
    return {"code": code, "status": status, "messages": list(messages), **extra}


def refuse(messages, status="refused", **extra) -> dict:
    return done(EXIT_FAIL, status, messages, **extra)


# ---------------------------------------------------------------- the content log

def find_entry(ws, kind: str, draft_text: str, slug: str):
    """The draft's own log entry: an article by slug; a GBP post by its first line (the log keeps a lower-cased,
    often cut-off copy of it). The newest match wins."""
    import json

    log = json.loads(ws.file("content_log").read_text(encoding="utf-8"))
    if kind == "article":
        return next((a for a in reversed(log.get("articles", [])) if slug and a.get("slug") == slug), None)
    first = dupes.norm(next((l for l in draft_text.splitlines() if l.strip()), ""))
    hits = [g for g in log.get("gbp", []) if len(dupes.norm(g.get("headline", ""))) >= dupes.MIN_HEADLINE_CHARS
            and first.startswith(dupes.norm(g["headline"]))]
    return max(hits, key=lambda g: g.get("date", ""), default=None)


def log_update(ws, kind: str, entry: dict, slug: str, status: str, **fields) -> None:
    key = {"slug": slug} if kind == "article" else {"date": entry["date"], "headline": entry["headline"]}
    log_mod.upsert_entry(ws, kind, {**key, "status": status, **{k: v for k, v in fields.items() if v is not None}})


# ---------------------------------------------------------------- step 2: event dates

def event_sources(research_text: str, dates) -> list:
    """[(date, source URL or None)] from the research file line that names each date."""
    out = []
    for d in dates:
        line = next((l for l in research_text.splitlines() if str(d) in l), "")
        urls = URL_RE.findall(line)
        out.append((str(d), urls[0].rstrip(".,;:)") if urls else None))
    return out


def mark_rechecked(path: Path, dates, on: dt.date) -> None:
    """Add or refresh `rechecked: YYYY-MM-DD` on each event line; temp file then replace."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if any(str(d) in line for d in dates):
            body, nl = line.rstrip("\n"), "\n" if line.endswith("\n") else ""
            if re.search(r"rechecked:\s*\d{4}-\d{2}-\d{2}", body):
                body = re.sub(r"rechecked:\s*\d{4}-\d{2}-\d{2}", f"rechecked: {on.isoformat()}", body)
            else:
                body += f" rechecked: {on.isoformat()}"
            lines[i] = body + nl
    write_atomic(path, "".join(lines))


# ---------------------------------------------------------------- Business Profile slots

def next_slot(slots, tz: str, now: dt.datetime | None = None) -> dt.datetime:
    """The first 'Mon 10:00'-style slot after `now`, in the workspace's timezone."""
    zone = ZoneInfo(tz)
    now = (now or dt.datetime.now(zone)).astimezone(zone)
    found = []
    for slot in slots:
        day, clock = slot.split()
        hour, minute = map(int, clock.split(":"))
        ahead = (DAY_NAMES.index(day[:3].lower()) - now.weekday()) % 7
        when = dt.datetime.combine(now.date() + dt.timedelta(days=ahead), dt.time(hour, minute), zone)
        found.append(when if when > now else when + dt.timedelta(days=7))
    return min(found)


def post_slot(logged_date, slots, tz: str, now: dt.datetime | None = None):
    """(when, note). The logged post date at its slot time when that is a slot day still ahead; otherwise the next
    slot, with a note saying why the logged date was not used (None when no date was logged)."""
    zone = ZoneInfo(tz)
    now = (now or dt.datetime.now(zone)).astimezone(zone)
    fallback = next_slot(slots, tz, now)
    if not logged_date:
        return fallback, None
    try:
        day = dt.date.fromisoformat(str(logged_date))
    except ValueError:
        return fallback, f"logged date {logged_date!r} is not a date; using the next slot"
    for slot in slots:
        name, clock = slot.split()
        if DAY_NAMES.index(name[:3].lower()) == day.weekday():
            hour, minute = map(int, clock.split(":"))
            when = dt.datetime.combine(day, dt.time(hour, minute), zone)
            if when > now:
                return when, None
            return fallback, f"logged date {day} has passed; using the next slot"
    return fallback, f"logged date {day} is not a posting day ({', '.join(slots)}); using the next slot"


# ---------------------------------------------------------------- step 5: which URLs count as live

def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower()


def on_site(url: str, site: str) -> bool:
    bare = lambda h: h[4:] if h.startswith("www.") else h  # noqa: E731
    return bool(_host(url)) and bare(_host(url)) == bare(_host(site))


def is_google_post_url(url: str) -> bool:
    host = _host(url)
    return urllib.parse.urlparse(url).scheme == "https" and any(host == g or host.endswith("." + g) for g in GOOGLE_HOSTS)


def live_problem(url: str, kind: str, slug: str, site: str) -> str | None:
    """Why `url` cannot be logged as the live piece, or None. The host is checked before anything is fetched."""
    if kind == "article" and not on_site(url, site):
        return f"{url} is not on the workspace site {site}"
    if kind == "gbp" and not is_google_post_url(url):
        return f"{url} is not a Google Business Profile URL ({', '.join(GOOGLE_HOSTS)})"
    live = check_url(url)
    if not live.get("ok"):
        return f"{url} did not load ({live.get('status') or live.get('error')})"
    final = live.get("final") or url
    if kind == "article" and not on_site(final, site):
        return f"{url} redirects to {final}, which is not on the workspace site {site}"
    if kind == "gbp" and not is_google_post_url(final):
        return f"{url} redirects to {final}, which is not a Google Business Profile URL"
    if kind == "article" and slug not in urllib.parse.urlparse(final).path.strip("/").split("/"):
        return f"{final} does not carry the slug {slug!r}"
    return None


# ---------------------------------------------------------------- the five steps

def publish(ws, args, *, indexnow_sender=None) -> dict:
    """indexnow_sender: an injectable `send(url, body) -> status`; None means the real HTTPS POST."""
    if args.kind == "article" and not args.slug:
        return refuse(["refused: an article needs --slug (its WordPress slug and log entry)"])
    draft = check_mod.Draft.load(ws, args.draft, args.kind, args.slug, args.query, args.theme, strict=True)
    entry = find_entry(ws, args.kind, draft.text, args.slug)
    if entry is None:
        return refuse(["refused: no content log entry for this draft; log it with mseo content log first"])
    if entry.get("status") == "published":
        if entry.get("check_sha") and entry["check_sha"] != draft.sha:
            return refuse([f"refused: the draft changed after it was published ({entry.get('url', '(no url logged)')});",
                           "publish does not update a live piece: edit it in WordPress, or log the new text under a new slug"],
                          status="published", url=entry.get("url"))
        return done(EXIT_OK, "published", [f"already published: {entry.get('url', '(no url logged)')}"], url=entry.get("url"))
    if not args.approved:
        return refuse(['refused: --approved "NAME DATE" is required'])
    resuming = entry.get("status") == "publishing"
    if args.confirm_live and args.kind == "article" and not resuming:
        return refuse(["refused: --confirm-live is for a Business Profile post, or an article already publishing"])
    if args.confirm_live and args.kind == "gbp" and not is_google_post_url(args.confirm_live):
        return refuse([f"refused: {args.confirm_live} is not a Google Business Profile URL ({', '.join(GOOGLE_HOSTS)})"])

    def changed() -> bool:
        return check_mod.sha256_file(draft.path) != draft.sha

    # step 1: the full check on the bytes just read; a missing section-order log blocks here (strict)
    checked = check_mod.check_and_write(draft, ws)
    if checked["result"] != "pass":
        bad = [f"  {g['status']}: {g['name']}: {g['detail']}" for g in checked["gates"]
               if g["status"] in ("fail", "error") or (g["status"] == "warn" and g["blocking"])]
        return refuse([f"refused: content check is {checked['result']}"] + bad, check=checked["result"])
    if changed():
        return refuse(["refused: the draft changed after it was checked; run mseo publish again"])
    msgs = [f"check: pass ({draft.sha[:12]})"]

    # step 2: event dates (done on the first run; a resumed publish already rechecked them)
    if not resuming:
        side, research = check_mod.research_file(draft, ws)
        dates = [str(d) for d in side.get("event_dates", [])]
        for d, src in event_sources(research.read_text(encoding="utf-8"), dates):
            msgs.append(f"event date {d}: confirm at {src or '(no source link in the research file)'}")
        if dates and not args.dates_confirmed:
            return refuse(msgs + ["stopped: confirm each date at its source today, then re-run with --dates-confirmed"])
        if dates and not args.dry_run:
            mark_rechecked(research, dates, today())
            msgs.append(f"rechecked: {today().isoformat()} written to {side['file']}")

    if args.dry_run:
        step4 = (f"verify {args.confirm_live}" if args.confirm_live else
                 f"post /{args.slug}/ to {wordpress.site_of(ws)}" if args.kind == "article"
                 else "print the post text and next Business Profile slot")
        return done(EXIT_OK, "dry-run", msgs + ["[dry-run] would: log publishing", f"[dry-run] would: {step4}",
                                                 "[dry-run] would: fetch the live URL, then log published"])

    if args.kind == "article" and not args.confirm_live:
        wordpress.credentials(ws)  # a missing or mismatched credential stops here, before anything is logged

    # step 3
    log_update(ws, args.kind, entry, args.slug, "publishing", check_sha=draft.sha, approved=args.approved)
    msgs.append("logged: publishing")

    # step 4
    if args.confirm_live:
        url = args.confirm_live
    elif args.kind == "article":
        if changed():
            return refuse(msgs + ["refused: the draft changed after it was checked; run mseo publish again"], status="publishing")
        url, problem = post_article(ws, draft, entry, args.slug)
        if problem:
            return refuse(msgs + [problem, "the entry stays publishing; re-run to resume"], status="publishing")
    else:
        posting = ws.config["posting"]
        slot, note = post_slot(entry.get("date"), posting["business_profile_slots"], ws.config["timezone"])
        text = draft.text.strip()
        return done(EXIT_OK, "awaiting-live", msgs + ([note] if note else []) + [f"schedule at {slot.isoformat()}:", "", text, "",
                                                       "then: mseo publish ... --confirm-live URL"], slot=slot.isoformat(), text=text)

    # step 5
    problem = live_problem(url, args.kind, args.slug, wordpress.site_of(ws))
    if problem:
        return refuse(msgs + [f"live check failed: {problem}; the entry stays publishing; re-run to resume"],
                      status="publishing", url=url)
    log_update(ws, args.kind, entry, args.slug, "published", url=url)
    msgs.append(f"published: {url}")
    # step 6: IndexNow (articles only; the publish result is settled above and never changes here)
    key = indexnow.key_for(ws)
    if args.kind == "article" and key:
        result = indexnow.ping(wordpress.site_of(ws), key, [url], sender=indexnow_sender)
        log_update(ws, args.kind, entry, args.slug, "published", indexnow=result)
        msgs.append(f"indexnow: {result['status']}" + (f" ({result['error']})" if result.get("error") else ""))
    return done(EXIT_OK, "published", msgs, url=url)


def post_article(ws, draft, entry: dict, slug: str):
    """(link, None) or (None, why). Posts the checked bytes. Never replaces implicitly: an existing post with the slug
    is updated only when its id is the wp_id this command logged on an earlier try."""
    code, res = wordpress.post_create(ws, draft.path, slug, publish=True, data=draft.data)
    if code == EXIT_EXISTS:
        existing, ours = res.get("id"), entry.get("wp_id")
        if ours is None or existing != ours:
            return None, (f"refused: /{slug}/ belongs to another WordPress post (id {existing}"
                          f"{'' if ours is None else f'; this piece logged id {ours}'}); it was not overwritten")
        code, res = wordpress.post_create(ws, draft.path, slug, publish=True, replace=True, data=draft.data)
    if res.get("id") is not None:  # logged even on a failed live check, so a resume can recognise its own post
        log_update(ws, "article", entry, slug, "publishing", wp_id=res["id"], wp_link=res.get("link"))
    link = res.get("link")
    if code != EXIT_OK or not link:
        return None, f"WordPress step failed (exit {code}): {res.get('problems') or res.get('messages') or res}"
    return link, None


def cmd_publish(args, ctx) -> int:
    ws = ctx.open(args.workspace)
    if not args.dry_run:
        from mseo import seal as seal_mod

        seal_mod.require_current(ws)  # a stale or missing seal stops a publish 
    try:
        payload = publish(ws, args)
    except check_mod.CouldNotRun as exc:
        payload = done(EXIT_COULD_NOT_RUN, "error", [f"error: {exc}"])
    except wordpress.WPError as exc:
        payload = done(EXIT_COULD_NOT_RUN, "error", [f"error: {exc}", "the entry stays publishing; re-run to resume"])
    except (OSError, ValueError, KeyError, log_mod.LogError) as exc:
        payload = done(EXIT_COULD_NOT_RUN, "error", [f"error: {type(exc).__name__}: {exc}"])
    code = payload.pop("code")
    ctx.emit(payload)
    return code


def register(sub, parents) -> None:
    p = sub.add_parser("publish", parents=[parents.workspace, parents.json, parents.dry_run],
                       help="Check, publish and log an approved article or Business Profile post")
    p.add_argument("draft", help="Article (.md) or Business Profile post (.txt), inside the workspace")
    p.add_argument("--kind", required=True, choices=["article", "gbp"])
    p.add_argument("--slug", default="", help="Article slug (WordPress and the content log)")
    p.add_argument("--query", default="", help="Passed to the duplicate check")
    p.add_argument("--theme", default="", help="Passed to the duplicate check")
    p.add_argument("--approved", help='Who approved it and when, e.g. "NAME 2026-01-15"')
    p.add_argument("--dates-confirmed", action="store_true", help="Each event date was confirmed at its source today")
    p.add_argument("--confirm-live", metavar="URL", help="Finish a scheduled post: verify URL loads, then log published")
    p.set_defaults(func=cmd_publish, command_id="publish")
