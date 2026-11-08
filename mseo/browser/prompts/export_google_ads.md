Open https://ads.google.com/nav/selectaccount in a new tab of the Chrome window you are connected to.
If Google shows "Choose an account", click only the entry whose text is {email}. Never choose another account, never
sign in, sign out or add an account. If {email} is not listed, stop and reply `failed: identity not listed`.

On the "Select a Google Ads account" page, click only the row that shows {customer_id}. If no row shows it, reply
`failed: account not listed` and stop. When the account opens, change only the path of the current URL to
`/aw/campaigns`, keeping its query string, and open it. Close any notice or feature tip with its X; never start a
tour, apply a recommendation or change a setting.

On the campaigns table click Download, then "Open in Report Editor". If a notice says not all parts of the
report are shown, click Ok. Close any welcome or feedback tip with its X. If the panel of metrics is collapsed, open it; in "Search metrics
and dimensions" type "Phone calls" and click the "Phone calls" result to add it as a column (skip this if it is
not offered). Open the date range, type {start_slash} as the start
date and {end_slash} as the end date, and click Apply. Check that the range shows {start_human} to {end_human}.
Then save the totals the report shows: use find to locate the table's total row (it starts with "Total"),
then read_page on the table's header row and that total row, and save the output exactly as returned, preceded by
one line `URL: <the page URL>`, with the Write tool to `data/inbox/{totals_snapshot}`; then run get_page_text and add its output to the same file after one line `--- page text ---`; right after that get_page_text, run tabs_context_mcp once (do not save its output, and do nothing else in between): Python takes the page's URL from it. If you cannot find it, reply `failed: totals not found` and stop: the export is only taken when its totals match these.
Then click Download (at the top of Report Editor; close any notice covering it first) and ".csv". Chrome saves it to this profile's download folder,
which you cannot see: do not look for the file, and do not open, move or rename it. Wait about 10 seconds. Do not
save the report.

Last, change only the path of the current URL to `/aw/campaigns`, keeping its query string, and open it (if a dialog
asks to leave without saving, leave). Save the account header: use find to locate the header text that shows the
customer ID and account name, then read_page on that element, and save the output exactly as returned, preceded by
one line `URL: <the page URL>`, with the Write tool to `data/inbox/{snapshot}`. Right after that read_page, run tabs_context_mcp once (do not save its output, and do nothing else in between): Python takes the page's URL from it. After this, do not click,
type or open anything else.

Close the tab you opened and reply with only `saved` or `failed: <reason>`.
