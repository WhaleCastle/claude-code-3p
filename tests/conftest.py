import subprocess
import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_user_config(tmp_path: Path, monkeypatch):
    """Keep the real machine-wide config out of every test.

    load_config layers $THREEP_USER_CONFIG (default ~/.config/3p/config.json)
    under the project config, so without this a developer's own reviewer models
    would leak in and break the DEFAULTS assertions. Subprocesses inherit it;
    the tests that pass an explicit env use a fake HOME, which isolates them
    the same way.
    """
    monkeypatch.setenv("THREEP_USER_CONFIG", str(tmp_path / "no-user-config.json"))


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
