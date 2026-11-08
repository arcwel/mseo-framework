Open {url} in a new tab of the Chrome window you are connected to.
If Google shows "Choose an account", click only the entry whose text is {email}. Never choose another account, never
sign in, sign out or add an account. If {email} is not listed, stop and reply `failed: identity not listed`.
A feature tip (a small blue box such as "Add filters to your reports" with "Got it" and "Start tour") may appear
a few seconds after the page loads: close it with its X. Never start a tour. Any other dialog (terms, data sharing,
account or property setup): do not accept or change it, reply `failed: dialog: <its title>` and stop.

This is the GA4 Traffic acquisition report, by session primary channel group, for {start_human} to {end_human}.
Check that the date range shows exactly that range; if it does not, set a custom range from {start_human} to
{end_human} (the only change you may make). Add the Date dimension as a secondary dimension if the table has no
date column.

Then save the totals the report shows: use find to locate the table's totals row (above the first channel row,
it holds the totals for Sessions and Engaged sessions), then read_page on the table's header row and that totals
row, and save the output exactly as returned, preceded by one line `URL: <the page URL>`, with the Write tool to
`data/inbox/{totals_snapshot}`; then run get_page_text and add its output to the same file after one line `--- page text ---`; right after that get_page_text, run tabs_context_mcp once (do not save its output, and do nothing else in between): Python takes the page's URL from it. If you cannot find it, reply `failed: totals not found` and stop: the export is only taken when its totals match these.

Then use Share this report (or the download icon), then Download File, then "Download CSV". Chrome saves it to this
profile's download folder, which you cannot see: do not look for the file, and do not open, move or rename it.
Wait about 10 seconds after the click.

Last, open the account and property picker at the top left, use read_page on the `Properties & Apps` list, and save
the output exactly as returned, preceded by one line `URL: <the page URL>`, with the Write tool to
`data/inbox/{snapshot}`. Right after that read_page, run tabs_context_mcp once (do not save its output, and do nothing else in between): Python takes the page's URL from it. Then press Escape to close the picker; never click a property. After this, do not
click, type or open anything else.

Close the tab you opened and reply with only `saved` or `failed: <reason>`.
