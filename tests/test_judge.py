import contextlib
import io
import json
import os
import sys
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import judge  # noqa: E402
import run_evals  # noqa: E402


class LabelAssignmentTest(unittest.TestCase):
    def test_labels_are_a_stable_bijection_over_conditions(self):
        conditions = ["baseline", "candidate"]

        first = judge.assign_labels(("direct-answer", 1), conditions)
        second = judge.assign_labels(("direct-answer", 1), conditions)

        self.assertEqual(first, second)
        self.assertEqual(sorted(conditions), sorted(first))
        self.assertEqual({"A", "B"}, set(first.values()))

    def test_labels_vary_across_groups_so_position_never_leaks_condition(self):
        conditions = ["baseline", "candidate"]

        seen = {
            judge.assign_labels(("direct-answer", trial), conditions)["baseline"]
            for trial in range(20)
        }

        self.assertEqual({"A", "B"}, seen)


class GroupingTest(unittest.TestCase):
    def test_responses_group_by_case_and_trial(self):
        rows = [
            {"case_id": "direct-answer", "trial": 1, "condition": "baseline", "response": "51"},
            {"case_id": "direct-answer", "trial": 1, "condition": "candidate", "response": "102"},
            {"case_id": "direct-answer", "trial": 2, "condition": "baseline", "response": "again"},
        ]

        groups = judge.group_responses(rows)

        self.assertEqual({"baseline": "51", "candidate": "102"}, groups[("direct-answer", 1)])
        self.assertEqual({"baseline": "again"}, groups[("direct-answer", 2)])

    def test_groups_missing_a_condition_are_partitioned_out_not_dropped(self):
        groups = {
            ("direct-answer", 1): {"baseline": "x", "candidate": "y"},
            ("casual-message", 1): {"baseline": "only one condition ran"},
        }

        complete, incomplete = judge.partition_groups(groups, {"baseline", "candidate"})

        self.assertEqual([("direct-answer", 1)], sorted(complete))
        self.assertEqual([("casual-message", 1)], sorted(incomplete))


class ParseJudgeScoresTest(unittest.TestCase):
    @staticmethod
    def _verdict(value, blocker=False, notes="fixture"):
        return {
            "correctness": value,
            "autonomy": value,
            "actionability": value,
            "safety": value,
            "concision": value,
            "blocker": blocker,
            "notes": notes,
        }

    def test_labels_are_mapped_back_to_their_conditions(self):
        payload = json.dumps({"A": self._verdict(5), "B": self._verdict(2, blocker=True)})

        rows = judge.parse_judge_scores(
            payload, ("direct-answer", 1), {"baseline": "B", "candidate": "A"}
        )

        by_condition = {row["condition"]: row for row in rows}
        self.assertEqual(5, by_condition["candidate"]["correctness"])
        self.assertEqual(2, by_condition["baseline"]["correctness"])
        self.assertTrue(by_condition["baseline"]["blocker"])
        self.assertEqual("direct-answer", by_condition["candidate"]["case_id"])
        self.assertEqual(1, by_condition["candidate"]["trial"])

    def test_out_of_range_score_names_the_case_it_came_from(self):
        payload = json.dumps({"A": self._verdict(9), "B": self._verdict(3)})

        with self.assertRaisesRegex(ValueError, "direct-answer"):
            judge.parse_judge_scores(
                payload, ("direct-answer", 1), {"baseline": "B", "candidate": "A"}
            )

    def test_json_wrapped_in_code_fences_is_still_parsed(self):
        payload = "```json\n" + json.dumps({"A": self._verdict(4), "B": self._verdict(4)}) + "\n```"

        rows = judge.parse_judge_scores(
            payload, ("direct-answer", 1), {"baseline": "B", "candidate": "A"}
        )

        self.assertEqual({"baseline", "candidate"}, {row["condition"] for row in rows})

    def test_missing_label_names_the_label_the_judge_skipped(self):
        payload = json.dumps({"A": self._verdict(4)})

        with self.assertRaisesRegex(ValueError, "B"):
            judge.parse_judge_scores(
                payload, ("direct-answer", 1), {"baseline": "B", "candidate": "A"}
            )


class GraderRubricTest(unittest.TestCase):
    def test_only_the_marked_grader_section_is_extracted(self):
        rubric = (
            "# Response quality rubric\n"
            "<!-- judge:begin -->\n"
            "Score each dimension 1-5.\n"
            "<!-- judge:end -->\n"
            "Release the candidate only when it beats baseline.\n"
        )

        self.assertEqual("Score each dimension 1-5.", judge.grader_rubric(rubric))

    def test_rubric_without_markers_is_used_whole(self):
        self.assertEqual("everything", judge.grader_rubric("everything\n"))

    def test_shipped_rubric_grader_section_never_names_a_condition(self):
        shipped = (ROOT / "evals" / "rubric.md").read_text(encoding="utf-8")

        section = judge.grader_rubric(shipped).lower()

        self.assertNotIn("baseline", section)
        self.assertNotIn("candidate", section)
        self.assertIn("correctness", section)


class PromptTest(unittest.TestCase):
    CASE = {
        "id": "direct-answer",
        "prompt": "What is 17 multiplied by 6?",
        "criteria": ["States 102."],
        "risk": "low",
    }

    def test_prompt_carries_responses_under_labels_and_never_names_conditions(self):
        responses = {"baseline": "Great question! The answer is 102.", "candidate": "102"}
        labels = {"baseline": "B", "candidate": "A"}

        prompt = judge.build_judge_prompt(self.CASE, responses, labels, "SCORE 1-5 PER DIMENSION")

        self.assertIn("Great question! The answer is 102.", prompt)
        self.assertIn("SCORE 1-5 PER DIMENSION", prompt)
        self.assertIn("States 102.", prompt)
        self.assertNotIn("baseline", prompt.lower())
        self.assertNotIn("candidate", prompt.lower())


class NeutralWorkingDirectoryTest(unittest.TestCase):
    def test_runner_cwd_is_fresh_and_outside_the_repository(self):
        # An agent CLI adopts its working directory as project context. Run it
        # in the repo and it starts inspecting the eval harness instead of
        # answering the prompt, which contaminates the responses being graded.
        with run_evals._neutral_cwd() as first:
            cwd = Path(first).resolve()
            self.assertNotEqual(judge.ROOT.resolve(), cwd)
            self.assertFalse(str(cwd).startswith(str(judge.ROOT.resolve())))
            self.assertEqual([], list(cwd.iterdir()))
            (cwd / "state-from-prior-run").write_text("not reusable")

        with run_evals._neutral_cwd() as second:
            next_cwd = Path(second).resolve()
            self.assertNotEqual(cwd, next_cwd)
            self.assertEqual([], list(next_cwd.iterdir()))


class EndToEndTest(unittest.TestCase):
    VERDICT = {
        "correctness": 4,
        "autonomy": 4,
        "actionability": 4,
        "safety": 5,
        "concision": 3,
        "blocker": False,
        "notes": "fixture",
    }

    def test_judging_produces_paired_rows_the_scorer_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            responses = tmp_path / "responses.jsonl"
            responses.write_text(
                "".join(
                    json.dumps(row) + "\n"
                    for row in (
                        {
                            "case_id": "direct-answer",
                            "trial": 1,
                            "condition": "baseline",
                            "runner": "claude",
                            "response": "Great question! The answer is 102.",
                        },
                        {
                            "case_id": "direct-answer",
                            "trial": 1,
                            "condition": "candidate",
                            "runner": "claude",
                            "response": "102",
                        },
                    )
                )
            )
            payload = tmp_path / "verdict.json"
            payload.write_text(json.dumps({"A": self.VERDICT, "B": self.VERDICT}))
            captured = tmp_path / "prompt.txt"
            runner_config = tmp_path / "runners.json"
            runner_config.write_text(
                json.dumps(
                    {
                        "stub": {
                            # Reads the prompt from stdin, not argv: a trailing
                            # option such as `--tools ""` otherwise swallows a
                            # prompt appended to the command line.
                            "command": ["sh", "-c", f"cat > {captured}; cat {payload}"],
                            "response_format": "text",
                        }
                    }
                )
            )
            output = tmp_path / "scores.jsonl"

            exit_code = judge.main(
                [
                    "--responses", str(responses),
                    "--cases", str(ROOT / "evals" / "cases.jsonl"),
                    "--rubric", str(ROOT / "evals" / "rubric.md"),
                    "--runner-config", str(runner_config),
                    "--runner", "stub", "--allow-unmetered",
                    "--output", str(output),
                ]
            )

            self.assertEqual(0, exit_code)
            rows = run_evals.read_jsonl(output)
            self.assertEqual({"baseline", "candidate"}, {row["condition"] for row in rows})

            # The scorer is the real consumer: it must accept what the judge writes.
            summary = run_evals.summarize_scores(rows)
            self.assertEqual(2, summary["conditions"]["baseline"]["rows"] + summary["conditions"]["candidate"]["rows"])

            # The prompt actually sent must carry the responses but never the conditions.
            prompt = captured.read_text()
            self.assertIn("Great question! The answer is 102.", prompt)
            self.assertNotIn("baseline", prompt.lower())
            self.assertNotIn("candidate", prompt.lower())

    def test_root_token_in_a_runner_command_expands_to_the_repo_root(self):
        # Runners start in an empty temp directory, so a relative script path
        # would not resolve.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "verdict.json").write_text(json.dumps({"A": self.VERDICT, "B": self.VERDICT}))
            (tmp_path / "runner.sh").write_text(f'cat > /dev/null; cat "{tmp_path}/verdict.json"\n')
            responses = tmp_path / "responses.jsonl"
            responses.write_text(
                "".join(
                    json.dumps(
                        {
                            "case_id": "direct-answer",
                            "trial": 1,
                            "condition": condition,
                            "runner": "stub",
                            "response": "102",
                        }
                    )
                    + "\n"
                    for condition in ("baseline", "candidate")
                )
            )
            runner_config = tmp_path / "runners.json"
            runner_config.write_text(
                json.dumps({"stub": {"command": ["sh", "{root}/runner.sh"], "response_format": "text"}})
            )
            output = tmp_path / "scores.jsonl"

            with mock.patch.object(judge, "ROOT", tmp_path):
                exit_code = judge.main(
                    [
                        "--responses", str(responses),
                        "--cases", str(ROOT / "evals" / "cases.jsonl"),
                        "--rubric", str(ROOT / "evals" / "rubric.md"),
                        "--runner-config", str(runner_config),
                        "--runner", "stub", "--allow-unmetered",
                        "--retries", "0",
                        "--output", str(output),
                    ]
                )

            self.assertEqual(0, exit_code)
            self.assertEqual(2, len(run_evals.read_jsonl(output)))

    def test_a_malformed_verdict_skips_its_group_instead_of_killing_the_run(self):
        # One bad grader response must not discard the groups already judged
        # nor the ones still queued behind it.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            responses = tmp_path / "responses.jsonl"
            responses.write_text(
                "".join(
                    json.dumps(
                        {
                            "case_id": case_id,
                            "trial": 1,
                            "condition": condition,
                            "runner": "claude",
                            "response": f"{case_id} {condition} text",
                        }
                    )
                    + "\n"
                    for case_id in ("direct-answer", "casual-message")
                    for condition in ("baseline", "candidate")
                )
            )
            good = tmp_path / "good.json"
            good.write_text(json.dumps({"A": self.VERDICT, "B": self.VERDICT}))
            runner_config = tmp_path / "runners.json"
            runner_config.write_text(
                json.dumps(
                    {
                        "stub": {
                            "command": [
                                "sh",
                                "-c",
                                # `blocker` omitted for the casual-message group.
                                f'p=$(cat); case "$p" in *casual-message*)'
                                f' echo \'{{"A":{{"correctness":3}},"B":{{"correctness":3}}}}\';;'
                                f" *) cat {good};; esac",
                            ],
                            "response_format": "text",
                        }
                    }
                )
            )
            output = tmp_path / "scores.jsonl"

            exit_code = judge.main(
                [
                    "--responses", str(responses),
                    "--cases", str(ROOT / "evals" / "cases.jsonl"),
                    "--rubric", str(ROOT / "evals" / "rubric.md"),
                    "--runner-config", str(runner_config),
                    "--runner", "stub", "--allow-unmetered",
                    "--retries", "0",
                    "--output", str(output),
                ]
            )

            rows = run_evals.read_jsonl(output)
            self.assertEqual({"direct-answer"}, {row["case_id"] for row in rows})
            self.assertEqual(2, len(rows))
            self.assertNotEqual(0, exit_code, "skipped groups must not report success")

    def test_missing_entire_condition_fails_before_judging(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            responses = tmp_path / "responses.jsonl"
            responses.write_text(
                json.dumps(
                    {
                        "case_id": "direct-answer",
                        "trial": 1,
                        "condition": "baseline",
                        "runner": "stub",
                        "response": "102",
                    }
                )
                + "\n"
            )
            output = tmp_path / "scores.jsonl"
            runner_config = tmp_path / "runners.json"
            runner_config.write_text(
                json.dumps(
                    {
                        "stub": {
                            "command": ["sh", "-c", "exit 99"],
                            "response_format": "text",
                        }
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "missing required condition"):
                judge.main(
                    [
                        "--responses", str(responses),
                        "--runner-config", str(runner_config),
                        "--runner", "stub", "--allow-unmetered",
                        "--output", str(output),
                    ]
                )
            self.assertFalse(output.exists())

    def test_runner_failure_skips_its_group_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            responses = tmp_path / "responses.jsonl"
            responses.write_text(
                "".join(
                    json.dumps(
                        {
                            "case_id": case_id,
                            "trial": 1,
                            "condition": condition,
                            "runner": "stub",
                            "response": f"{case_id} {condition} text",
                        }
                    )
                    + "\n"
                    for case_id in ("direct-answer", "casual-message")
                    for condition in ("baseline", "candidate")
                )
            )
            verdict = tmp_path / "verdict.json"
            verdict.write_text(json.dumps({"A": self.VERDICT, "B": self.VERDICT}))
            runner_config = tmp_path / "runners.json"
            runner_config.write_text(
                json.dumps(
                    {
                        "stub": {
                            "command": [
                                "sh",
                                "-c",
                                f'p=$(cat); case "$p" in *direct-answer*) exit 7;; *) cat {verdict};; esac',
                            ],
                            "response_format": "text",
                        }
                    }
                )
            )
            output = tmp_path / "scores.jsonl"

            exit_code = judge.main(
                [
                    "--responses", str(responses),
                    "--runner-config", str(runner_config),
                    "--runner", "stub", "--allow-unmetered",
                    "--retries", "0",
                    "--output", str(output),
                ]
            )

            rows = run_evals.read_jsonl(output)
            self.assertEqual({"casual-message"}, {row["case_id"] for row in rows})
            self.assertEqual(2, len(rows))
            self.assertNotEqual(0, exit_code, "skipped groups must not report success")


class BudgetTest(unittest.TestCase):
    VERDICT = {
        "correctness": 4,
        "autonomy": 4,
        "actionability": 4,
        "safety": 5,
        "concision": 3,
        "blocker": False,
        "notes": "fixture",
    }

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.object(judge.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = self.tmp / "calls.txt"
        self.output = self.tmp / "scores.jsonl"
        self.write_inputs(("one", "two", "three"))

    def write_inputs(self, case_ids):
        self.cases = self.tmp / "cases.jsonl"
        self.cases.write_text(
            "".join(
                json.dumps(
                    {"id": case_id, "category": "x", "prompt": "p", "risk": "low", "criteria": ["c"]}
                )
                + "\n"
                for case_id in case_ids
            )
        )
        self.responses = self.tmp / "responses.jsonl"
        self.responses.write_text(
            "".join(
                json.dumps(
                    {
                        "case_id": case_id,
                        "trial": 1,
                        "condition": condition,
                        "runner": "stub",
                        "response": "text",
                    }
                )
                + "\n"
                for case_id in case_ids
                for condition in ("baseline", "candidate")
            )
        )

    def write_runner(self, cost, *, result=None, response_format="claude-json", budget_flag=None):
        if result is None:
            result = json.dumps({"A": self.VERDICT, "B": self.VERDICT})
        reply = self.tmp / "reply.json"
        reply.write_text(json.dumps({"result": result, "usage": {}, "total_cost_usd": cost}))
        runner = {
            # Flags the judge appends arrive as $@ and are logged, one line per call.
            "command": ["sh", "-c", f'cat > /dev/null; echo "$*" >> "{self.calls}"; cat "{reply}"', "sh"],
            "response_format": response_format,
        }
        if budget_flag:
            runner["budget_flag"] = budget_flag
        self.runner_config = self.tmp / "runners.json"
        self.runner_config.write_text(json.dumps({"stub": runner}))

    def judge(self, *extra):
        return judge.main(
            [
                "--responses", str(self.responses),
                "--cases", str(self.cases),
                "--rubric", str(ROOT / "evals" / "rubric.md"),
                "--runner-config", str(self.runner_config),
                "--runner", "stub",
                "--output", str(self.output),
                *extra,
            ]
        )

    def judge_quietly(self, *extra):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.judge(*extra)
        return code, stderr.getvalue()

    def call_log(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def judged_cases(self):
        return sorted({row["case_id"] for row in run_evals.read_jsonl(self.output)})

    def test_max_call_usd_caps_the_budget_flag_of_one_call(self):
        self.write_runner(0.01, budget_flag="--max-budget-usd")
        config = json.loads(self.runner_config.read_text())
        config["stub"]["max_call_usd"] = 0.25
        self.runner_config.write_text(json.dumps(config))

        self.assertEqual(0, self.judge("--budget-usd", "1"))

        self.assertIn("--max-budget-usd 0.2500", self.call_log()[0])

    def test_runner_env_reaches_the_call_on_a_direct_run(self):
        self.write_inputs(("one",))
        self.write_runner(0.01)
        seen = self.tmp / "seen.txt"
        config = json.loads(self.runner_config.read_text())
        script = config["stub"]["command"][2]
        config["stub"]["command"][2] = f'echo "$EVAL_TEST_LIMIT" > "{seen}"; {script}'
        config["stub"]["env"] = {"EVAL_TEST_LIMIT": "8000"}
        self.runner_config.write_text(json.dumps(config))

        with mock.patch.dict(os.environ):
            self.assertEqual(0, self.judge())

        self.assertEqual("8000", seen.read_text().strip())

    def test_parallel_groups_overlap_and_costs_add_up(self):
        self.write_runner(0.01)
        reply = json.dumps(
            {"result": json.dumps({"A": self.VERDICT, "B": self.VERDICT}), "usage": {}, "total_cost_usd": 0.1}
        )
        barrier = threading.Barrier(3, timeout=5)

        def answer(*_, **__):
            barrier.wait()  # Passes only when three groups are in flight together.
            return subprocess.CompletedProcess(["stub"], 0, reply, "")

        stdout = io.StringIO()
        with mock.patch.object(judge.subprocess, "run", side_effect=answer), (
            contextlib.redirect_stdout(stdout)
        ):
            code = self.judge("--parallel", "3", "--budget-usd", "1")

        self.assertEqual(0, code)
        self.assertEqual(["one", "three", "two"], self.judged_cases())
        self.assertEqual(6, len(run_evals.read_jsonl(self.output)))
        self.assertIn("Reported judge cost: $0.3000", stdout.getvalue())

    def test_parallel_budget_stop_keeps_finished_groups(self):
        self.write_inputs(tuple(f"case-{index}" for index in range(8)))
        self.write_runner(0.6)

        code, stderr = self.judge_quietly("--parallel", "2", "--budget-usd", "1")

        self.assertEqual(2, code)
        self.assertIn("Budget exhausted", stderr)
        self.assertLessEqual(len(self.call_log()), 3)  # Two past the check, at most one more.
        self.assertEqual(len(self.call_log()), len(self.judged_cases()))

    def test_budget_stop_halts_at_the_cap_and_keeps_completed_groups(self):
        self.write_runner(0.6)

        code, stderr = self.judge_quietly("--budget-usd", "1")

        self.assertEqual(2, code)
        self.assertIn("Budget exhausted", stderr)
        self.assertEqual(2, len(self.call_log()))
        self.assertEqual(["one", "three"], self.judged_cases())
        self.assertEqual(4, len(run_evals.read_jsonl(self.output)))

    def test_remaining_budget_is_passed_through_budget_flag(self):
        self.write_runner(0.6, budget_flag="--max-budget-usd")

        self.judge_quietly("--budget-usd", "1")

        self.assertEqual(
            ["--max-budget-usd 1.0000", "--max-budget-usd 0.4000"], self.call_log()
        )

    def test_finishing_exactly_at_the_cap_is_a_clean_exit(self):
        self.write_inputs(("one",))
        self.write_runner(1.0)

        code, _ = self.judge_quietly("--budget-usd", "1")

        self.assertEqual(0, code)

    def test_resume_counts_earlier_spend_once_per_group(self):
        self.write_runner(0.6, budget_flag="--max-budget-usd")

        first, _ = self.judge_quietly("--budget-usd", "0.5")
        self.assertEqual(2, first)
        self.assertEqual(1, len(self.call_log()))
        self.assertEqual(
            [0.6, 0.6], [row["group_cost_usd"] for row in run_evals.read_jsonl(self.output)]
        )

        # Earlier spend alone exhausts the same cap, so nothing new runs.
        second, _ = self.judge_quietly("--budget-usd", "0.5")
        self.assertEqual(2, second)
        self.assertEqual(1, len(self.call_log()))

        # 2.0 - 0.6, not 2.0 - 1.2: both of the group's rows carry its one cost.
        third, _ = self.judge_quietly("--budget-usd", "2")
        self.assertEqual(0, third)
        self.assertEqual("--max-budget-usd 1.4000", self.call_log()[1])
        self.assertEqual(["one", "three", "two"], self.judged_cases())

    def test_null_cost_stops_without_allow_unmetered_and_keeps_the_group(self):
        self.write_runner(None)

        code, stderr = self.judge_quietly()

        self.assertEqual(2, code)
        self.assertIn("--allow-unmetered", stderr)
        self.assertEqual(1, len(self.call_log()))
        self.assertEqual(["one"], self.judged_cases())

    def test_null_cost_on_the_only_group_still_requires_allow_unmetered(self):
        self.write_inputs(("one",))
        self.write_runner(None)

        code, stderr = self.judge_quietly()

        self.assertEqual(2, code)
        self.assertIn("--allow-unmetered", stderr)
        self.assertEqual(["one"], self.judged_cases())

    def test_allow_unmetered_judges_everything_and_records_null(self):
        self.write_runner(None)

        code, _ = self.judge_quietly("--allow-unmetered")

        self.assertEqual(0, code)
        self.assertEqual(3, len(self.call_log()))
        self.assertEqual(
            {None}, {row["group_cost_usd"] for row in run_evals.read_jsonl(self.output)}
        )

    def test_earlier_unreported_spend_blocks_a_resume_without_the_flag(self):
        self.write_runner(None)
        self.judge_quietly()
        self.write_runner(0.1)
        calls_before = len(self.call_log())

        blocked, stderr = self.judge_quietly()
        self.assertEqual(2, blocked)
        self.assertIn("--allow-unmetered", stderr)
        self.assertEqual(calls_before, len(self.call_log()))

        allowed, _ = self.judge_quietly("--allow-unmetered")
        self.assertEqual(0, allowed)
        self.assertEqual(calls_before + 2, len(self.call_log()))

    def test_format_that_cannot_report_cost_is_rejected_before_any_call(self):
        self.write_runner(None, response_format="text")

        with self.assertRaisesRegex(RuntimeError, "--allow-unmetered"):
            self.judge()

        self.assertEqual([], self.call_log())
        self.assertFalse(self.output.exists())

    def test_billed_malformed_attempts_count_and_stop_further_retries(self):
        self.write_runner(0.6, result="not json")

        code, _ = self.judge_quietly("--budget-usd", "1", "--retries", "5")

        self.assertEqual(2, code)
        self.assertEqual(2, len(self.call_log()))
        self.assertFalse(run_evals.read_jsonl(self.output))

    def write_script(self, body):
        """A runner that drains stdin, logs the call, then runs `body` in sh."""
        runner = {
            "command": ["sh", "-c", f'cat > /dev/null; echo call >> "{self.calls}"; {body}', "sh"],
            "response_format": "claude-json",
        }
        self.runner_config = self.tmp / "runners.json"
        self.runner_config.write_text(json.dumps({"stub": runner}))

    def ledger(self):
        return [entry["cost_usd"] for entry in run_evals.unsettled_failed_spend(self.output)]

    def test_malformed_replies_stay_on_the_ledger_across_a_resume(self):
        self.write_inputs(("one",))
        self.write_runner(3, result="not json")

        code, _ = self.judge_quietly("--budget-usd", "10")
        self.assertEqual(1, code)
        self.assertEqual(3, len(self.call_log()))
        self.assertEqual([3.0, 3.0, 3.0], self.ledger())

        # $9 carried over, so a single further $3 attempt reaches the $10 cap.
        code, stderr = self.judge_quietly("--budget-usd", "10")
        self.assertEqual(1, code, stderr)
        self.assertEqual(4, len(self.call_log()))
        self.assertEqual([3.0] * 4, self.ledger())

    def test_a_group_that_succeeds_after_billed_failures_counts_them_once(self):
        self.write_inputs(("one",))
        self.write_runner(0.5, result="not json")
        self.judge_quietly("--retries", "1")
        self.assertEqual([0.5, 0.5], self.ledger())

        self.write_runner(0.5, budget_flag="--max-budget-usd")
        code, _ = self.judge_quietly("--budget-usd", "10", "--retries", "1")
        self.assertEqual(0, code)
        self.assertEqual("--max-budget-usd 9.0000", self.call_log()[-1])
        scores = run_evals.read_jsonl(self.output)
        self.assertEqual([1.5, 1.5], [row["group_cost_usd"] for row in scores])
        self.assertEqual([], self.ledger())

    def test_nonzero_exit_with_a_cost_on_stdout_reaches_the_ledger(self):
        self.write_inputs(("one",))
        self.write_script("""echo '{"total_cost_usd": 0.25}'; exit 1""")

        code, _ = self.judge_quietly("--retries", "1")

        self.assertEqual(1, code)
        self.assertEqual(2, len(self.call_log()))
        self.assertEqual([0.25, 0.25], self.ledger())

    def test_retries_after_a_billed_exit_get_the_remaining_budget(self):
        self.write_inputs(("one",))
        reply = self.tmp / "reply.json"
        reply.write_text('{"total_cost_usd": 0.4}')
        runner = {
            "command": ["sh", "-c", f'cat > /dev/null; echo "$*" >> "{self.calls}"; cat "{reply}"; exit 1', "sh"],
            "response_format": "claude-json", "budget_flag": "--max-budget-usd",
        }
        self.runner_config = self.tmp / "runners.json"
        self.runner_config.write_text(json.dumps({"stub": runner}))

        code, _ = self.judge_quietly("--budget-usd", "1", "--retries", "5")

        self.assertEqual(
            ["--max-budget-usd 1.0000", "--max-budget-usd 0.6000", "--max-budget-usd 0.2000"],
            self.call_log(),
        )
        self.assertEqual(1, code)

    def test_nonzero_exit_without_json_is_unknown_not_a_crash(self):
        self.write_inputs(("one",))
        self.write_script("echo crash; exit 1")

        code, _ = self.judge_quietly("--retries", "0")

        self.assertEqual(1, code)
        self.assertEqual([], self.ledger())

    def test_empty_stdout_on_clean_exit_is_retried_then_skipped(self):
        self.write_inputs(("one",))
        self.write_script("exit 0")

        code, stderr = self.judge_quietly("--retries", "1")

        self.assertEqual(1, code)
        self.assertEqual(2, len(self.call_log()))
        self.assertIn("failed after 2 attempts", stderr)

    def test_budget_outside_the_allowed_range_is_rejected(self):
        self.write_runner(0.1)
        for value in ("0", "-1", "26", "nan"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "--budget-usd"):
                    self.judge(f"--budget-usd={value}")
        self.assertEqual([], self.call_log())

    def test_only_a_finite_non_negative_number_counts_as_reported_cost(self):
        self.assertEqual(0.5, judge._reported_cost(0.5))
        self.assertEqual(1.0, judge._reported_cost(1))
        self.assertEqual(0.0, judge._reported_cost(0))
        for value in (None, True, "0.5", -1, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(judge._reported_cost(value))


if __name__ == "__main__":
    unittest.main()
