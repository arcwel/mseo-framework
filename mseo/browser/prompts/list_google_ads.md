Open https://ads.google.com/nav/selectaccount in a new tab of the Chrome window you are connected to.
If Google shows "Choose an account", click only the entry whose text is {email}. Never choose another account, never
sign in, sign out or add an account. If {email} is not listed, stop and reply `failed: identity not listed`.

On the "Select a Google Ads account" page, read the page text with get_page_text and save it exactly as returned with
the Write tool to `data/inbox/{snapshot}`. Right after that get_page_text, run tabs_context_mcp once (do not save its output, and do nothing else in between): Python takes the page's URL from it.

Do not click any Ads account or change anything. Close the tab you opened and reply with only `saved` or
`failed: <reason>`.
