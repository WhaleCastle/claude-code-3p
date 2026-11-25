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


def test_unavailable_raw_is_windowed(script_path, tmp_path):
    """A bloated unparseable response (agent CLI echoing its transcript) must not
    be stored whole — it is windowed head+tail so round files stay bounded, while
    keeping both the start and the (usually trailing) conclusion."""
    head = "TRANSCRIPT-START " + ("x" * 40000)
    tail = ("y" * 40000) + " TRANSCRIPT-END"
    out = parse(script_path, head + tail, tmp_path)
    assert out["status"] == "unavailable"
    raw = out["raw"]
    assert len(raw) < 9000                      # bounded, far below the ~80k input
    assert "chars elided" in raw
    assert raw.startswith("TRANSCRIPT-START")   # head preserved
    assert raw.rstrip().endswith("TRANSCRIPT-END")  # tail (the verdict) preserved


def test_finding_field_is_truncated(script_path, tmp_path):
    """A finding field that absorbs a trailing transcript (no further field label
    after Rationale) is bounded rather than dumping the whole transcript."""
    content = "[Critical] Boom\nLocation: a.py:1\nIssue: bad\nRationale: " + ("z" * 50000) + "\n"
    out = parse(script_path, content, tmp_path)
    assert out["status"] == "findings"
    rationale = out["findings"][0]["rationale"]
    assert len(rationale) < 2100                 # bounded
    assert "truncated" in rationale
    # Short fields are untouched.
    assert out["findings"][0]["location"] == "a.py:1"


def test_short_response_unchanged(script_path, tmp_path):
    """Caps must not alter normal-sized responses."""
    out = parse(script_path, "ramble nothing structured here\n", tmp_path)
    assert out["status"] == "unavailable"
    assert out["raw"].strip() == "ramble nothing structured here"
    assert "elided" not in out["raw"]


# --- markdown-dressed replies (real shapes from Antigravity under PAL's stock
# codereviewer prompt, which asks for a Critical/High/Medium/Low report) ------

def test_heading_dressed_finding(script_path, tmp_path):
    content = """\
### [Important] `add_one` subtracts 1 instead of adding 1
Location: snippet:1
Issue: returns x - 1.
Rationale: every result is off by two.

<SUMMARY>
- **Top Risk (High / Correctness):** add_one is wrong.
</SUMMARY>
"""
    out = parse(script_path, content, tmp_path)
    assert out["status"] == "findings"
    assert len(out["findings"]) == 1
    f = out["findings"][0]
    assert f["severity"] == "Important"
    assert f["title"] == "`add_one` subtracts 1 instead of adding 1"
    # The trailing <SUMMARY> is not glued onto the last field.
    assert f["rationale"] == "every result is off by two."


def test_pal_scale_finding_with_bold_labels(script_path, tmp_path):
    content = """\
I have completed the review.

#### [High / Blocker] Size budget unmet
- **Reference:** brain/bin/size:37
- **Impact:** The core is 25 KB against a 15 KB target.
- **Context & Mitigation:** Aki must choose:
  1. drop steps; or
  2. raise the budget.

#### [Medium] Inventory row missing
- **Location:** tests/inventory.tsv
- **Issue:** One rule has no row.
"""
    out = parse(script_path, content, tmp_path)
    assert out["status"] == "findings"
    first, second = out["findings"]
    assert (first["severity"], first["title"]) == ("Blocker", "Size budget unmet")
    assert first["location"] == "brain/bin/size:37"
    assert first["rationale"] == "The core is 25 KB against a 15 KB target."
    assert "Aki" not in first["rationale"]          # unknown label ends the field
    assert (second["severity"], second["title"]) == ("Important", "Inventory row missing")
    assert second["issue"] == "One rule has no row."


def test_alias_severities(script_path, tmp_path):
    content = "[High] a\nLocation: x\n\n[P1] b\nLocation: y\n\n[Medium / Important] c\nLocation: z\n"
    out = parse(script_path, content, tmp_path)
    assert [(f["severity"], f["title"]) for f in out["findings"]] == [
        ("Critical", "a"), ("Critical", "b"), ("Important", "c")]


def test_header_dressings(script_path, tmp_path):
    content = """\
1. **[Important] Bold-wrapped title**
Location: a.py:1

- [Critical]: Colon separated
Location: b.py:2

> [Blocker] — Dash separated
Location: c.py:3
"""
    out = parse(script_path, content, tmp_path)
    assert [(f["severity"], f["title"]) for f in out["findings"]] == [
        ("Important", "Bold-wrapped title"),
        ("Critical", "Colon separated"),
        ("Blocker", "Dash separated"),
    ]
    assert out["findings"][2]["location"] == "c.py:3"


def test_preamble_paragraph_becomes_issue(script_path, tmp_path):
    content = "### [Important] Missing guard\nThe handler never checks the role.\n- **Location:** routes.py:12\n"
    f = parse(script_path, content, tmp_path)["findings"][0]
    assert f["issue"] == "The handler never checks the role."
    assert f["location"] == "routes.py:12"


def test_fenced_findings_have_no_fence_in_fields(script_path, tmp_path):
    content = "```\n[Important] Fenced\nLocation: a.py\nIssue: i\nRationale: r\n```\n"
    f = parse(script_path, content, tmp_path)["findings"][0]
    assert f["rationale"] == "r"


def test_field_stops_at_rule_and_heading(script_path, tmp_path):
    content = """\
[Critical] One
Location: a.py
Rationale: real reason

---

### Positive Practices Worth Keeping
- **Audit trail:** kept.
"""
    f = parse(script_path, content, tmp_path)["findings"][0]
    assert f["rationale"] == "real reason"


def test_same_title_at_different_locations_is_kept(script_path, tmp_path):
    content = """\
[Important] Missing null check
Location: a.py:10
Issue: x may be None.

[Important] Missing null check
Location: b.py:20
Issue: y may be None.

[Important] Missing null check
Location: a.py:10
Issue: x may be None.
"""
    out = parse(script_path, content, tmp_path)
    # The two locations are separate defects; only the word-for-word repeat goes.
    assert [f["location"] for f in out["findings"]] == ["a.py:10", "b.py:20"]


def test_locations_differing_only_in_punctuation_are_kept(script_path, tmp_path):
    content = """\
[Important] X
Location: src/api-user.py:10
Issue: i

[Important] X
Location: src/api/user.py:10
Issue: i
"""
    out = parse(script_path, content, tmp_path)
    assert [f["location"] for f in out["findings"]] == [
        "src/api-user.py:10", "src/api/user.py:10"]


def test_summary_tags_inside_a_sentence_are_not_an_approval(script_path, tmp_path):
    content = ("I cannot return <SUMMARY>APPROVED</SUMMARY> because the checks "
               "are still running.\n")
    assert parse(script_path, content, tmp_path)["status"] == "unavailable"


def test_findings_differing_after_the_truncation_point_are_kept(script_path, tmp_path):
    shared = "x" * 2500
    content = (f"[Important] Long\nLocation: a.py\nIssue: {shared}A\n\n"
               f"[Important] Long\nLocation: a.py\nIssue: {shared}B\n")
    out = parse(script_path, content, tmp_path)
    assert len(out["findings"]) == 2


def test_quoted_approval_is_not_a_verdict(script_path, tmp_path):
    content = ("I cannot return the requested token:\n> APPROVED\n"
               "The checks are still running.\n")
    assert parse(script_path, content, tmp_path)["status"] == "unavailable"


def test_code_comments_and_keys_in_fenced_evidence_are_kept(script_path, tmp_path):
    content = """\
[Critical] Missing auth
Location: app.py:3
Issue: the handler skips the check:
```python
# admin route
def admin(): return secret()
```
```yaml
---
description: not a field
```
Rationale: anyone reaches admin.
"""
    f = parse(script_path, content, tmp_path)["findings"][0]
    assert "# admin route" in f["issue"]
    assert "description: not a field" in f["issue"]
    assert f["rationale"] == "anyone reaches admin."


def test_fenced_dressed_approval_example_is_not_a_verdict(script_path, tmp_path):
    content = ("I have no verdict yet. The format will be:\n```\nVerdict: APPROVED\n```\n"
               "Tests are still running.\n")
    assert parse(script_path, content, tmp_path)["status"] == "unavailable"
    # A bare APPROVED token in a fence was always accepted and still is.
    assert parse(script_path, "```\nAPPROVED\n```\n", tmp_path)["status"] == "approved"


def test_severity_shaped_lines_in_fenced_evidence_are_not_headers(script_path, tmp_path):
    content = """\
[Important] Wrong log level
Location: app.ini:4
Issue: the config sets:
```ini
[critical]
level = debug
```
Rationale: debug logs leak secrets.
"""
    out = parse(script_path, content, tmp_path)
    assert len(out["findings"]) == 1
    f = out["findings"][0]
    assert "level = debug" in f["issue"]
    assert f["rationale"] == "debug logs leak secrets."


def test_longer_fence_may_quote_shorter_fence_lines(script_path, tmp_path):
    content = """\
[Important] Broken docs example
Location: README.md:9
Issue: the snippet reads:
````markdown
```
# heading inside the quoted block
```
````
Rationale: readers copy a broken example.
"""
    f = parse(script_path, content, tmp_path)["findings"][0]
    assert "# heading inside the quoted block" in f["issue"]
    assert f["rationale"] == "readers copy a broken example."


def test_fenced_summary_approval_example_is_not_a_verdict(script_path, tmp_path):
    content = ("Not finished; the final line will look like:\n```\n"
               "<SUMMARY>APPROVED</SUMMARY>\n```\nTests are still running.\n")
    assert parse(script_path, content, tmp_path)["status"] == "unavailable"


def test_fenced_report_with_intro_heading_keeps_its_findings(script_path, tmp_path):
    content = """\
```
# Findings
[Important] Missing auth
Location: app.py:3
Issue: no role check.
```
APPROVED
"""
    out = parse(script_path, content, tmp_path)
    assert out["status"] == "findings"
    assert out["findings"][0]["title"] == "Missing auth"
    assert out["findings"][0]["issue"] == "no role check."


def test_evidence_fence_inside_a_wrapper_fence(script_path, tmp_path):
    content = """\
````
[Critical] Missing auth
Location: app.py:3
Issue: the handler skips the check:
```python
# admin route
def admin(): return secret()
```
Rationale: anyone reaches admin.
````
"""
    f = parse(script_path, content, tmp_path)["findings"][0]
    assert "# admin route" in f["issue"]
    assert f["rationale"] == "anyone reaches admin."


def test_titled_severity_line_in_evidence_is_not_a_finding(script_path, tmp_path):
    content = """\
[Important] Wrong log level
Location: app.ini:4
Issue: the config sets:
```ini
[critical] ; emergency logger
level = debug
```
Rationale: debug logs leak secrets.
"""
    out = parse(script_path, content, tmp_path)
    assert [f["title"] for f in out["findings"]] == ["Wrong log level"]
    assert out["findings"][0]["rationale"] == "debug logs leak secrets."


def test_repeated_findings_are_not_doubled(script_path, tmp_path):
    content = """\
[Important] Same one
Location: a.py
<SUMMARY>
[Important] Same one
Location: a.py
</SUMMARY>
"""
    out = parse(script_path, content, tmp_path)
    assert len(out["findings"]) == 1


def test_dressed_approvals(script_path, tmp_path):
    for content in (
        "# Review\n\n**Verdict: APPROVED**\n\nTable follows.\n",
        "**Decision:** **APPROVED**\n",
        "### Decision: APPROVED\n",
        "Status: Approved\n",
        "`APPROVED`\n",
        "APPROVED ✅\n",
        "<SUMMARY>APPROVED</SUMMARY>\n",
    ):
        assert parse(script_path, content, tmp_path)["status"] == "approved", content


def test_approval_not_read_from_prose_or_negation(script_path, tmp_path):
    for content in (
        "NOT APPROVED\n",
        "**Verdict: NOT APPROVED**\n",
        "The changes are production-ready and APPROVED.\n",
        "Approved\n",                       # bare word must be the APPROVED token
    ):
        assert parse(script_path, content, tmp_path)["status"] == "unavailable", content


def test_placeholder_and_below_bar_sections_are_not_findings(script_path, tmp_path):
    content = """\
**Verdict: APPROVED**

#### [Critical]
*None.*

#### [High] None found

#### [Low] / Operational considerations
1. **Host-side check** before merge.
"""
    out = parse(script_path, content, tmp_path)
    assert out["status"] == "approved"


def test_below_bar_only_without_token_is_unavailable(script_path, tmp_path):
    out = parse(script_path, "[Low] tiny nit\nLocation: a.py\n", tmp_path)
    assert out["status"] == "unavailable"


def test_checkboxes_and_links_are_not_findings(script_path, tmp_path):
    content = "- [x] tests pass\n[plan.md](file:///plan.md) is fine.\n- [ ] later\n\nAPPROVED\n"
    assert parse(script_path, content, tmp_path)["status"] == "approved"


def test_real_title_starting_with_no_is_kept(script_path, tmp_path):
    out = parse(script_path, "[Important] No auth check on /admin\nLocation: r.py\n", tmp_path)
    assert out["findings"][0]["title"] == "No auth check on /admin"


def test_reply_that_stopped_early_is_unavailable(script_path, tmp_path):
    content = ("Running pytest in the background to verify test results.\n"
               "Task is running; standing by for completion.\n")
    assert parse(script_path, content, tmp_path)["status"] == "unavailable"
