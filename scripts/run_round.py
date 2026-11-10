#!/usr/bin/env python3
"""Run one evaluation round for one variant and write its results folder.

Composes run_evals, run_scenario_eval, judge, format_checks, judge_agreement and
compare as subprocesses. Each step is resumable, so rerunning the same command after
a failure continues from the rows already on disk.
"""

import argparse
import datetime
import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import judge  # noqa: E402
import run_evals  # noqa: E402
import run_scenario_eval  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SPEND_LIMIT_USD = 120.0
SKILL_PATH = "skills/i-have-adhd/SKILL.md"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
SHARED_CONDITIONS = ("baseline", "comparator")
COMPARE_PAIRS = (
    ("baseline", "comparator"),
    ("baseline", "candidate"),
    ("comparator", "candidate"),
)
ROUND_ZERO_FOLDER = "v0"
SHARED_SETUP = ("runner", "model", "cli_version", "effort", "limits", "comparator", "case_set")
VARIANT_SETUP = (
    "skills", "runner", "model", "cli_version", "effort", "limits", "trials", "case_set",
    "rubric_sha256", "judges", "responses_from",
)
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)


class RoundError(Exception):
    """A refusal or failed step, reported as one line with no traceback."""


# ---- validation -----------------------------------------------------------------------


def _name(value: str, what: str) -> str:
    if not NAME.fullmatch(value) or ".." in value:
        raise RoundError(f"invalid {what}: {value!r}")
    return value


def _ref(value: str) -> str:
    if (
        not REF.fullmatch(value)
        or ".." in value
        or "//" in value
        or value.endswith(("/", ".", ".lock"))
    ):
        raise RoundError(f"invalid git ref or path: {value!r}")
    return value


def _parse_variant(value: str) -> tuple[str, Optional[str]]:
    """Split `name=skill_ref`. Blank means Round 0: no candidate to run."""
    if not value:
        return ROUND_ZERO_FOLDER, None
    name, separator, ref = value.partition("=")
    if not separator:
        raise RoundError(f"--variant must be name=skill_ref: {value!r}")
    _name(name, "variant")
    # Lowercased because macOS and Windows would map SHARED onto the shared folder.
    if name.lower() == "shared":
        raise RoundError("variant name 'shared' is reserved")
    return name, _ref(ref)


def _inside(path: Path, workspace: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(workspace):
        raise RoundError(f"path is outside the workspace: {path}")
    return resolved


def _given_file(value: Path, workspace: Path) -> Path:
    path = _inside(workspace / value, workspace)
    if not path.is_file():
        raise RoundError(f"not a file: {value}")
    return path


def _optional_file(value: Optional[Path], workspace: Path, default: str) -> Path:
    return _given_file(value, workspace) if value else ROOT / "evals" / default


def _comma_list(value: str, what: str) -> list[str]:
    return [_name(item.strip(), what) for item in value.split(",")] if value else []


def _flag(command: list[str], name: str) -> Optional[str]:
    return command[command.index(name) + 1] if name in command[:-1] else None


def _judge_model(config: dict[str, Any], name: str) -> str:
    return _flag(config[name]["command"], "--model") or config[name].get("model") or name


# ---- git and files --------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )


def _commit_for(ref: str) -> str:
    # A CI checkout of one branch knows other branches only as origin/<name>.
    for name in (ref, f"origin/{ref}"):
        found = _git("rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        if found.returncode == 0:
            return found.stdout.strip()
    raise RoundError(f"unknown git ref: {ref}")


def _skill(ref: str, workspace: Path) -> tuple[str, dict[str, Any]]:
    """Return a skill's text and provenance; `ref` is a workspace file or a public-repo ref."""
    if os.path.lexists(workspace / ref):
        text = _given_file(Path(ref), workspace).read_text(encoding="utf-8")
        commit = None
    else:
        commit = _commit_for(ref)
        shown = _git("show", f"{commit}:{SKILL_PATH}")
        if shown.returncode:
            raise RoundError(f"{ref} has no {SKILL_PATH}")
        text = shown.stdout
    if not run_evals._strip_frontmatter(text).strip():
        raise RoundError(f"skill is empty: {ref}")
    return text, {"ref": ref, "commit": commit, "sha256": _sha256(text)}


def _write_atomic(path: Path, text: str) -> None:
    """Replace a file in one step, so a kill mid-write cannot truncate paid-for rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    _write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _rows(path: Path) -> list[dict[str, Any]]:
    return run_evals.read_jsonl(path) if path.exists() else []


def _display(path: Path, workspace: Path) -> str:
    """A path for meta.json, relative so results carry no operator home directory."""
    for base in (workspace, ROOT):
        if path.is_relative_to(base):
            return path.relative_to(base).as_posix()
    return path.name


# ---- subprocess steps -----------------------------------------------------------------


def _scrub(text: str) -> str:
    for name, value in os.environ.items():
        if len(value) >= 8 and SECRET_NAME.search(name):
            text = text.replace(value, "***")
    return text


def _runner_env(runner: Optional[dict[str, Any]]) -> dict[str, str]:
    env = dict(os.environ, PYTHONUNBUFFERED="1")  # So a CI log shows each child's lines as they happen.
    if runner and runner.get("path_prepend"):
        prepend = runner["path_prepend"].replace("{root}", str(ROOT))
        env["PATH"] = prepend + os.pathsep + env.get("PATH", "")
    if runner:
        env.update(runner.get("env") or {})
    return env


def _limits(runner: dict[str, Any]) -> dict[str, Any]:
    """The per-call limits of a runner. They shape replies, so they are part of the setup."""
    for name, value in (runner.get("env") or {}).items():
        # Same test as _scrub: meta.json is committed, so no secret may sit in a runner's env.
        if len(value) >= 8 and SECRET_NAME.search(name):
            raise RoundError(f"runner env {name} looks like a secret; keep it out of the config")
    limits = {key: runner[key] for key in ("env", "max_call_usd") if key in runner}
    note = _flag(runner["command"], "--append-system-prompt")
    return {**limits, "system_note": note} if note else limits


def _step(
    script: str,
    *args: Any,
    runner: Optional[dict[str, Any]] = None,
    output: Optional[Path] = None,
) -> None:
    """Run one harness script; progress is streamed with secrets removed."""
    command = [sys.executable, str(SCRIPTS / script), *map(str, args)]
    if output is not None:
        done = subprocess.run(
            command, env=_runner_env(runner), stdin=subprocess.DEVNULL,
            capture_output=True, text=True,
        )
        print(_scrub(done.stderr), end="")
        code = done.returncode
        if code == 0:
            _write_atomic(output, done.stdout)
    else:
        with subprocess.Popen(
            command, env=_runner_env(runner), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        ) as process:
            assert process.stdout is not None
            for line in process.stdout:
                print(_scrub(line), end="", flush=True)
        code = process.returncode
    if code:
        raise RoundError(f"{script} exited with {code}")


def _cli_version(runner: dict[str, Any]) -> str:
    done = subprocess.run(
        [runner["command"][0], "--version"], env=_runner_env(runner),
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )
    if done.returncode or not done.stdout.strip():
        raise RoundError(f"could not read the version of {runner['command'][0]}")
    return done.stdout.strip()


# ---- spend ----------------------------------------------------------------------------


def _usd(rows: list[dict[str, Any]]) -> float:
    return round(sum(judge._reported_cost(row.get("cost_usd")) or 0.0 for row in rows), 6)


def _failed_usd(directory: Path, pattern: str, condition: Optional[str] = None) -> float:
    """Billed attempts under `pattern` that no row carries yet."""
    entries = [
        entry
        for ledger in directory.glob(f"{pattern}.failed-spend")
        for entry in run_evals.unsettled_failed_spend(ledger.with_suffix(""))
    ]
    return sum(e["cost_usd"] for e in entries if condition in (None, e.get("condition")))


def _shared_costs(directory: Path) -> dict[str, Any]:
    rows = [row for path in sorted(directory.glob("responses-t*.jsonl")) for row in _rows(path)]
    generation = {
        c: round(
            _usd([r for r in rows if r.get("condition") == c])
            + _failed_usd(directory, "responses-t*.jsonl", c),
            6,
        )
        for c in SHARED_CONDITIONS
    }
    scenarios = round(
        _usd([r for p in directory.glob("scenarios/*/*.jsonl") for r in _rows(p)])
        + _failed_usd(directory, "scenarios/*/*.jsonl"),
        6,
    )
    return {
        "generation_usd": generation,
        "scenarios_usd": scenarios,
        "total_usd": round(sum(generation.values()) + scenarios, 6),
    }


def _variant_costs(directory: Path) -> dict[str, Any]:
    """This folder's own spend. Shared rows are counted once, in shared/meta.json."""
    rows = _rows(directory / "responses.jsonl")
    generation = round(
        _usd([row for row in rows if row.get("condition") == "candidate"])
        + _failed_usd(directory, "responses.jsonl"),
        6,
    )
    captures = directory.glob("scenarios/*/candidate-*.jsonl")
    scenarios = round(
        _usd([row for path in captures for row in _rows(path)])
        + _failed_usd(directory, "scenarios/*/candidate-*.jsonl"),
        6,
    )
    judges: dict[str, dict[str, Any]] = {}
    for path in [*directory.glob("scores-*.jsonl"), *directory.glob("scenarios/*/scores-*.jsonl")]:
        name = path.stem.removeprefix("scores-")
        entry = judges.setdefault(name, {"usd": 0.0, "unreported_groups": 0})
        # Every row of a group repeats the group's cost.
        groups = {(row["case_id"], row["trial"]): row.get("group_cost_usd") for row in _rows(path)}
        known = [judge._reported_cost(cost) for cost in groups.values()]
        failed = _failed_usd(path.parent, path.name)
        entry["usd"] = round(
            entry["usd"] + sum(cost for cost in known if cost is not None) + failed, 6
        )
        entry["unreported_groups"] += known.count(None)
    total = generation + scenarios + sum(entry["usd"] for entry in judges.values())
    return {
        "generation_usd": generation,
        "scenarios_usd": scenarios,
        "judges": judges,
        "total_usd": round(total, 6),
    }


def _folder_costs(directory: Path) -> dict[str, Any]:
    return (_shared_costs if directory.name == "shared" else _variant_costs)(directory)


def reported_spend(results: Path) -> float:
    """Sum reported spend across every results folder.

    A folder whose meta.json is missing or malformed has unknown spend, and unknown is
    not zero: the whole run refuses until someone repairs or removes that folder. Unmetered
    runners report null, which the operator opted into, so it adds nothing here.
    """
    total = 0.0
    for directory in sorted(path for path in results.glob("*/*") if path.is_dir()):
        try:
            meta = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
            spent = judge._reported_cost(meta["reported_costs"]["total_usd"])
            if spent is None:
                raise ValueError("total_usd is not a usable dollar figure")
            if meta.get("status") != "complete":
                # A killed run never refreshed its meta.json, so its rows are the better number.
                spent = max(spent, _folder_costs(directory)["total_usd"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RoundError(
                f"{directory}: missing or malformed meta.json, so its spend is unknown; "
                f"repair or remove that folder before running another round ({exc})"
            ) from exc
        total += spent
    return total


def _captures(plan: SimpleNamespace) -> list[tuple[str, int, str, Path]]:
    """Every scenario capture this invocation needs, in run order."""
    captures = []
    for scenario, trial, condition in itertools.product(
        plan.scenarios, range(1, plan.trials + 1), plan.conditions
    ):
        owner = plan.shared_dir if condition in SHARED_CONDITIONS else plan.variant_dir
        path = owner / "scenarios" / scenario / f"{condition}-{trial}.jsonl"
        captures.append((scenario, trial, condition, path))
    return captures


def _captured(path: Path) -> bool:
    # The capture opens its file up front and leaves it empty when a call fails.
    return path.exists() and path.stat().st_size > 0


def _missing_rows(path: Path, condition: str, expected: int) -> bool:
    return sum(row.get("condition") == condition for row in _rows(path)) != expected


def _run_caps(plan: SimpleNamespace) -> float:
    """The most this invocation can add to reported spend, counting only work left to do."""
    sources = [(condition, plan.shared_responses) for condition in SHARED_CONDITIONS]
    if plan.candidate:
        sources.append(("candidate", plan.responses))
    generation = sum(_missing_rows(path, c, plan.expected_rows) for c, path in sources)
    captures = sum(not _captured(path) for *_, path in _captures(plan))
    metered_judges = [name for name in plan.judges if name not in plan.unmetered]
    metered = len(metered_judges)
    # Each worker can start one more call before it sees a cap is reached.
    # shortcut: a runner with no max_call_usd adds nothing, so set one on any runner used in parallel.
    overshoot = plan.parallel * (
        generation * plan.runner.get("max_call_usd", 0)
        + sum(plan.config[name].get("max_call_usd", 0) for name in metered_judges)
    )
    return (
        plan.budget * (generation + metered)
        + plan.scenario_budget * (captures + metered * len(plan.scenarios))
        + overshoot
    )


# ---- planning -------------------------------------------------------------------------


def _prepare(args: argparse.Namespace) -> SimpleNamespace:
    """Validate every input and read what the run needs; writes nothing and spends nothing."""
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        raise RoundError(f"workspace is not a directory: {args.workspace}")
    if not 1 <= args.parallel <= 16:
        raise RoundError("--parallel must be from 1 to 16")
    round_name = _name(args.round, "round")
    variant, candidate_ref = _parse_variant(args.variant)
    comparator_ref = _ref(args.comparator_ref)
    change = _name(args.change, "change") if args.change else None
    if args.trials < 1 or args.retries < 0:
        raise RoundError("--trials must be at least 1 and --retries at least 0")
    # Written this way round so that NaN is rejected too.
    if not 0 < args.budget_usd <= 25 or not 0 < args.scenario_budget_usd <= 25:
        raise RoundError("budgets must be greater than 0 and no more than 25")

    # Defaults live in the harness itself; only paths the caller supplies need the fence.
    config_path = _optional_file(args.runner_config, workspace, "runners.example.json")
    cases_path = _optional_file(args.cases, workspace, "cases.jsonl")
    rubric_path = _optional_file(args.rubric, workspace, "rubric.md")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    generator = _name(args.generator_runner, "generator runner")
    judges = _comma_list(args.judges, "judge")
    if not judges:
        raise RoundError("--judges needs at least one runner")
    unmetered = _comma_list(args.unmetered_judges, "unmetered judge")
    if not set(unmetered) <= set(judges):
        raise RoundError("--unmetered-judges must all be named in --judges")
    unknown = sorted({generator, *judges} - set(config))
    if unknown:
        raise RoundError(f"not in the runner config: {', '.join(unknown)}")

    command = config[generator]["command"]
    model = _flag(command, "--model") or config[generator].get("model")
    if not model:
        raise RoundError(f"cannot determine the model of generator runner {generator}")
    scenarios = _comma_list(args.scenarios, "scenario")
    responses_from = _name(args.responses_from, "--responses-from round") if args.responses_from else None
    if responses_from == round_name:
        raise RoundError("--responses-from must name another round")
    # run_scenario_eval always drives `claude --model <model>` itself.
    if scenarios and command[0] != "claude":
        raise RoundError("scenarios need a claude generator runner")
    for scenario in scenarios:
        run_scenario_eval.load_scenario(ROOT / "evals" / "scenarios" / scenario)

    cases = run_evals.load_cases(cases_path)
    errors = run_evals.validate_cases(cases)
    if errors:
        raise RoundError("\n".join(errors))

    comparator_text, comparator = _skill(comparator_ref, workspace)
    skills = {"comparator": comparator}
    candidate_text = None
    if candidate_ref:
        candidate_text, skills["candidate"] = _skill(candidate_ref, workspace)

    results = workspace / "results"
    # Rejudging reads another round's shared rows in place, so their spend is counted once.
    shared_dir = _inside(results / (responses_from or round_name) / "shared", workspace)
    if responses_from and _load_meta(shared_dir / "meta.json").get("status") != "complete":
        raise RoundError(f"--responses-from {responses_from}: its shared rows are not complete")
    variant_dir = _inside(results / round_name / variant, workspace)
    for directory in (shared_dir, variant_dir):
        if directory.exists() and any(path.is_symlink() for path in directory.rglob("*")):
            raise RoundError(f"refusing a results folder that contains a symlink: {directory}")

    return SimpleNamespace(
        workspace=workspace, results=results, round=round_name, variant=variant, change=change,
        shared_dir=shared_dir, variant_dir=variant_dir,
        shared_responses=shared_dir / f"responses-t{args.trials}.jsonl",
        responses=variant_dir / "responses.jsonl",
        comparator_skill=shared_dir / "comparator-skill.md",
        comparator_text=comparator_text,
        candidate_skill=variant_dir / "skill.md",
        candidate=bool(candidate_ref), candidate_text=candidate_text,
        conditions=[*SHARED_CONDITIONS, *(["candidate"] if candidate_ref else [])],
        skills=skills, config=config, config_path=config_path, cases_path=cases_path,
        rubric_path=rubric_path, generator=generator, runner=config[generator], model=model,
        effort=_flag(command, "--effort"), judges=judges, unmetered=unmetered,
        scenarios=scenarios, trials=args.trials, retries=args.retries, parallel=args.parallel,
        expected_rows=len(cases) * args.trials, budget=args.budget_usd,
        scenario_budget=args.scenario_budget_usd,
        case_set={
            "path": _display(cases_path, workspace),
            "sha256": _sha256(cases_path.read_text(encoding="utf-8")),
            "cases": len(cases),
        },
        rubric_sha256=_sha256(rubric_path.read_text(encoding="utf-8")),
        responses_from=responses_from,
    )


def _refuse_changed_setup(
    old: dict[str, Any], new: dict[str, Any], keys: tuple, where: str
) -> None:
    changed = [key for key in keys if key in old and old[key] != new[key]]
    if changed:
        raise RoundError(
            f"{where} was made with a different {', '.join(changed)}, so its rows are not "
            "comparable; use a new round name"
        )


def _load_meta(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _shared_record(plan: SimpleNamespace) -> dict[str, Any]:
    return {
        "runner": plan.generator, "model": plan.model, "cli_version": plan.cli_version,
        "effort": plan.effort, "limits": _limits(plan.runner),
        "comparator": plan.skills["comparator"], "case_set": plan.case_set,
    }


def _check_resume(plan: SimpleNamespace, meta: dict[str, Any]) -> None:
    # Scenario captures are shared across trial counts, so every set must match.
    for earlier in _load_meta(plan.shared_dir / "meta.json").get("sets", {}).values():
        _refuse_changed_setup(earlier, _shared_record(plan), SHARED_SETUP, str(plan.shared_dir))
    old = _load_meta(plan.variant_dir / "meta.json")
    _refuse_changed_setup(old, meta, VARIANT_SETUP, str(plan.variant_dir))


def _base_meta(plan: SimpleNamespace, public_sha: str) -> dict[str, Any]:
    return {
        "round": plan.round,
        "variant": plan.variant,
        "conditions": plan.conditions,
        "public_sha": public_sha,
        "skills": plan.skills,
        "runner": plan.generator,
        "model": plan.model,
        "cli_version": plan.cli_version,
        "effort": plan.effort,
        "limits": _limits(plan.runner),
        "trials": plan.trials,
        "case_set": plan.case_set,
        "rubric_sha256": plan.rubric_sha256,
        "responses_from": plan.responses_from,
        "scenarios": plan.scenarios,
        "judges": [
            {
                "runner": name,
                "model": _judge_model(plan.config, name),
                **({"limits": _limits(plan.config[name])} if _limits(plan.config[name]) else {}),
            }
            for name in plan.judges
        ],
        "budget_usd": plan.budget,
        "scenario_budget_usd": plan.scenario_budget,
    }


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _write_meta(plan: SimpleNamespace, status: str) -> None:
    # Reported costs always come from rows on disk, so a resume includes earlier spend.
    costs = {
        **_variant_costs(plan.variant_dir),
        "shared_usd_not_in_total": _shared_costs(plan.shared_dir)["total_usd"],
    }
    _write_json(
        plan.variant_dir / "meta.json",
        {**plan.meta, "status": status, "reported_costs": costs, "date": _now()},
    )


def _write_shared_meta(plan: SimpleNamespace, status: str) -> None:
    if plan.responses_from and not plan.scenarios:
        return  # another round's folder: only new scenario captures change it
    path = plan.shared_dir / "meta.json"
    sets = _load_meta(path).get("sets", {})
    sets[str(plan.trials)] = _shared_record(plan)
    _write_json(path, {
        "round": plan.round, "status": status, "sets": sets,
        "reported_costs": _shared_costs(plan.shared_dir), "date": _now(),
    })


# ---- round stages ---------------------------------------------------------------------


def _stage(plan: SimpleNamespace, name: str) -> None:
    """Say where the round is and what comes next, in one plain log line."""
    index = plan.stages.index(name)
    following = plan.stages[index + 1] if index + 1 < len(plan.stages) else "reports"
    elapsed = run_evals.format_duration(time.monotonic() - plan.started)
    print(
        f"Stage {index + 1}/{len(plan.stages)}: {name}. Next: {following}. "
        f"Reported spend ${reported_spend(plan.results):.2f}, {elapsed} so far.",
        flush=True,
    )


def _generate(plan: SimpleNamespace, condition: str, output: Path, skill: Optional[Path]) -> None:
    _stage(plan, f"generate {condition}")
    _step(
        "run_evals.py", "run",
        "--cases", plan.cases_path,
        "--runner-config", plan.config_path,
        "--runner", plan.generator,
        "--condition", condition,
        *(["--condition-skill", skill] if skill else []),
        "--trials", plan.trials,
        "--retries", plan.retries,
        "--parallel", plan.parallel,
        "--budget-usd", plan.budget,
        "--output", output,
        runner=plan.runner,
    )
    if _missing_rows(output, condition, plan.expected_rows):
        raise RoundError(f"{output}: {condition} rows are incomplete after generation")


def _copy_shared_rows(plan: SimpleNamespace) -> None:
    """Copy shared rows as raw lines ahead of any candidate rows already written."""
    existing = plan.responses.read_text(encoding="utf-8") if plan.responses.exists() else ""
    candidate = [
        line for line in existing.splitlines(keepends=True)
        if line.strip() and json.loads(line)["condition"] == "candidate"
    ]
    shared = plan.shared_responses.read_text(encoding="utf-8")
    _write_atomic(plan.responses, shared + "".join(candidate))


def _run_scenarios(plan: SimpleNamespace) -> None:
    if plan.scenarios:
        _stage(plan, "capture scenarios")
    for scenario, trial, condition, path in _captures(plan):
        if _captured(path):
            continue
        path.unlink(missing_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        skill = {
            "baseline": None,
            "comparator": plan.comparator_skill,
            "candidate": plan.candidate_skill,
        }[condition]
        _step(
            "run_scenario_eval.py", "run",
            "--scenario", ROOT / "evals" / "scenarios" / scenario,
            "--condition", condition,
            *(["--condition-skill", skill] if skill else []),
            "--model", plan.model,
            *(["--effort", plan.effort] if plan.effort else []),
            "--trial", trial,
            "--budget-usd", plan.scenario_budget,
            "--output", path,
            runner=plan.runner,
        )
    for scenario in plan.scenarios:
        paths = [path for name, _, _, path in _captures(plan) if name == scenario]
        _write_atomic(
            plan.variant_dir / "scenarios" / scenario / "responses.jsonl",
            "".join(path.read_text(encoding="utf-8") for path in paths),
        )


def _judge_and_report(
    plan: SimpleNamespace, directory: Path, responses: Path, cases: Path, budget: float
) -> None:
    """Judge one responses file with every judge, then write its reports."""
    scores = {name: directory / f"scores-{name}.jsonl" for name in plan.judges}
    for name, path in scores.items():
        if directory == plan.variant_dir:
            _stage(plan, f"judge {name}")
        _step(
            "judge.py",
            "--responses", responses,
            "--cases", cases,
            "--rubric", plan.rubric_path,
            "--runner-config", plan.config_path,
            "--runner", name,
            "--conditions", *plan.conditions,
            "--retries", plan.retries,
            "--parallel", plan.parallel,
            "--budget-usd", budget,
            *(["--allow-unmetered"] if name in plan.unmetered else []),
            "--output", path,
            runner=plan.config[name],
        )
        _write_meta(plan, "running")
    reports = directory / "reports"
    _step("run_evals.py", "measure", responses, output=reports / "measure.json")
    _step("format_checks.py", responses, output=reports / "format-checks.json")
    pairs = [pair for pair in COMPARE_PAIRS if set(pair) <= set(plan.conditions)]
    for name, path in scores.items():
        _step("run_evals.py", "score", path, output=reports / f"score-{name}.json")
        for reference, treatment in pairs:
            change = ["--change", plan.change] if plan.change and treatment == "candidate" else []
            _step(
                "compare.py", path, cases, reference, treatment, *change,
                output=reports / f"compare-{name}-{reference}-vs-{treatment}.json",
            )
    for first, second in itertools.combinations(plan.judges, 2):
        _step(
            "judge_agreement.py", scores[first], scores[second],
            output=reports / f"judge-agreement-{first}-vs-{second}.json",
        )


def _run_stages(plan: SimpleNamespace) -> None:
    plan.started = time.monotonic()
    plan.stages = [
        *(f"generate {c}" for c in plan.conditions if c == "candidate" or not plan.responses_from),
        *(["capture scenarios"] if plan.scenarios else []),
        *(f"judge {name}" for name in plan.judges),
        *(f"judge scenario {scenario}" for scenario in plan.scenarios),
    ]
    plan.shared_dir.mkdir(parents=True, exist_ok=True)
    plan.variant_dir.mkdir(parents=True, exist_ok=True)
    _write_shared_meta(plan, "running")
    _write_meta(plan, "running")

    if not plan.responses_from:
        _write_atomic(plan.comparator_skill, plan.comparator_text)
        _generate(plan, "baseline", plan.shared_responses, None)
        _generate(plan, "comparator", plan.shared_responses, plan.comparator_skill)
    for condition in SHARED_CONDITIONS:
        if _missing_rows(plan.shared_responses, condition, plan.expected_rows):
            raise RoundError(f"{plan.shared_responses}: {condition} rows are incomplete")
    _copy_shared_rows(plan)
    _write_shared_meta(plan, "running")
    _write_meta(plan, "running")

    if plan.candidate:
        _write_atomic(plan.candidate_skill, plan.candidate_text)
        _generate(plan, "candidate", plan.responses, plan.candidate_skill)
        _write_meta(plan, "running")

    _run_scenarios(plan)
    plan.shared_status = "complete"
    _write_shared_meta(plan, "complete")
    _write_meta(plan, "running")

    _judge_and_report(plan, plan.variant_dir, plan.responses, plan.cases_path, plan.budget)
    for scenario in plan.scenarios:
        directory = plan.variant_dir / "scenarios" / scenario
        _stage(plan, f"judge scenario {scenario}")
        _judge_and_report(
            plan, directory, directory / "responses.jsonl",
            ROOT / "evals" / "scenarios" / scenario / "case.jsonl", plan.scenario_budget,
        )


def run_round(args: argparse.Namespace) -> int:
    plan = _prepare(args)
    spent = reported_spend(plan.results)
    caps = _run_caps(plan)
    # Runners can overshoot a cap by one call, so reaching the limit counts as crossing it.
    if spent + caps >= SPEND_LIMIT_USD:
        raise RoundError(
            f"reported spend ${spent:.2f} plus this run's caps ${caps:.2f} would reach "
            f"the ${SPEND_LIMIT_USD:.0f} limit; refusing to start"
        )
    public_sha = _commit_for("HEAD")
    plan.cli_version = _cli_version(plan.runner)
    plan.meta = _base_meta(plan, public_sha)
    _check_resume(plan, plan.meta)

    status = plan.shared_status = "failed"
    try:
        _run_stages(plan)
        status = "complete"
    finally:
        if plan.variant_dir.exists():
            _write_meta(plan, status)
            _write_shared_meta(plan, plan.shared_status)
    print(f"Round complete: {_display(plan.variant_dir, plan.workspace)}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add = parser.add_argument
    add("--workspace", type=Path, required=True, help="Results go under <workspace>/results")
    add("--round", required=True, help="Round label, such as r0-baseline")
    add("--variant", default="", help="name=skill_ref; blank for Round 0 (no candidate)")
    add("--comparator-ref", default="main", help="Public-repo git ref for the v0 skill")
    add("--generator-runner", required=True)
    add("--judges", required=True, help="Comma-separated judge runner names")
    add("--unmetered-judges", default="", help="Judges allowed to report null cost")
    add("--budget-usd", type=float, required=True, help="Cap per condition and per judge (max 25)")
    add(
        "--scenario-budget-usd", type=float, default=1.0,
        help="Cap per scenario capture and per scenario judge run",
    )
    add("--scenarios", default="", help="Comma-separated names under evals/scenarios")
    add("--trials", type=int, default=3)
    add("--retries", type=int, default=2)
    add("--parallel", type=int, default=1, help="Calls at once (1-16)")
    add("--change", help="Change ID for the targeted numbers in candidate compare reports")
    add("--runner-config", type=Path, help="Inside the workspace; default runners.example.json")
    add("--cases", type=Path, help="Inside the workspace; default evals/cases.jsonl")
    add("--rubric", type=Path, help="Inside the workspace; default evals/rubric.md")
    add("--responses-from", default="", help="Judge another round's baseline and comparator rows")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return run_round(args)
    except (RoundError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"error: {_scrub(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
