Open https://search.google.com/search-console in a new tab of the Chrome window you are connected to.
If Google shows "Choose an account", click only the entry whose text is {email}. Never choose another account, never
sign in, sign out or add an account. If {email} is not listed, stop and reply `failed: identity not listed`.

Open the property selector (the combobox showing a property name; in a narrow window open the menu first). With the
list open, use read_page on the list (the listbox that holds the property options and "Add property") and save its
output exactly as returned, preceded by one line `URL: <the page URL>`, with the Write tool to `data/inbox/{snapshot}`.
Right after that read_page, run tabs_context_mcp once (do not save its output, and do nothing else in between): Python takes the page's URL from it.

Do not click any property or change anything. Close the tab you opened and reply with only `saved` or `failed: <reason>`.
