# Changes in skill-v1

Defect fixes to `skills/i-have-adhd/SKILL.md`, one commit each, on top of `main`. The `.cursor` mirror is updated in the same commit. Each diff touches only the rule it names.

| ID | SHA | Rule | Problem fixed | Gated |
| --- | --- | --- | --- | --- |
| C1 | 65c9758 | 9 | "Retain internally" cannot work, because models have no storage between turns. Overflow is now one visible line with the count and topics, offered on request. | No |
| C2 | 3f40fae | 7 | "What now works" claims had nothing behind them, which pushed toward overclaiming. A claim now names the check that showed it, or says what changed and what would confirm it. | No |
| C3 | 84dcb74 | 6 | Demanding specific numbers invited false precision and clashed with the pre-send check. An estimate is now a concrete range plus the deciding factor, given when there is work ahead. | No |
| C4 | b4ea0f5 | 8 | "State cause and fix" pushed the model to assert causes without evidence (`partial-success`, mean −0.63 in `evals/RESULTS.md`). The cause is now stated only when the evidence identifies it; otherwise evidence, labelled hypothesis, confirming check. | No |

Gated means subject to a keep/drop result. C1 to C4 are defect fixes, kept unless they regress.

Skill length: 142 lines at v0, 143 after C4. C1, C2 and C3 add no lines.

## Edits beyond the literal spec

Both stay inside the named rule.

- C1: the kept presentation-only sentence ended with "or retained information". That referred to the retention this change removes, so it now ends with "or completeness".
- C4: the v0 Good example asserted "Cause: missing auth header" from a 401 alone, which is the claim the `RESULTS.md` grader flagged. It now shows the evidence, and one Good line shows the hypothesis form for the same failure.

## Conflicts

None found. No test pins any of the changed text. `tests/test_omp_package.py` reads the skill frontmatter only, and it is unchanged.

`.cursor/skills/i-have-adhd/SKILL.md` is tracked, as the handoff expects.

Left for Stage 5, as condensed copies: the skill `description` frontmatter and `GEMINI.md` still say "give specific time estimates".

## Verification

Run for each commit: the block in `handoffs/reference-skill-edits.md`.

Accepted failures, the same three at every commit, because the mise `pwsh` shim has no version set:

- `test_hook_is_silent_without_opt_in_flag` (`runtime='powershell'`)
- `test_runtimes_keep_content_when_frontmatter_is_unclosed` (`runtime='powershell'`)
- `test_runtimes_strip_frontmatter_with_trailing_whitespace` (`runtime='powershell'`)

Checks not run:

- `bun scripts/check_context_compat.ts`: `bun` is not installed.
- `claude plugin validate .`: not part of the handoff's block, and no manifest changed.
- Paired evaluations: no model calls in this stage.
