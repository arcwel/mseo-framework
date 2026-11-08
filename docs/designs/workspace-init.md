# Guided onboarding: `mseo workspace init`

## Problem

A new workspace fails every command until `customer.json` has no placeholder left and the credentials sit in the
Keychain. The operator learns this one error at a time and has to know which file, which key and which command
fixes each one. Nothing asks.

## Decision

A guided terminal wizard, not a local web portal. Secrets are read with hidden input and written straight to the
Keychain, so they never reach argv, shell history or a browser. The questions come from the config itself: every
`<...>` placeholder in `customer.json` is a question, and the schema rule for that path validates the answer. There
is no second definition of the fields to keep in sync.

## Command

`mseo workspace init SLUG [--answers FILE] [--set PATH=VALUE ...] [--json]`

1. If the workspace does not exist, it is created exactly as `workspace new` does.
2. The config is walked in file order. Each placeholder (a value, or a key such as the `wording.always` entry)
   becomes a prompt: the path, the hint inside the angle brackets, the current value. The answer is validated against
   the schema rule for that path (pattern, enum, format) and asked again on a mismatch, with the rule's message.
   A placeholder in a list asks for one value. Keys starting with `_` are comments and never asked.
3. Credentials: for each source with a credential format (WordPress today) whose Keychain item is missing, the
   wizard asks for the fields; passwords use hidden input. The site defaults to `cms.site`. The payload is validated
   and stored with the same code path as `secret set`.
4. Optional keys the runbook adds later (`accounts.google_ads.scripts_sheet_id`, `site_audit.indexnow_key`,
   `site_audit.psi_key_env`) are offered once each: Enter skips, a value is written.
5. The config is written atomically, then the doctor report runs in-process and prints. The last line names the
   next command (`workspace seal --workspace SLUG --pull-test`).

Re-running asks only what is still a placeholder, so quitting midway loses nothing. `--set PATH=VALUE` writes one
field without prompting (updates and automation). `--answers FILE` takes `{path: value}` for every non-secret field;
secrets still prompt. Without a TTY and without answers for every placeholder, exit 3 lists what is missing.

Every command that stops on a placeholder or a missing credential now ends its message with
`run: mseo workspace init SLUG`.

## Isolation

`workspace init` is an operator command run from the framework checkout, like `workspace new` and `secret set`. It
reads and writes only `<root>/SLUG/customer.json` and the Keychain item `mseo.SLUG.<source>`. It joins the isolation
suite as exempt from the sibling-read check for the same reason `workspace new` is (it must create the folder) and
must never open a sibling's files.

## Tests

A scripted prompter replaces the terminal: each test feeds a list of answers and asserts the written config, the
Keychain calls, the re-asks on bad input, the resume behaviour and the non-TTY exit. The fixtures `fx`/`fy` and the
template are the inputs; no real value appears.
