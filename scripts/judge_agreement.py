#!/usr/bin/env python3
"""Report where two judges' score files agree and disagree, with no model calls."""

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import run_evals


TIE = "tie"

Group = dict[str, dict[str, Any]]
Key = tuple[str, int]


def _load_groups(rows: list[dict[str, Any]], label: str) -> dict[Key, Group]:
    try:
        # Same contract as `score`: valid rows and conditions paired on identical groups.
        run_evals.summarize_scores(rows)
    except ValueError as exc:
        raise ValueError(f"{label} score file: {exc}") from exc
    groups: dict[Key, Group] = defaultdict(dict)
    for row in rows:
        groups[(row["case_id"], row["trial"])][row["condition"]] = row
    return dict(groups)


def _top_condition(group: Group) -> str:
    weighted = {
        condition: sum(row[metric] * weight for metric, weight in run_evals.WEIGHTS.items())
        for condition, row in group.items()
    }
    # Rounded so float noise in the weights cannot break a genuine tie.
    best = round(max(weighted.values()), 9)
    leaders = [condition for condition, value in weighted.items() if round(value, 9) == best]
    return leaders[0] if len(leaders) == 1 else TIE


def _blocked(group: Group) -> list[str]:
    return sorted(condition for condition, row in group.items() if row["blocker"])


def _rate(hits: int, total: int) -> Optional[float]:
    return hits / total if total else None


def _keys(keys: set[Key]) -> list[dict[str, Any]]:
    return [{"case_id": case_id, "trial": trial} for case_id, trial in sorted(keys)]


def summarize_agreement(
    first: list[dict[str, Any]], second: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compare two judges over the groups both scored.

    A group in only one file has nothing to compare against, so it is listed
    under `groups_only_in_a` or `groups_only_in_b` and left out of every figure.
    """
    a = _load_groups(first, "first")
    b = _load_groups(second, "second")
    shared = sorted(a.keys() & b.keys())
    mismatched = [key for key in shared if a[key].keys() != b[key].keys()]
    if mismatched:
        raise ValueError(
            "Score files judged different conditions for "
            + run_evals._describe_rows(mismatched)
        )

    groups = []
    for key in shared:
        top_a, top_b = _top_condition(a[key]), _top_condition(b[key])
        blockers_a, blockers_b = _blocked(a[key]), _blocked(b[key])
        groups.append(
            {
                "case_id": key[0],
                "trial": key[1],
                "top_a": top_a,
                "top_b": top_b,
                "top_agrees": top_a == top_b,
                "blockers_a": blockers_a,
                "blockers_b": blockers_b,
                "blockers_agree": blockers_a == blockers_b,
            }
        )

    pairs = [(a[key][condition], b[key][condition]) for key in shared for condition in a[key]]
    return {
        "shared_groups": len(shared),
        "groups_only_in_a": _keys(a.keys() - b.keys()),
        "groups_only_in_b": _keys(b.keys() - a.keys()),
        "top_condition_agreement": _rate(sum(g["top_agrees"] for g in groups), len(groups)),
        "blocker_agreement": _rate(sum(g["blockers_agree"] for g in groups), len(groups)),
        "mean_absolute_difference": {
            metric: (
                sum(abs(x[metric] - y[metric]) for x, y in pairs) / len(pairs) if pairs else None
            )
            for metric in run_evals.WEIGHTS
        },
        "groups": groups,
        "disagreeing_groups": [
            g for g in groups if not (g["top_agrees"] and g["blockers_agree"])
        ],
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scores_a", type=Path)
    parser.add_argument("scores_b", type=Path)
    args = parser.parse_args(argv)
    report = summarize_agreement(
        run_evals.read_jsonl(args.scores_a), run_evals.read_jsonl(args.scores_b)
    )
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
