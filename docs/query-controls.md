# Query Validation and Stopping Controls (Issue #11)

CLARITY's discovery engine enforces query safeguards before invoking the oracle. The implementation currently lives in `backend/app/domain/discovery_engine.py` and its run/accounting contracts in `backend/app/domain/discovery_contracts.py`.

## Query accounting policy

- `attempted_queries` counts each actual `oracle.query()` invocation, including retries and invocations that fail or return an invalid response.
- `successful_queries` counts validated successful oracle responses committed as observations.
- `failed_queries` counts oracle invocations that raise or return a failed/invalid response. A failed attempt followed by a successful retry increments both failed and successful counts.
- `rejected_queries` counts invalid candidate/planner selections rejected before an oracle invocation. They do not consume query budget.
- `duplicate_queries` counts candidate inputs skipped by the configured duplicate-input policy. They do not consume query budget.
- `retry_count` counts additional invocations started after an explicitly retryable transient exception.
- `in_flight_queries` counts invocations actively executing against the oracle.

The accounting invariant is `attempted_queries = successful_queries + failed_queries + in_flight_queries`. Atomically under `_query_lock`, `attempted_queries` reserves budget before the oracle call with `in_flight_queries = 1`. When the call completes, `in_flight_queries` resets to 0 and either `successful_queries` or `failed_queries` increments. Rejected and duplicate proposals are separate proposal diagnostics, not oracle invocations.

## Duplicate policy and validation

Every selected candidate passes the oracle's public `validate_input()` contract before `query()`. A validation error is recorded as a structured rejection. All errors from `validate_input()` and non-success `Observation` responses are strictly sanitized before being emitted to warnings or experiment history: only safe structured error information (codes and cause codes) is retained, while arbitrary adapter messages and details are discarded to guarantee evaluator and simulator isolation. Unless `allow_duplicate_queries=True`, an input within `query_exclusion_tolerance` of a committed input is skipped before invocation. The default duplicate policy is to skip duplicates.

## Retry policy

Retries default to disabled (`max_query_retries=0`); when enabled, the value must be between 0 and 5. Only `TimeoutError`, `ConnectionError`, or `OracleQueryError` values with the explicit codes `ORACLE_TIMEOUT`, `ORACLE_RATE_LIMITED`, `ORACLE_TEMPORARY_FAILURE`, or `ORACLE_UNAVAILABLE` are retryable. Validation failures, malformed oracle responses, generic exceptions, and other oracle error codes are not retried.

Each retry reserves another unit of query budget before calling the oracle. A retry is not attempted when the budget is exhausted. The engine never retries indefinitely. Previously committed observations are retained if a later invocation fails.

## Concurrency and lifecycle

The engine's lifecycle start is protected by a lock, preventing the same engine instance from being started twice concurrently. Query-budget reservations are also protected by a lock so concurrent reservations cannot increment attempted-query accounting beyond the configured ceiling.

**Sequential execution per engine instance.** The current discovery loop submits at most one oracle experiment at a time. The duplicate-input check reads `_observations`, which is only mutated after a successful, serialized oracle response. This means the duplicate check is safe and correct for a single engine instance. Shared budgets or deduplication across multiple engine instances or remote workers require the per-run budget manager in Issue #14 and are not protected by the current `_query_lock`.

**Cancellation (deferred scope).** Cancellation is not currently exposed as a core `DiscoveryEngine` operation. A future orchestration or API cancellation path should map the cancellation signal to an explicit terminal reason (e.g., `USER_CANCELLED`) and call the finalization path rather than interrupting the run mid-iteration. Until that support is added, a run that is externally interrupted has no graceful terminal state and will not appear as `RUNNING` only if the engine process itself is killed.

## Stopping conditions and precedence

The engine terminates with explicit status and reason according to deterministic precedence:
1. **Configured scientific stopping criteria**: A satisfied stopping criterion on a fresh ranking (e.g. target plausible count or metric threshold) takes highest precedence (`TERMINATED_STOPPING_CRITERION`).
2. **Exhausted query budget**: When `attempted_queries >= max_query_budget`, the run terminates as `TERMINATED_BUDGET_EXHAUSTED` (`BUDGET_EXHAUSTED`). This takes precedence over consecutive oracle failures when the final permitted query fails or retries consume the remaining budget.
3. **Maximum iterations**: When `iterations_completed >= max_iterations`, the run terminates as `TERMINATED_MAX_ITERATIONS` (`MAX_ITERATIONS_REACHED`).
4. **No eligible experiments**: When candidate pools are exhausted or no informative candidate remains (`TERMINATED_NO_EXPERIMENTS`).
5. **Repeated oracle failures**: Consecutive query failures exceeding `consecutive_failure_limit` when budget remains terminate as `FAILED` (`CONSECUTIVE_ORACLE_FAILURES`).
6. **Component and unrecoverable failures**: Contract violations and unexpected crashes finalize immediately as `FAILED` (`COMPONENT_FAILURE` or `UNRECOVERABLE_ERROR`).

Scientific success uses only discovery-visible observations and configured ranking criteria; evaluator-only ground truth is not available to the engine.

## Verification

Run:

```bash
cd backend
python -m pytest tests/test_query_controls.py -q
python -m pytest tests/test_discovery_loop.py -q
```
