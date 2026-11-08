# Operator steps the framework cannot do for you

Everything else is code. These eight steps need a human with the customer's logins, once per customer unless
noted. Each says what the framework checks afterwards. Re-sealing always runs from the framework checkout:
`cd ~/Projects/mseo && bin/mseo workspace seal --workspace S --pull-test`.

## 1. Google Ads report script (one-time, replaces the Report Editor click-through)

1. Create an empty Google Sheet in the customer identity's Drive. Copy the ID from its URL
   (`/spreadsheets/d/<ID>/edit`) into `customer.json` as `accounts.google_ads.scripts_sheet_id`.
2. Share the Sheet with the Google Ads login that will own the script (view is enough for the identity, because the script
   runs as the login that authorizes it).
3. Print the script: `mseo ads script install --workspace S --dry-run`. In Google Ads: Tools > Bulk actions >
   Scripts > plus, paste, Authorize, Preview once (the Sheet fills), then schedule Weekly, Monday, 03:00 account time.
4. Re-seal (`bin/mseo workspace seal --workspace S --pull-test` from the checkout). The next weekly job pulls the Sheet instead of the
   Report Editor and attests the file by the Sheet ID in the URL the agent opened.

Check: `mseo doctor` shows the Sheet ID, and the first `google_ads` run record has `seen_via: sheet:<ID>`.

## 2. Applying an approved batch

`mseo actions apply` writes `data/batches/<batch>.approved_changes.csv`. Paste its rows into the Sheet tab
`approved_changes`, set `APPLY = true` in the script once you have reviewed one batch on paper, and run the script
(Preview, then Run). The script appends one line per change to `applied_log`, which the next pull reads.

## 3. WordPress application password

In WordPress: Users > the editor user > Application Passwords > add `mseo`. Then:

`mseo workspace init S` asks for it with hidden input and stores it. To store or replace it on its own, write
`{"site":"https://<domain>","user":"<username>","app_password":"<password>"}` to a file outside any repo (never on
the command line: shell history keeps arguments), then:

```bash
mseo secret set --workspace S --source wordpress --replace < wordpress.json && rm wordpress.json
```

Check: `mseo doctor --workspace S` passes the credential check, and `mseo wp verify --workspace S` lists the capabilities.

## 4. Business Profile access for the agent identity

Business Profile > People and access > add the identity email as Manager (there is no view-only role). Then set
`accounts.business_profile.check` back to `auto` and remove `identity_override`, re-seal.

## 5. Alerts and the heartbeat

`~/.config/mseo/alerts.toml`: `from`, `to`, and `heartbeat_url` (a dead-man's-switch URL from any ping service: the
watchdog GETs it after every run, so a missed ping means the Mac or the job is down). Store the sender's app
password with `mseo alerts password --workspace S` (reads stdin).

## 6. PageSpeed Insights key (optional)

Create an API key in a Google Cloud project with the PageSpeed Insights API enabled and export it as the variable
named by `site_audit.psi_key_env` (default `MSEO_PSI_KEY`) in the shell that runs `mseo site audit`. Without it the
audit reports Core Web Vitals as unchecked.

## 7. IndexNow key (optional, Bing and partners)

Generate a key (any 8-128 character token), host it at `https://<domain>/<key>.txt` (upload through WordPress media
or the host), set `site_audit.indexnow_key`. `mseo publish` pings IndexNow after each published article.

## 8. AI visibility answers

`mseo ai-visibility prompts --workspace S` prints the prompt set. Run each prompt on each engine (or point
`MSEO_AI_CHECK_CMD` at a tool that does) and save the answers as the JSON the command describes, then
`mseo ai-visibility run --workspace S --answers FILE`. Monthly.
