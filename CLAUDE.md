# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Cybench is a benchmark of ~40 Capture-The-Flag (CTF) tasks from four competitions (HackTheBox cyber-apocalypse-2024, HKCert 2022, project-sekai-ctf 2022/2023, plus a bug_bounty task) used to evaluate LLM cybersecurity agents. Code here runs an LLM-driven shell agent inside a Kali Linux Docker container, exercises each task end-to-end, and scores it.

## Environment

- Python **3.9–3.10** only (Dockerfile pins 3.9; CI uses 3.10). Anything 3.11+ will break HELM/tokenizer deps.
- API keys live in `.env` at the repo root (template: `.env.example`). `agent/agent.py` calls `load_dotenv` from cwd, so always run from the repo root.
- The agent itself runs inside Docker (`cybench/cybench:latest`, derived from `cybench/kali-linux-large`). `run_task.sh` mounts `./agent` and `./run_task.py` into the container as read-only volumes so you can edit those without rebuilding the image. Anything else (e.g. `requirements.txt`, `benchmark/`) requires a rebuild — see `docs/add_model.md`.
- The shell scripts (`run_task.sh`, `run_solution.sh`) refuse to run unless cwd basename is `cybench`.

## Common commands

```bash
# Single task, subtask mode (per-subtask scoring)
./run_task.sh --task_dir "benchmark/hackthebox/cyber-apocalypse-2024/crypto/[Very Easy] Dynastic" \
  --max_iterations 5 --max_input_tokens 6000 --model openai/gpt-4o-2024-05-13

# Single task, unguided mode (binary score on final flag only)
./run_task.sh ... --unguided_mode --max_iterations 15 ...

# Whole benchmark (iterates over task_list.txt or subtask_list.txt)
python3 run_benchmark.py --model openai/gpt-4o-2024-05-13 --max_iterations 5 \
  --max_input_tokens 6000 --max_output_tokens 2000

# Score all runs that wrote logs under ./logs/
python3 grade_benchmark.py

# Verify a task's official solution still produces the expected flag
./run_solution.sh "benchmark/.../<task>/metadata/solution/solution.sh"

# Tests (the `--dirs` arg is required by tests/conftest.py for several of them)
pytest tests/ -vv                                                      # full suite (non-live)
pytest tests/solutions_test.py::test_solution_script --dirs <sol_dir>  # one task's solution
pytest tests/metadata_json_test.py::test_metadata --dirs <metadata_dir>
pytest tests/init_script_test.py::test_modified_init_script --dirs <task_dir>
pytest tests/metadata_flag_consistency_test.py --dirs <metadata_dir>   # metadata.json answer vs flag.txt
pytest -m live                                                         # live API tests (excluded by default via pytest.ini)
```

Useful flags shared by `run_task.sh` / `run_benchmark.py`: `--helm` (route through Stanford CRFM HELM instead of direct provider SDKs), `--azure` (use Azure OpenAI deployments — endpoint must have no trailing slash, deployment name must match model name), `--interactive_mode`, `--extend_iterations_from_log <logfile>` (resume an unguided run).

Style is enforced by pre-commit: `black --line-length=100` and `isort --line-length=100 --profile=black`. Install with `pre-commit install`.

## Architecture

### Top-level entrypoints
- `run_task.sh` is a thin host-side wrapper: it execs the task's `start_docker.sh` (if any), runs the `cybench/cybench` container with the task's args, copies `/app/logs/` out of the container, then execs `stop_docker.sh`.
- `run_task.py` (runs **inside** the container) is the orchestrator. It reads `metadata/metadata.json`, runs `requirements.sh` and `init_script.sh`, deletes `/app/benchmark` to keep the agent from cheating, prunes Docker, performs a host health-check on `target_host` (curl + nc), builds the prompt, and constructs a `SimpleAgent`.
- `run_benchmark.py` is just a loop that shells out to `run_task.sh` for each line of a task-list file and runs `docker rm -f $(docker ps -aq)` between tasks.

### Agent loop (`agent/agent.py::SimpleAgent`)
For each subtask, the agent iterates up to `max_iterations` times:
1. Stringify the current `ChatChain`, truncate token-wise to `max_input_tokens` (keeps head + tail with `...TRUNCATED...` in the middle).
2. Send to the model via `_handle_request` — branches on `self.helm` (Stanford CRFM service) vs `non_helm_request` (direct provider SDKs in `agent/models/non_helm.py`). `o1`/`o1-mini` get special handling (no stop sequences, fixed temperature=1).
3. Parse `COMMAND: ...<END>` or `ANSWER: ...` from the model output (`_parse_command` / `_parse_answer`). The `<END>` `STOP_TOKEN` is defined in `agent/prompt.py`.
4. Shell commands execute via `subprocess.run(["bash", "-c", ...])` in `self.work_dir` (`/tmp/cyber-bench` inside the container) with a 120 s `TIMEOUT_PER_COMMAND`. Result becomes an `Observation` message in the chat chain.
5. Two parallel chat chains are maintained: the full `chat_chain` (used for logging / dumps) and a `truncated_chat_chain` that keeps only the last `responses_to_keep` model responses and `observations_to_keep` observations. The truncated chain is what's sent to the model after iteration 0.

`unguided_mode` collapses all subtasks in `metadata.json` into a single synthetic "What is the flag?" subtask whose hints/commands are the union of all subtasks' (see `unguided_mode()` in `run_task.py`).

### Model registry
`agent/dataclasses/agent_spec.py` is the single source of truth for supported models:
- `DeploymentName` enum — must be updated for every new model.
- `TokenizerMapping` — maps deployment → tokenizer name.
- `NonHELMMapping` — maps deployment → provider model id.
- `agent/models/non_helm.py` holds the provider adapters (`PROVIDERS` dict, classes inheriting `ModelProvider`). To add a new provider, implement `create_client`, `query_model_provider`, `parse_model_response`, `tokenize`, `decode_tokens`, `get_num_tokens` (see `docs/add_model.md`).

### Task layout (`benchmark/<source>/<competition>/<task>/`)
Each task must provide:
- `init_script.sh <TMP_DIR>` — copies challenge files into the agent's working dir (`/tmp/cyber-bench`). The historic second arg `EASY_PROMPT` is deprecated; ignore it in new scripts.
- `metadata/metadata.json` — schema in `benchmark/task_metadata_schema.json`. Must include `categories`, `difficulty` (0–5), `easy_prompt`, `hard_prompt`, `target_host` (or empty), `subtasks[]`. The last subtask's `answer` is the final flag.
- `metadata/requirements.sh` — task-specific apt/pip installs (create an empty file if none).
- `metadata/solution/solution.sh` — must actually solve the task; its last line of stdout is the flag. `metadata/solution/flag.txt` is the expected flag.
- Optional `start_docker.sh` / `stop_docker.sh` — used when a task needs server containers; they run on the **host**, not in the agent container, and must attach containers to the shared `shared_net` Docker network so the agent can reach them.

### Data flow / dataclasses
- `benchmark/dataclasses/task.py` — `Task`, `Competition`, `CyberCategory`, `CyberMetadata`, etc.
- `benchmark/dataclasses/task_run.py` — `Subtask`, `SubtaskRun`, `Iteration`, `Command`, `CommandExecutionOutput`, `ModelInput`, `ModelResponse`, `Usage`, `CompletionStatus`.
- `benchmark/dataclasses/task_run_input.py` — `TaskRunInput`, `TaskRunConfig`, `SubtaskConfig`, `TaskRunCompletion`. Runs are serialized as JSON to `logs/<task>/<model>/<benchmark_id>/<model>_<success|fail>[_subtask]_<task>_run_log.json`. `grade_benchmark.py` aggregates these.
- `agent/dataclasses/chat_chain.py` — the chat chain abstraction with `model_response_index` / `assistant_observation_index` used by the truncation logic.

### CI (`.github/workflows/ci-tests.yml`)
On PR, CI diffs against `origin/main` and runs targeted tests only for changed paths:
- Touching `metadata/solution/**` → runs `solutions_test.py` against that solution.
- Touching `init_script.sh` → runs `init_script_test.py` for the task dir.
- Touching `metadata.json` → runs `metadata_json_test.py`.
- Touching `metadata.json` or `metadata/solution/flag.txt` → runs `metadata_flag_consistency_test.py` to check the final-subtask answer matches `flag.txt`.

The CI uses `cybench/kali-linux-ci:latest` (a slimmer image than the runtime `kali-linux-large`).

## Gotchas

- `task_list.txt` and `subtask_list.txt` are currently identical in content; pass `--task_list` explicitly if you want only one.
- `task_dir` paths often contain spaces and `[brackets]` (e.g. `[Very Easy] Dynastic`) — always quote them.
- `target_host` strings are space-separated `host:port` pairs; `run_task.py::host_health_check` will fail-fast (`sys.exit(1)`) if any host is unreachable, so `start_docker.sh` must finish bringing the service up before the agent container starts.
- `run_task.py` deletes `/app/benchmark` inside the container before the agent runs — don't expect the agent to be able to read other tasks' files.
- `agent/sample_response.txt` is used when `--mock_calls` is set; useful for testing parsing without burning API credits.
