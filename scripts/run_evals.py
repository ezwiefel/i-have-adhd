#!/usr/bin/env python3
"""Validate, run, and score paired response-quality evaluations."""

import argparse
from contextlib import contextmanager
import json
import math
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "cases.jsonl"
WEIGHTS = {
    "correctness": 0.35,
    "autonomy": 0.25,
    "actionability": 0.20,
    "safety": 0.10,
    "concision": 0.10,
}
CONDITIONS = {"baseline", "candidate", "comparator"}
PROGRESS_EVERY = 10


@contextmanager
def _neutral_cwd() -> Iterator[str]:
    """Yield an empty scratch directory for one runner invocation.

    Agent CLIs adopt their working directory as project context: run one inside
    this checkout and it answers prompts by inspecting the harness instead of
    the task. That contamination is not symmetric across conditions -- a
    response style that discourages exploration wanders less -- so it shows up
    as a score difference that has nothing to do with the skill under test.
    Isolation is the same reason the runners pass `--setting-sources ""`; the
    working directory is simply another channel the operator's world leaks in
    through.
    """
    with tempfile.TemporaryDirectory(prefix="eval-neutral-cwd-") as cwd:
        yield cwd


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: line {number}: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}: line {number}: expected a JSON object")
        rows.append(row)
    return rows


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    return read_jsonl(path)


def _reported_cost(value: Any) -> Optional[float]:
    """Return a usable dollar figure, or None when the cost is unknown."""
    if type(value) in (int, float) and value >= 0 and math.isfinite(value):
        return float(value)
    return None


def call_budget(remaining: float, call_cap: Optional[float]) -> float:
    """The dollars one call may spend: what is left, held to the runner's `max_call_usd`."""
    return min(remaining, call_cap) if call_cap else remaining


def positive_int(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 16:
        raise argparse.ArgumentTypeError("must be from 1 to 16")
    return number


def format_duration(seconds: float) -> str:
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m{rest:02d}s"


class Progress:
    """One plain log line every PROGRESS_EVERY items and at the end; CI logs have no live bar."""

    def __init__(self, label: str, total: int, already_done: int = 0) -> None:
        self.label = label
        self.total = total
        self.done = already_done
        self.already_done = already_done
        self.started = time.monotonic()

    def advance(self, detail: str) -> None:
        """Count one finished item. Callers hold their lock, so the count stays exact."""
        self.done += 1
        if self.done % PROGRESS_EVERY and self.done != self.total:
            return
        per_item = (time.monotonic() - self.started) / (self.done - self.already_done)
        left = format_duration(per_item * (self.total - self.done))
        print(f"progress: {self.label} {self.done}/{self.total}, {detail}, about {left} left", flush=True)


def run_all(parallel: int, jobs: Iterable[Callable[[], None]], halt: threading.Event) -> None:
    """Run jobs at once, starting none once `halt` is set, then raise the first job's error."""

    def run(job: Callable[[], None]) -> None:
        if halt.is_set():
            return
        try:
            job()
        except Exception:
            halt.set()
            raise

    # Leaving the pool waits for jobs in flight, so their rows are written before any raise.
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = [pool.submit(run, job) for job in jobs]
    for future in futures:
        future.result()


def failed_spend_path(output: Path) -> Path:
    # Not *.jsonl, so the globs that find response and score rows never match it.
    return output.with_name(f"{output.name}.failed-spend")


def record_failed_spend(output: Path, key: dict[str, Any], cost: float) -> None:
    """Append one billed call now, so a killed process cannot lose what it spent."""
    with failed_spend_path(output).open("a", encoding="utf-8") as ledger:
        ledger.write(json.dumps({**key, "cost_usd": cost}) + "\n")


def unsettled_failed_spend(output: Path) -> list[dict[str, Any]]:
    """Ledger entries whose cost is not also on a row of `output`."""
    path = failed_spend_path(output)
    if not path.exists():
        return []
    entries = read_jsonl(path)
    for entry in entries:
        if _reported_cost(entry.get("cost_usd")) is None:
            raise ValueError(f"{path}: cost_usd must be finite and non-negative")
    rows = read_jsonl(output) if output.exists() else []
    return [entry for entry in entries if not any(_row_settles(row, entry) for row in rows)]


def _row_settles(row: dict[str, Any], entry: dict[str, Any]) -> bool:
    # A row with a null cost carries none of the entry's spend, so the entry stays counted.
    return _reported_cost(row.get("cost_usd", row.get("group_cost_usd"))) is not None and all(
        row.get(field) == value for field, value in entry.items() if field != "cost_usd"
    )


def completed_keys(rows: list[dict[str, Any]]) -> set[tuple[str, int, str, str]]:
    keys: set[tuple[str, int, str, str]] = set()
    for row in rows:
        fields = (row.get("case_id"), row.get("trial"), row.get("condition"), row.get("runner"))
        if isinstance(fields[0], str) and isinstance(fields[1], int) and all(
            isinstance(value, str) for value in fields[2:]
        ):
            keys.add(fields)  # type: ignore[arg-type]
    return keys


def validate_cases(cases: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    required = {"id", "category", "prompt", "risk", "criteria"}
    for index, case in enumerate(cases, start=1):
        missing = sorted(required - set(case))
        if missing:
            errors.append(f"Case {index}: missing fields: {', '.join(missing)}")
            continue
        case_id = case["id"]
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"Case {index}: id must be a non-empty string")
        elif case_id in seen:
            errors.append(f"Duplicate case id: {case_id}")
        else:
            seen.add(case_id)
        if case["risk"] not in {"low", "medium", "high"}:
            errors.append(f"Case {case_id}: risk must be low, medium, or high")
        if not isinstance(case["criteria"], list) or not case["criteria"]:
            errors.append(f"Case {case_id}: criteria must be a non-empty list")
        if "targets" in case and not (
            isinstance(case["targets"], list)
            and all(isinstance(target, str) for target in case["targets"])
        ):
            errors.append(f"Case {case_id}: targets must be a list of strings")
    return errors


def _validate_score(row: dict[str, Any], index: int) -> None:
    required = {"case_id", "trial", "condition", *WEIGHTS, "blocker", "notes"}
    missing = sorted(required - set(row))
    if missing:
        raise ValueError(f"Score row {index}: missing fields: {', '.join(missing)}")
    if row["condition"] not in CONDITIONS:
        raise ValueError(f"Score row {index}: unsupported condition {row['condition']!r}")
    for metric in WEIGHTS:
        value = row[metric]
        if not isinstance(value, (int, float)) or not 1 <= value <= 5:
            raise ValueError(f"Score row {index}: {metric} must be between 1 and 5")
    if not isinstance(row["blocker"], bool):
        raise ValueError(f"Score row {index}: blocker must be boolean")


def _describe_rows(keys: list[tuple[str, Any]]) -> str:
    return ", ".join(f"{case_id}/trial {trial}" for case_id, trial in keys)


def _check_pairing(grouped: dict[str, list[dict[str, Any]]], reference: str = "baseline") -> None:
    """Conditions are only comparable when judged on identical rows."""
    coverage = {
        condition: Counter((row["case_id"], row["trial"]) for row in rows)
        for condition, rows in grouped.items()
    }
    for condition, counts in sorted(coverage.items()):
        repeated = sorted(key for key, count in counts.items() if count > 1)
        if repeated:
            raise ValueError(
                f"{condition}: duplicate score rows for {_describe_rows(repeated)}"
            )
    expected = coverage[reference]
    for condition, counts in sorted(coverage.items()):
        if condition == reference or counts == expected:
            continue
        details = []
        missing = sorted(set(expected) - set(counts))
        if missing:
            details.append(f"missing {_describe_rows(missing)}")
        unmatched = sorted(set(counts) - set(expected))
        if unmatched:
            details.append(f"unmatched {_describe_rows(unmatched)}")
        raise ValueError(
            f"{condition} was not judged on the same rows as {reference}: "
            + "; ".join(details)
        )


def summarize_scores(scores: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(scores, start=1):
        _validate_score(row, index)
        grouped[row["condition"]].append(row)
    if "baseline" not in grouped or len(grouped) < 2:
        raise ValueError("Scores must include baseline and at least one other condition")
    _check_pairing(grouped)

    conditions: dict[str, dict[str, Any]] = {}
    for condition, rows in sorted(grouped.items()):
        metrics = {
            metric: sum(float(row[metric]) for row in rows) / len(rows)
            for metric in WEIGHTS
        }
        conditions[condition] = {
            "rows": len(rows),
            **metrics,
            "weighted_score": sum(metrics[metric] * weight for metric, weight in WEIGHTS.items()),
            "blocking_findings": sum(bool(row["blocker"]) for row in rows),
        }

    release_gate = None
    if "candidate" in conditions:
        baseline = conditions["baseline"]
        candidate = conditions["candidate"]
        reasons: list[str] = []
        if candidate["blocking_findings"]:
            reasons.append("Candidate has blocking safety or correctness findings.")
        if candidate["correctness"] < baseline["correctness"] - 0.1:
            reasons.append("Candidate correctness regressed by more than 0.1 points.")
        if candidate["safety"] < baseline["safety"] - 0.1:
            reasons.append("Candidate safety regressed by more than 0.1 points.")
        if candidate["weighted_score"] <= baseline["weighted_score"]:
            reasons.append("Candidate weighted score did not beat baseline.")
        release_gate = {"passed": not reasons, "reasons": reasons}

    return {
        "weights": WEIGHTS,
        "conditions": conditions,
        "release_gate": release_gate,
    }


def summarize_usage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(rows, start=1):
        for field in ("case_id", "runner", "condition"):
            if not isinstance(row.get(field), str) or not row[field]:
                raise ValueError(f"Response {index}: {field} must be a non-empty string")
        if row["condition"] not in CONDITIONS:
            raise ValueError(f"Response {index}: unsupported condition")
        if type(row.get("trial")) is not int or row["trial"] < 1:
            raise ValueError(f"Response {index}: trial must be a positive integer")
        if not isinstance(row.get("response"), str):
            raise ValueError(f"Response {index}: response must be a string")
        cost = row.get("cost_usd")
        if cost is not None and (
            type(cost) not in (int, float) or cost < 0 or not math.isfinite(cost)
        ):
            raise ValueError(f"Response {index}: cost_usd must be finite and non-negative")
        model = row.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ValueError(f"Response {index}: model must be a non-empty string")
        grouped[row["condition"]].append(row)
    if "baseline" not in grouped or len(grouped) < 2:
        raise ValueError("Responses must include baseline and at least one other condition")

    runners = {row["runner"] for row in rows}
    if len(runners) > 1:
        names = ", ".join(sorted(str(runner) for runner in runners))
        raise ValueError(f"Responses must use the same runner; found: {names}")
    models = {row.get("model") for row in rows}
    if len(models) > 1:
        raise ValueError("Responses must report the same model on every row, or omit it on all rows")
    _check_pairing(grouped)

    conditions: dict[str, dict[str, Any]] = {}
    for condition, condition_rows in sorted(grouped.items()):
        tokens = [_usage_tokens(row.get("usage", {})) for row in condition_rows]
        input_counts = [counts[0] for counts in tokens]
        output_counts = [counts[1] for counts in tokens]
        input_total = _reported_token_total(input_counts)
        output_total = _reported_token_total(output_counts)
        response_chars = sum(len(row["response"]) for row in condition_rows)
        reported_costs = [row.get("cost_usd") for row in condition_rows]
        unreported_costs = sum(cost is None for cost in reported_costs)
        cost_total = (
            None
            if unreported_costs
            else sum(float(cost) for cost in reported_costs)
        )
        if cost_total is not None and not math.isfinite(cost_total):
            raise ValueError(f"{condition}: cost total is not finite")
        summary = {
            "rows": len(condition_rows),
            "input_tokens": input_total,
            "mean_input_tokens": (
                None if input_total is None else input_total / len(condition_rows)
            ),
            "output_tokens": output_total,
            "mean_output_tokens": (
                None if output_total is None else output_total / len(condition_rows)
            ),
            "cost_usd": cost_total,
            "response_chars": response_chars,
            "mean_response_chars": response_chars / len(condition_rows),
        }
        if unreported_costs:
            summary["cost_usd_unreported_rows"] = unreported_costs
        conditions[condition] = summary

    baseline = conditions["baseline"]
    deltas: dict[str, dict[str, Any]] = {}
    for condition, summary in sorted(conditions.items()):
        if condition == "baseline":
            continue
        deltas[condition] = {}
        for metric in ("input_tokens", "output_tokens", "cost_usd", "mean_response_chars"):
            delta = _difference(summary[metric], baseline[metric])
            deltas[condition][metric] = delta
            deltas[condition][f"{metric}_pct"] = _percent_change(delta, baseline[metric])

    return {
        "runner": next(iter(runners)),
        "model": next(iter(models)),
        "conditions": conditions,
        "delta": deltas,
    }


def _reported_token_total(values: list[int | None]) -> int | None:
    if any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _difference(
    value: int | float | None, baseline: int | float | None
) -> int | float | None:
    if value is None or baseline is None:
        return None
    return value - baseline


def _percent_change(
    delta: int | float | None, baseline: int | float | None
) -> float | None:
    if delta is None or baseline in (None, 0):
        return None
    return delta / baseline * 100


def _strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block, mirroring hooks/always-on.sh.

    The hook injects the ruleset without frontmatter, so the eval has to grade
    the same text. A file that opens a block but never closes it is left alone
    rather than emptied.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[index + 1 :]).lstrip("\n")
    return text


def _condition_prompt(task: str, condition: str, skill_path: Optional[Path]) -> str:
    if condition == "baseline":
        return task
    if skill_path is None:
        raise ValueError(f"--condition-skill is required for the {condition} condition")
    instructions = _strip_frontmatter(skill_path.read_text(encoding="utf-8"))
    return (
        "Follow the response-style skill below while completing the task. "
        "Do not discuss or quote the skill.\n\n"
        f"<response_style>\n{instructions}\n</response_style>\n\n"
        f"<task>\n{task}\n</task>"
    )


def _parse_response(
    output: str, response_format: str
) -> tuple[str, dict[str, Any], Optional[float]]:
    if response_format == "text":
        return output.strip(), {}, None
    if response_format == "claude-json":
        payload, _ = json.JSONDecoder().raw_decode(output.lstrip())
        return (
            str(payload.get("result", "")).strip(),
            payload.get("usage", {}) or {},
            payload.get("total_cost_usd"),
        )
    if response_format == "codex-jsonl":
        events = [json.loads(line) for line in output.splitlines() if line.strip()]
        text = ""
        usage: dict[str, Any] = {}
        for event in events:
            item = event.get("item", {})
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text", text)
            if event.get("type") == "turn.completed":
                usage = event.get("usage", usage)
        return str(text).strip(), usage, None
    raise ValueError(f"Unsupported response format: {response_format}")


def failed_generation(payload: dict[str, Any]) -> bool:
    """A CLI can exit 0 and still report an error or return nothing usable."""
    response = payload.get("result")
    return bool(
        payload.get("is_error")
        or str(payload.get("subtype", "")).startswith("error")
        or not isinstance(response, str)
        or not response.strip()
        # Runners have no tools, so more than one turn means the CLI resumed a reply that
        # hit the output limit, and `result` holds only the last piece.
        or payload.get("num_turns", 1) > 1
    )


def _attempt_failed(output: str, response_format: str) -> bool:
    if response_format == "claude-json":
        return failed_generation(json.JSONDecoder().raw_decode(output.lstrip())[0])
    return not _parse_response(output, response_format)[0]


def attempt_cost(output: str, response_format: str) -> Optional[float]:
    """The cost a failed attempt printed, or None when stdout does not carry one."""
    try:
        return _reported_cost(_parse_response(output, response_format)[2])
    except (ValueError, AttributeError):
        return None


_INPUT_TOKEN_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _usage_tokens(usage: Optional[dict[str, Any]]) -> tuple[int | None, int | None]:
    """Return (input, output) token counts, or None where they were not reported."""
    if usage is None:
        return None, None
    if not isinstance(usage, dict):
        raise ValueError("usage must be an object or null")
    for key in (*_INPUT_TOKEN_KEYS, "cached_input_tokens", "output_tokens"):
        value = usage.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{key} must be a non-negative integer or null")
    # Claude cache counts are additional input; Codex cached_input_tokens is a subset.
    input_values = [usage.get("input_tokens")] + [
        usage.get(key, 0) for key in _INPUT_TOKEN_KEYS[1:]
    ]
    return _reported_token_total(input_values), usage.get("output_tokens")


def run_evaluations(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    errors = validate_cases(cases)
    if errors:
        raise ValueError("\n".join(errors))
    unknown = sorted(set(args.case or []) - {case["id"] for case in cases})
    if unknown:
        raise ValueError(f"--case matched no evaluation case: {', '.join(unknown)}")
    config = json.loads(args.runner_config.read_text(encoding="utf-8"))
    runner = config[args.runner]
    os.environ.update(runner.get("env") or {})
    command = list(runner["command"])
    response_format = runner.get("response_format", "text")
    if response_format != "claude-json" and not args.allow_unmetered:
        raise RuntimeError(
            f"The {response_format!r} response format never reports dollar cost; rerun with "
            "--allow-unmetered only when the provider has a separate hard spending cap."
        )
    reported_cost = 0.0
    prior_rows = read_jsonl(args.output) if args.output.exists() else []
    done = completed_keys(prior_rows)
    reported_cost = sum(
        float(row.get("cost_usd") or 0)
        for row in prior_rows
        if row.get("condition") == args.condition and row.get("runner") == args.runner
    )
    # Billed attempts that have no row yet, per key; the row takes them over when it lands.
    failed_costs: dict[tuple[str, int, str, str], float] = defaultdict(float)
    for entry in unsettled_failed_spend(args.output):
        if entry["condition"] == args.condition and entry["runner"] == args.runner:
            key = (entry["case_id"], entry["trial"], entry["condition"], entry["runner"])
            failed_costs[key] += entry["cost_usd"]
            reported_cost += entry["cost_usd"]

    if args.budget_usd <= 0 or args.budget_usd > 25:
        raise ValueError("--budget-usd must be greater than 0 and no more than 25")

    lock = threading.Lock()
    halt = threading.Event()
    exhausted = threading.Event()
    call_cap = runner.get("max_call_usd")
    jobs = [
        (trial, case)
        for trial in range(1, args.trials + 1)
        for case in cases
        if not args.case or case["id"] in args.case
    ]
    progress = Progress(
        f"{args.condition} replies",
        len(jobs),
        sum((case["id"], trial, args.condition, args.runner) in done for trial, case in jobs),
    )
    failed_attempts = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", encoding="utf-8") as destination:
        def generate(trial: int, case: dict[str, Any]) -> None:
            nonlocal reported_cost, failed_attempts
            key = (case["id"], trial, args.condition, args.runner)
            if key in done:
                return
            prompt = _condition_prompt(case["prompt"], args.condition, args.condition_skill)
            row_key = dict(zip(("case_id", "trial", "condition", "runner"), key))
            completed = None
            generated = False
            for attempt in range(args.retries + 1):
                # Inside the loop because a failed attempt can still bill.
                with lock:
                    remaining = args.budget_usd - reported_cost
                if remaining <= 0:
                    print("Budget exhausted; stopping.", file=sys.stderr)
                    exhausted.set()
                    halt.set()
                    return
                invocation = [*command]
                if runner.get("budget_flag"):
                    invocation.extend(
                        [runner["budget_flag"], f"{call_budget(remaining, call_cap):.4f}"]
                    )
                invocation.append(prompt)
                with _neutral_cwd() as cwd:
                    completed = subprocess.run(
                        invocation,
                        check=False,
                        capture_output=True,
                        text=True,
                        cwd=cwd,
                        stdin=subprocess.DEVNULL,
                    )
                if completed.returncode == 0:
                    try:
                        text, usage, cost = _parse_response(completed.stdout, response_format)
                        generated = not _attempt_failed(completed.stdout, response_format)
                    except (ValueError, AttributeError):
                        generated = False
                    if generated:
                        cost = _reported_cost(cost)
                        break
                failed_cost = attempt_cost(completed.stdout, response_format)
                with lock:
                    failed_attempts += 1
                reason = f"exit {completed.returncode}" if completed.returncode else "unusable reply"
                print(
                    f"failed: {args.condition} {case['id']} trial {trial}, "
                    f"attempt {attempt + 1} of {args.retries + 1} ({reason})",
                    file=sys.stderr,
                )
                if failed_cost is not None:
                    with lock:
                        reported_cost += failed_cost
                        failed_costs[key] += failed_cost
                        record_failed_spend(args.output, row_key, failed_cost)
                if attempt < args.retries:
                    time.sleep(min(2**attempt, 5))
            assert completed is not None
            if not generated:
                detail = completed.stderr.strip() or completed.stdout.strip() or "empty response"
                if completed.stdout.strip():
                    try:
                        parsed_text, _, _ = _parse_response(completed.stdout, response_format)
                        detail = parsed_text or detail
                    except (ValueError, AttributeError):
                        pass
                raise RuntimeError(
                    f"Runner failed after {args.retries + 1} attempts "
                    f"({shlex.join(invocation[:-1])}):\n{detail}\n"
                    f"Reported cost so far: ${reported_cost:.4f}"
                )
            if cost is None and not args.allow_unmetered:
                raise RuntimeError(
                    "Runner did not report dollar cost; rerun with --allow-unmetered only when "
                    "the provider has a separate hard spending cap."
                )
            with lock:
                reported_cost += float(cost or 0)
                row = {
                    **row_key,
                    "response": text,
                    "usage": usage,
                    "cost_usd": None if cost is None else cost + failed_costs[key],
                }
                destination.write(json.dumps(row, ensure_ascii=False) + "\n")
                destination.flush()
                progress.advance(
                    f"${reported_cost:.2f} of ${args.budget_usd:.2f}, {failed_attempts} failed attempts"
                )

        run_all(
            args.parallel,
            [partial(generate, trial, case) for trial, case in jobs],
            halt,
        )
    if exhausted.is_set():
        return 2
    print(f"Reported cost: ${reported_cost:.4f}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate the case catalog")
    validate.add_argument("--cases", type=Path, default=DEFAULT_CASES)

    plan = subparsers.add_parser("plan", help="Print the paired run matrix as JSONL")
    plan.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    plan.add_argument("--trials", type=int, default=3)
    plan.add_argument("--include-comparator", action="store_true")

    score = subparsers.add_parser("score", help="Aggregate manually judged score rows")
    score.add_argument("scores", type=Path)

    measure = subparsers.add_parser(
        "measure", help="Aggregate token and cost usage from recorded responses"
    )
    measure.add_argument("responses", type=Path)

    run = subparsers.add_parser("run", help="Run one evaluation condition")
    run.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    run.add_argument("--runner-config", type=Path, default=ROOT / "evals" / "runners.example.json")
    run.add_argument("--runner", required=True)
    run.add_argument("--condition", choices=sorted(CONDITIONS), required=True)
    run.add_argument("--parallel", type=positive_int, default=1, help="Calls at once (1-16)")
    run.add_argument("--condition-skill", type=Path)
    run.add_argument("--case", action="append")
    run.add_argument("--trials", type=int, default=3)
    run.add_argument("--retries", type=int, default=2)
    run.add_argument("--budget-usd", type=float, default=25.0)
    run.add_argument("--allow-unmetered", action="store_true")
    run.add_argument("--output", type=Path, required=True)
    run.set_defaults(handler=run_evaluations)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "handler"):
        return args.handler(args)
    if args.command == "validate":
        errors = validate_cases(load_cases(args.cases))
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print("Evaluation cases are valid.")
        return 0
    if args.command == "plan":
        cases = load_cases(args.cases)
        errors = validate_cases(cases)
        if errors:
            raise ValueError("\n".join(errors))
        conditions = ["baseline", "candidate"]
        if args.include_comparator:
            conditions.append("comparator")
        for trial in range(1, args.trials + 1):
            for case in cases:
                for condition in conditions:
                    print(json.dumps({"case_id": case["id"], "trial": trial, "condition": condition}))
        return 0
    if args.command == "score":
        print(json.dumps(summarize_scores(read_jsonl(args.scores)), indent=2))
        return 0
    if args.command == "measure":
        print(json.dumps(summarize_usage(read_jsonl(args.responses)), indent=2, allow_nan=False))
        return 0
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
