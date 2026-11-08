Open https://analytics.google.com/analytics/web/ in a new tab of the Chrome window you are connected to.
If Google shows "Choose an account", click only the entry whose text is {email}. Never choose another account, never
sign in, sign out or add an account. If {email} is not listed, stop and reply `failed: identity not listed`.
A feature tip (a small blue box with "Got it" and "Start tour") may appear a few seconds after the page loads:
close it with its X and never start a tour. If a first-run dialog asks about email preferences, or any other dialog
appears, do not save or change it: reply `failed: first-run dialog` and stop.

Open the account and property picker (the account and property name at the top left). With it open, use read_page on
the `Analytics Accounts` list and on the `Properties & Apps` list, and save both outputs exactly as returned, in that
order, preceded by one line `URL: <the page URL>`, with the Write tool to `data/inbox/{snapshot}`. Read the two lists one
right after the other. Right after the second read_page, run tabs_context_mcp once (do not save its output, and do nothing else in between): Python takes the page's URL from it.

Do not click any account or property or change anything. Close the tab you opened and reply with only `saved` or
`failed: <reason>`.
