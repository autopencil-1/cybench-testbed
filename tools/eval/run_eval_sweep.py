"""
M4 sweep runner: iterate every task in `eval_task_list.txt` through
`run_eval.py`, then call `grade.py` for the aggregated view.

Subprocess-per-task gives clean isolation: a crash, hang-then-timeout, or
scanner-side failure on one task doesn't poison the rest of the sweep.
Each line in the task list may carry an optional per-task wall-clock budget
(seconds), overriding the default.

Usage:
    python3 tools/eval/run_eval_sweep.py
    python3 tools/eval/run_eval_sweep.py --skip-existing
    python3 tools/eval/run_eval_sweep.py --task-list custom_list.txt

`--skip-existing` skips any task whose latest run already produced a
`success` cybench log — useful for re-running a partial sweep without
paying twice for tasks that already passed.
"""

from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time
from typing import Iterable

DEFAULT_WALL_TIMEOUT = 1800   # 30 min


def parse_task_list(path: str) -> list[tuple[str, int]]:
    rows: list[tuple[str, int]] = []
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.rsplit(None, 1)
            if len(parts) == 2 and parts[-1].isdigit():
                rows.append((parts[0], int(parts[-1])))
            else:
                rows.append((line, DEFAULT_WALL_TIMEOUT))
    return rows


def has_successful_run(task_slug: str, logs_root: str) -> bool:
    pattern = os.path.join(
        logs_root, task_slug, "aluminum-can", "*",
        "aluminum-can_success_*_run_log.json",
    )
    return bool(glob.glob(pattern))


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{sec:02d}s"
    return f"{m}m{sec:02d}s"


def main() -> int:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    default_list = os.path.join(repo_root, "tools/eval/eval_task_list.txt")

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task-list", default=default_list)
    p.add_argument("--skip-existing", action="store_true",
                   help="Skip tasks whose latest run already passed")
    p.add_argument("--poll-interval", type=int, default=15)
    p.add_argument("--no-grade", action="store_true",
                   help="Skip the final grade.py invocation")
    args = p.parse_args()

    tasks = parse_task_list(args.task_list)
    if not tasks:
        print(f"no tasks in {args.task_list}", file=sys.stderr)
        return 1

    logs_root = os.path.join(repo_root, "logs")
    sweep_start = time.time()
    skipped = 0
    completed = 0
    failed = 0

    print(f"sweep starts: {len(tasks)} tasks, list={args.task_list}", flush=True)
    print("=" * 60, flush=True)

    for i, (task_dir, timeout) in enumerate(tasks, start=1):
        slug = os.path.basename(task_dir)
        header = f"[{i}/{len(tasks)}] {slug}"

        if args.skip_existing and has_successful_run(slug, logs_root):
            print(f"\n{header} — already passed, skipping", flush=True)
            skipped += 1
            continue

        print(f"\n{header} (budget {timeout}s)", flush=True)
        t0 = time.time()
        cmd = [
            sys.executable, "tools/eval/run_eval.py",
            "--task_dir", task_dir,
            "--timeout", str(timeout),
            "--poll-interval", str(args.poll_interval),
        ]
        try:
            rc = subprocess.run(cmd, cwd=repo_root).returncode
        except KeyboardInterrupt:
            print(f"\n{header} — interrupted by user; aborting sweep", flush=True)
            return 130
        elapsed = time.time() - t0

        if rc == 0:
            completed += 1
            status = "OK"
        else:
            failed += 1
            status = f"FAILED (rc={rc})"
        print(f"{header} → {status} in {_fmt_dur(elapsed)}", flush=True)

    total = time.time() - sweep_start
    print("\n" + "=" * 60, flush=True)
    print(f"sweep done: completed={completed} failed={failed} skipped={skipped} "
          f"total_time={_fmt_dur(total)}", flush=True)

    if not args.no_grade:
        print("\nAggregated results:", flush=True)
        subprocess.run([sys.executable, "tools/eval/grade.py",
                        "--expected-total", str(len(tasks))], cwd=repo_root)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
