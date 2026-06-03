You are reviewing a plan written by Claude for the following user task. Apply the **plan-phase severity bar** (broader): Blocker / Critical / Important + missing requirements / scope confusion / ambiguity / risk. Skip nits, style, taste, hypothetical edge-case speculation.

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
