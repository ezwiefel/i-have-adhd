# Evaluations

The harness compares response quality, not just length. Cases live in `cases.jsonl`; the scoring contract lives in `rubric.md`.

## Validate and plan

```bash
python3 scripts/run_evals.py validate
python3 scripts/run_evals.py plan --trials 3 --include-comparator
```

## Run

Run each condition into the same results file. Candidate and comparator instructions are injected from the supplied skill file; task prompts remain identical.

```bash
python3 scripts/run_evals.py run \
  --runner claude \
  --condition baseline \
  --trials 3 \
  --budget-usd 12.50 \
  --output evals/results/responses.jsonl

python3 scripts/run_evals.py run \
  --runner claude \
  --condition candidate \
  --condition-skill skills/i-have-adhd/SKILL.md \
  --trials 3 \
  --budget-usd 12.50 \
  --output evals/results/responses.jsonl
```

The default Claude runner reports dollar cost and receives the remaining condition budget on every call. Runners without cost reporting are rejected unless `--allow-unmetered` is supplied; use that flag only when the provider account has its own hard cap.

A reply can run away: with tools off, a model can still loop on imagined commands until it reaches the 128,000-token output maximum, about $3 on Opus. So a runner can set `env` (variables for the call, such as `CLAUDE_CODE_MAX_OUTPUT_TOKENS`) and `max_call_usd` (a cap on one call's `budget_flag`, under the condition budget). The CLI resumes a reply cut off at the output limit by itself, up to four requests, and can report that as success with `result` holding only the last piece, so a `claude-json` reply with `num_turns` above 1 counts as failed. `run_round.py` records both settings as `limits` in `meta.json` and refuses a resume that changes them.

The Opus and Sonnet runners also pass one fixed `--append-system-prompt` saying there are no tools or files. Without it, Opus 5.5 acted as if it had a working directory on action-style prompts: replies named the harness temp folder, and some looped to the output limit. Every condition gets the same note, so comparisons stay paired, and `run_round.py` records it under `limits`.

Progress goes to stdout as plain lines, so a GitHub Actions log can show it live: `run_round.py` prints a `Stage n/m` line with the next stage and reported spend, and each generation or judge step prints a `progress:` line every 10 items with spend and time left. Items that succeed print nothing else; each failed attempt prints a `failed:` line naming the item.

`--parallel N` (1 to 16, default 1) runs up to N calls at once, here and in `judge.py`. Rows are still written as each call finishes, so file order follows completion. A budget check cannot see calls already in flight, so a cap can be overshot by up to one call per worker; `run_round.py` counts that against the $120 limit for any runner with `max_call_usd`.

All example runners isolate the call from the operator's own agent configuration: `--setting-sources ""` for Claude, `--ignore-user-config --ephemeral` for Codex. Keep that isolation when adding runners: without it, user-level plugins, hooks, memory, and output styles leak into every condition and shape the responses being judged. The sharpest case is this repo's own always-on flag (`~/.claude/.i-have-adhd-always`), which would inject the full i-have-adhd ruleset into the **baseline** condition and make the comparison measure the skill against itself.

Isolation also drops the operator's saved model and effort settings, so the claude runner pins `--model` explicitly. Keep a pin when editing the runner: without one, the eval silently runs whatever the operator (or the CLI release) defaults to; the model would vary between operators and over time, and per-token cost varies with it. The pinned model is part of the result: record it with published numbers, as below.

Runs are resumable: rerun the same command after a provider failure and completed `(case, trial, condition, runner)` rows are skipped. Each incomplete call is retried twice by default, and the final provider error is preserved. A call that exits cleanly but reports `is_error`, an `error*` subtype, or an empty result counts as incomplete and is never written as a row. Any reported cost from a failed attempt, including one that exits non-zero with `total_cost_usd` on stdout, is appended to `<output>.failed-spend` as soon as it is known, so it survives a killed process and counts against the budget on resume. The row for that key carries the attempts' total once it lands; costs that never reach a row stay in the ledger, and a failed attempt with no parseable cost remains unknown and adds nothing.

## Measure

Aggregate token usage, reported cost, and response length from the completed responses file:

```bash
python3 scripts/run_evals.py measure evals/results/responses.jsonl
```

The summary reports input/output token totals, reported generation cost, stored
response length, and candidate-minus-baseline deltas. Positive deltas mean more
usage or cost; negative deltas mean less. Read these alongside quality scores.

Comparisons require the same runner and identical `(case_id, trial)` coverage,
without duplicates. If rows include `model`, every row must name the same model.
Older runner output does not record model metadata: `model: null` means model
comparability is unverified. Check the original model/CLI settings yourself;
a matching runner alias alone does not establish the same model or configuration.

Missing costs or token counts produce `null` totals and deltas, not zero. Invalid
negative, boolean, non-finite, or fractional token counts are rejected. A zero
baseline has no meaningful percentage change, so that percentage is `null`.
Claude input totals include cache creation/read tokens; Codex cached input is
already part of its input count and is not added again.

This measures recorded generation rows, not total provider billing: judge costs
and failed calls that never reached a row (see `<output>.failed-spend`) are excluded. Scenario captures currently omit
token usage, and their response length includes transcript JSON and user prompts.
The command is read-only and makes no model calls.

## Format checks

Report per-condition format diagnostics from the same responses file:

```bash
python3 scripts/format_checks.py evals/results/responses.jsonl
```

The JSON gives a count and rate out of rows for an opening filler sentence, a
closing pleasantry, either of those, a numbered list, a mention of the skill or
hidden instructions, and tool-call syntax written as text. It also gives the mean
and maximum of each row's largest list group. Input rules match `measure`. A
scenario row counts once and shows a trait when any assistant turn does; its
user prompts are not checked. These are diagnostics, not a gate, and the command
makes no model calls.

## Judge and score

`scripts/judge.py` grades the responses and writes the score rows for you:

```bash
python3 scripts/judge.py \
  --runner claude \
  --responses evals/results/responses.jsonl \
  --output evals/results/scores.jsonl
```

It groups responses by `(case_id, trial)` and grades every condition for a case
in one call, so the conditions are compared against each other rather than
scored in isolation. Blinding is structural, not a convention the grader is
asked to respect: each condition is relabelled `A`/`B`/`C` before the prompt is
built, and the label order is permuted per group, so position carries no signal.
The permutation comes from a digest of the group key rather than a random
source, so a resumed run reproduces the labels it used the first time.

For a cross-family judge, use `--runner gemini-3-8-flash --allow-unmetered` with
`GEMINI_API_KEY` set. The runner waits out 429 responses for about two minutes before failing.
Free-tier prompts may be used to improve Google's products, so do not send
cases you need to keep private. In a runner's `command`, `{root}` expands to the
repository root, because runners start in an empty temporary directory. Only
`judge.py` expands it.

Only the region of `rubric.md` between the `<!-- judge:begin -->` and
`<!-- judge:end -->` markers reaches the grader. The release-gate rules below
those markers name the conditions, and sending them to a blind grader would
leak the vocabulary the blinding exists to hide. Keep anything condition-identifying
outside that block.

The grader lists each response's problems before scoring it: `false` statements,
`unsupported` claims, `missing` detail, and `unsure` suspicions that do not count.
Score rows keep these lists under `issues`, so you can see why a response lost points.

Runs are resumable the same way generation is: groups already present in the
output file are skipped. A group missing a condition cannot be scored — the
conditions would no longer be judged on identical rows — so it is reported on
stderr and left out rather than silently dropped.

Every judge run has a spend cap. `--budget-usd` defaults to 25, which is also the
maximum, and the remaining budget goes to any runner with a `budget_flag`. When
reported cost reaches the cap, the judge stops between groups with exit code 2,
and everything already judged stays in the output file. Each group's reported
cost is stored on its score rows as `group_cost_usd`, so a resumed run counts it.
Reported cost from a malformed reply or a failed runner call is appended to
`<output>.failed-spend`, so a resumed run counts that too; a group that later
succeeds carries those attempts in its `group_cost_usd` and the ledger stops
counting them.

Unknown cost is not zero cost. A runner that reports no cost needs
`--allow-unmetered`: without it the judge stops at the first null cost, keeping
that group, and refuses to resume from rows with no recorded cost. Use the flag
only when the provider has its own hard cap or the calls are free.

Judging by hand instead is still supported: blind the `condition` field
yourself and write one JSON object per response with these fields.

```json
{"case_id":"direct-answer","trial":1,"condition":"candidate","accuracy":5,"grounding":5,"completeness":5,"autonomy":5,"actionability":5,"safety":5,"concision":5,"blocker":false,"notes":"Direct and correct."}
```

Either way, apply the release gate:

```bash
python3 scripts/run_evals.py score evals/results/scores.jsonl
```

Both `score` and `measure` need `baseline` plus at least one other condition. With no `candidate` rows there is nothing to gate, so `score` reports `release_gate` as `null`.

To see where two judges differ on the same responses:

```bash
python3 scripts/judge_agreement.py evals/results/scores-a.jsonl evals/results/scores-b.jsonl
```

The JSON gives each shared `(case_id, trial)` group's top condition by weighted
score, with `tie` as its own outcome, and the conditions each judge blocked. It
also gives the agreement rates, the mean absolute difference per dimension, and
the groups that disagree on either. Each file must pass `score`'s checks. A group
in only one file is listed under `groups_only_in_a` or `groups_only_in_b` and left
out of every figure; with no shared groups the figures are `null`.

For the numbers the keep/drop rules read, compare any two conditions:

```bash
python3 scripts/compare.py evals/results/scores.jsonl evals/cases.jsonl comparator candidate --change C3
```

Every delta is the second condition minus the first. Trials are averaged within a
case, then cases are paired, so each case counts once. The JSON gives per-case
weighted deltas, `overall` mean delta and standard error (the Noise band) for
weighted score, accuracy, grounding and safety, and blocker row counts per condition. A
standard error over fewer than two cases is `null`. Conditions not paired over the
same groups, and scored cases missing from the cases file, are errors.

`--change` adds `targeted`: the paired mean over cases whose `targets` name that
change, leaving out and listing ceiling cases, where `comparator`'s mean weighted
score is above 4.7. Ceiling always reads `comparator`, even when it is not one of
the two conditions, and then it must be paired with them too. Without `comparator`
rows, `status` is `ceiling_unknown` and the mean is `null`. `status` also reports
`no_targeted_cases`, `no_scored_targeted_cases` and `all_targeted_cases_at_ceiling`,
each with a `null` mean. The command makes no model calls.

## Run a whole round

`scripts/run_round.py` runs one variant through a round: generation, scenarios, every
judge, and every report. Repeat it per variant, naming the same round.

```bash
python3 scripts/run_round.py \
  --workspace /path/to/eval-workspace \
  --round r1-c3 \
  --variant c3=skill-v1 \
  --comparator-ref main \
  --generator-runner claude-opus-5-5 \
  --judges claude-opus-5-5,gemini-3-8-flash \
  --unmetered-judges gemini-3-8-flash \
  --budget-usd 15 \
  --trials 3
```

`--workspace` is required and has no default, so results are only written to a folder you
name. `--variant` is `name=skill_ref`, where `skill_ref` is a file in the workspace or a git
ref of this repository. Leave it out for a round with no candidate; results then go to a
folder named `v0`. Names, refs and paths are validated, and `--cases`, `--rubric` and
`--runner-config` must resolve inside `--workspace`.

Results go to `<workspace>/results/<round>/<variant>/`, including `meta.json` and
`reports/`. Baseline and comparator rows are generated once per round and trial count in
`results/<round>/shared/` and copied to each variant, so a later variant reuses them
unchanged. Rerunning the same command resumes from the rows on disk. A rerun that changes
the skill, runner, model, CLI version, cases, trials, rubric or judge models is refused,
and so is a change to the comparator or shared setup at any trial count, because scenario
captures are shared across trial counts; use a new round name.

To reuse an earlier round's baseline and comparator replies, give a new round name and
`--responses-from <earlier round>`. This judges them again (for example under a new rubric)
or adds a candidate variant without regenerating them. The new round reads that round's
`shared/` rows in place and never changes them, so their generation spend is counted once. The earlier round's shared setup must
match, as on any resume. With `--scenarios`, the baseline and comparator scenario captures go
into that earlier round's `shared/` folder, like any shared rows, and its `meta.json` counts
their spend.

A run with `--responses-from` and no `--scenarios` can pass `--billing subscription` when the
`claude` CLI is signed in to a Claude plan rather than holding an API key. It refuses to start while
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `GEMINI_API_KEY` is set. The CLI still reports a cost per
call, so per-call and per-condition caps still apply, but the variant's `meta.json` records
`"billing": "subscription"` and the $120 spend limit leaves that folder out.

A generator runner must have a determinable model (a `--model` flag or a `"model"` key in
the runner config). A judge's recorded model is that flag, then that key, then the runner
name.

Before any work it adds reported spend from every `results/*/*/meta.json` to the caps of
the work left to do, and refuses to reach $120. A results folder with a missing or
malformed `meta.json` has unknown spend, so it also refuses until that folder is repaired
or removed. `reported_costs.total_usd` covers the folder's own spend; `shared/meta.json`
holds the shared rows, so each dollar is counted once. Costs are read back from the rows,
so a resume includes earlier spend, and `*.failed-spend` ledgers add attempts that left
no row. A judge named in `--unmetered-judges` records `null` costs
and adds no cap.

`evals/runners.stub.json` runs the same pipeline against canned JSON at $0. Copy it into
the workspace and pass `--runner-config`, `--generator-runner stub` and
`--judges stub-judge-a,stub-judge-b`.

Record the exact CLI and model versions with published results, including measured token and cost numbers. Do not compare conditions produced with different cases, models, trial counts, or rubrics.
