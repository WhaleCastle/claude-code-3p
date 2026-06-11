You are performing the final whole-task integration review. Apply the **plan-phase severity bar** (broader): Blocker / Critical / Important + missing requirements / scope confusion / ambiguity / risk.

# Review scope (hard boundary)

Review ONLY the cumulative diff and artifacts provided below, and only as they pertain to the task and approved plan above. Do NOT go hunting through the repository for new issues, do NOT raise findings about pre-existing code outside this diff, and do NOT report anything outside the task's scope. If something is out of scope, omit it entirely — do not mention it even in passing. You MAY read an unchanged file when you need its context to judge an integration concern in the diff (e.g. the signature of a function the new code calls), but every finding you raise must point to a specific file/line in the diff or artifacts below — never to an issue living entirely in unchanged code.

# Original user task

{{task}}

# Approved plan

{{plan}}

# Cumulative diff (whole build vs pre-build baseline)

```diff
{{cumulative_diff}}
```

# Changed and newly-created files

{{file_list}}

# Per-step summary chain

{{step_summaries}}

# Test output (consolidated across steps)

{{test_output}}

{{rebuttal_section}}

# Response Contract

Reply with **exactly one** of:

(a) `APPROVED` on its own line.

(b) Findings in the exact format:

```
[<Blocker|Critical|Important|Risk>] <one-line title>
Location: <file:lines or section>
Issue: <description>
Rationale: <concrete failure mode>
```

Focus on integration-level concerns that per-step reviews could not see: contract mismatches between steps, plan-drift across the whole task, missing wiring, security at the boundaries.

Output ONLY the final answer — the single `APPROVED` token, or the findings list in the exact format above. Do NOT include your chain-of-thought, tool-call logs, scratch work, file dumps, or any transcript of how you reached the verdict. No preamble before the verdict; a short trailing summary block is fine if your CLI requires one.
