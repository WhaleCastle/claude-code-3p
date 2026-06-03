import subprocess
import pytest
from pathlib import Path


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """An initialized empty git repo at tmp_path."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def tmp_non_git(tmp_path: Path) -> Path:
    """A non-git working directory."""
    return tmp_path


@pytest.fixture
def script_path() -> Path:
    """Path to the 3p.py CLI for invoking as a subprocess."""
    return Path(__file__).parent.parent / "scripts" / "3p.py"
