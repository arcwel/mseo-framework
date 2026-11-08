Open https://business.google.com/locations in a new tab of the Chrome window you are connected to.
If Google shows "Choose an account", click only the entry whose text is {email}. Never choose another account, never
sign in, sign out or add an account. If {email} is not listed, stop and reply `failed: identity not listed`.

Close any notice or tip with its X. Do not open any location, and do not edit, post or verify anything.
Use find to locate the table of locations, then read_page on it, and save the output exactly as returned, preceded by
one line `URL: <the page URL>`, with the Write tool to `data/inbox/{snapshot}`.
Right after that read_page, run tabs_context_mcp once (do not save its output, and do nothing else in between): Python takes the page's URL from it.

Close the tab you opened and reply with only `saved` or `failed: <reason>`.
