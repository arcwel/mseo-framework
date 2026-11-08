"""`mseo ads script install`: the Ads Script with the workspace's Sheet ID filled in, plus the install steps.

It never contacts Google: the operator pastes the script into the account once (a Standard user; a read-only role
cannot create scripts), authorizes it and schedules it weekly."""

from __future__ import annotations

from mseo.adapters import google_ads_sheet as sheet
from mseo.ads import TEMPLATES

SCRIPT = TEMPLATES / "mseo-report-export.gs"
PLACEHOLDER = "__SHEET_ID__"
STEPS = [
    "Create an empty Google Sheet with the identity's Google account (or share one with it) and record its ID, the part "
    "of the URL between /d/ and /edit, as accounts.{source}.scripts_sheet_id in customer.json.",
    "In Google Ads open Tools > Bulk actions > Scripts (the Scripts tab) and click +, then New script.",
    "Replace the editor's contents with the script printed above, name it `mseo report export`, and click Authorize; "
    "accept the Ads and Sheets permissions it asks for (they are scoped to this account and this Sheet).",
    "Click Preview once; the first sheet becomes `_manifest` (gid 0, the table of contents mseo reads first) and the "
    "Sheet gains one tab per table plus `applied_log` and `_errors`, each starting with `# mseo.ads_sheet/1`.",
    "Save, then set Frequency to Weekly, Monday, 03:00 account time, so the tables are ready before the weekly job.",
    "Run `mseo pull --workspace {slug} --source {source} --week <W>`; the record's seen_via is `sheet:<id>`.",
    "Leave APPLY = false until the first approved batch has been reviewed on paper .",
]


class ScriptError(Exception):
    """The script cannot be rendered: no Sheet ID in the config, or the template is missing."""


def render(ws) -> dict:
    """{script, sheet_id, steps} for this workspace."""
    sheet_id = sheet.configured(ws)
    if not sheet_id:
        raise ScriptError(f"accounts.{sheet.SOURCE}.scripts_sheet_id is not set in {ws.slug}: create a Google Sheet with the "
                          "identity's account, copy the ID from its URL (between /d/ and /edit) and record it there, "
                          "then run this again")
    try:
        template = SCRIPT.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScriptError(f"the script template {SCRIPT} could not be read ({exc})") from None
    if PLACEHOLDER not in template:
        raise ScriptError(f"the script template {SCRIPT.name} has no {PLACEHOLDER} to fill")
    steps = [s.format(source=sheet.SOURCE, slug=ws.slug) for s in STEPS]
    return {"script": template.replace(PLACEHOLDER, sheet_id), "sheet_id": sheet_id, "steps": steps}
