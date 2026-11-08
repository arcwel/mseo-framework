Open {url} in a new tab of the Chrome window you are connected to.
If Google shows "Choose an account", click only the entry whose text is {email}. Never choose another account, never
sign in, sign out or add an account. If {email} is not listed, stop and reply `failed: identity not listed`.

This is Search Console for the property {customer_id}. Open the Performance section in the left menu and pick the
report named "Search generative AI" (also shown as "AI Mode" or "Generative AI"; it sits beside "Search results" and
"Discover"). If the Performance section has no such report, reply `failed: generative AI report not available` and
stop: Google is still rolling it out. Never open another report or change any other setting.

Check that the date filter shows {start_human} to {end_human}; if it does not, set a custom range from {start_human}
to {end_human} (the only change you may make). Leave the search type (text or multimodal) at its default.

Then save the totals the report shows: use find to locate the summary card(s) ("Total impressions"; the report has
no clicks), then read_page on the element that holds them, and save the output exactly as returned, preceded by one
line `URL: <the page URL>`, with the Write tool to `data/inbox/{totals_snapshot}`. Right after that read_page, run
tabs_context_mcp once (do not save its output, and do nothing else in between): Python takes the page's URL from it.
If you cannot find them, reply `failed: totals not found` and stop.

Then click Export, then "Download CSV" (Search Console saves it as a ZIP of CSV files). Chrome saves it to this
profile's download folder, which you cannot see: do not look for the file, and do not open, move or rename it.
Wait about 10 seconds after the click.

Last, save the property selector: use find to locate the property combobox, then read_page on it, and save the output
exactly as returned, preceded by one line `URL: <the page URL>`, with the Write tool to `data/inbox/{snapshot}`.
Right after that read_page, run tabs_context_mcp once (do not save its output, and do nothing else in between): Python takes the page's URL from it. After this, do not click, type or open anything else.

Close the tab you opened and reply with only `saved` or `failed: <reason>`.
