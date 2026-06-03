You are performing the final whole-task integration review. Apply the **plan-phase severity bar** (broader): Blocker / Critical / Important + missing requirements / scope confusion / ambiguity / risk.

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
