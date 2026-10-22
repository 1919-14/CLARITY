# Parallel Hypothesis Evaluation on Nodus (Issue #13)

This document describes how CLARITY evaluates independent candidate
hypotheses concurrently on top of the Issue #12 Nodus Compute integration.

> **Verification status:** the orchestration, contracts, failure handling, and
> concurrency limits are verified by offline tests. **Live remote execution and
> remote benchmarking are pending** because Nodus credits/service access are not
> available. No remote result, task ID, duration, cost, or speedup is claimed
> here. See [nodus-readiness.md](nodus-readiness.md).

## 1. Purpose and boundaries

The service evaluates many candidates independently and aggregates the results
deterministically. It does **not** implement fitting, metrics, ranking, or SDK
details:

| Responsibility | Owner |
|---|---|
| Candidate fitting/evaluation | `app/domain/evaluation.py` (Issue #6, unchanged) |
| Provider SDK, client lifecycle, error sanitization | `app/adapters/nodus/` (Issue #12) |
| Task construction, mode selection, scheduling, retries, correlation, aggregation, metrics | `app/services/parallel_evaluation.py` (new) |
| Serializable task payload and worker execution | `app/services/evaluation_task_runtime.py` (new) |
| Remote Function entry point | `app/nodus_worker.py` (new) |
| Discovery integration | `ParallelEvaluationPipeline` implements the existing `evaluate_many` boundary; `DiscoveryEngine` is unchanged |

Submission and aggregation are separate stages. `ParallelEvaluationRunner.run`
builds and dispatches tasks; `_aggregate` orders validated results by the
original request order, never by completion order.

## 2. Execution modes

`NODUS_PARALLEL_EXECUTION_MODE` is required; there is no default. Omitting it
raises before any work starts.

| Mode | Value | Behaviour |
|---|---|---|
| Local sequential | `local_sequential` | Runs the shared worker function in-process, one request at a time. No adapter call, no credentials, no network. Baseline for correctness comparison. |
| Nodus parallel | `nodus_parallel` | Submits tasks through `NodusExecutionAdapter.execute_many`. Requires `NODUS_EXECUTION_MODE=nodus_remote`. |

`nodus_parallel` refuses to start unless the adapter is configured for
`nodus_remote`. A remote configuration error, missing credential, deployment
problem, or provider failure never falls back to local or mock execution, and
every result records the actual `execution_mode` that produced it.

### Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `NODUS_PARALLEL_EXECUTION_MODE` | yes | — | `local_sequential` or `nodus_parallel` |
| `NODUS_MAX_CONCURRENCY` | no | `4` | In-flight task limit, 1–32 |
| `NODUS_PARALLEL_MAX_RETRIES` | no | `1` | Extra pre-acceptance submissions per task, 0–5 |
| `NODUS_PARALLEL_RETRY_BACKOFF_SECONDS` | no | `1.0` | Fixed delay between retry rounds |
| `NODUS_PARALLEL_TASK_TIMEOUT_SECONDS` | no | adapter timeout | Per-task timeout |

The Issue #12 variables (`NODUS_EXECUTION_MODE`, `NODUS_API_KEY`,
`NODUS_APP_NAME`, `NODUS_FUNCTION_NAME`, `NODUS_PROJECT`, …) still apply.
`NODUS_PARALLEL_*` values are validated on load; invalid values fail loudly.

## 3. Task and result contracts

### Payload (`schema_version = "1.0"`)

Built by `build_evaluation_task_payload` from an `EvaluationRequest` and the
committed observations:

```text
schema_version, request_id, hypothesis_id,
candidate            # HypothesisRepresentation.to_dict()
observations[]       # Observation.to_dict() for referenced IDs only
observation_ids[]    # training partition (missing IDs are counted as exclusions)
held_out_observation_ids[]
metric_names[]
fitting_config       # method, rank_tolerance (reproducibility)
dataset_id
```

The payload is checked three times:

1. `verify_no_ground_truth_leakage` rejects evaluator-only keys.
2. Strict JSON serialization (`allow_nan=False`) rejects non-serializable or
   non-finite values before submission.
3. `validate_evaluation_task_payload` re-validates on the worker side.

### Worker envelope

```text
schema_version, hypothesis_id, request_id,
status: "succeeded" | "failed"
evaluation: EvaluationResult.to_dict() | null
error: {code, message, details} | null
```

`status` describes the **task**, not the science. A failed fit is a valid
scientific outcome: the task envelope is `succeeded` and
`evaluation.status == "failed"`. A task-level failure carries **no** evaluation
object, so a failure can never be mistaken for a score.

`parse_task_result_envelope` rejects schema mismatches, unknown statuses,
non-finite metrics, failed evaluations without an error, and hypothesis-ID
mismatches.

### Per-task result

`EvaluationTaskResult` records `task_id`, `hypothesis_id`, `request_id`,
`outcome` (`succeeded` / `failed` / `timed_out` / `cancelled`), `execution_mode`,
`attempts`, the validated evaluation or structured error, provider task ID,
provider metadata, durations, and whether cancellation was requested.

Task IDs are deterministic (`eval-<index>-<payload digest>`): the same inputs at
the same position produce the same ID, while different candidate payloads never
collide.

## 4. Concurrency

`NodusExecutionAdapter.execute_many(tasks, max_concurrency=n)` extends Issue #12
without changing `execute()`:

1. Acquires the process-global client lock **once** for the whole batch.
2. Configures the SDK default client once and restores it once at teardown.
3. Runs up to `n` tasks on a worker-thread pool; each task keeps its own
   `FunctionCall` handle, timeout, and error mapping.
4. Returns results in input order.

The single client scope is required by `nodus-compute==1.0.27`, which keeps one
process-wide default client. Without it, concurrent tasks could swap or close
each other's client transport. Because the scope is held for the whole batch,
one task cannot close the client while another is in flight.

A configured limit of `1` behaves sequentially. The limit is enforced in both
the adapter (worker pool size) and the orchestrator (batch request); observed
concurrency is asserted in tests with a fake that tracks active tasks.

Local mode is intentionally sequential: in-process evaluation has no I/O to
overlap, and concurrent local execution would add scheduling noise without
evidence.

## 5. Failure, retry, and cancellation semantics

| Situation | Result | Retried? |
|---|---|---|
| Task succeeded, valid envelope | `succeeded` + validated `EvaluationResult` | — |
| Task succeeded, scientific fit failed | `succeeded`, `evaluation.status = failed`, no metrics | no |
| Malformed/mismatched envelope | `failed`, `NODUS_PARALLEL_INVALID_TASK_RESULT`, no score | no |
| Provider timeout | `timed_out`, no score | no (already accepted; billable) |
| Provider failure before acceptance | `failed` | yes, if code is `NODUS_RATE_LIMITED` / `NODUS_SDK_ERROR` and `provider_task_id` is absent |
| Provider failure after acceptance | `failed` with provider ID | never (avoids duplicate billable work) |
| Auth/permission/invalid payload | `failed` | no |
| Missing result for a submitted task | `failed`, `NODUS_PARALLEL_MISSING_TASK_RESULT` | no (ambiguous) |
| Duplicate result | first result kept, ID recorded in `duplicate_task_ids` | — |
| Unexpected result | excluded, ID recorded in `unexpected_task_ids` | — |
| Adapter raised for the batch | every pending task `failed`, `NODUS_PARALLEL_DISPATCH_FAILED` | no |
| Cancel requested before dispatch | `cancelled`, no provider call | — |
| Cancel requested before a later retry round | remaining tasks `cancelled` | — |

Retries are owned by the orchestrator: tasks are submitted with the adapter's
own `max_retries=0` so the total attempt count is bounded by one documented
policy. Retries apply only to failures observed before the provider accepted the
task (`provider_task_id is None`). A task that may already be running is never
resubmitted.

Cancellation is cooperative between dispatch rounds. In-flight remote calls are
allowed to finish and their results are preserved. The adapter requests
cancellation after a client-side `get()` timeout, but the SDK does not confirm
remote termination, so results record `cancellation_requested` and
`cancellation_confirmed: false`. No result claims a remote task was stopped.

## 6. Deterministic aggregation and metrics

Results are returned in the original request order regardless of completion
order. Aggregation rejects missing, duplicate, unexpected, and mismatched
results explicitly; it never fills gaps with zeros or inferred scores.

The summary reports only values derived from real events:

```text
requested_mode, actual_mode, requested_tasks, submitted_tasks,
completed_tasks, evaluation_failures, failed_tasks, timed_out_tasks,
cancelled_tasks, max_concurrency, retry_attempts, duration_seconds,
provider_metadata_available, cost_status, estimated_cost, settled_cost
```

If the provider reports no metadata, `provider_metadata_available` is `false`
and provider fields stay `null`. `cost_status` is always `unavailable` on the
verified SDK surface; costs are never estimated or presented as charges.

## 7. Scientific safeguards

- Fitting still runs through `EvaluationPipeline`; algorithms are unchanged.
- Payloads carry only the candidate specification and committed observations.
  Ground-truth/evaluator-only keys are rejected before submission and before
  execution.
- Failed tasks produce `EvaluationResult(status=FAILED)` with no metrics and no
  fitted specification, so they cannot rank favorably.
- Parallel evaluation never queries the oracle. It evaluates already-committed
  observations, so adding parallelism cannot duplicate billable oracle calls.
  The discovery loop keeps its own query budget and duplicate guard.
- Logging uses task IDs, hypothesis IDs, modes, and counts; credentials, headers,
  and payload contents are not logged.

## 8. Remote worker deployment

Deploy `app.nodus_worker:evaluate` as the Function named by `NODUS_APP_NAME` /
`NODUS_FUNCTION_NAME`. The Issue #12 adapter invokes it with
`operation`, `payload`, `task_id`, `correlation_id` as keyword arguments.

Requirements:

- The deployed image must be able to import `app.nodus_worker` and its
  dependencies (`app.domain`, `app.services`); no Flask app object is needed.
- No database, local files, working-directory assumptions, or extra secrets.
- Operations: `evaluate_candidate` (payload contract above) and `smoke_test`
  (echoes `payload["echo_value"]`, used by the smoke test and benchmark).
- Unknown operations raise, so the platform reports a failed call rather than a
  fabricated success.
- Deployment/registration is performed outside the application with the
  provider's supported procedure; CLARITY does not deploy Functions itself.

## 9. Commands

```bash
cd backend

# Offline suite (no credentials, no network, no credits)
python -m pytest tests/test_nodus_parallel.py -q

# Issue #12 adapter regressions
python -m pytest tests/test_nodus_adapter.py -q

# Issue #6/#8 regressions
python -m pytest tests/test_evaluation.py tests/test_discovery_loop.py -q

# Full backend suite
python -m pytest -q

# Local benchmark (correctness + local timing only; not a provider speedup)
python -m benchmarks.nodus_parallel_benchmark

# Opt-in remote smoke test (billable, NOT run in CI)
NODUS_EXECUTION_MODE=nodus_remote \
NODUS_API_KEY=... \
NODUS_APP_NAME=... \
NODUS_FUNCTION_NAME=... \
NODUS_PARALLEL_EXECUTION_MODE=nodus_parallel \
python -m app.nodus_parallel_smoke_test

# Opt-in remote benchmark
python -m benchmarks.nodus_parallel_benchmark --remote
```

Smoke-test exit codes: `0` all tasks succeeded and echoed their own input, `1`
failure/mismatch, `2` mode guard or unusable configuration.

## 10. Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `NODUS_PARALLEL_EXECUTION_MODE is not set` | No mode configured | Set the mode explicitly |
| `requires NODUS_EXECUTION_MODE=nodus_remote` | Adapter is mock/local | Configure remote; no fallback exists |
| `NODUS_MISSING_CREDENTIALS` | `NODUS_API_KEY` absent | Set it for remote runs only |
| `NODUS_NOT_FOUND` | App/Function not deployed or wrong name | Verify `NODUS_APP_NAME`/`NODUS_FUNCTION_NAME` |
| `NODUS_PARALLEL_INVALID_TASK_RESULT` | Worker returned a malformed envelope | Check the deployed worker version matches the payload schema |
| `NODUS_PARALLEL_MISSING_TASK_RESULT` | Adapter returned no result for a task | Inspect provider logs; missing results are never retried |
| All tasks `timed_out` | Timeout too low / cold start | Raise `NODUS_PARALLEL_TASK_TIMEOUT_SECONDS` |
| `NODUS_PARALLEL_INVALID_TASK_PAYLOAD` | Non-serializable or ground-truth-bearing payload | Fix the request/candidate before submission |

## 11. Known limitations

- **Live remote execution unverified**: no credits/access, so remote
  concurrency, deployment import behavior, and provider metadata are pending.
- **No venue-matched speedup claim**: local-vs-remote timing mixes venues. A
  real parallel-versus-sequential provider comparison needs two billable remote
  runs and has not been performed.
- **Cancellation is best-effort**: the SDK exposes a cancel request but no
  confirmation; in-flight results are preserved and no remote stop is claimed.
- **Costs unavailable**: the verified SDK surface exposes no per-call charge, so
  cost fields remain explicitly unavailable.
- **Thread-based adapter concurrency**: the pinned SDK exposes blocking wrappers,
  so the batch path uses a bounded thread pool inside one client scope rather
  than an async gather. Concurrency is real at the HTTP level only if the
  provider accepts overlapping calls; that remains to be verified live.
