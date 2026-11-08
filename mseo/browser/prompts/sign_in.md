Open https://myaccount.google.com/ in a new tab of the Chrome window you are connected to. Do not click anything on
the page, do not choose or switch accounts, and do not sign in or out.

Read the page text with get_page_text and save it, exactly as read, with the Write tool to `data/inbox/{snapshot}`. Right after that get_page_text, run tabs_context_mcp once (do not save its output, and do nothing else in between): Python takes the page's URL from it.

Then close the tab you opened and reply with only the word `saved`, or `failed: <reason>` if you could not save it.
