# Nodus Remote Readiness Checklist (Issue #13)

Status of every claim needed before parallel remote execution can be called
verified. Each item is labelled with the evidence that actually exists.

**Current state: offline-ready; live remote execution and remote benchmarking
are NOT verified.** Nodus credits and service access are unavailable. Nothing in
this repository contains remote task IDs, remote durations, costs, or measured
remote speedups.

Legend:

- **Local** — verified by executed offline tests in this repository.
- **SDK** — verified against the installed `nodus-compute==1.0.27` source and the
  Issue #12 adapter contract; not exercised against the live service.
- **Pending** — requires live credentials/credits/service access.
- **Limit** — known limitation, documented and accepted for now.

## 1. Locally verified

| # | Item | Evidence |
|---|---|---|
| 1 | Configuration structure and validation (`NODUS_PARALLEL_*`, bounds 1–32, 0–5) | `tests/test_nodus_parallel.py::TestConfiguration` |
| 2 | Explicit modes; `nodus_parallel` refuses a non-remote adapter | `TestRemoteOrchestration::test_requires_remote_adapter_and_never_falls_back` |
| 3 | Task payload construction, strict JSON, ground-truth rejection | `TestTaskContract` |
| 4 | Worker execution and result parsing (completed and scientifically failed fits) | `TestTaskContract::test_payload_round_trips_and_matches_local_pipeline`, `test_scientific_failure_is_a_valid_task_result` |
| 5 | Identity: every result maps to its task, hypothesis, and request | `TestRemoteOrchestration::test_results_correlate_with_tasks_and_carry_provider_ids` |
| 6 | Deterministic aggregation independent of completion order | `test_aggregation_order_is_independent_of_completion_order`, `test_repeated_runs_are_deterministic` |
| 7 | Concurrency limit enforcement, including limit 1 and 40-task batches | `TestRemoteOrchestration::test_concurrency_limit_is_enforced`, `test_concurrency_of_one_is_sequential`, `test_large_collection_respects_limit` |
| 8 | Failure isolation: successes preserved, failures/timeouts/missing/duplicate/unexpected results explicit | `TestFailureHandling` |
| 9 | Bounded retries; no retry for non-retryable, post-acceptance, or ambiguous failures | `TestFailureHandling` retry tests |
| 10 | Cancellation before dispatch and between retry rounds; no score for cancelled tasks | `TestLocalSequential` / `TestFailureHandling` cancel tests |
| 11 | Adapter batch path: one client scope, bounded threads, timeout cancellation request | `TestAdapterBatch` |
| 12 | Issue #12 adapter behaviour unchanged | `tests/test_nodus_adapter.py` (70 tests) |
| 13 | Issue #6 evaluation and Issue #8 discovery regressions | `tests/test_evaluation.py`, `tests/test_discovery_loop.py` |
| 14 | Discovery integration through `evaluate_many`; no duplicated oracle queries | `TestDiscoveryIntegration` |
| 15 | Failed tasks become `EvaluationResult(status=FAILED)` with no metrics | `test_failed_task_becomes_failed_evaluation_not_a_score` |
| 16 | Payloads contain no evaluator-only/ground-truth keys | `test_discovery_payloads_contain_no_evaluator_only_keys` |
| 17 | Smoke test and benchmark refuse mock/local modes and missing configuration | smoke-test runs below (exit code 2 / 1) |

Recorded commands and results are in the pull request, not asserted here.

## 2. Verified against the SDK contract (not live)

| # | Item | Source |
|---|---|---|
| 1 | `Function.from_name(app, name, project)` lookup | installed SDK, Issue #12 ADR-002 |
| 2 | `fn.spawn(**kwargs)` returns a `FunctionCall` handle | installed SDK |
| 3 | `handle.name` is the provider call identifier | installed SDK |
| 4 | `handle.get(timeout=...)` returns the function's value and raises `TimeoutError` on client-side timeout | installed SDK |
| 5 | `handle.cancel()` exists but returns no confirmation | installed SDK |
| 6 | Blocking wrappers mean concurrency requires threads | installed SDK (`nodus._sync`) |
| 7 | `NodusError.retryable` is the retry classification (`TooManyRequests`, `Unavailable`) | installed SDK, Issue #12 |
| 8 | One process-wide default client; one lock must cover the batch | installed SDK (`nodus.api._client._default`) |
| 9 | No cost field on the verified call surface | installed SDK |

## 3. Pending live verification

| # | Item | How it will be verified |
|---|---|---|
| 1 | API key accepted; project access correct | Stage 1 smoke task |
| 2 | Function deployed with `app.nodus_worker:evaluate` and importable in the remote image | Stage 1 |
| 3 | Payload compatibility (kwargs, JSON size, encoding) | Stage 1 |
| 4 | `evaluate_candidate` returns the expected envelope shape | Stage 1 |
| 5 | Provider task IDs and metadata appear per task | Stage 1/2 |
| 6 | Actual provider concurrency when several tasks overlap | Stage 2 |
| 7 | Correlation and aggregation under real out-of-order completion | Stage 2 |
| 8 | Behaviour on controlled failure/timeout (only where cost is understood) | Stage 3 |
| 9 | Remote durations, outcomes, and any provider cost metadata | Stage 4 |
| 10 | Venue-matched parallel-vs-sequential remote comparison | Stage 4 (two billable runs) |
| 11 | Whether timeout `cancel()` actually stops remote work | Stage 3 |
| 12 | Cold-start behaviour and timeout headroom | Stage 1 |

## 4. Known limitations

| # | Limitation |
|---|---|
| 1 | No live remote run has been performed, so no remote claim is made anywhere. |
| 2 | Local-vs-remote timing is not a speedup measurement; venues differ. |
| 3 | Remote cancellation is requested but never confirmed. |
| 4 | Cost is unavailable, not zero and not estimated. |
| 5 | Concurrency is thread-based because the SDK exposes blocking wrappers. |
| 6 | Only the polynomial family and MAE/RMSE metrics exist today (Issue #5/#6 scope). |

## 5. No-credit preflight

Run from `backend/`. All of these are offline and must pass before any billable
step:

```bash
python -m compileall app
python -m pytest tests/test_nodus_parallel.py -q
python -m pytest tests/test_nodus_adapter.py -q
python -m pytest tests/test_evaluation.py tests/test_discovery_loop.py -q
python -m pytest -q
python -m benchmarks.nodus_parallel_benchmark          # local correctness + local timing

# Safe failure without credentials/mode (expect non-zero exit, no provider call)
python -m app.nodus_parallel_smoke_test
NODUS_EXECUTION_MODE=mock NODUS_APP_NAME=a NODUS_FUNCTION_NAME=f \
  NODUS_PARALLEL_EXECUTION_MODE=nodus_parallel \
  python -m app.nodus_parallel_smoke_test              # expect exit 2
```

## 6. Staged live procedure

Do not start with a large or expensive benchmark. Record redacted output for
each stage; never paste keys, authorization headers, or full provider payloads.

### Stage 1 — one inexpensive remote evaluation

1. Deploy `app.nodus_worker:evaluate` as the configured App/Function.
2. Set `NODUS_EXECUTION_MODE=nodus_remote`, `NODUS_API_KEY`,
   `NODUS_APP_NAME`, `NODUS_FUNCTION_NAME`,
   `NODUS_PARALLEL_EXECUTION_MODE=nodus_parallel`.
3. Run `python -m app.nodus_parallel_smoke_test` (four tiny echo tasks, no
   evaluation compute).
4. Confirm: exit code 0, four distinct provider task IDs, each echo matches its
   own task, all results report `nodus_remote`.
5. Then run one real evaluation batch with `--tasks 1` through the benchmark in
   local mode for comparison, and one remote evaluation task through the runner
   (or a one-request script) to confirm the `evaluate_candidate` envelope.

### Stage 2 — small batch

1. Run `python -m app.nodus_parallel_smoke_test` with a small batch and note
   whether measured task durations overlap.
2. Run `python -m benchmarks.nodus_parallel_benchmark --remote --tasks 4`.
3. Confirm result equivalence against the local run, correlation under real
   ordering, and that `max_concurrency` was actually exercised.

### Stage 3 — controlled failures (only with understood cost)

1. Submit one task with an intentionally invalid payload; confirm a structured
   task failure with no score.
2. Submit one task with a very small timeout; confirm `timed_out`,
   `cancellation_requested: true`, `cancellation_confirmed: false`, and no
   automatic resubmission.
3. Check the provider console to see whether the cancelled call actually stopped
   (this is the only way to confirm cancellation).

### Stage 4 — benchmark

1. Run the same workload at concurrency 1 and concurrency N on the provider
   (two billable runs) to obtain a venue-matched comparison.
2. Record wall-clock, task outcomes, provider IDs/metadata, and any provider cost
   fields. Label estimates as estimates and unavailable values as unavailable.
3. Only then may a speedup claim be made, together with its conditions.

## 7. What offline checks do not prove

Offline tests do **not** validate: credential validity, project permissions,
Function registration, remote importability, provider concurrency, provider
task-ID semantics under load, real timeout/cancellation behaviour, or cost.
Passing them means the local orchestration and contracts are correct — nothing
more.
