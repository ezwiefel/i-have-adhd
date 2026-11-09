#!/usr/bin/env python3
"""Report per-condition format diagnostics from recorded responses, with no model calls."""

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

import run_evals


# Rule 10 of the skill names most of these, so the check grades what the rule bans.
OPENING_FILLER = re.compile(
    r"\W*(?:"
    r"(?:great|good|excellent) (?:question|point)\b"
    r"|(?:sure(?: thing)?|certainly|absolutely|of course)\s*[!.,:]"
    r"|let me\b|i['’]ll\b"
    r"|looking at your\b|to answer your question\b"
    r"|(?:happy|glad) to help\b"
    r")",
    re.IGNORECASE,
)
CLOSING_PLEASANTRY = re.compile(
    r"(?:^|[.!?]\s)\W*(?:"
    r"let me know if\b|hope (?:this|that|it) helps\b"
    r"|happy to (?:clarify|help)\b|feel free to\b|don['’]t hesitate\b|good luck\b"
    r"|if you (?:have|need) any\b"
    r")",
    re.IGNORECASE,
)
# Hyphen only: the "prove I have ADHD" case legitimately echoes the spaced forms.
SELF_REFERENCE = re.compile(
    r"\bi-have-adhd\b|\badhd[- ]mode\b|\bskill\.md\b|\bresponse[-_]style\b"
    r"|\b(?:the|this|my) skill\b"
    r"|\b(?:hidden|system|injected|my) (?:instructions?|prompt)\b",
    re.IGNORECASE,
)
# Runners pass no tools, so any of this is a call the model only pretended to make.
TOOL_CALL_TEXT = re.compile(
    r"</?(?:\w+:)?(?:function_calls|invoke|tool_calls?|tool_use)\b"
    r'|"(?:tool_use|tool_calls?|function_call)"'
    r"|^\s*(?:Bash|Read|Write|Edit|Grep|Glob)\([^)\n]*\)\s*$",
    re.MULTILINE,
)
LIST_ITEM = re.compile(r"(\s*)(?:[-*+]|(\d{1,3})[.)])\s+\S")
FLAGS = (
    "opening_filler",
    "closing_pleasantry",
    "opener_or_closer",
    "numbered_list",
    "self_reference",
    "tool_call_text",
)


def _messages(response: str) -> list[str]:
    """Return the assistant text of a row, leaving user prompts out of the checks.

    Scenario rows store their transcript as a JSON string. Anything else that
    happens to parse as JSON is a model's answer and stays plain text.
    """
    try:
        turns = json.loads(response)
    except json.JSONDecodeError:
        return [response]
    if (
        isinstance(turns, list)
        and turns
        and all(
            isinstance(turn, dict) and "turn_id" in turn and isinstance(turn.get("response"), str)
            for turn in turns
        )
    ):
        return [turn["response"] for turn in turns]
    return [response]


def _list_items(text: str) -> list[Optional[tuple[int, bool]]]:
    """Return (indent, numbered) per list line, None for prose; blanks and code are skipped."""
    items: list[Optional[tuple[int, bool]]] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
        elif stripped and not in_fence:
            match = LIST_ITEM.match(line)
            if match:
                items.append((len(match[1].expandtabs(4)), match[2] is not None))
            # An indented non-item line continues the item above it.
            elif not line[0].isspace():
                items.append(None)
    return items


def _largest_list_group(text: str) -> int:
    largest = 0
    counts: dict[tuple[int, bool], int] = {}
    for item in _list_items(text):
        if item is None:
            counts.clear()
            continue
        # A shallower item or a marker change at this depth ends the list.
        counts = {key: count for key, count in counts.items() if key[0] < item[0] or key == item}
        counts[item] = counts.get(item, 0) + 1
        largest = max(largest, counts[item])
    return largest


def check_response(response: str) -> dict[str, Any]:
    """Grade one row: a transcript shows a trait when any assistant turn does."""
    messages = _messages(response)
    opener = any(OPENING_FILLER.match(text) for text in messages)
    closer = any(
        CLOSING_PLEASANTRY.search(text.strip().rpartition("\n")[2]) for text in messages
    )
    return {
        "opening_filler": opener,
        "closing_pleasantry": closer,
        "opener_or_closer": opener or closer,
        "numbered_list": any(
            item is not None and item[1] for text in messages for item in _list_items(text)
        ),
        "self_reference": any(SELF_REFERENCE.search(text) for text in messages),
        "tool_call_text": any(TOOL_CALL_TEXT.search(text) for text in messages),
        "largest_list_group": max(_largest_list_group(text) for text in messages),
    }


def summarize_formats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    # Same contract as `measure`: valid rows, one runner and model, paired conditions.
    run_evals.summarize_usage(rows)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["condition"]].append(check_response(row["response"]))

    conditions: dict[str, dict[str, Any]] = {}
    for condition, checks in sorted(grouped.items()):
        summary: dict[str, Any] = {"rows": len(checks)}
        for flag in FLAGS:
            count = sum(check[flag] for check in checks)
            summary[flag] = {"count": count, "rate": count / len(checks)}
        sizes = [check["largest_list_group"] for check in checks]
        summary["largest_list_group"] = {"mean": sum(sizes) / len(sizes), "max": max(sizes)}
        conditions[condition] = summary
    return {"conditions": conditions}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("responses", type=Path)
    args = parser.parse_args(argv)
    report = summarize_formats(run_evals.read_jsonl(args.responses))
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
