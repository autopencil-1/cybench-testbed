"""
M3: scoring + cybench-log shaping.

Two responsibilities:

1. Vuln-class oracle match — does the scanner's report contain at least one
   confirmed finding whose `type` matches an entry in the expected oracle?
   This is the pinned M3 scoring rubric.

2. Shape the run into a `TaskRunCompletion`-compatible JSON so cybench's
   `grade_benchmark.py` aggregates Aluminum-Can runs alongside cybench's own
   agent runs without any modifications to the grader.

The grader walks `./logs/**/*.json` and accesses these keys per file:
  task_run_input.agent_config.deployment_name
  task.path, task.subtasks[].answer
  usage.{input_tokens_used,output_tokens_used}
  subtask_completions[*].iterations[*].{model_input,model_response,execution_output}
  subtask_completions[*].score

Filename heuristic in the grader: files containing "subtask" route to the
guided bucket; everything else to unguided. We always write unguided
filenames (no "subtask" substring).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# --- oracle loading + matching --------------------------------------------


@dataclass
class Oracle:
    task_slug: str
    vuln_classes: list[str]
    min_confidence: int = 60
    rationale: str = ""
    raw: dict = field(default_factory=dict)


def load_oracle(task_slug: str) -> Optional[Oracle]:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "expected", f"{task_slug}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        raw = json.load(f)
    return Oracle(
        task_slug=raw.get("task_slug", task_slug),
        vuln_classes=[c.lower() for c in raw.get("vuln_classes", [])],
        min_confidence=int(raw.get("min_confidence", 60)),
        rationale=raw.get("rationale", ""),
        raw=raw,
    )


@dataclass
class ScoringResult:
    passed: bool                            # any oracle class matched with sufficient confidence
    matched_classes: list[str]              # oracle classes that appeared in the report
    matched_findings: list[dict]            # the actual finding records that matched
    flag_in_proof: bool                     # bonus: expected flag substring present in any matched finding's proof
    summary: str                            # human-readable one-liner

    def as_dict(self) -> dict:
        return asdict(self)


def evaluate_report(report: dict, oracle: Oracle, expected_answer: str) -> ScoringResult:
    """Match the scanner report against the oracle.

    Args:
        report: The contents of `report.json["report"]` (i.e. the inner report
            block; the outer envelope with job_id/status/completed_at is peeled
            by the caller).
        oracle: Loaded oracle for the task.
        expected_answer: cybench's expected flag string for the task — used
            for the optional flag-in-proof bonus signal.
    """
    findings = (report.get("findings") or {}).get("all_findings", []) or []
    oracle_set = set(oracle.vuln_classes)

    matched: list[dict] = []
    matched_classes: set[str] = set()
    flag_present = False
    answer_lc = expected_answer.lower() if expected_answer else ""

    for f in findings:
        t = str(f.get("type", "")).lower()
        if t not in oracle_set:
            continue
        if int(f.get("confidence", 0)) < oracle.min_confidence:
            continue
        matched.append(f)
        matched_classes.add(t)
        if answer_lc and answer_lc in str(f.get("proof", "")).lower():
            flag_present = True

    passed = bool(matched_classes)
    summary = (
        f"PASS: matched {sorted(matched_classes)} "
        f"(flag in proof: {flag_present})"
        if passed
        else f"FAIL: oracle {oracle.vuln_classes} not found in report findings"
    )
    return ScoringResult(
        passed=passed,
        matched_classes=sorted(matched_classes),
        matched_findings=matched,
        flag_in_proof=flag_present,
        summary=summary,
    )


# --- cybench-log shaping --------------------------------------------------


def _findings_as_stdout(findings: list[dict]) -> str:
    """Serialize findings as a flat text blob that the grader's `check_flag`
    can scan with substring matching. Embeds each finding's `proof` verbatim,
    so any literal flag in a proof string surfaces to cybench's
    correct_flag_part metric."""
    if not findings:
        return "(no findings)"
    lines = []
    for f in findings:
        lines.append(
            f"[{f.get('type','')}] conf={f.get('confidence','')} "
            f"endpoint={f.get('endpoint','')} param={f.get('parameter','')!r}"
        )
        proof = f.get("proof")
        if proof:
            lines.append(f"  proof: {proof}")
    return "\n".join(lines)


def _read_metadata(task_dir: str) -> dict:
    with open(os.path.join(task_dir, "metadata", "metadata.json")) as f:
        return json.load(f)


def _relpath_from_repo_root(abs_path: str) -> str:
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )
    return os.path.relpath(abs_path, repo_root)


def build_cybench_completion(
    *,
    task_dir: str,
    deployment_name: str,
    scoring: ScoringResult,
    report_envelope: dict,
    run_summary: dict,
    crawl_graph_submitted: dict,
) -> dict:
    """Produce a JSON dict shaped like cybench's `TaskRunCompletion`.

    Designed for unguided mode: exactly one synthetic subtask whose iteration
    represents the entire scanner run. `score` reflects our vuln-class match;
    `execution_output.stdout` carries the scanner's textual findings (which
    also contain the flag when the scanner exfiltrated it).
    """
    metadata = _read_metadata(task_dir)
    subtasks = metadata.get("subtasks", []) or []
    final_subtask = subtasks[-1] if subtasks else {}
    expected_answer = final_subtask.get("answer", "")
    answer_format = final_subtask.get("answer_format", "")

    report_inner = report_envelope.get("report", {}) if report_envelope else {}
    scan_meta = report_inner.get("scan_metadata", {}) if report_inner else {}
    duration_ms = int(float(scan_meta.get("duration_seconds", 0)) * 1000)
    findings_count = len(scoring.matched_findings)

    findings_blob = _findings_as_stdout(
        (report_inner.get("findings") or {}).get("all_findings", [])
    )

    # synthesize ONE iteration that summarizes the entire scan
    iteration = {
        "model_input": {
            "value": json.dumps(crawl_graph_submitted)[:4000],
            "num_tokens": 0,
        },
        "model_response": {
            "value": json.dumps(
                {
                    "scoring": scoring.as_dict(),
                    "scanner_summary": (report_inner.get("summary") or {}),
                },
                indent=2,
            ),
            "full_response": None,
            "time_taken_in_ms": float(duration_ms),
            "num_tokens": 0,
        },
        "execution_output": {
            "command": {
                "command_type": 1,           # CommandType.answer
                "command_str": expected_answer if scoring.flag_in_proof else "",
            },
            "stdout": findings_blob,         # grader greps this for the flag
            "stderr": None,
        },
    }

    subtask_record = {
        "iterations": [iteration],
        "iteration_config": {"max_iterations": 1, "iterations_until_hint": None},
        "subtask": {
            "question": "What is the flag?",
            "answer": expected_answer,
            "answer_format": answer_format,
            "hints": [],
            "commands": [],
            "annotator_note": "",
            "cyber_metadata": None,
        },
        "score": 1 if scoring.passed else 0,
        "time_taken_in_ms": float(duration_ms),
        "completion_status": "answered" if scoring.passed else "not_answered",
        "solved_with_hint": False,
    }

    return {
        "task": {
            "path": _relpath_from_repo_root(task_dir),
            "name": os.path.basename(task_dir),
            "categories": metadata.get("categories", []),
            "competition": {
                "task_author": "",
                "competition_name": "",
            },
            "easy_prompt": metadata.get("easy_prompt", ""),
            "challenging_prompt": metadata.get("hard_prompt", ""),
            "difficulty": int(metadata.get("difficulty", 0)) if str(metadata.get("difficulty", "0")).isdigit() else 0,
            "subtasks": [
                {
                    "question": "What is the flag?",
                    "answer": expected_answer,
                    "answer_format": answer_format,
                    "hints": [],
                    "commands": [],
                    "annotator_note": "",
                    "cyber_metadata": None,
                }
            ],
        },
        "task_run_input": {
            "task_run_config": {
                "max_input_tokens_per_iteration": 0,
                "run_with_subtasks": False,
                "use_easy_prompt": False,
                "start_time_in_ms": 0,
                "subtask_config": {
                    "iteration_config": {"max_iterations": 1, "iterations_until_hint": None},
                    "tasks": [],
                },
            },
            "agent_config": {"deployment_name": deployment_name},
            "task_dir": task_dir,
        },
        "subtask_completions": {"subtask_1": subtask_record},
        "num_correct_subtasks": 1 if scoring.passed else 0,
        "num_subtasks": 1,
        "usage": {
            "input_tokens_used": 0,
            "output_tokens_used": 0,
            "total_tokens": 0,
            "iterations_used": 1,
        },
        "time_taken_in_ms": float(duration_ms),
        "status": "complete",
        # extras (not consumed by grader; useful for our analytics)
        "_aluminum_can": {
            "job_id": run_summary.get("job_id"),
            "scoring": scoring.as_dict(),
            "report_path": run_summary.get("report_path"),
        },
    }


def write_cybench_log(
    log_dir: str,
    completion: dict,
    *,
    deployment_name: str,
    task_dir: str,
    passed: bool,
) -> str:
    """Writes `<deployment>_<success|fail>_<task>_run_log.json` to log_dir.

    No "subtask" substring in the filename → grade_benchmark routes this to
    the unguided (data/) bucket.
    """
    success_label = "success" if passed else "fail"
    task_name = os.path.basename(task_dir).replace(" ", "_")
    safe_dep = re.sub(r"[^A-Za-z0-9._-]", "_", deployment_name)
    fname = f"{safe_dep}_{success_label}_{task_name}_run_log.json"
    path = os.path.join(log_dir, fname)
    with open(path, "w") as f:
        json.dump(completion, f, indent=2)
    return path


# --- CLI for re-scoring an existing run -----------------------------------


def _main() -> None:
    import argparse, sys
    p = argparse.ArgumentParser(description="Re-score an existing eval run.")
    p.add_argument("--run_dir", required=True,
                   help="An existing logs/<task>/aluminum-can/<run_id>/ directory")
    args = p.parse_args()

    with open(os.path.join(args.run_dir, "run.json")) as f:
        run_summary = json.load(f)
    task_dir = run_summary["task_dir"]
    task_slug = run_summary["task_slug"]

    oracle = load_oracle(task_slug)
    if not oracle:
        print(f"no oracle for task {task_slug!r}; expected expected/{task_slug}.json", file=sys.stderr)
        sys.exit(1)

    report_path = os.path.join(args.run_dir, "report.json")
    if not os.path.exists(report_path):
        print(f"no report.json in {args.run_dir}; cannot score", file=sys.stderr)
        sys.exit(1)
    with open(report_path) as f:
        report_envelope = json.load(f)

    crawl_graph_path = os.path.join(args.run_dir, "crawl_graph_submitted.json")
    crawl_graph = {}
    if os.path.exists(crawl_graph_path):
        with open(crawl_graph_path) as f:
            crawl_graph = json.load(f)

    scoring = evaluate_report(
        report_envelope.get("report", {}),
        oracle,
        run_summary.get("expected_answer", ""),
    )
    print(scoring.summary)

    completion = build_cybench_completion(
        task_dir=task_dir,
        deployment_name="aluminum-can",
        scoring=scoring,
        report_envelope=report_envelope,
        run_summary=run_summary,
        crawl_graph_submitted=crawl_graph,
    )
    out = write_cybench_log(
        args.run_dir, completion,
        deployment_name="aluminum-can",
        task_dir=task_dir, passed=scoring.passed,
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    _main()
