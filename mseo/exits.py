"""Exit codes shared by every command."""

EXIT_OK = 0  # pass
EXIT_FAIL = 1  # a check failed: a gate, doctor, the lint, a wrong account
EXIT_EXISTS = 2  # already exists: workspace new, secret set, wp create without --replace/--force
EXIT_COULD_NOT_RUN = 3  # bad config, workspace mismatch, path escape, stale seal, missing credential, usage error
