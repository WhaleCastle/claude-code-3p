import subprocess
import sys
from pathlib import Path


def run_slug(script_path: Path, task: str) -> str:
    result = subprocess.run(
        [sys.executable, str(script_path), "slug", task],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def test_basic_slugification(script_path):
    assert run_slug(script_path, "Add login form") == "add-login-form"


def test_strips_special_chars(script_path):
    assert run_slug(script_path, "Fix: bug #123 (urgent)") == "fix-bug-123-urgent"


def test_collapses_dashes(script_path):
    assert run_slug(script_path, "a -- b --- c") == "a-b-c"


def test_trims_edges(script_path):
    assert run_slug(script_path, "-- hello --") == "hello"


def test_caps_at_50(script_path):
    long_task = "x" * 100
    assert len(run_slug(script_path, long_task)) <= 50


def test_no_trailing_dash_after_truncation(script_path):
    task = "a" * 49 + "-bbbb"
    slug = run_slug(script_path, task)
    assert not slug.endswith("-")


def test_empty_input_uses_hash_fallback(script_path):
    slug = run_slug(script_path, "!@#$%^&*()")
    assert len(slug) == 8
    assert all(c in "0123456789abcdef" for c in slug)


def test_deterministic_hash_fallback(script_path):
    s1 = run_slug(script_path, "!@#$%")
    s2 = run_slug(script_path, "!@#$%")
    assert s1 == s2


def test_no_leading_dot(script_path):
    slug = run_slug(script_path, ".hidden file")
    assert not slug.startswith(".")


def test_no_double_dots(script_path):
    slug = run_slug(script_path, "foo..bar")
    assert ".." not in slug


def test_passes_check_ref_format(script_path, tmp_git_repo):
    slug = run_slug(script_path, "Complex: Fix [issue] (#42) - urgent!!!")
    ref_path = f"refs/3p/{slug}-20260603-1430/pre-build"
    result = subprocess.run(
        ["git", "check-ref-format", ref_path],
        cwd=tmp_git_repo,
    )
    assert result.returncode == 0, f"slug {slug!r} produced invalid ref {ref_path!r}"
