import json
import subprocess
import sys
from pathlib import Path


def parse(script_path: Path, content: str, tmp_path: Path):
    f = tmp_path / "resp.txt"
    f.write_text(content)
    r = subprocess.run([sys.executable, str(script_path), "parse-response", str(f)],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def test_pure_approved_token(script_path, tmp_path):
    out = parse(script_path, "APPROVED\n", tmp_path)
    assert out["status"] == "approved"
    assert out["findings"] == []


def test_approved_token_with_intro(script_path, tmp_path):
    out = parse(script_path, "After review:\n\nAPPROVED\n\nGood work.\n", tmp_path)
    assert out["status"] == "approved"


def test_findings_only(script_path, tmp_path):
    content = """\
[Critical] Null pointer on line 42
Location: src/foo.py:42
Issue: dereferencing without check.
Rationale: crashes on empty input.

[Important] Missing test
Location: tests/foo_test.py
Issue: edge case not covered.
Rationale: regression risk.
"""
    out = parse(script_path, content, tmp_path)
    assert out["status"] == "findings"
    assert len(out["findings"]) == 2
    assert out["findings"][0]["severity"] == "Critical"
    assert out["findings"][0]["title"] == "Null pointer on line 42"
    assert "src/foo.py:42" in out["findings"][0]["location"]


def test_approved_plus_findings_treated_as_findings(script_path, tmp_path):
    content = """\
APPROVED

[Important] Even so
Location: x
Issue: y
Rationale: z
"""
    out = parse(script_path, content, tmp_path)
    assert out["status"] == "findings"
    assert len(out["findings"]) == 1


def test_garbled_unavailable(script_path, tmp_path):
    out = parse(script_path, "ramble ramble nothing structured\n", tmp_path)
    assert out["status"] == "unavailable"
    assert "raw" in out


def test_parser_tolerates_markdown_bold(script_path, tmp_path):
    out = parse(script_path, "**[Critical]** something\nLocation: x\nIssue: y\nRationale: z\n", tmp_path)
    assert out["status"] == "findings"
    assert len(out["findings"]) == 1
    assert out["findings"][0]["severity"] == "Critical"
    assert out["findings"][0]["title"] == "something"


def test_parser_tolerates_markdown_bold_no_title_space(script_path, tmp_path):
    """**[Important]** with multi-word title."""
    content = "**[Important]** Missing null check\nLocation: foo.py\nIssue: crash\nRationale: test\n"
    out = parse(script_path, content, tmp_path)
    assert out["status"] == "findings"
    assert out["findings"][0]["severity"] == "Important"
    assert out["findings"][0]["title"] == "Missing null check"
