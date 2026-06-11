You are reviewing one step's implementation against the approved plan. Apply the **build-phase severity bar** (tighter): Blocker / Critical / Important. Focus on logic bugs, security, correctness, and plan-drift for THIS STEP. Skip nits, style, taste, and speculative edge cases. **Security and concurrency "what-ifs" remain in scope** — these are the substance of those review dimensions, not speculation.

# Review scope (hard boundary)

Review ONLY the diff provided below for THIS step, and only as it pertains to the task and approved plan above. Do NOT go hunting through the repository for new issues, do NOT raise findings about earlier steps or files outside this diff, and do NOT report anything outside the task's scope. If something is out of scope, omit it entirely — do not mention it even in passing. You MAY read an unchanged file when you need its context to judge something in the diff (e.g. the signature of a function the changed code calls), but every finding you raise must point to a specific line in the diff below — never to an issue living entirely in unchanged code.

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

Output ONLY the final answer — the single `APPROVED` token, or the findings list in the exact format above. Do NOT include your chain-of-thought, tool-call logs, scratch work, file dumps, or any transcript of how you reached the verdict. No preamble before the verdict; a short trailing summary block is fine if your CLI requires one.
