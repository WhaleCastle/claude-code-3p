import subprocess
import sys
from pathlib import Path


def test_no_args_prints_usage(script_path: Path):
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "Usage:" in (result.stdout + result.stderr)


def test_unknown_subcommand_errors(script_path: Path):
    result = subprocess.run(
        [sys.executable, str(script_path), "bogus-cmd"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "bogus-cmd" in (result.stdout + result.stderr)
