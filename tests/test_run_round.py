"""End-to-end checks of the round runner against the stub CLI: temp dirs, $0, no network."""

import collections
import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_evals
import run_round
import run_scenario_eval

SCENARIO = "persistence-third-party-draft"
SKILL_MARKER = "CANDIDATE-SKILL-MARKER"
SECRET = "sk-test-do-not-leak-0123456789"
TRIALS = 2


class RoundTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = pathlib.Path(temporary.name).resolve()
        self.ws = self.base / "workspace"
        self.ws.mkdir()
        shutil.copy(ROOT / "evals/runners.stub.json", self.ws)
        self.cases = [
            {
                "id": f"case-{index}", "category": "test", "prompt": f"Prompt number {index}.",
                # Criteria reach judge prompts only, so a test can fail just the judge.
                "risk": "low", "criteria": [f"Answers {index}. CRITERIA-{index}"], "targets": ["C1"],
            }
            for index in (1, 2)
        ]
        (self.ws / "cases.jsonl").write_text(
            "".join(json.dumps(case) + "\n" for case in self.cases), encoding="utf-8"
        )
        (self.ws / "skill.md").write_text(f"Be brief. {SKILL_MARKER}\n", encoding="utf-8")
        self.results = self.ws / "results"
        self.calls_log = self.ws / "calls.log"

    def run_cli(self, *extra, variant="c1=skill.md", env=None):
        argv = [
            "--workspace", str(self.ws), "--round", "r1", f"--variant={variant}",
            "--comparator-ref", "HEAD", "--trials", str(TRIALS), "--retries", "0",
            "--runner-config", "runners.stub.json", "--cases", "cases.jsonl",
            "--generator-runner", "stub", "--judges", "stub-judge-a,stub-judge-b",
            "--budget-usd", "1", "--scenario-budget-usd", "0.5", *extra,
        ]
        out, err = io.StringIO(), io.StringIO()
        full_env = {"STUB_CALL_LOG": str(self.calls_log), "ANTHROPIC_API_KEY": SECRET, **(env or {})}
        with mock.patch.dict(os.environ, full_env), contextlib.redirect_stdout(out), (
            contextlib.redirect_stderr(err)
        ):
            code = run_round.main(argv)
        return code, out.getvalue(), err.getvalue()

    def set_runner_limits(self, name, **limits):
        config = json.loads((self.ws / "runners.stub.json").read_text("utf-8"))
        config[name].update(limits)
        (self.ws / "runners.stub.json").write_text(json.dumps(config), encoding="utf-8")

    def calls(self):
        text = self.calls_log.read_text(encoding="utf-8") if self.calls_log.exists() else ""
        return collections.Counter(text.split())

    def meta(self, variant="c1", round_name="r1"):
        return json.loads((self.results / round_name / variant / "meta.json").read_text("utf-8"))

    def responses(self, variant="c1"):
        return run_evals.read_jsonl(self.results / "r1" / variant / "responses.jsonl")

    def rows_for(self, variant, *conditions):
        return [row for row in self.responses(variant) if row["condition"] in conditions]

    def write_prior_meta(self, total, round_name="old", variant="v"):
        directory = self.results / round_name / variant
        directory.mkdir(parents=True)
        (directory / "meta.json").write_text(
            json.dumps({"status": "complete", "reported_costs": {"total_usd": total}}),
            encoding="utf-8",
        )
        return directory


class ValidationTest(RoundTestCase):
    def assert_rejected(self, *extra, **kwargs):
        code, _, err = self.run_cli(*extra, **kwargs)
        self.assertEqual(1, code, err)
        self.assertIn("error:", err)
        self.assertFalse((self.results / "r1").exists())
        self.assertEqual(collections.Counter(), self.calls())

    def test_bad_names_and_refs_are_rejected(self):
        for variant in (
            "-x=HEAD", "..=HEAD", "a..b=HEAD", "a;b=HEAD", "a b=HEAD", "$(id)=HEAD", "shared=HEAD",
            "SHARED=HEAD", "noequals", "c1=-x", "c1=../skill.md", "c1=main..dev", "c1=HEAD;rm",
            "c1=$(id)", "c1=/etc/passwd", "c1=a//b", "c1=a.lock", "c1=a\nb",
        ):
            with self.subTest(variant=variant):
                self.assert_rejected(variant=variant)
        for round_name in ("-r", "../r", "r/1", "r;1", "a..b", ""):
            with self.subTest(round=round_name):
                self.assert_rejected(f"--round={round_name}")
        self.assert_rejected("--comparator-ref=-x")
        self.assert_rejected("--comparator-ref=a..b")
        self.assert_rejected("--generator-runner=not-in-config")
        self.assert_rejected("--judges=stub-judge-a,not-in-config")
        self.assert_rejected("--scenarios=../evil")
        self.assert_rejected("--budget-usd=26")
        self.assert_rejected("--budget-usd=nan")
        self.assert_rejected("--parallel=0")
        self.assert_rejected("--trials=0")

    def test_paths_outside_the_workspace_are_rejected(self):
        outside = self.base / "outside.jsonl"
        shutil.copy(self.ws / "cases.jsonl", outside)
        (self.ws / "link.jsonl").symlink_to(outside)
        (self.ws / "skill-link.md").symlink_to(outside)
        for value in (str(outside), "../outside.jsonl", "link.jsonl"):
            with self.subTest(value=value):
                self.assert_rejected(f"--cases={value}")
        self.assert_rejected(variant="c1=skill-link.md")

    def test_results_symlink_out_of_the_workspace_is_rejected(self):
        outside = self.base / "outside-results"
        outside.mkdir()
        self.results.symlink_to(outside)
        code, _, err = self.run_cli()
        self.assertEqual(1, code, err)
        self.assertIn("outside the workspace", err)
        self.assertEqual([], list(outside.iterdir()))

    def test_symlink_inside_a_results_folder_is_rejected(self):
        folder = self.results / "r1" / "c1"
        folder.mkdir(parents=True)
        (folder / "meta.json").write_text(
            json.dumps({"status": "complete", "reported_costs": {"total_usd": 0}}), encoding="utf-8"
        )
        (folder / "responses.jsonl").symlink_to(self.base / "elsewhere.jsonl")
        code, _, err = self.run_cli()
        self.assertEqual(1, code, err)
        self.assertIn("symlink", err)
        self.assertEqual(collections.Counter(), self.calls())


class CommitLookupTest(unittest.TestCase):
    def test_ref_known_only_on_origin_resolves(self):
        missing, found = (
            subprocess.CompletedProcess([], 1, "", ""),
            subprocess.CompletedProcess([], 0, "abc123\n", ""),
        )
        with mock.patch("run_round._git", side_effect=[missing, found]) as git:
            self.assertEqual("abc123", run_round._commit_for("main"))
        self.assertIn("origin/main^{commit}", git.call_args.args)


class SpendLimitTest(RoundTestCase):
    def assert_refused(self, *extra, expect):
        code, _, err = self.run_cli(*extra)
        self.assertEqual(1, code, err)
        self.assertIn(expect, err)
        self.assertFalse((self.results / "r1").exists())
        self.assertEqual(collections.Counter(), self.calls())

    def test_refuses_when_spend_plus_caps_would_cross_the_limit(self):
        # Caps here: 3 conditions + 2 judges at $1 each.
        self.write_prior_meta(115.5)
        self.assert_refused(expect="$120")

    def test_runs_when_spend_plus_caps_stays_under_the_limit(self):
        self.write_prior_meta(114.5)
        code, _, err = self.run_cli()
        self.assertEqual(0, code, err)

    def test_parallel_overshoot_counts_toward_the_limit(self):
        # Caps are $5, and each worker can start one $0.25 call past each of 3 generation caps.
        self.set_runner_limits("stub", max_call_usd=0.25)
        self.write_prior_meta(114)
        self.assert_refused("--parallel=4", expect="$120")
        code, _, err = self.run_cli("--parallel=1")
        self.assertEqual(0, code, err)

    def test_unmetered_judge_adds_no_cap(self):
        # 3 conditions + 1 metered judge = $4; counting the unmetered judge would make it $5.
        self.write_prior_meta(115.5)
        code, _, err = self.run_cli(
            "--judges=stub-judge-a,stub-judge-unmetered", "--unmetered-judges=stub-judge-unmetered"
        )
        self.assertEqual(0, code, err)

    def test_missing_meta_is_unknown_spend_and_refuses(self):
        (self.results / "old" / "v").mkdir(parents=True)
        self.assert_refused(expect="meta.json")

    def test_malformed_meta_is_unknown_spend_and_refuses(self):
        for content in (
            "not json", "[]", "{}", '{"reported_costs": {}}',
            '{"status": "complete", "reported_costs": {"total_usd": null}}',
            '{"status": "complete", "reported_costs": {"total_usd": "3"}}',
            '{"status": "complete", "reported_costs": {"total_usd": -1}}',
            '{"status": "complete", "reported_costs": {"total_usd": true}}',
        ):
            with self.subTest(content=content):
                directory = self.results / "old" / "v"
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "meta.json").write_text(content, encoding="utf-8")
                self.assert_refused(expect="meta.json")

    def test_a_killed_run_counts_rows_its_meta_never_saw(self):
        directory = self.write_prior_meta(0, round_name="killed", variant="c9")
        meta = {"status": "running", "reported_costs": {"total_usd": 0}}
        (directory / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (directory / "responses.jsonl").write_text(
            json.dumps({"condition": "candidate", "cost_usd": 115.5}) + "\n", encoding="utf-8"
        )
        self.assert_refused(expect="$120")

    def test_shared_rows_already_made_are_not_capped_again(self):
        code, _, err = self.run_cli("--budget-usd=20", env={"STUB_COST_USD": "0"})
        self.assertEqual(0, code, err)
        # 3 conditions + 2 judges at $20 would be $100; with shared rows done it is $60.
        self.write_prior_meta(30)
        code, _, err = self.run_cli("--budget-usd=20", variant="c2=skill.md")
        self.assertEqual(0, code, err)


class SharedRowsTest(RoundTestCase):
    def test_second_variant_reuses_shared_rows_unchanged(self):
        code, _, err = self.run_cli()
        self.assertEqual(0, code, err)
        shared_file = self.results / "r1" / "shared" / f"responses-t{TRIALS}.jsonl"
        shared_bytes = shared_file.read_bytes()
        first = self.rows_for("c1", "baseline", "comparator")
        self.assertEqual(len(self.cases) * TRIALS * 2, len(first))
        self.assertEqual(len(self.cases) * TRIALS * 3, self.calls()["generate"])
        self.calls_log.unlink()

        code, _, err = self.run_cli(variant="c2=skill.md")
        self.assertEqual(0, code, err)

        self.assertEqual(shared_bytes, shared_file.read_bytes())
        self.assertEqual(first, self.rows_for("c2", "baseline", "comparator"))
        # Only the new candidate rows were generated.
        self.assertEqual(len(self.cases) * TRIALS, self.calls()["generate"])
        candidates = [row["response"] for row in self.rows_for("c1", "candidate")]
        self.assertFalse(set(candidates) & {r["response"] for r in self.rows_for("c2", "candidate")})

    def test_a_new_trial_count_gets_its_own_shared_rows(self):
        self.assertEqual(0, self.run_cli()[0])
        self.assertEqual(0, self.run_cli("--trials=1", variant="c2=skill.md")[0])
        shared = self.results / "r1" / "shared"
        self.assertTrue((shared / f"responses-t{TRIALS}.jsonl").exists())
        self.assertEqual(
            len(self.cases) * 2, len(run_evals.read_jsonl(shared / "responses-t1.jsonl"))
        )

    def test_shared_rows_from_another_setup_are_refused(self):
        self.assertEqual(0, self.run_cli()[0])
        (self.ws / "cases.jsonl").write_text(
            json.dumps(self.cases[0]) + "\n", encoding="utf-8"
        )
        code, _, err = self.run_cli(variant="c2=skill.md")
        self.assertEqual(1, code)
        self.assertIn("case_set", err)

    def test_resume_with_a_changed_skill_is_refused(self):
        self.assertEqual(0, self.run_cli()[0])
        (self.ws / "skill.md").write_text("A different skill.\n", encoding="utf-8")
        code, _, err = self.run_cli()
        self.assertEqual(1, code)
        self.assertIn("skills", err)


class ResponsesFromTest(RoundTestCase):
    def test_rejudge_reads_another_rounds_rows_without_generating_or_changing_them(self):
        cost = {"STUB_COST_USD": "0.01"}
        self.assertEqual(0, self.run_cli(variant="", env=cost)[0])
        shared = self.results / "r1" / "shared"
        before = {path.name: path.read_bytes() for path in shared.iterdir() if path.is_file()}
        spent = run_round.reported_spend(self.results)
        self.calls_log.unlink()

        code, _, err = self.run_cli("--round=r2", "--responses-from=r1", variant="", env=cost)

        self.assertEqual(0, code, err)
        self.assertEqual(0, self.calls()["generate"])
        self.assertGreater(self.calls()["judge"], 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in shared.iterdir() if p.is_file()})
        self.assertFalse((self.results / "r2" / "shared").exists())
        read = lambda name: run_evals.read_jsonl(self.results / name / "v0" / "responses.jsonl")
        self.assertEqual(read("r1"), read("r2"))
        meta = self.meta("v0", "r2")
        self.assertEqual("r1", meta["responses_from"])
        # Generation spend stays counted once, in r1.
        self.assertAlmostEqual(
            spent + meta["reported_costs"]["total_usd"], run_round.reported_spend(self.results)
        )

    def test_scenarios_add_shared_captures_to_the_source_round_and_count_once(self):
        cost = {"STUB_COST_USD": "0.01"}
        self.assertEqual(0, self.run_cli(variant="", env=cost)[0])
        shared = self.results / "r1" / "shared"
        rows = (shared / f"responses-t{TRIALS}.jsonl").read_bytes()
        spent = run_round.reported_spend(self.results)

        code, _, err = self.run_cli(
            "--round=r2", "--responses-from=r1", f"--scenarios={SCENARIO}", env=cost
        )

        self.assertEqual(0, code, err)
        self.assertEqual(rows, (shared / f"responses-t{TRIALS}.jsonl").read_bytes())
        self.assertTrue((shared / "scenarios" / SCENARIO / "baseline-1.jsonl").exists())
        self.assertFalse((self.results / "r2" / "shared").exists())
        shared_meta = json.loads((shared / "meta.json").read_text("utf-8"))
        self.assertEqual("complete", shared_meta["status"])
        self.assertGreater(shared_meta["reported_costs"]["scenarios_usd"], 0)
        added = shared_meta["reported_costs"]["scenarios_usd"]
        self.assertAlmostEqual(
            spent + added + self.meta("c1", "r2")["reported_costs"]["total_usd"],
            run_round.reported_spend(self.results),
        )

    def test_a_failed_scenario_status_does_not_block_reusing_whole_rows(self):
        self.assertEqual(0, self.run_cli(variant="")[0])
        meta_path = self.results / "r1" / "shared" / "meta.json"
        meta_path.write_text(
            json.dumps({**json.loads(meta_path.read_text("utf-8")), "status": "failed"}), "utf-8"
        )

        code, _, err = self.run_cli("--round=r2", "--responses-from=r1", variant="")

        self.assertEqual(0, code, err)

    def test_unusable_sources_are_refused(self):
        self.assertEqual(0, self.run_cli(variant="")[0])
        for extra in (("--responses-from=r1",), ("--round=r2", "--responses-from=nope")):
            with self.subTest(extra=extra):
                code, _, err = self.run_cli(*extra, variant="")
                self.assertEqual(1, code)
                self.assertIn("responses-from", err)


    def test_comparator_change_is_refused_whatever_the_trial_count(self):
        (self.ws / "alt.md").write_text("Another comparator.\n", encoding="utf-8")
        self.assertEqual(0, self.run_cli("--trials=1", f"--scenarios={SCENARIO}")[0])
        capture = self.results / "r1" / "shared" / "scenarios" / SCENARIO / "comparator-1.jsonl"
        before = capture.read_bytes()
        self.calls_log.unlink()

        code, _, err = self.run_cli(
            "--trials=2", "--comparator-ref=alt.md", f"--scenarios={SCENARIO}", variant="c2=skill.md"
        )

        self.assertEqual(1, code, err)
        self.assertIn("comparator", err)
        self.assertEqual(before, capture.read_bytes())
        self.assertEqual(collections.Counter(), self.calls())

    def test_cli_version_change_is_refused_on_resume(self):
        self.assertEqual(0, self.run_cli()[0])
        self.assertEqual("0.0.0 (stub)", self.meta("shared")["sets"][str(TRIALS)]["cli_version"])
        with mock.patch("run_round._cli_version", return_value="9.9.9"):
            code, _, err = self.run_cli()
            self.assertEqual(1, code)
            self.assertIn("cli_version", err)
            # A new variant must not attach its version to rows the old CLI made.
            code, _, err = self.run_cli(variant="c2=skill.md")
        self.assertEqual(1, code)
        self.assertIn("cli_version", err)
        self.assertEqual("0.0.0 (stub)", self.meta("shared")["sets"][str(TRIALS)]["cli_version"])

    def test_judge_model_prefers_flag_then_key_then_name(self):
        for runner, expected in (
            ({"command": ["x", "--model", "flagged"], "model": "keyed"}, "flagged"),
            ({"command": ["x"], "model": "keyed"}, "keyed"),
            ({"command": ["x"]}, "name"),
        ):
            with self.subTest(runner=runner):
                self.assertEqual(expected, run_round._judge_model({"name": runner}, "name"))

    def test_example_gemini_judge_records_its_pinned_model(self):
        config = json.loads((ROOT / "evals/runners.example.json").read_text("utf-8"))
        self.assertEqual("gemini-3.8-flash", run_round._judge_model(config, "gemini-3-8-flash"))

    def test_generator_with_no_determinable_model_is_refused(self):
        config = json.loads((self.ws / "runners.stub.json").read_text("utf-8"))
        config["bare"] = {"command": ["claude"], "response_format": "claude-json"}
        (self.ws / "runners.stub.json").write_text(json.dumps(config), encoding="utf-8")
        code, _, err = self.run_cli("--generator-runner=bare")
        self.assertEqual(1, code)
        self.assertIn("model", err)
        self.assertEqual(collections.Counter(), self.calls())

    def test_generator_model_key_is_recorded(self):
        config = json.loads((self.ws / "runners.stub.json").read_text("utf-8"))
        config["keyed"] = {**config["stub"], "command": ["claude"], "model": "keyed-model"}
        (self.ws / "runners.stub.json").write_text(json.dumps(config), encoding="utf-8")
        code, _, err = self.run_cli("--generator-runner=keyed")
        self.assertEqual(0, code, err)
        self.assertEqual("keyed-model", self.meta()["model"])

    def test_judge_model_change_is_refused_on_resume(self):
        self.assertEqual(0, self.run_cli()[0])
        config = json.loads((self.ws / "runners.stub.json").read_text("utf-8"))
        config["stub-judge-a"]["command"][-1] = "another-judge-model"
        (self.ws / "runners.stub.json").write_text(json.dumps(config), encoding="utf-8")
        code, _, err = self.run_cli()
        self.assertEqual(1, code)
        self.assertIn("judges", err)

    def test_runner_env_reaches_the_call_and_is_recorded(self):
        own_log = self.ws / "generator-only.log"
        self.set_runner_limits("stub", env={"STUB_CALL_LOG": str(own_log)}, max_call_usd=0.25)

        code, _, err = self.run_cli()

        self.assertEqual(0, code, err)
        self.assertIn("generate", own_log.read_text("utf-8"))
        expected = {"env": {"STUB_CALL_LOG": str(own_log)}, "max_call_usd": 0.25}
        self.assertEqual(expected, self.meta()["limits"])
        self.assertEqual(expected, self.meta("shared")["sets"][str(TRIALS)]["limits"])

    def test_system_note_is_recorded_and_a_change_is_refused_on_resume(self):
        config = json.loads((self.ws / "runners.stub.json").read_text("utf-8"))
        command = config["stub"]["command"]
        self.set_runner_limits("stub", command=[*command, "--append-system-prompt", "No tools."])
        self.assertEqual(0, self.run_cli()[0])
        self.assertEqual("No tools.", self.meta()["limits"]["system_note"])
        self.set_runner_limits("stub", command=[*command, "--append-system-prompt", "Changed."])

        code, _, err = self.run_cli()

        self.assertEqual(1, code)
        self.assertIn("limits", err)

    def test_generator_limit_change_is_refused_on_resume(self):
        self.set_runner_limits("stub", max_call_usd=0.25)
        self.assertEqual(0, self.run_cli()[0])
        self.set_runner_limits("stub", max_call_usd=0.5)

        code, _, err = self.run_cli()

        self.assertEqual(1, code)
        self.assertIn("limits", err)

    def test_judge_limit_change_is_refused_on_resume(self):
        self.set_runner_limits("stub-judge-a", max_call_usd=0.25)
        self.assertEqual(0, self.run_cli()[0])
        self.set_runner_limits("stub-judge-a", max_call_usd=0.5)

        code, _, err = self.run_cli()

        self.assertEqual(1, code)
        self.assertIn("judges", err)

    def test_parallel_round_writes_the_same_rows(self):
        code, _, err = self.run_cli("--parallel=3")

        self.assertEqual(0, code, err)
        self.assertEqual(2 * len(self.cases) * TRIALS, len(self.rows_for("c1", "baseline", "comparator")))

    def test_secret_in_runner_env_is_refused_before_it_reaches_meta(self):
        self.set_runner_limits("stub", env={"SOME_API_KEY": "sk-long-enough-value"})

        code, _, err = self.run_cli()

        self.assertEqual(1, code)
        self.assertIn("SOME_API_KEY", err)
        self.assertNotIn("sk-long-enough-value", err)

    def test_workspace_is_required(self):
        argv = ["--round", "r1", "--generator-runner", "stub", "--judges", "x", "--budget-usd", "1"]
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            run_round.main(argv)
        self.assertIn("--workspace", err.getvalue())


class ProgressLogTest(RoundTestCase):
    def test_log_says_where_the_round_is_and_what_is_next(self):
        code, out, err = self.run_cli()

        self.assertEqual(0, code, err)
        self.assertIn("Stage 1/5: generate baseline. Next: generate comparator.", out)
        self.assertIn("Stage 3/5: generate candidate. Next: judge stub-judge-a.", out)
        self.assertIn("Stage 5/5: judge stub-judge-b. Next: reports.", out)
        self.assertIn("progress: baseline replies", out)
        self.assertIn("progress: judge stub-judge-a groups", out)

    def test_child_scripts_stream_their_output(self):
        self.assertEqual("1", run_round._runner_env(None)["PYTHONUNBUFFERED"])


class EndToEndTest(RoundTestCase):
    def test_stub_round_writes_a_complete_results_folder(self):
        code, out, err = self.run_cli(
            f"--scenarios={SCENARIO}", "--change=C1",
            env={"GEMINI_API_KEY": SECRET},
        )
        self.assertEqual(0, code, err)
        folder = self.results / "r1" / "c1"
        for name in (
            "meta.json", "skill.md", "responses.jsonl",
            "scores-stub-judge-a.jsonl", "scores-stub-judge-b.jsonl",
            "reports/measure.json", "reports/format-checks.json",
            "reports/score-stub-judge-a.json", "reports/score-stub-judge-b.json",
            "reports/judge-agreement-stub-judge-a-vs-stub-judge-b.json",
            "reports/compare-stub-judge-a-baseline-vs-comparator.json",
            "reports/compare-stub-judge-a-baseline-vs-candidate.json",
            "reports/compare-stub-judge-a-comparator-vs-candidate.json",
            f"scenarios/{SCENARIO}/responses.jsonl",
            f"scenarios/{SCENARIO}/candidate-1.jsonl",
            f"scenarios/{SCENARIO}/scores-stub-judge-a.jsonl",
            f"scenarios/{SCENARIO}/reports/score-stub-judge-b.json",
        ):
            with self.subTest(name=name):
                self.assertTrue((folder / name).is_file(), name)
        for name in (
            "meta.json", "comparator-skill.md", f"responses-t{TRIALS}.jsonl",
            f"scenarios/{SCENARIO}/baseline-1.jsonl", f"scenarios/{SCENARIO}/comparator-2.jsonl",
        ):
            with self.subTest(shared=name):
                self.assertTrue((self.results / "r1" / "shared" / name).is_file(), name)

        score = json.loads((folder / "reports/score-stub-judge-a.json").read_text("utf-8"))
        self.assertEqual({"baseline", "candidate", "comparator"}, set(score["conditions"]))
        self.assertIn("passed", score["release_gate"])
        compared = json.loads(
            (folder / "reports/compare-stub-judge-a-comparator-vs-candidate.json").read_text("utf-8")
        )
        self.assertEqual("C1", compared["targeted"]["change_id"])
        merged = run_evals.read_jsonl(folder / "scenarios" / SCENARIO / "responses.jsonl")
        self.assertEqual(TRIALS * 3, len(merged))

        meta = self.meta()
        self.assertEqual("complete", meta["status"])
        self.assertRegex(meta["public_sha"], r"^[0-9a-f]{40}$")
        self.assertRegex(meta["skills"]["comparator"]["commit"], r"^[0-9a-f]{40}$")
        self.assertRegex(meta["skills"]["comparator"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertIsNone(meta["skills"]["candidate"]["commit"])
        self.assertRegex(meta["skills"]["candidate"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual("stub", meta["runner"])
        self.assertEqual("stub-model", meta["model"])
        self.assertEqual("0.0.0 (stub)", meta["cli_version"])
        self.assertEqual("medium", meta["effort"])
        self.assertEqual(TRIALS, meta["trials"])
        self.assertEqual("cases.jsonl", meta["case_set"]["path"])
        self.assertEqual(len(self.cases), meta["case_set"]["cases"])
        self.assertRegex(meta["case_set"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(meta["rubric_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            [{"runner": "stub-judge-a", "model": "stub-judge-a"},
             {"runner": "stub-judge-b", "model": "stub-judge-b"}],
            meta["judges"],
        )
        self.assertEqual([SCENARIO], meta["scenarios"])
        self.assertEqual(0, meta["reported_costs"]["total_usd"])
        self.assertRegex(meta["date"], r"^\d{4}-\d{2}-\d{2}T")
        self.assert_no_secret(out + err)

    def assert_no_secret(self, output=""):
        self.assertNotIn(SECRET, output)
        for path in self.results.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET, path.read_text("utf-8"), path)

    def test_round_zero_has_no_candidate(self):
        code, _, err = self.run_cli(variant="", env={"STUB_COST_USD": "0.01"})
        self.assertEqual(0, code, err)
        self.assertEqual({"baseline", "comparator"}, {r["condition"] for r in self.responses("v0")})
        folder = self.results / "r1" / "v0"
        score = json.loads((folder / "reports/score-stub-judge-a.json").read_text("utf-8"))
        self.assertIsNone(score["release_gate"])
        reports = sorted(path.name for path in (folder / "reports").glob("compare-stub-judge-a-*"))
        self.assertEqual(["compare-stub-judge-a-baseline-vs-comparator.json"], reports)
        self.assertFalse((folder / "skill.md").exists())
        self.assertEqual(["comparator"], list(self.meta("v0")["skills"]))
        # The shared rows are paid for in shared/meta.json, so counting them here would double them.
        groups = len(self.cases) * TRIALS
        self.assertAlmostEqual(0.01 * groups * 2, self.meta("v0")["reported_costs"]["total_usd"])
        self.assertAlmostEqual(0.01 * groups * 2, self.meta("shared")["reported_costs"]["total_usd"])

    def test_unmetered_judge_needs_opt_in_and_is_recorded_unknown(self):
        judges = "--judges=stub-judge-a,stub-judge-unmetered"
        code, out, err = self.run_cli(judges)
        self.assertEqual(1, code)
        self.assertIn("did not report dollar cost", out + err)

        code, _, err = self.run_cli(judges, "--unmetered-judges=stub-judge-unmetered")
        self.assertEqual(0, code, err)
        meta = self.meta()
        judge_costs = meta["reported_costs"]["judges"]
        self.assertEqual(len(self.cases) * TRIALS, judge_costs["stub-judge-unmetered"]["unreported_groups"])
        self.assertEqual(0, judge_costs["stub-judge-a"]["unreported_groups"])
        self.assertEqual(
            [{"runner": "stub-judge-a", "model": "stub-judge-a"},
             {"runner": "stub-judge-unmetered", "model": "stub-judge-unmetered"}],
            meta["judges"],
        )

    def test_unmetered_flag_must_name_a_judge(self):
        code, _, err = self.run_cli("--unmetered-judges=stub-judge-unmetered")
        self.assertEqual(1, code)
        self.assertIn("--unmetered-judges", err)


class ResumeTest(RoundTestCase):
    COST = 0.01

    def env(self, **extra):
        return {"STUB_COST_USD": str(self.COST), **extra}

    def test_judge_failure_keeps_rows_and_resume_spends_once(self):
        leak = f"provider said key={SECRET} is bad"
        code, out, err = self.run_cli(
            env=self.env(STUB_FAIL_MATCH="CRITERIA-2", STUB_FAIL_MESSAGE=leak)
        )
        self.assertEqual(1, code)
        self.assertNotIn(SECRET, out + err)
        self.assertNotIn(SECRET, (self.results / "r1" / "c1" / "meta.json").read_text("utf-8"))
        self.assertEqual("failed", self.meta()["status"])
        groups = len(self.cases) * TRIALS
        # Candidate rows, plus the first judge's groups for case-1 in each trial.
        self.assertAlmostEqual(
            self.COST * groups + self.COST * TRIALS, self.meta()["reported_costs"]["total_usd"]
        )

        before = self.responses()
        self.assertEqual(groups * 3, len(before))
        self.calls_log.unlink()

        code, _, err = self.run_cli(env=self.env())
        self.assertEqual(0, code, err)
        self.assertEqual(before, self.responses())
        self.assertEqual(0, self.calls()["generate"])
        self.assertEqual(groups * 2 - TRIALS, self.calls()["judge"])
        meta = self.meta()
        self.assertEqual("complete", meta["status"])
        # Candidate rows from before the failure plus both judges' groups, each counted once.
        self.assertAlmostEqual(
            self.COST * groups + self.COST * groups * 2, meta["reported_costs"]["total_usd"]
        )
        self.assertAlmostEqual(self.COST * groups, meta["reported_costs"]["generation_usd"])
        self.assertAlmostEqual(
            self.COST * groups * 2,
            sum(item["usd"] for item in meta["reported_costs"]["judges"].values()),
        )

    def test_generation_failure_resumes_without_regenerating(self):
        code, _, _ = self.run_cli(env=self.env(STUB_FAIL_MATCH=SKILL_MARKER))
        self.assertEqual(1, code)
        self.assertEqual("failed", self.meta()["status"])
        shared_file = self.results / "r1" / "shared" / f"responses-t{TRIALS}.jsonl"
        shared_bytes = shared_file.read_bytes()
        self.assertEqual(len(self.cases) * TRIALS * 2, len(self.responses()))
        self.calls_log.unlink()

        self.assertEqual(0, self.run_cli(env=self.env())[0])
        self.assertEqual(shared_bytes, shared_file.read_bytes())
        self.assertEqual(len(self.cases) * TRIALS, self.calls()["generate"])

    def test_scenario_failure_resumes_past_the_empty_capture(self):
        turn = run_scenario_eval.load_scenario(ROOT / "evals/scenarios" / SCENARIO)["turns"][0]
        code, _, err = self.run_cli(
            f"--scenarios={SCENARIO}", env=self.env(STUB_FAIL_MATCH=turn["prompt"])
        )
        self.assertEqual(1, code, err)
        capture = self.results / "r1" / "shared" / "scenarios" / SCENARIO / "baseline-1.jsonl"
        self.assertTrue(capture.exists())
        self.assertEqual(0, capture.stat().st_size)

        code, _, err = self.run_cli(f"--scenarios={SCENARIO}", env=self.env())
        self.assertEqual(0, code, err)
        self.assertEqual(1, len(run_evals.read_jsonl(capture)))

    def test_billed_failures_reach_meta_and_reported_spend(self):
        code, _, _ = self.run_cli(
            f"--scenarios={SCENARIO}",
            env=self.env(STUB_FAIL_MATCH=SKILL_MARKER, STUB_FAIL_COST_USD="0.5"),
        )
        self.assertEqual(1, code)
        groups = len(self.cases) * TRIALS
        costs = self.meta()["reported_costs"]
        # Shared rows carry baseline and comparator; the one failed candidate attempt is on top.
        self.assertAlmostEqual(0.5, costs["generation_usd"])
        self.assertAlmostEqual(0.5, costs["total_usd"])
        shared_total = self.COST * groups * 2
        self.assertAlmostEqual(shared_total, costs["shared_usd_not_in_total"])
        self.assertAlmostEqual(0.5 + shared_total, run_round.reported_spend(self.results))

    def test_failed_scenario_spend_is_counted_once_and_does_not_block_resume(self):
        turn = run_scenario_eval.load_scenario(ROOT / "evals/scenarios" / SCENARIO)["turns"][0]
        code, _, err = self.run_cli(
            f"--scenarios={SCENARIO}",
            env=self.env(STUB_FAIL_MATCH=turn["prompt"], STUB_FAIL_COST_USD="0.25"),
        )
        self.assertEqual(1, code, err)
        shared = self.results / "r1" / "shared"
        capture = shared / "scenarios" / SCENARIO / "baseline-1.jsonl"
        self.assertFalse(run_round._captured(capture))
        self.assertTrue(run_evals.failed_spend_path(capture).exists())
        generated = self.COST * len(self.cases) * TRIALS * 2
        self.assertAlmostEqual(generated + 0.25, self.meta("shared")["reported_costs"]["total_usd"])

        code, _, err = self.run_cli(f"--scenarios={SCENARIO}", env=self.env())
        self.assertEqual(0, code, err)
        self.assertEqual(1, len(run_evals.read_jsonl(capture)))
        turns = len(run_scenario_eval.load_scenario(ROOT / "evals/scenarios" / SCENARIO)["turns"])
        expected = 0.25 + self.COST * turns * TRIALS * 2 + generated
        costs = self.meta("shared")["reported_costs"]
        # The re-captured row carries the failed attempt, so the ledger adds nothing more.
        self.assertAlmostEqual(expected, costs["total_usd"])

    def test_failed_judge_spend_is_in_the_variant_total(self):
        code, _, _ = self.run_cli(
            env=self.env(STUB_FAIL_MATCH="CRITERIA-2", STUB_FAIL_COST_USD="0.5")
        )
        self.assertEqual(1, code)
        costs = self.meta()["reported_costs"]
        # Each case-2 group failed once per judge at $0.50 (retries are 0).
        self.assertGreaterEqual(costs["judges"]["stub-judge-a"]["usd"], 0.5 * TRIALS)
        self.assertAlmostEqual(
            costs["generation_usd"] + sum(j["usd"] for j in costs["judges"].values()),
            costs["total_usd"],
        )

    def test_ledger_files_are_not_read_as_rows_and_shared_rows_copy_cleanly(self):
        self.assertEqual(0, self.run_cli(env=self.env())[0])
        shared = self.results / "r1" / "shared"
        rows = shared / f"responses-t{TRIALS}.jsonl"
        total = self.meta("shared")["reported_costs"]["total_usd"]
        run_evals.record_failed_spend(
            rows, {"case_id": "case-1", "trial": 1, "condition": "baseline", "runner": "stub"}, 0.5
        )
        # Settled: that key already has a row with a reported cost.
        self.assertAlmostEqual(total, run_round._shared_costs(shared)["total_usd"])
        run_evals.record_failed_spend(
            rows, {"case_id": "ghost", "trial": 1, "condition": "comparator", "runner": "stub"}, 0.5
        )
        costs = run_round._shared_costs(shared)
        self.assertAlmostEqual(total + 0.5, costs["total_usd"])
        self.assertAlmostEqual(self.COST * len(self.cases) * TRIALS + 0.5, costs["generation_usd"]["comparator"])

        code, _, err = self.run_cli(variant="c2=skill.md", env=self.env())
        self.assertEqual(0, code, err)
        self.assertEqual({"baseline", "comparator", "candidate"}, {r["condition"] for r in self.responses("c2")})
        self.assertFalse(list((self.results / "r1" / "c2").glob("*.failed-spend")))
        # Shared rows and their ledger stay out of the variant's own total.
        self.assertAlmostEqual(self.COST * len(self.cases) * TRIALS * 3, self.meta("c2")["reported_costs"]["total_usd"])

    def test_rerunning_a_complete_round_spends_nothing(self):
        self.assertEqual(0, self.run_cli(env=self.env())[0])
        before = self.responses()
        spent = self.meta()["reported_costs"]["total_usd"]
        self.calls_log.unlink()
        self.assertEqual(0, self.run_cli(env=self.env())[0])
        self.assertEqual(collections.Counter(), self.calls())
        self.assertEqual(before, self.responses())
        self.assertEqual(spent, self.meta()["reported_costs"]["total_usd"])


if __name__ == "__main__":
    unittest.main()
