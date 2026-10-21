# Query Validation and Stopping Controls (Issue #11)

CLARITY's discovery engine enforces query safeguards before invoking the oracle. The implementation currently lives in `backend/app/domain/discovery_engine.py` and its run/accounting contracts in `backend/app/domain/discovery_contracts.py`.

## Query accounting policy

- `attempted_queries` counts each actual `oracle.query()` invocation, including retries and invocations that fail or return an invalid response.
- `successful_queries` counts validated successful oracle responses committed as observations.
- `failed_queries` counts oracle invocations that raise or return a failed/invalid response. A failed attempt followed by a successful retry increments both failed and successful counts.
- `rejected_queries` counts invalid candidate/planner selections rejected before an oracle invocation. They do not consume query budget.
- `duplicate_queries` counts candidate inputs skipped by the configured duplicate-input policy. They do not consume query budget.
- `retry_count` counts additional invocations started after an explicitly retryable transient exception.

The accounting invariant is `attempted_queries = successful_queries + failed_queries`. Rejected and duplicate proposals are separate proposal diagnostics, not oracle invocations.

## Duplicate policy and validation

Every selected candidate passes the oracle's public `validate_input()` contract before `query()`. A validation error is recorded as a structured rejection. Unless `allow_duplicate_queries=True`, an input within `query_exclusion_tolerance` of a committed input is skipped before invocation. The default duplicate policy is to skip duplicates.

## Retry policy

Retries default to disabled (`max_query_retries=0`); when enabled, the value must be between 0 and 5. Only `TimeoutError`, `ConnectionError`, or `OracleQueryError` values with the explicit codes `ORACLE_TIMEOUT`, `ORACLE_RATE_LIMITED`, `ORACLE_TEMPORARY_FAILURE`, or `ORACLE_UNAVAILABLE` are retryable. Validation failures, malformed oracle responses, generic exceptions, and other oracle error codes are not retried.

Each retry reserves another unit of query budget before calling the oracle. A retry is not attempted when the budget is exhausted. The engine never retries indefinitely. Previously committed observations are retained if a later invocation fails.

## Concurrency and lifecycle

The engine's lifecycle start is protected by a lock, preventing the same engine instance from being started twice concurrently. Query-budget reservations are also protected by a lock so concurrent reservations cannot increment attempted-query accounting beyond the configured ceiling. The current discovery loop itself submits at most one selected experiment at a time; shared budgets across multiple engine instances or remote workers must be coordinated by the per-run budget manager in Issue #14.

Cancellation is not currently exposed as a core `DiscoveryEngine` operation. A future orchestration/API cancellation path should map to an explicit terminal state/reason rather than pretending the run completed normally.

## Stopping conditions

The engine terminates with explicit status and reason for configured scientific stopping criteria, exhausted query budget, maximum iterations, no eligible experiment remaining, repeated oracle failures, and terminal component errors. Scientific success uses only discovery-visible observations and configured ranking criteria; evaluator-only ground truth is not available to the engine.

## Verification

Run:

```bash
cd backend
python -m pytest tests/test_query_controls.py -q
python -m pytest tests/test_discovery_loop.py -q
```
