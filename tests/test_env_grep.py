"""The CI env-var grep, runnable locally: no legacy variables or credential dotfiles in the framework."""

import re
import subprocess

from tests.conftest import FRAMEWORK

PATTERN = r"WP_(SITE|USER|APP_PASSWORD)|GEMINI_API_KEY|[A-Z]+_APP_PASSWORD|-wp\.env|-gemini\.env"
EXCLUDED = {".github/workflows/tests.yml", "tests/test_env_grep.py"}


def test_no_legacy_env_vars_or_dotfiles():
    files = subprocess.run(["git", "-C", str(FRAMEWORK), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for rel in files:
        if rel in EXCLUDED:
            continue
        for n, line in enumerate((FRAMEWORK / rel).read_text(errors="replace").splitlines(), 1):
            if re.search(PATTERN, line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert hits == []
