# mseo

The framework behind the monthly search strategy service: one codebase, any customer. It holds no customer data.
Each customer lives in a private workspace repo under the workspaces root (`~/Projects/mseo-workspaces/<slug>/` by
default), and every command runs against one workspace at a time inside a seal that denies the others.

macOS only: the scheduler is launchd, credentials live in the login Keychain, the sealed agent is Claude Code with
its sandbox, and the browser pulls drive Chrome through Claude in Chrome. Python 3.12 or newer, managed by uv.

Each week, per customer, it pulls Google Ads (through the account's own report script and a Sheet), Search Console,
GA4 and Business Profile. It checks the identity it ran as, assembles the week file and audits the Ads account
against tracking and ten data checks. It proposes numbered action batches that an operator approves outside the
workspace, then writes the week report. Each month it writes the customer report, scores AI visibility and audits
the site. For content it picks a post recipe, fills a template, runs the pre-publish gates (variation, E-E-A-T,
claims, links, wording, duplicates, schema) and publishes to WordPress or as a Business Profile post.

## Layout

| Path | Holds |
|---|---|
| `mseo/` | the package: one module per command group (`ads/`, `content/`, `adapters/`, `browser/`, `runner/`, `site_checks/`, `cms/`) |
| `mseo/schema/` | JSON Schemas for `customer.json` (v2), run records, week files, outcomes, batches and approvals |
| `templates/posts/` | post recipes (`recipes.json`), 8 article and 6 Business Profile templates, the variation rules (`README.md` there) |
| `templates/ads/` | the Google Ads report script and the seed negative-keyword lists |
| `workspace-template/` | what `mseo workspace new` copies: `customer.json`, `facts.json`, `RULES.md`, empty data folders |
| `docs/runbook/operator-steps.md` | the steps that need a human with the customer's logins |
| `tests/` | the suite; `tests/workspaces/` holds two synthetic customers (`fx`, `fy`) and no real values |

## Install

```bash
git clone <this repo> ~/Projects/mseo && cd ~/Projects/mseo
uv sync --locked
uv tool install ~/Projects/mseo
```

Two ways to run a command:

- `mseo ...` (the installed tool) for everyday commands from any folder.
- `bin/mseo ...` from this checkout for `workspace seal`, which builds the locked venv the sealed agent runs in. The
  installed tool refuses that step and says so.

After `git pull`, run `uv tool install --reinstall ~/Projects/mseo` so the installed tool matches the checkout.

## A new customer, start to finish

1. `mseo workspace new <slug>` creates the workspace repo from `workspace-template/`.
2. Fill `customer.json` (every `<...>` value, the loader refuses to run while one is left), `facts.json` (only claims
   the business confirmed in writing) and `RULES.md`. Industry vocabulary, claim patterns and brand names live in
   `customer.json`, never in this repo: `mseo lint vocabulary` fails the build if they leak in.
3. Store the WordPress application password: step 3 of the runbook.
4. List every folder outside the workspaces root that holds this or any other customer's data in
   `~/.config/mseo/deny-read.txt`, one path per line. The seal denies them to the agent.
5. `cd ~/Projects/mseo && bin/mseo workspace seal --workspace <slug> --pull-test`. The seal writes the workspace's
   `.claude/settings.json`, runs a live agent against the sandbox (eleven rows that must be denied or must run) and
   records the result in `.mseo/seal.json`. Never edit either file by hand: re-seal.
6. `mseo doctor --workspace <slug>` checks the config, the credential, the identity, the installed tools and the
   week's preflight. It lists every unfilled placeholder until the config is complete.
7. Runbook steps 1, 4 to 8: the Ads report script and Sheet, the Business Profile grant, alerts and heartbeat, the
   PageSpeed and IndexNow keys, the AI visibility prompts.
8. `mseo schedule generate --workspace <slug>` installs the weekly launchd job. `mseo run weekly` runs it by hand.

Start Claude Code sessions inside the workspace, never from `~` or this repo. Scheduled runs use
`claude -p --settings <workspace>/.claude/settings.json --permission-mode dontAsk`.

## Commands

| Group | Commands |
|---|---|
| Workspace | `workspace new\|seal`, `doctor`, `secret set`, `identity profile\|check`, `capture verify` |
| Weekly data | `pull --source search_console\|ga4\|google_ads\|business_profile\|open_seo`, `run weekly`, `week assemble\|report`, `schedule generate\|watchdog`, `watchdog`, `heartbeat`, `alerts password`, `lock run`, `shadow compare` (optional: a week file against the totals the report pages showed) |
| Google Ads | `ads script install`, `ads tracking`, `ads audit`, `actions propose\|approve\|apply\|status` |
| Content | `content recipe\|new\|check\|lint\|wording\|dupes\|topics`, `content log article\|gbp`, `publish`, `loop verify-live\|measure` |
| Site and reports | `wp verify\|post-create\|page-create\|snippet-deploy\|schema-deploy\|posts-refresh`, `site audit\|indexnow`, `ai-visibility prompts\|run`, `report monthly` |
| Framework | `lint customer-strings`, `lint vocabulary` |

`mseo <command> --help` documents each one. Exit codes: 0 pass, 1 a check failed, 2 already exists, 3 could not
run (bad config, workspace mismatch, path escape, stale seal, missing credential, usage error). `--json` prints
the result as JSON on stdout. Errors go to stderr.

## Environment

| Variable | Meaning (default) |
|---|---|
| `MSEO_WORKSPACES` | root of the workspace repos (`~/Projects/mseo-workspaces`) |
| `MSEO_HOME` | the framework checkout the seal builds (this checkout) |
| `MSEO_OPERATOR_DENY` | the operator's deny list (`~/.config/mseo/deny-read.txt`) |
| `MSEO_APPROVALS_DIR` | where approvals are written; must be outside the workspaces root (`~/.config/mseo/approvals`) |
| `MSEO_STATE_DIR` | locks and runner state (`~/.local/state/mseo`) |
| `MSEO_ALERTS_CONFIG` | SMTP and heartbeat settings (`~/.config/mseo/alerts.toml`) |
| `MSEO_LAUNCH_AGENTS` | where `schedule generate` writes plists (`~/Library/LaunchAgents`) |
| `MSEO_TEMPLATES` | an alternative `templates/` folder |
| `MSEO_PSI_KEY` | PageSpeed Insights API key for `site audit` |
| `MSEO_AI_CHECK_CMD` | a command that answers an AI visibility prompt, for `ai-visibility run` |
| `MSEO_OPENSEO_CMD` | the OpenSEO client binary (`~/.local/bin/openseo-client`) |
| `MSEO_AGENT_MODEL` | the model browser pulls run on (`sonnet`) |
| `MSEO_LIVE_TEST_MODEL` | the model the seal's live test runs on (`haiku`) |
| `MSEO_TODAY`, `MSEO_NOW` | pin the date or clock for tests and replays |

## Developing

- `git config core.hooksPath .githooks` once per clone: the pre-commit hook runs the four gates below.
- Gates: `uv run pytest -q`, `uv run ruff check mseo tests`, `mseo lint customer-strings` (fails on any value from a
  real workspace's `customer.json`, so it proves nothing on a machine with no workspaces), `mseo lint vocabulary` (fails
  on industry terms from any workspace's config). CI runs the same four plus gitleaks over the full history.
- Tests use the `fx` and `fy` fixtures under `tests/workspaces/` and never a real customer value.
- `.claude/settings.json` at the repo root is the sandbox for Claude Code sessions in this checkout: it denies the
  workspaces root and the Keychain, so development never reads customer data.
- Every milestone build gets a security audit and a pentest on a model that did not write the code before it ships.
- Releases are described in `CHANGELOG.md` by what the software does. The version is in `pyproject.toml`.

## License

MIT, see `LICENSE`.
