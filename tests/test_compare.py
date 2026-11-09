import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import compare  # noqa: E402


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


def case(case_id, *targets):
    row = {
        "id": case_id,
        "category": "test",
        "prompt": "prompt",
        "risk": "low",
        "criteria": ["criterion"],
    }
    return {**row, "targets": list(targets)} if targets else row


def trials(condition, case_id, *levels, **fields):
    return [
        score(condition, level, case_id, trial, **fields)
        for trial, level in enumerate(levels, start=1)
    ]


# Comparator mean -> candidate mean, delta:
# a 3.5 -> 4.5, +1.0   b 4.0 -> 3.5, -0.5   c 5.0 -> 4.5, -0.5 (ceiling)   d 2.0 -> 4.0, +2.0
FIXTURE_SCORES = [
    *trials("comparator", "a", 3, 4),
    *trials("candidate", "a", 4, 5),
    *trials("comparator", "b", 4, 4),
    *trials("candidate", "b", 3, 4),
    *trials("comparator", "c", 5, 5),
    *trials("candidate", "c", 5, 4),
    *trials("comparator", "d", 2, 2),
    *trials("candidate", "d", 4, 4),
]
FIXTURE_CASES = [case("a", "C3"), case("b", "C3", "C4"), case("c", "C3"), case("d")]


def paired(case_id, comparator, candidate, baseline=None):
    rows = [score("comparator", comparator, case_id), score("candidate", candidate, case_id)]
    return rows + ([score("baseline", baseline, case_id)] if baseline is not None else [])


class DeltaTest(unittest.TestCase):
    def test_per_case_deltas_are_treatment_minus_reference_means_over_trials(self):
        report = compare.compare(FIXTURE_SCORES, FIXTURE_CASES, "comparator", "candidate")

        self.assertEqual("comparator", report["reference"])
        self.assertEqual("candidate", report["treatment"])
        self.assertEqual(
            [
                {"case_id": "a", "reference_mean": 3.5, "treatment_mean": 4.5, "delta": 1.0},
                {"case_id": "b", "reference_mean": 4.0, "treatment_mean": 3.5, "delta": -0.5},
                {"case_id": "c", "reference_mean": 5.0, "treatment_mean": 4.5, "delta": -0.5},
                {"case_id": "d", "reference_mean": 2.0, "treatment_mean": 4.0, "delta": 2.0},
            ],
            report["cases"],
        )

    def test_mean_delta_and_standard_error_on_hand_computed_fixture(self):
        weighted = compare.compare(FIXTURE_SCORES, FIXTURE_CASES, "comparator", "candidate")[
            "overall"
        ]["weighted_score"]

        self.assertEqual(4, weighted["n_cases"])
        self.assertEqual(0.5, weighted["mean_delta"])
        # Deviations 0.5, -1, -1, 1.5: variance 4.5/3 = 1.5, so SE = sqrt(1.5 / 4).
        self.assertAlmostEqual(math.sqrt(0.375), weighted["standard_error"], places=9)

    def test_swapping_the_conditions_flips_the_sign(self):
        weighted = compare.compare(FIXTURE_SCORES, FIXTURE_CASES, "candidate", "comparator")[
            "overall"
        ]["weighted_score"]

        self.assertEqual(-0.5, weighted["mean_delta"])

    def test_each_case_counts_once_however_many_trials_it_has(self):
        rows = [
            *trials("baseline", "many", 3, 3, 3),
            *trials("candidate", "many", 5, 5, 5),
            *trials("baseline", "one", 3),
            *trials("candidate", "one", 3),
        ]

        weighted = compare.compare(rows, [case("many"), case("one")], "baseline", "candidate")[
            "overall"
        ]["weighted_score"]

        self.assertEqual(1.0, weighted["mean_delta"])

    def test_delta_uses_the_weights_not_the_raw_sum(self):
        rows = [
            score("baseline", 3),
            score("candidate", 3, correctness=5, concision=1),
        ]

        weighted = compare.compare(rows, [case("case")], "baseline", "candidate")["overall"][
            "weighted_score"
        ]

        self.assertEqual(0.5, weighted["mean_delta"])  # +2 * 0.35, -2 * 0.10

    def test_standard_error_over_one_case_is_null(self):
        weighted = compare.compare(
            paired("only", 3, 4), [case("only")], "comparator", "candidate"
        )["overall"]["weighted_score"]

        self.assertEqual(1, weighted["n_cases"])
        self.assertEqual(1.0, weighted["mean_delta"])
        self.assertIsNone(weighted["standard_error"])

    def test_identical_conditions_have_zero_standard_error_over_two_cases(self):
        rows = paired("one", 4, 4) + paired("two", 3, 3)

        weighted = compare.compare(rows, [case("one"), case("two")], "comparator", "candidate")[
            "overall"
        ]["weighted_score"]

        self.assertEqual(0.0, weighted["mean_delta"])
        self.assertEqual(0.0, weighted["standard_error"])

    def test_compares_any_two_conditions_without_baseline(self):
        report = compare.compare(paired("one", 3, 4), [case("one")], "comparator", "candidate")

        self.assertEqual(1.0, report["overall"]["weighted_score"]["mean_delta"])

    def test_correctness_and_safety_deltas_are_paired_means(self):
        rows = [
            score("baseline", 3, "one", correctness=4, safety=2),
            score("candidate", 3, "one", correctness=5, safety=2),
            score("baseline", 3, "two", correctness=4, safety=5),
            score("candidate", 3, "two", correctness=4, safety=2),
        ]

        overall = compare.compare(rows, [case("one"), case("two")], "baseline", "candidate")[
            "overall"
        ]

        self.assertEqual(0.5, overall["correctness"]["mean_delta"])
        self.assertEqual(0.5, overall["correctness"]["standard_error"])
        self.assertEqual(-1.5, overall["safety"]["mean_delta"])
        self.assertEqual(1.5, overall["safety"]["standard_error"])

    def test_blocker_counts_are_rows_per_condition(self):
        rows = [
            score("comparator", 3, "one", 1, blocker=True),
            score("comparator", 3, "one", 2),
            score("candidate", 3, "one", 1, blocker=True),
            score("candidate", 3, "one", 2, blocker=True),
            score("baseline", 3, "one", 1, blocker=True),
            score("baseline", 3, "one", 2, blocker=True),
        ]

        report = compare.compare(rows, [case("one")], "comparator", "candidate")

        self.assertEqual({"comparator": 1, "candidate": 2}, report["blockers"])

    def test_no_change_id_gives_no_targeted_section(self):
        report = compare.compare(FIXTURE_SCORES, FIXTURE_CASES, "comparator", "candidate")

        self.assertIsNone(report["targeted"])


class TargetedTest(unittest.TestCase):
    def targeted(self, scores, cases, reference="comparator", treatment="candidate", change="C3"):
        return compare.compare(scores, cases, reference, treatment, change)["targeted"]

    def test_leaves_out_and_lists_ceiling_cases(self):
        targeted = self.targeted(FIXTURE_SCORES, FIXTURE_CASES)

        self.assertEqual("ok", targeted["status"])
        self.assertIsNone(targeted["message"])
        self.assertEqual("C3", targeted["change_id"])
        self.assertEqual(["a", "b"], targeted["included_cases"])
        self.assertEqual(
            [{"case_id": "c", "comparator_mean_weighted_score": 5.0}],
            targeted["ceiling_cases"],
        )
        self.assertEqual(2, targeted["n_cases"])
        # With c it would be 0.0. Deviations +-0.75: SE = sqrt(1.125 / 2).
        self.assertEqual(0.25, targeted["mean_delta"])
        self.assertEqual(0.75, targeted["standard_error"])

    def test_only_cases_naming_the_change_are_targeted(self):
        targeted = self.targeted(FIXTURE_SCORES, FIXTURE_CASES, change="C4")

        self.assertEqual(["b"], targeted["included_cases"])
        self.assertEqual([], targeted["ceiling_cases"])
        self.assertEqual(-0.5, targeted["mean_delta"])
        self.assertIsNone(targeted["standard_error"])

    def test_score_of_exactly_the_threshold_is_not_a_ceiling_case(self):
        rows = [score("comparator", 5, concision=2), score("candidate", 5, concision=3)]

        targeted = self.targeted(rows, [case("case", "C3")])

        self.assertEqual(["case"], targeted["included_cases"])
        self.assertEqual([], targeted["ceiling_cases"])

    def test_just_above_the_threshold_is_a_ceiling_case(self):
        rows = [score("comparator", 5, concision=3), score("candidate", 5)]

        targeted = self.targeted(rows, [case("case", "C3")])

        self.assertEqual([], targeted["included_cases"])
        self.assertEqual(
            [{"case_id": "case", "comparator_mean_weighted_score": 4.8}],
            targeted["ceiling_cases"],
        )

    def test_ceiling_is_the_mean_over_trials(self):
        rows = [*trials("comparator", "case", 5, 4), *trials("candidate", "case", 5, 5)]

        targeted = self.targeted(rows, [case("case", "C3")])

        self.assertEqual(["case"], targeted["included_cases"])

    def test_ceiling_reads_comparator_even_when_it_is_not_compared(self):
        rows = paired("high", 5, 4, baseline=3) + paired("low", 3, 5, baseline=4)
        cases = [case("high", "C3"), case("low", "C3")]

        targeted = self.targeted(rows, cases, "baseline", "candidate")

        self.assertEqual(["low"], targeted["included_cases"])
        self.assertEqual(["high"], [item["case_id"] for item in targeted["ceiling_cases"]])
        self.assertEqual(1.0, targeted["mean_delta"])

    def test_ceiling_reads_comparator_when_it_is_the_treatment(self):
        rows = paired("high", 5, 4) + paired("low", 3, 5)
        cases = [case("high", "C3"), case("low", "C3")]

        targeted = self.targeted(rows, cases, "candidate", "comparator")

        self.assertEqual(["low"], targeted["included_cases"])

    def test_without_comparator_rows_the_targeted_mean_is_withheld(self):
        rows = [score("baseline", 5), score("candidate", 5)]

        targeted = self.targeted(rows, [case("case", "C3")], "baseline", "candidate")

        self.assertEqual("ceiling_unknown", targeted["status"])
        self.assertIn("comparator", targeted["message"])
        self.assertIsNone(targeted["mean_delta"])
        self.assertIsNone(targeted["standard_error"])
        self.assertIsNone(targeted["ceiling_cases"])
        self.assertIsNone(targeted["included_cases"])
        self.assertEqual(["case"], targeted["targeted_cases"])

    def test_change_with_no_targeted_cases_reports_that_not_a_zero(self):
        targeted = self.targeted(FIXTURE_SCORES, FIXTURE_CASES, change="C9")

        self.assertEqual("no_targeted_cases", targeted["status"])
        self.assertIn("C9", targeted["message"])
        self.assertEqual(0, targeted["n_cases"])
        self.assertIsNone(targeted["mean_delta"])
        self.assertIsNone(targeted["standard_error"])
        self.assertEqual([], targeted["targeted_cases"])

    def test_targeted_cases_that_were_not_scored_are_reported(self):
        rows = paired("scored", 3, 4)
        cases = [case("scored"), case("absent", "C3")]

        targeted = self.targeted(rows, cases)

        self.assertEqual("no_scored_targeted_cases", targeted["status"])
        self.assertIn("C3", targeted["message"])
        self.assertEqual(["absent"], targeted["unscored_targeted_cases"])
        self.assertIsNone(targeted["mean_delta"])

    def test_partly_scored_targets_are_listed_alongside_the_mean(self):
        cases = [case("scored", "C3"), case("absent", "C3")]

        targeted = self.targeted(paired("scored", 3, 4), cases)

        self.assertEqual("ok", targeted["status"])
        self.assertEqual(["absent"], targeted["unscored_targeted_cases"])
        self.assertEqual(1.0, targeted["mean_delta"])

    def test_every_targeted_case_at_ceiling_reports_that_not_a_zero(self):
        targeted = self.targeted(paired("high", 5, 4), [case("high", "C3")])

        self.assertEqual("all_targeted_cases_at_ceiling", targeted["status"])
        self.assertIn("C3", targeted["message"])
        self.assertEqual(0, targeted["n_cases"])
        self.assertIsNone(targeted["mean_delta"])
        self.assertEqual(["high"], [item["case_id"] for item in targeted["ceiling_cases"]])


class InputErrorTest(unittest.TestCase):
    def test_unpaired_conditions_are_an_error(self):
        rows = [
            *trials("comparator", "case", 3, 4),
            *trials("candidate", "case", 3),
        ]

        with self.assertRaisesRegex(ValueError, "case/trial 2"):
            compare.compare(rows, [case("case")], "comparator", "candidate")

    def test_duplicate_rows_are_an_error(self):
        rows = paired("case", 3, 4) + [score("candidate", 4, "case")]

        with self.assertRaisesRegex(ValueError, "duplicate"):
            compare.compare(rows, [case("case")], "comparator", "candidate")

    def test_comparator_used_for_ceiling_must_be_paired_too(self):
        rows = [
            score("baseline", 3, "one"),
            score("candidate", 4, "one"),
            score("baseline", 3, "two"),
            score("candidate", 4, "two"),
            score("comparator", 5, "one"),
        ]
        cases = [case("one", "C3"), case("two", "C3")]

        with self.assertRaisesRegex(ValueError, "two/trial 1"):
            compare.compare(rows, cases, "baseline", "candidate", "C3")

    def test_unrelated_third_condition_need_not_be_paired(self):
        rows = paired("one", 3, 4) + paired("two", 3, 4, baseline=2)

        report = compare.compare(rows, [case("one"), case("two")], "comparator", "candidate", "C3")

        self.assertEqual(1.0, report["overall"]["weighted_score"]["mean_delta"])

    def test_case_missing_from_the_cases_file_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "stray"):
            compare.compare(paired("stray", 3, 4), [case("known")], "comparator", "candidate")

    def test_condition_not_in_the_score_file_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "baseline"):
            compare.compare(paired("case", 3, 4), [case("case")], "baseline", "candidate")

    def test_comparing_a_condition_with_itself_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "two different"):
            compare.compare(paired("case", 3, 4), [case("case")], "candidate", "candidate")

    def test_invalid_score_row_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 5"):
            compare.compare(
                [score("comparator", 9), score("candidate", 4)],
                [case("case")],
                "comparator",
                "candidate",
            )

    def test_invalid_cases_file_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "targets"):
            compare.compare(
                paired("case", 3, 4),
                [{**case("case"), "targets": "C3"}],
                "comparator",
                "candidate",
            )


class CliTest(unittest.TestCase):
    def run_cli(self, *extra):
        with tempfile.TemporaryDirectory() as tmp:
            scores = Path(tmp) / "scores.jsonl"
            cases = Path(tmp) / "cases.jsonl"
            for path, rows in ((scores, FIXTURE_SCORES), (cases, FIXTURE_CASES)):
                path.write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
                )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = compare.main(
                    [str(scores), str(cases), "comparator", "candidate", *extra]
                )
        return code, json.loads(stdout.getvalue())

    def test_prints_json_with_a_change_id(self):
        code, report = self.run_cli("--change", "C3")

        self.assertEqual(0, code)
        self.assertEqual(0.25, report["targeted"]["mean_delta"])

    def test_change_id_is_optional(self):
        code, report = self.run_cli()

        self.assertEqual(0, code)
        self.assertIsNone(report["targeted"])
        self.assertEqual({"comparator": 0, "candidate": 0}, report["blockers"])


if __name__ == "__main__":
    unittest.main()
