# Seed negative keywords

Starter lists for `mseo actions propose`: `customer.json` names one with `ads.negative_seed_list` (default `generic`),
and a list can name the one it `extends`. `generic.json` holds the terms every local service business excludes (jobs, free,
DIY, training). The other files are worked examples for two industries; add a file for a new industry here, or put
the terms straight into the workspace. The proposal marks every seed as possibly present already, because existing
negatives cannot be read from the sheet; the script logs a duplicate instead of adding one.
