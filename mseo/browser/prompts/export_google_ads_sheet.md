Open https://docs.google.com/spreadsheets/d/{sheet_id}/edit in a new tab of the Chrome window you are connected to.
If Google shows "Choose an account", click only the entry whose text is {email}. Never choose another account, never
sign in, sign out or add an account. If {email} is not listed, stop and reply `failed: identity not listed`. If the
page says you need access or the file does not exist, reply `failed: sheet not shared with {email}` and stop.

This Sheet is written by the account's own Ads Script. Do not edit any cell, tab or setting in it, and do not open
any other spreadsheet: every address you open in this run must start with
`https://docs.google.com/spreadsheets/d/{sheet_id}/`.

Download {count} tab(s) of the report for {start} to {end}, one at a time, by opening each of these addresses in
the same tab with navigate (do not retype or change them; each is the CSV export of one tab by its gid, and this
may be the first of two runs: the table of contents first, then the tables it lists):
{tab_urls}

Each address makes Chrome save one CSV file to this profile's download folder, which you cannot see: do not look for
the files, and do not open, move or rename them. After each navigate wait about 5 seconds before the next one. If an
address shows an error page instead of downloading, reply `failed: tab <name> did not download` and stop.

When all {count} downloads are done, do not click, type or open anything else. Close the tab you opened and reply
with only `saved` or `failed: <reason>`.
