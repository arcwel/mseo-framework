"""The CLI shell: help works, usage errors exit 3 (never argparse's 2, which means "already exists")."""

import subprocess
import sys

import pytest

from mseo import cli
from mseo.exits import EXIT_COULD_NOT_RUN, EXIT_EXISTS, EXIT_FAIL, EXIT_OK


def test_exit_codes_are_the_documented_ones():
    assert (EXIT_OK, EXIT_FAIL, EXIT_EXISTS, EXIT_COULD_NOT_RUN) == (0, 1, 2, 3)


def test_help_exits_0(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
    assert "mseo" in capsys.readouterr().out


def test_usage_error_exits_3_not_2(capsys):
    assert cli.main(["no-such-command"]) == EXIT_COULD_NOT_RUN
    assert "error" in capsys.readouterr().err


def test_no_command_exits_3():
    assert cli.main([]) == EXIT_COULD_NOT_RUN


def test_console_script_runs():
    proc = subprocess.run([sys.executable, "-m", "mseo", "--help"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
