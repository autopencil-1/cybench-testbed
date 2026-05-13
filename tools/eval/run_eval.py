"""
End-to-end eval driver: run one cybench task through Aluminum-Can.

Pipeline:
    1. setup_task(task_dir)              — bring task up on shared_net (M1)
    2. POST /api/scans                   — submit crawled graph + config(risk_level=2)
    3. Poll GET /api/scans/{job_id}      — wait until terminal status
    4. GET /api/scans/{job_id}/report    — fetch the scanner report
    5. Write artifacts under logs/<task>/aluminum-can/<run_id>/
    6. teardown_task(task_dir)

Scoring is intentionally NOT done here — that's M3 (`scoring.py`). This driver
just runs the pipeline and persists results.

Assumes (per tools/eval/README.md):
- Scanner stack is running with the eval override applied (worker on shared_net).
- ScanAPI reachable at http://localhost:8890.
- shared_net network exists.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from setup import TaskHandle, setup_task, teardown_task
from scoring import (
    build_cybench_completion,
    evaluate_report,
    load_oracle,
    write_cybench_log,
)

DEPLOYMENT_NAME = "aluminum-can"

logger = logging.getLogger(__name__)

SCAN_API = os.environ.get("SCAN_API_URL", "http://localhost:8890")
DEFAULT_RISK_LEVEL = 2
DEFAULT_POLL_INTERVAL_S = 10
DEFAULT_WALL_TIMEOUT_S = 20 * 60   # 20 min per task
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


# --- HTTP helpers ----------------------------------------------------------


def _http_json(method: str, path: str, body: Optional[dict] = None, timeout: int = 30) -> dict:
    url = f"{SCAN_API}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_s = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {body_s}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"{method} {url} failed: {e}") from e


def submit_scan(crawl_graph: dict, risk_level: int, server_info_overrides: Optional[dict] = None) -> dict:
    body = {
        "crawl_graph": crawl_graph,                     # post-rename wrapper key (was "sitemap")
        "config": {"risk_level": risk_level},
    }
    if server_info_overrides:
        body["server_info_overrides"] = server_info_overrides
    return _http_json("POST", "/api/scans", body=body)


def get_status(job_id: str) -> dict:
    return _http_json("GET", f"/api/scans/{job_id}")


def get_report(job_id: str) -> dict:
    return _http_json("GET", f"/api/scans/{job_id}/report")


def poll_until_terminal(job_id: str, poll_s: int, wall_timeout_s: int) -> dict:
    deadline = time.time() + wall_timeout_s
    last_status = None
    while time.time() < deadline:
        info = get_status(job_id)
        status = info.get("status")
        if status != last_status:
            logger.info("job %s status: %s", job_id, status)
            last_status = status
        if status in TERMINAL_STATUSES:
            return info
        time.sleep(poll_s)
    raise TimeoutError(f"job {job_id} did not reach a terminal status within {wall_timeout_s}s")


# --- main pipeline ---------------------------------------------------------


def _load_crawl_graph(task_slug: str) -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "crawl_graphs", f"{task_slug}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"no crawled graph for task {task_slug!r} (expected {path})"
        )
    with open(path) as f:
        return json.load(f)


def _make_log_dir(task_slug: str) -> str:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    ts = datetime.now().strftime("%Y_%m_%d_%H-%M-%S")
    log_dir = os.path.join(repo_root, "logs", task_slug, "aluminum-can", ts)
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def _dump(log_dir: str, name: str, payload: object) -> str:
    path = os.path.join(log_dir, name)
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path


def run_eval(task_dir: str, *, risk_level: int = DEFAULT_RISK_LEVEL,
             poll_s: int = DEFAULT_POLL_INTERVAL_S,
             wall_timeout_s: int = DEFAULT_WALL_TIMEOUT_S,
             keep_task_up: bool = False) -> dict:
    """Returns a summary dict describing the eval run."""
    task_dir = os.path.abspath(task_dir)
    task_slug = os.path.basename(task_dir)

    crawl_graph = _load_crawl_graph(task_slug)
    log_dir = _make_log_dir(task_slug)
    logger.info("log dir: %s", log_dir)

    summary: dict = {
        "task_slug": task_slug,
        "task_dir": task_dir,
        "log_dir": log_dir,
        "scanner": "aluminum-can",
        "risk_level": risk_level,
        "started_at": datetime.now().isoformat(),
    }

    _dump(log_dir, "crawl_graph_submitted.json", crawl_graph)

    handle: Optional[TaskHandle] = None
    try:
        handle = setup_task(task_dir)
        summary["target_host"] = handle.target_host
        summary["expected_answer"] = handle.expected_answer

        logger.info("submitting scan to %s", SCAN_API)
        submit_resp = submit_scan(crawl_graph, risk_level=risk_level)
        _dump(log_dir, "submit_response.json", submit_resp)
        job_id = submit_resp.get("job_id")
        if not job_id:
            raise RuntimeError(f"submit response missing job_id: {submit_resp}")
        summary["job_id"] = job_id
        logger.info("job_id: %s", job_id)

        final_status = poll_until_terminal(job_id, poll_s=poll_s, wall_timeout_s=wall_timeout_s)
        _dump(log_dir, "final_status.json", final_status)
        summary["final_status"] = final_status.get("status")

        if final_status.get("status") == "completed":
            try:
                report = get_report(job_id)
                _dump(log_dir, "report.json", report)
                summary["report_path"] = os.path.join(log_dir, "report.json")
            except Exception:
                logger.exception("failed to fetch report for completed job %s", job_id)
                summary["report_error"] = "fetch failed"
            else:
                # M3: score against oracle and emit a cybench-shaped log
                oracle = load_oracle(task_slug)
                if oracle is None:
                    logger.warning("no oracle for %r — skipping scoring", task_slug)
                    summary["scoring"] = {"skipped": "no oracle"}
                else:
                    scoring = evaluate_report(
                        report.get("report", {}),
                        oracle,
                        handle.expected_answer if handle else "",
                    )
                    logger.info(scoring.summary)
                    summary["scoring"] = scoring.as_dict()
                    completion = build_cybench_completion(
                        task_dir=task_dir,
                        deployment_name=DEPLOYMENT_NAME,
                        scoring=scoring,
                        report_envelope=report,
                        run_summary=summary,
                        crawl_graph_submitted=crawl_graph,
                    )
                    cybench_log_path = write_cybench_log(
                        log_dir, completion,
                        deployment_name=DEPLOYMENT_NAME,
                        task_dir=task_dir,
                        passed=scoring.passed,
                    )
                    summary["cybench_log_path"] = cybench_log_path
                    logger.info("wrote cybench-shaped log: %s", cybench_log_path)
        else:
            logger.warning("job ended in non-completed state: %s", final_status.get("status"))

    finally:
        summary["finished_at"] = datetime.now().isoformat()
        _dump(log_dir, "run.json", summary)
        if handle and not keep_task_up:
            teardown_task(task_dir)

    return summary


# --- CLI -------------------------------------------------------------------


def _main() -> None:
    p = argparse.ArgumentParser(description="Run one cybench task through Aluminum-Can.")
    p.add_argument("--task_dir", required=True)
    p.add_argument("--risk-level", type=int, default=DEFAULT_RISK_LEVEL)
    p.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL_S)
    p.add_argument("--timeout", type=int, default=DEFAULT_WALL_TIMEOUT_S,
                   help="wall-clock max for scan, seconds")
    p.add_argument("--keep-task-up", action="store_true",
                   help="don't run stop_docker.sh after; useful for debugging")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    summary = run_eval(
        args.task_dir,
        risk_level=args.risk_level,
        poll_s=args.poll_interval,
        wall_timeout_s=args.timeout,
        keep_task_up=args.keep_task_up,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    _main()
