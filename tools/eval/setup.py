"""
Task setup/teardown for the cybench × Aluminum-Can eval harness.

Lifted (and pared down) from `cybench/run_task.py`. Deliberately skipped:

- `requirements.sh` — installs tools into the cybench agent container, not
  the host. The scanner runs in its own container stack, so these don't
  apply (and running them would only pollute the host).
- `init_script.sh` — copies challenge files into the agent's $TMP_DIR. The
  scanner consumes `target_host` only; it does not read source artifacts.
- `rm -rf /app/benchmark` / `docker system prune` — agent-container-only
  sanitization in cybench's flow; no place in a host-side driver.

What we keep: `start_docker.sh` / `stop_docker.sh` (bring task target
servers up/down on `shared_net`) and a health check (verifies the target
responds before handing off to the scanner).

Note on the health check: cybench's original runs it from inside its agent
container, which sits on `shared_net` and can DNS-resolve names like
`otp:80`. From the host we can't. We route the check through the
`scanner-worker` container instead — same shared_net path the scanner will
actually use, so a passing check guarantees the scanner can reach the
target.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TaskHandle:
    task_dir: str
    task_name: str
    target_host: str            # space-separated "host:port [host:port ...]"
    expected_answer: str
    expected_answer_format: str
    categories: list[str]

    @property
    def primary_target(self) -> str:
        return self.target_host.split()[0] if self.target_host else ""


# --- internal helpers ------------------------------------------------------


def _read_metadata(task_dir: str) -> dict:
    path = os.path.join(task_dir, "metadata", "metadata.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"metadata.json not found at {path}")
    with open(path) as f:
        return json.load(f)


def _find_scanner_worker_cid() -> str:
    out = subprocess.check_output(
        ["docker", "ps", "--filter", "name=scanner-worker", "--format", "{{.ID}}"],
        text=True,
    ).strip()
    if not out:
        raise RuntimeError(
            "no running scanner-worker container found. Bring up the scanner "
            "stack with the eval override first (see tools/eval/README.md)."
        )
    return out.splitlines()[0]


def _curl_from_worker(host: str, port: int, timeout: int = 10) -> Optional[int]:
    """Returns the HTTP status code curled from inside scanner-worker, or None on failure."""
    cid = _find_scanner_worker_cid()
    try:
        proc = subprocess.run(
            ["docker", "exec", cid,
             "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(timeout),
             f"http://{host}:{port}/"],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        code_s = proc.stdout.strip()
        return int(code_s) if code_s.isdigit() else None
    except (subprocess.TimeoutExpired, ValueError):
        return None


# --- public API ------------------------------------------------------------


def host_health_check(target_host: str, attempts: int = 1, per_attempt_timeout: int = 10) -> None:
    """Verifies every host:port in `target_host` responds via HTTP from inside scanner-worker.

    Any non-zero HTTP status counts as alive (matches the spirit of the
    original, which falls back to nc if HTTP isn't 200). Raises RuntimeError
    if any host fails after `attempts`.
    """
    hosts = target_host.split()
    failed: list[str] = []
    for entry in hosts:
        try:
            host, port_s = entry.split(":")
            port = int(port_s)
        except ValueError:
            raise ValueError(f"invalid target_host {entry!r} (want host:port)")

        ok = False
        for _ in range(attempts):
            code = _curl_from_worker(host, port, timeout=per_attempt_timeout)
            if code is not None and code > 0:
                logger.info("health OK: %s:%s -> HTTP %s", host, port, code)
                ok = True
                break
        if not ok:
            failed.append(entry)

    if failed:
        raise RuntimeError(f"health check failed for: {', '.join(failed)}")


def setup_task(task_dir: str, health_timeout_s: int = 60) -> TaskHandle:
    """Brings up a cybench task's docker stack on shared_net and verifies
    target_host is reachable from inside scanner-worker.

    Args:
        task_dir: path to a cybench task directory (containing metadata/
            and start_docker.sh). Absolute or repo-relative.
        health_timeout_s: wall-clock seconds to wait for target_host to
            become reachable after start_docker.sh returns.

    Returns:
        TaskHandle with the fields downstream phases need.
    """
    task_dir = os.path.abspath(task_dir)
    if not os.path.isdir(task_dir):
        raise FileNotFoundError(f"task_dir does not exist: {task_dir}")

    metadata = _read_metadata(task_dir)
    target_host = metadata.get("target_host", "")
    if not target_host:
        raise ValueError(
            f"task has no target_host — this eval only handles networked tasks ({task_dir})"
        )
    subtasks = metadata.get("subtasks", []) or []
    if not subtasks:
        raise ValueError(f"metadata has no subtasks: {task_dir}")
    final = subtasks[-1]

    start_script = os.path.join(task_dir, "start_docker.sh")
    if not os.path.exists(start_script):
        raise FileNotFoundError(f"start_docker.sh not found at {start_script}")

    logger.info("starting task containers: %s", start_script)
    subprocess.run(["bash", "start_docker.sh"], check=True, cwd=task_dir)

    deadline = time.time() + health_timeout_s
    last_err: Optional[Exception] = None
    while time.time() < deadline:
        try:
            host_health_check(target_host, attempts=1, per_attempt_timeout=5)
            break
        except RuntimeError as e:
            last_err = e
            time.sleep(2)
    else:
        raise RuntimeError(
            f"target_host {target_host!r} did not become healthy within "
            f"{health_timeout_s}s (last: {last_err})"
        )

    return TaskHandle(
        task_dir=task_dir,
        task_name=os.path.basename(task_dir),
        target_host=target_host,
        expected_answer=final.get("answer", ""),
        expected_answer_format=final.get("answer_format", ""),
        categories=metadata.get("categories", []),
    )


def teardown_task(task_dir: str) -> None:
    """Runs stop_docker.sh if present. Idempotent; never raises."""
    task_dir = os.path.abspath(task_dir)
    stop_script = os.path.join(task_dir, "stop_docker.sh")
    if not os.path.exists(stop_script):
        logger.info("no stop_docker.sh at %s (skipping)", stop_script)
        return
    logger.info("stopping task containers: %s", stop_script)
    subprocess.run(["bash", "stop_docker.sh"], cwd=task_dir, check=False)


# --- CLI -------------------------------------------------------------------


def _main() -> None:
    p = argparse.ArgumentParser(
        description="Bring a cybench task up/down on shared_net. M1 acceptance helper."
    )
    p.add_argument("--task_dir", required=True)
    p.add_argument("--down", action="store_true", help="only run teardown")
    p.add_argument("--health-timeout", type=int, default=60)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.down:
        teardown_task(args.task_dir)
        return

    try:
        handle = setup_task(args.task_dir, health_timeout_s=args.health_timeout)
    except Exception:
        logger.exception("setup_task failed")
        teardown_task(args.task_dir)
        sys.exit(1)

    print(json.dumps({
        "task_name": handle.task_name,
        "target_host": handle.target_host,
        "primary_target": handle.primary_target,
        "expected_answer": handle.expected_answer,
        "expected_answer_format": handle.expected_answer_format,
        "categories": handle.categories,
    }, indent=2))


if __name__ == "__main__":
    _main()
