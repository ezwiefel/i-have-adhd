# Changes in skill-v1

Defect fixes (C1 to C4) and the bets Erik picked after Round 0 (C5, C6) to `skills/i-have-adhd/SKILL.md`, one commit each, on top of `main`. The `.cursor` mirror is updated in the same commit. Each diff touches only the rule it names.

| ID | SHA | Rule | Problem fixed | Gated |
| --- | --- | --- | --- | --- |
| C1 | 65c9758 | 9 | "Retain internally" cannot work, because models have no storage between turns. Overflow is now one visible line with the count and topics, offered on request. | No |
| C2 | 3f40fae | 7 | "What now works" claims had nothing behind them, which pushed toward overclaiming. A claim now names the check that showed it, or says what changed and what would confirm it. | No |
| C3 | 84dcb74 | 6 | Demanding specific numbers invited false precision and clashed with the pre-send check. An estimate is now a concrete range plus the deciding factor, given when there is work ahead. | No |
| C4 | b4ea0f5 | 8 | "State cause and fix" pushed the model to assert causes without evidence (`partial-success`, mean −0.63 in `evals/RESULTS.md`). The cause is now stated only when the evidence identifies it; otherwise evidence, labelled hypothesis, confirming check. | No |
| C5 | a205723 | 1, 2, 3 | Every Bad/Good pair was from coding, so outside coding the model invented actions for questions that need only an answer. Rule 1 now says a question's answer is the first line, with a factual example; rule 2 adds a life-admin plan; rule 3 adds a finished writing task that ends with the content. | Yes |
| C6 | d209f02 | Persistence | "If you are unsure whether they still apply, they do" pushed the rules onto drafts meant for other readers. The rules now shape replies addressed to the reader; content for another audience follows that audience's conventions, and the reply around it still follows the rules. | Yes |

Gated means subject to a keep/drop result. C1 to C4 are defect fixes, kept unless they regress. C5 and C6 are bets, kept only if Round 1 shows they earn it. C7 was not written: its premise failed in Round 0, where v0 had no filler openers or closing pleasantries in any of its 78 rows (`RESULTS-v2.md` in the private repo).

Skill length: 142 lines at v0, 143 after C4, 147 after C5, 149 after C6. C1, C2 and C3 add no lines. The limit is about 160.

## Edits beyond the literal spec

Each stays inside the named rule.

- C1: the kept presentation-only sentence ended with "or retained information". That referred to the retention this change removes, so it now ends with "or completeness".
- C4: the v0 Good example asserted "Cause: missing auth header" from a 401 alone, which is the claim the `RESULTS.md` grader flagged. It now shows the evidence, and one Good line shows the hypothesis form for the same failure.
- C5: no example repeats a dev or held-out case prompt, so the skill is not tuned to its own test cases. The rule 3 example shows a finished draft ending with the content, because rule 3 applies only when something is left open. That also bears on Erik's 2026-11-10 note about habitual "Next:" and time lines; swap it for an example with a real blank if C5 should stay neutral on that.
- C6: the "if unsure" line is removed. The handoff allows this only if the scoping sentence makes it redundant. The two sentences before it already state that the rules persist across turns and topics, so the line's only remaining effect was to settle unclear cases, such as a draft, in favour of applying the rules.

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

## skill-v1-final

Cut from `main` with only the kept changes, after Round 1 (`RESULTS-v2.md` in the private repo):

| ID | SHA on `skill-v1-final` | Round 1 decision |
| --- | --- | --- |
| C1 | 7e4a5eb | Keep |
| C2 | 99e111a | Keep |
| C3 | 1bfd525 | Keep |
| C4 | 42a3c1b | Keep |
| C5 | | Drop: every targeted case at ceiling, so it cannot show the +0.3 required |
| C6 | | Drop: targeted cases gained +0.075, below the +0.3 required |

The SHA column in the first table above refers to `skill-v1`.

### `[sync]` commit

Brings this file over from `skill-v1`, and updates every condensed copy whose paraphrase no longer matched the kept rules:

- `skills/i-have-adhd/agents/gemini.toml`, rules 6 to 9:
  - Rule 6: a range plus the deciding factor.
  - Rule 7: name the check that showed the result.
  - Rule 8: give the cause only when the evidence shows it.
  - Rule 9: an overflow line replaces "retain internally".
- `INSTALL.md`, rules 6 to 8 in all seven condensed lists.
- `README.md`, rule 6.

Left unchanged on purpose:
- `GEMINI.md`, `.opencode/command/i-have-adhd.md`, and the description strings in `SKILL.md`, `package.json`, `kimi.plugin.json` and `.opencode/plugins/i-have-adhd.mjs`. They promise "specific" or "concrete" time estimates, and the kept rule 6 still does. Its heading, kept for compatibility, is "Give specific time estimates", and a range with its deciding factor is a specific estimate.
- The other plugin manifests. They promise only "make wins visible" or "visible progress", which the kept C2 still delivers.
- README rules 7 to 9, and `INSTALL.md` rule 9. Their one-line wording does not carry the old meaning.

Grep found no remaining copy of any of these:
- "retain … internally"
- "concrete units"
- "State cause and fix", or the bare "cause, and fix" error rule
- "Ballpark"

### `[I1]` Time and Next lines only when there is work ahead (#16, iteration round 1)

Erik's target: too many replies end with "Time:" and "Next:" lines. The edit changes three places in `SKILL.md`, with the `.cursor` copy kept identical:
- Rule 3: end with a next action only when the reader still has a step to take, and never invent a step to fill a "Next:" line.
- Rule 6: give estimates only for work the reader will carry out.
- The pre-send check: "(a) what to do next, if anything".

The condensed copies are updated to match: rules 3 and 6 in `gemini.toml`, in all seven `INSTALL.md` lists, and in `README.md`. The description strings are left unchanged, for the same reason as in `[sync]`.

`r3-i1` was run locally on Erik's subscription, with Opus judging:
- "Next:" lines fell from 108 to 79 of 130 replies.
- Time lines fell from 42 to 11.
- Weighted score was +0.072 against final (SE 0.056).
- Accuracy −0.077 and safety −0.069, both inside the −0.1 gate.

Erik decided to keep it on 2026-11-13. Details are in `RESULTS-v2.md`, under Iteration round 1.

Verification: the handoff block, with the same three accepted PowerShell failures. `bun` was not run.

### Needs translation follow-up

These translated copies were not changed and still carry the v0 wording of rules 6 to 9, and of rule 3 for `[I1]`:

- `.github/readme/`: README.ar, es, fa, fr, id, ja, ko, pt-BR, th
- `.github/install/`: INSTALL.ar, fa, id, ja, ko, pt-BR, vi, zh-CN
