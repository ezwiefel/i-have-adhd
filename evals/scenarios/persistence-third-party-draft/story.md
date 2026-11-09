# Persistence through a third-party draft

`case.jsonl` contains four turns and the judging criteria in the existing eval
case format: a coding task, an email to the user's team, a debugging follow-up,
and a stop request.

This evaluates whether the response style survives a draft written for someone
else, whether the draft itself is written for its real audience, and the stop
acknowledgment. It does not establish behavior after stopping, plugin loading,
or compaction handling.

## Capture and judge

Validate offline:

```sh
python3 scripts/run_scenario_eval.py validate evals/scenarios/persistence-third-party-draft
```

Live runs cost money. Capture and judge the same way as
[persistence-topic-switch-stop](../persistence-topic-switch-stop/story.md),
with this scenario's directory and `case.jsonl`.
