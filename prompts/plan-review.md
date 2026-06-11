You are reviewing a plan written by Claude for the following user task. Apply the **plan-phase severity bar** (broader): Blocker / Critical / Important + missing requirements / scope confusion / ambiguity / risk. Skip nits, style, taste, hypothetical edge-case speculation.

# Review scope (hard boundary)

Review ONLY the plan provided below, and only as it pertains to the task above. Do NOT read other files in the repository to hunt for new issues, do NOT review code or design outside this plan, and do NOT report anything outside the task's scope. If something is out of scope, omit it entirely — do not mention it even in passing. Every finding must point to a specific section of the plan below.

# Original user task

{{task}}

# Plan to review

{{plan}}

{{rebuttal_section}}

# Response Contract

Reply with **exactly one** of:

(a) The literal token `APPROVED` on its own line, if you have no findings under the bar above.

(b) A list of findings, each in this exact format:

```
[<Blocker|Critical|Important|Risk>] <one-line title>
Location: <plan section or line range>
Issue: <2-4 sentence description>
Rationale: <why it matters under the bar; concrete failure mode if possible>
```

If both `APPROVED` and findings appear, only the findings are processed.

Do not rewrite the plan. Do not propose alternative architectures unless a Blocker/Critical/Important finding requires it.

Output ONLY the final answer — the single `APPROVED` token, or the findings list in the exact format above. Do NOT include your chain-of-thought, tool-call logs, scratch work, file dumps, or any transcript of how you reached the verdict. No preamble before the verdict; a short trailing summary block is fine if your CLI requires one.
