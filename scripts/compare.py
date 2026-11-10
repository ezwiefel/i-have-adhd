#!/usr/bin/env python3
"""Report the paired numbers the keep/drop rules read for two conditions, with no model calls.

Every delta is treatment minus reference. Trials are averaged within a case first,
then cases are paired, so each case counts once however many trials it has.
"""

import argparse
import json
import math
import statistics
from collections import defaultdict
from operator import itemgetter
from pathlib import Path
from typing import Any, Callable, Optional

import run_evals


CEILING_CONDITION = "comparator"
CEILING_THRESHOLD = 4.7

Rows = list[dict[str, Any]]


def _weighted(row: dict[str, Any]) -> float:
    return sum(row[metric] * weight for metric, weight in run_evals.WEIGHTS.items())


def _round(value: float) -> float:
    # Float noise in the weights must not move a number across the 4.7 or +0.3 lines.
    return round(value, 9)


def _case_means(rows: Rows, value: Callable[[dict[str, Any]], float]) -> dict[str, float]:
    by_case: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_case[row["case_id"]].append(value(row))
    return {case_id: _round(statistics.fmean(values)) for case_id, values in by_case.items()}


def _paired(deltas: list[float]) -> dict[str, Any]:
    """Mean delta and its standard error (the Noise band), from sample stdev over cases."""
    n = len(deltas)
    return {
        "n_cases": n,
        "mean_delta": _round(statistics.fmean(deltas)) if deltas else None,
        "standard_error": _round(statistics.stdev(deltas) / math.sqrt(n)) if n >= 2 else None,
    }


def _metric_deltas(
    reference: Rows, treatment: Rows, value: Callable[[dict[str, Any]], float]
) -> list[float]:
    before, after = _case_means(reference, value), _case_means(treatment, value)
    return [_round(after[case_id] - before[case_id]) for case_id in sorted(before)]


def _targeted(
    change_id: str,
    cases: Rows,
    deltas: dict[str, float],
    ceiling_means: Optional[dict[str, float]],
) -> dict[str, Any]:
    targeted = sorted(case["id"] for case in cases if change_id in case.get("targets", []))
    scored = [case_id for case_id in targeted if case_id in deltas]
    section = {
        "change_id": change_id,
        "status": "ok",
        "message": None,
        "ceiling_threshold": CEILING_THRESHOLD,
        "targeted_cases": scored,
        "unscored_targeted_cases": [case_id for case_id in targeted if case_id not in deltas],
        "ceiling_cases": [],
        "included_cases": [],
        **_paired([]),
    }
    if not targeted:
        return {
            **section,
            "status": "no_targeted_cases",
            "message": f"No case in the cases file targets change {change_id}.",
        }
    if not scored:
        return {
            **section,
            "status": "no_scored_targeted_cases",
            "message": f"No case targeting change {change_id} has scores for these conditions.",
        }
    if ceiling_means is None:
        # Without v0's scores any mean would silently include ceiling cases, so withhold it.
        return {
            **section,
            "status": "ceiling_unknown",
            "message": (
                f"Condition {CEILING_CONDITION!r} is not in the score file, so ceiling cases "
                "cannot be identified and the targeted mean is withheld."
            ),
            "ceiling_cases": None,
            "included_cases": None,
        }

    included = [case_id for case_id in scored if ceiling_means[case_id] <= CEILING_THRESHOLD]
    section["included_cases"] = included
    section["ceiling_cases"] = [
        {"case_id": case_id, "comparator_mean_weighted_score": ceiling_means[case_id]}
        for case_id in scored
        if case_id not in included
    ]
    if not included:
        return {
            **section,
            "status": "all_targeted_cases_at_ceiling",
            "message": f"Every scored case targeting change {change_id} is a ceiling case.",
        }
    return {**section, **_paired([deltas[case_id] for case_id in included])}


def compare(
    scores: Rows,
    cases: Rows,
    reference: str,
    treatment: str,
    change_id: Optional[str] = None,
) -> dict[str, Any]:
    if reference == treatment:
        raise ValueError(f"Choose two different conditions, not {reference} twice")
    errors = run_evals.validate_cases(cases)
    if errors:
        raise ValueError("\n".join(errors))

    grouped: dict[str, Rows] = defaultdict(list)
    for index, row in enumerate(scores, start=1):
        run_evals._validate_score(row, index)
        grouped[row["condition"]].append(row)
    absent = [condition for condition in (reference, treatment) if condition not in grouped]
    if absent:
        raise ValueError(f"Score file has no rows for {', '.join(absent)}")
    unknown = {row["case_id"] for row in scores} - {case["id"] for case in cases}
    if unknown:
        raise ValueError(f"Scores for cases not in the cases file: {', '.join(sorted(map(str, unknown)))}")

    has_ceiling = CEILING_CONDITION in grouped
    compared = {condition: grouped[condition] for condition in (reference, treatment)}
    if change_id is not None and has_ceiling:
        # A ceiling read from other rows than the ones compared would not describe them.
        compared[CEILING_CONDITION] = grouped[CEILING_CONDITION]
    run_evals._check_pairing(compared, reference)

    before = _case_means(grouped[reference], _weighted)
    after = _case_means(grouped[treatment], _weighted)
    per_case = [
        {
            "case_id": case_id,
            "reference_mean": before[case_id],
            "treatment_mean": after[case_id],
            "delta": _round(after[case_id] - before[case_id]),
        }
        for case_id in sorted(before)
    ]
    targeted = None
    if change_id is not None:
        ceiling_means = _case_means(grouped[CEILING_CONDITION], _weighted) if has_ceiling else None
        deltas = {item["case_id"]: item["delta"] for item in per_case}
        targeted = _targeted(change_id, cases, deltas, ceiling_means)

    return {
        "reference": reference,
        "treatment": treatment,
        "delta_definition": (
            "treatment minus reference; per-case means over trials, then paired across cases"
        ),
        "cases": per_case,
        "overall": {
            "weighted_score": _paired([item["delta"] for item in per_case]),
            **{
                metric: _paired(
                    _metric_deltas(grouped[reference], grouped[treatment], itemgetter(metric))
                )
                for metric in run_evals.GATED
            },
        },
        "blockers": {
            condition: sum(row["blocker"] for row in grouped[condition])
            for condition in (reference, treatment)
        },
        "targeted": targeted,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scores", type=Path)
    parser.add_argument("cases", type=Path)
    parser.add_argument("reference", choices=sorted(run_evals.CONDITIONS))
    parser.add_argument("treatment", choices=sorted(run_evals.CONDITIONS))
    parser.add_argument("--change", help="Change ID, such as C3, to report targeted cases for")
    args = parser.parse_args(argv)
    report = compare(
        run_evals.read_jsonl(args.scores),
        run_evals.load_cases(args.cases),
        args.reference,
        args.treatment,
        args.change,
    )
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
