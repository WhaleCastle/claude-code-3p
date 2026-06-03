You are reviewing one step's implementation against the approved plan. Apply the **build-phase severity bar** (tighter): Blocker / Critical / Important. Focus on logic bugs, security, correctness, and plan-drift for THIS STEP. Skip nits, style, taste, and speculative edge cases. **Security and concurrency "what-ifs" remain in scope** — these are the substance of those review dimensions, not speculation.

# Original user task

{{task}}

# Approved plan

{{plan}}

# This step's scope

Step {{step_index}}: {{step_description}}

# Diff (working tree vs step baseline)

```diff
{{diff}}
```

# Test output (if any)

{{test_output}}

{{rebuttal_section}}

# Response Contract

Reply with **exactly one** of:

(a) The literal token `APPROVED` on its own line if you have no findings under the bar.

(b) A list of findings, each in this exact format:

```
[<Blocker|Critical|Important>] <one-line title>
Location: <file:lines>
Issue: <description>
Rationale: <concrete failure mode>
```

Do not rewrite the code. Do not raise findings about earlier steps or unrelated files.
