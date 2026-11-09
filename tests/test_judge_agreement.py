import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import judge_agreement  # noqa: E402


def score(condition, level, case_id="case", trial=1, blocker=False, **overrides):
    row = {
        "case_id": case_id,
        "trial": trial,
        "condition": condition,
        "correctness": level,
        "autonomy": level,
        "actionability": level,
        "safety": level,
        "concision": level,
        "blocker": blocker,
        "notes": "",
    }
    return {**row, **overrides}


def group(baseline, candidate, case_id="case", trial=1, **candidate_fields):
    return [
        score("baseline", baseline, case_id, trial),
        score("candidate", candidate, case_id, trial, **candidate_fields),
    ]


class JudgeAgreementTest(unittest.TestCase):
    def test_identical_files_agree_everywhere(self):
        rows = group(3, 4, "one") + group(5, 2, "two")

        report = judge_agreement.summarize_agreement(rows, rows)

        self.assertEqual(2, report["shared_groups"])
        self.assertEqual(1.0, report["top_condition_agreement"])
        self.assertEqual(1.0, report["blocker_agreement"])
        self.assertEqual(
            {metric: 0.0 for metric in judge_agreement.run_evals.WEIGHTS},
            report["mean_absolute_difference"],
        )
        self.assertEqual([], report["disagreeing_groups"])
        self.assertEqual([], report["groups_only_in_a"])
        self.assertEqual([], report["groups_only_in_b"])

    def test_reports_per_group_top_condition_and_disagreements(self):
        first = group(3, 4, "agree") + group(3, 4, "differ")
        second = group(2, 5, "agree") + group(4, 3, "differ")

        report = judge_agreement.summarize_agreement(first, second)

        self.assertEqual(0.5, report["top_condition_agreement"])
        differ = {
            "case_id": "differ",
            "trial": 1,
            "top_a": "candidate",
            "top_b": "baseline",
            "top_agrees": False,
            "blockers_a": [],
            "blockers_b": [],
            "blockers_agree": True,
        }
        self.assertEqual([differ], report["disagreeing_groups"])
        self.assertEqual(
            [("agree", "candidate", "candidate"), ("differ", "candidate", "baseline")],
            [(item["case_id"], item["top_a"], item["top_b"]) for item in report["groups"]],
        )

    def test_tie_is_its_own_outcome(self):
        tied = group(4, 4, "tied")

        with self.subTest("tie against a winner disagrees"):
            report = judge_agreement.summarize_agreement(tied, group(3, 4, "tied"))
            entry = report["groups"][0]
            self.assertEqual(("tie", "candidate"), (entry["top_a"], entry["top_b"]))
            self.assertFalse(entry["top_agrees"])
            self.assertEqual(0.0, report["top_condition_agreement"])

        with self.subTest("two ties agree"):
            report = judge_agreement.summarize_agreement(tied, group(2, 2, "tied"))
            self.assertEqual("tie", report["groups"][0]["top_b"])
            self.assertTrue(report["groups"][0]["top_agrees"])
            self.assertEqual(1.0, report["top_condition_agreement"])

    def test_tie_is_on_weighted_score_and_survives_float_noise(self):
        # Different scores, equal weighted totals: +0.35 +0.25 -0.40 -0.10 -0.10.
        first = [
            score("baseline", 3),
            score("candidate", 3, correctness=4, autonomy=4, actionability=1, safety=2, concision=2),
        ]

        report = judge_agreement.summarize_agreement(first, first)

        self.assertEqual("tie", report["groups"][0]["top_a"])

    def test_top_condition_follows_the_weights_not_the_raw_sum(self):
        rows = [
            score("baseline", 3, correctness=5, concision=1),
            score("candidate", 3, correctness=1, concision=5),
        ]

        report = judge_agreement.summarize_agreement(rows, rows)

        self.assertEqual("baseline", report["groups"][0]["top_a"])

    def test_blocker_disagreement_marks_the_group(self):
        first = group(3, 4, blocker=True)
        second = group(3, 4)

        report = judge_agreement.summarize_agreement(first, second)

        self.assertEqual(1.0, report["top_condition_agreement"])
        self.assertEqual(0.0, report["blocker_agreement"])
        entry = report["disagreeing_groups"][0]
        self.assertEqual(["candidate"], entry["blockers_a"])
        self.assertEqual([], entry["blockers_b"])
        self.assertFalse(entry["blockers_agree"])

    def test_blockers_on_different_conditions_disagree(self):
        first = [score("baseline", 3, blocker=True), score("candidate", 4)]
        second = [score("baseline", 3), score("candidate", 4, blocker=True)]

        report = judge_agreement.summarize_agreement(first, second)

        self.assertEqual(0.0, report["blocker_agreement"])

    def test_mean_absolute_difference_is_per_dimension_over_shared_rows(self):
        first = [score("baseline", 3), score("candidate", 4, safety=5)]
        second = [score("baseline", 2), score("candidate", 4, safety=2)]

        difference = judge_agreement.summarize_agreement(first, second)[
            "mean_absolute_difference"
        ]

        self.assertEqual(0.5, difference["correctness"])
        self.assertEqual(0.5, difference["concision"])
        self.assertEqual(2.0, difference["safety"])

    def test_groups_in_one_file_are_reported_and_excluded(self):
        first = group(3, 4, "shared") + group(3, 4, "first-only")
        second = group(3, 4, "shared") + group(5, 1, "second-only", trial=2)

        report = judge_agreement.summarize_agreement(first, second)

        self.assertEqual([{"case_id": "first-only", "trial": 1}], report["groups_only_in_a"])
        self.assertEqual([{"case_id": "second-only", "trial": 2}], report["groups_only_in_b"])
        self.assertEqual(1, report["shared_groups"])
        self.assertEqual(["shared"], [item["case_id"] for item in report["groups"]])
        self.assertEqual(1.0, report["top_condition_agreement"])

    def test_same_case_on_another_trial_is_a_different_group(self):
        report = judge_agreement.summarize_agreement(
            group(3, 4, "case", trial=1), group(3, 4, "case", trial=2)
        )

        self.assertEqual([{"case_id": "case", "trial": 1}], report["groups_only_in_a"])
        self.assertEqual([{"case_id": "case", "trial": 2}], report["groups_only_in_b"])

    def test_no_shared_groups_reports_null_not_zero(self):
        report = judge_agreement.summarize_agreement(group(3, 4, "one"), group(3, 4, "two"))

        self.assertEqual(0, report["shared_groups"])
        self.assertIsNone(report["top_condition_agreement"])
        self.assertIsNone(report["blocker_agreement"])
        self.assertIsNone(report["mean_absolute_difference"]["correctness"])

    def test_cost_and_notes_do_not_affect_agreement(self):
        first = group(3, 4, group_cost_usd=0.5, notes="one view")
        second = group(3, 4, group_cost_usd=None, notes="another view")

        self.assertEqual([], judge_agreement.summarize_agreement(first, second)["disagreeing_groups"])

    def test_rejects_invalid_rows_and_names_the_file(self):
        good = group(3, 4)
        out_of_range = [score("baseline", 9), score("candidate", 4)]
        with self.assertRaisesRegex(ValueError, "second score file.*between 1 and 5"):
            judge_agreement.summarize_agreement(good, out_of_range)
        with self.assertRaisesRegex(ValueError, "first score file.*duplicate"):
            judge_agreement.summarize_agreement(good + good, good)

    def test_rejects_shared_group_judged_on_other_conditions(self):
        first = group(3, 4)
        second = [score("baseline", 3), score("comparator", 4)]

        with self.assertRaisesRegex(ValueError, "case/trial 1"):
            judge_agreement.summarize_agreement(first, second)

    def test_cli_prints_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(tmp) / "a.jsonl", Path(tmp) / "b.jsonl"]
            for path, rows in zip(paths, (group(4, 4), group(3, 4))):
                path.write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
                )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(0, judge_agreement.main([str(path) for path in paths]))

        self.assertEqual("tie", json.loads(stdout.getvalue())["groups"][0]["top_a"])


if __name__ == "__main__":
    unittest.main()
