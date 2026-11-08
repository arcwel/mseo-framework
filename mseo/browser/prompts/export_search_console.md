Open {url} in a new tab of the Chrome window you are connected to.
If Google shows "Choose an account", click only the entry whose text is {email}. Never choose another account, never
sign in, sign out or add an account. If {email} is not listed, stop and reply `failed: identity not listed`.

This is the Search Console Performance report for {start_human} to {end_human}. Check that the date filter shows
exactly that range; if it does not, set a custom range from {start_human} to {end_human} (the only change you may make).

Then save the totals the report shows: use find to locate the summary cards ("Total clicks", "Total
impressions", "Average CTR", "Average position"), then read_page on the element that holds all four, and save the
output exactly as returned, preceded by one line `URL: <the page URL>`, with the Write tool to
`data/inbox/{totals_snapshot}`. Right after that read_page, run tabs_context_mcp once (do not save its output, and do nothing else in between): Python takes the page's URL from it. If you cannot find them, reply `failed: totals not found` and stop: the export is only taken when its totals match these.

Then click Export, then "Download CSV" (Search Console saves it as a ZIP of CSV files). Chrome saves it to this
profile's download folder, which you cannot see: do not look for the file, and do not open, move or rename it.
Wait about 10 seconds after the click.

Last, save the property selector: use find to locate the property combobox, then read_page on it, and save the output
exactly as returned, preceded by one line `URL: <the page URL>`, with the Write tool to `data/inbox/{snapshot}`.
Right after that read_page, run tabs_context_mcp once (do not save its output, and do nothing else in between): Python takes the page's URL from it. After this, do not click, type or open anything else.

Close the tab you opened and reply with only `saved` or `failed: <reason>`.
