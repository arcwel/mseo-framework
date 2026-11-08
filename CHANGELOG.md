# Changelog

Releases list what the software does, not the commits that got it there.

## 1.1.0 (2026-11-08)

### Guided onboarding
- `workspace init SLUG` walks every unfilled value of `customer.json` in file order, validates each answer against
  the schema rule for its path and asks again on a mismatch, asks for the WordPress application password with hidden
  input and stores it in the Keychain, offers the optional keys the runbook adds later, then runs doctor. Re-running
  asks only what is still missing. `--set PATH=VALUE` writes one field, `--answers FILE` fills the non-secret fields
  from JSON, `--no-prompt` reports what is missing with exit 3. The config is saved after every accepted answer, so
  Ctrl-D or Ctrl-C loses nothing. Exit 1 when doctor still reports a failed check, 3 when a value or credential is
  missing.
- Every command that stops on an unfilled placeholder now names that command.

## 1.0.0 (2026-11-08)

First production release. One framework for any customer. Each customer lives in its own sealed workspace.

### Workspaces and isolation
- `workspace new` creates a customer repo from a template. `customer.json` (schema customer/2) carries identity,
  accounts, schedule, content rules and industry vocabulary. Unfilled placeholders are listed in file order.
- `workspace seal` writes the workspace's Claude Code settings (deny rules and sandbox for every other workspace,
  transcripts, the Keychain and the operator's deny list), hashes `customer.json` and `facts.json`, and runs a live
  agent against the sandbox: seven rows that must be denied or must run, plus four for the pull settings. A skipped
  attempt is inconclusive and keeps the previous seal. A denied action that succeeds blocks every command.
- `doctor` checks config, credentials, identity clashes across workspaces, installed tools and the week's preflight.
- Credentials live in the macOS Keychain under `mseo.<slug>.<source>`, and `secret set` reads them from stdin.
- `lint customer-strings` fails on any identifying value from a real workspace, and `lint vocabulary` fails on industry
  terms from any workspace's config. Both run in the pre-commit hook and CI.

### Weekly data
- Browser pulls for Search Console, GA4 and Business Profile through a sealed Claude in Chrome agent, with the
  account attested from the tool's own output and the export tied to the attested account.
- Google Ads through the account's own report script writing ten tables a week to a Google Sheet, attested by the
  Sheet ID. `ads script install` prints the script to paste.
- Run records (mseo.run/2), week files (mseo.week/1) and outcomes (mseo.outcome/1), every derived file written
  through one symlink-refusing atomic writer.
- `run weekly` under launchd with a per-workspace lock, a machine-wide browser lock, a process-group kill and a
  coalition sweep. `schedule generate` writes the LaunchAgents, and `watchdog` alerts by email and pings a heartbeat.
- `week assemble` and `week report`, plus `shadow compare` as an optional check of a week file against the totals the
  report pages showed.

### Google Ads actions
- `ads tracking` gates everything on working conversion tracking. `ads audit` runs ten data-driven checks over the
  week's tables.
- `actions propose` writes numbered batches (mseo.batch/1). `actions approve` records an approval outside the
  workspaces root that signs the exact rows the script applies. `actions apply` writes the approved-changes file,
  and `actions status` reads the script's applied log. The sealed agent is denied approve and apply.
- `loop measure` compares four weeks before and after each applied item.

### Content
- Recipes (`templates/posts/recipes.json`): eight article shapes, ten variation axes, six Business Profile shapes.
  `content recipe` picks the next recipe most different from the last six posts, and `content new` fills a template.
- Pre-publish gates in `content check`: variation, E-E-A-T, claims against `facts.json`, links, wording, duplicates,
  humanizer receipt, research sidecar, schema. `publish` runs them again on the exact bytes and refuses any marker.
- Article, BreadcrumbList and FAQPage JSON-LD per post. a site-wide business schema snippet (`wp schema-deploy`).
- `content topics` shortlists next week's query gaps. `loop verify-live` checks a published piece.

### Site, visibility and reporting
- `site audit`: per-page and site-wide technical checks, Core Web Vitals through PageSpeed Insights, IndexNow
  submission (`site indexnow`, also after each publish).
- `ai-visibility prompts|run`: a monthly prompt set per customer and citation scoring of the answers.
- `report monthly`: the customer's month from the week files, with `TODO:` markers where a human sentence belongs.

### Quality evidence
- Every milestone build passed a security audit and a pentest run on a model that did not write the code.
