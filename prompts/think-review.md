You are reviewing a memo Claude wrote to answer the question below. Your job is to **challenge the reasoning**, not to proofread it. Apply the **think-phase severity bar**: Blocker / Critical / Important / Risk. Raise:

- **Weak or unstated assumptions** the conclusion depends on.
- **Logical gaps** — steps that don't follow, evidence that doesn't support the claim, a recommendation that doesn't follow from the analysis.
- **Missing options** — a credible alternative the memo never considers. Proposing alternatives is encouraged: name the option and why it deserves weighing.
- **Wrong framing** — the memo answers a different or narrower question than the one asked, or the question itself rests on a false premise.
- **Unweighed risks** — a consequence of the recommendation the memo ignores.

Skip wording, formatting, style, and taste. Skip objections that would not change the conclusion or the confidence it deserves.

# Review scope (hard boundary)

Review ONLY the memo below, and only as it bears on the question. You MAY read the context files listed under "Context files" — and ONLY those; do not open any other file. Every finding must point to a specific section of the memo.

# Question

{{task}}

# Goal (north-star)

{{north_star}}

Also flag, as an `[Important]` finding, anything in the memo that does not serve this goal, or any part of the question it silently drops.

# Context files

{{context_files}}

# Memo to review

{{memo}}

{{rebuttal_section}}

# What APPROVED means

`APPROVED` means **no substantive objections remain** — you would not change the recommendation or its stated confidence. It does NOT mean the memo is certainly correct. An open question the memo already names is not an objection **only if** the recommendation and its stated confidence already account for it. An acknowledged uncertainty that would undercut a confidently stated recommendation (for example, a decisive assumption the memo admits is unverified) is still an objection — raise it.

# Response Contract

Reply with **exactly one** of:

(a) The literal token `APPROVED` on its own line, if you have no findings under the bar above.

(b) A list of findings, each in this exact format:

```
[<Blocker|Critical|Important|Risk>] <one-line title>
Location: <memo section or line range>
Issue: <2-4 sentence description; for a missing option, name the option>
Rationale: <why it changes the conclusion or its confidence>
```

If both `APPROVED` and findings appear, only the findings are processed.

Do not rewrite the memo.

Output ONLY the final answer — the single `APPROVED` token, or the findings list in the exact format above. Do NOT include your chain-of-thought, tool-call logs, scratch work, file dumps, or any transcript of how you reached the verdict. No preamble before the verdict; a short trailing summary block is fine if your CLI requires one.
