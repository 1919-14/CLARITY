# Auditable Researcher History & Evaluation Provenance (Issue #17)

This document specifies how CLARITY records researcher and automated actions, preserves
every hypothesis definition that was ever evaluated, and ties each evaluation to the
exact scientific inputs it used.

It extends Issue #15's append-only audit trail and Issue #16's hypothesis management.
There is **no second audit pipeline**: history is a read model over `audit_events`,
`hypothesis_revisions`, and `evaluation_results`.

---

## 1. Core invariants

1. **Audit events are append-only.** Normal application code cannot update or delete an
   audit event. Corrections append a new `AUDIT_EVENT_CORRECTED` record that references
   the superseded event through `corrects_event_id`; the original is untouched.
2. **Hypothesis revisions are immutable.** Editing a hypothesis appends revision *n+1*
   and links it to revision *n*. The earlier definition (formula, parameters,
   complexity) remains exactly as recorded.
3. **Lifecycle state is not scientific evidence.** `is_pinned` / `is_excluded` are
   researcher preferences. They are audited with before/after values, but they never
   create a revision, never alter metrics, and never change rankings.
4. **Every evaluation names its inputs.** An evaluation records both the `revision_id`
   it evaluated and a `dataset_snapshot_id` fingerprint of the *content* of the exact
   observations used, plus the canonical manifest it was computed from.
5. **Failed work stays distinguishable from successful work.** A failed evaluation keeps
   its `failed` status and error message; missing metrics stay empty rather than being
   converted to zeros.
6. **State changes and their events commit together.** A revision is never committed
   without its audit event, and an event is never committed for a mutation that failed.

### What these guarantees are *not*

- They are not cryptographic tamper-proofing. There is no hash chain or signed ledger.
- Append-only is enforced in the ORM/session layer and by the absence of update/delete
  service operations. Raw SQL executed outside the ORM is not intercepted.
- There is no database permission grant restricting `UPDATE`/`DELETE` on `audit_events`.

---

## 2. Event model

Table: `audit_events` (append-only). Serialized field names in API responses are shown in
parentheses where they differ from the column name.

| Column | API field | Meaning |
|---|---|---|
| `event_id` | `event_id` | Stable identifier (`evt-…`). Never reused for different content. |
| `run_id` | `run_id` | Discovery run the action belongs to. Required. |
| `event_type` | `event_type` | Stable action name (see §3). |
| `actor` | `actor` | Actor category: `researcher`, `automated_agent`, `system`, `worker`, `lifecycle_manager`, `system_recovery`. |
| `actor_id` | `actor_id` | Authenticated identity when available (nullable). |
| `hypothesis_id` | `hypothesis_id` | Associated hypothesis (nullable). |
| `revision_id` | `revision_id` | Associated hypothesis revision (nullable). |
| `correlation_id` | `correlation_id` | Request/correlation identifier supplied by the caller (`X-Request-ID` / `X-Correlation-ID`). |
| `before_json` | `before` | State snapshot before the action, or `null` when the action has no relevant "before". |
| `after_json` | `after` | State snapshot after the action, or `null`. |
| `reason` | `reason` | Optional researcher-supplied explanation. |
| `corrects_event_id` | `corrects_event_id` | Earlier event superseded by this correction. |
| `metadata_json` | `metadata` | Additional structured, sanitized context (`{}` when none). |
| `timestamp` | `timestamp` | UTC timestamp; serialized with an explicit `+00:00` offset. |

### Presence semantics

Response payloads preserve the difference between:

- **omitted / not applicable** — the key is present with `null` (`before: null`),
- **explicitly empty object** — `{}` (an action whose state snapshot exists but has no fields),
- **explicitly empty list** — `[]` (e.g. `revisions: []` for a hypothesis with no history),
- **explicit zero** — `0` (a measured count, never used for missing data).

### Actors

- `researcher` — a request from the researcher-facing API.
- `automated_agent` — reserved for automated agents acting outside the deterministic loop.
- `worker` — background evaluation consumer (`HYPOTHESIS_REEVALUATION_CONSUMED`).
- `system` / `lifecycle_manager` / `system_recovery` — run lifecycle and recovery.

`actor_id` is populated from a trusted gateway header (`X-Actor-ID`). CLARITY has no
authentication layer yet, so this value is an audit attribute only and is never used to
make an authorization decision.

### Sanitization

`metadata`, `before` and `after` pass through `sanitize_metadata`, which redacts
passwords, tokens, bearer credentials, API keys and connection strings recursively.
Free-text `reason` (and a revision's `change_reason`) pass through
`sanitize_reason_text`, so a credential pasted into an explanation is redacted while the
wording is kept. Secrets are never stored in an event, and whole ORM objects are never
serialized into a payload.

---

## 3. Event types

Event names introduced or reused by Issue #17 (Issue #15 names are unchanged):

| Event type | Actor | Meaning | `before` → `after` |
|---|---|---|---|
| `HYPOTHESIS_PROPOSED` | researcher / automated_agent | A candidate hypothesis and its initial revision were created. | `null` → revision snapshot |
| `HYPOTHESIS_REVISED` | researcher | A new revision was appended (edit). | previous revision snapshot → new revision snapshot |
| `HYPOTHESIS_PINNED` | researcher | Display preference set. | `{is_pinned: false}` → `{is_pinned: true}` |
| `HYPOTHESIS_UNPINNED` | researcher | Display preference cleared. | `{is_pinned: true}` → `{is_pinned: false}` |
| `HYPOTHESIS_EXCLUDED` | researcher | Removed from active candidate selection. | `{is_excluded: false}` → `{is_excluded: true}` |
| `HYPOTHESIS_RESTORED` | researcher | Returned to active selection. | `{is_excluded: true}` → `{is_excluded: false}` |
| `HYPOTHESIS_REEVALUATE_REQUESTED` | researcher | Re-evaluation enqueued for the latest revision. | `null` → `{evaluation_id, status: "pending"}` |
| `HYPOTHESIS_REEVALUATED` | researcher | Re-evaluation completed synchronously. | `null` → `{status, sample_count, dataset_snapshot_id, metrics}` |
| `HYPOTHESIS_REEVALUATION_CONSUMED` | worker | Background worker completed an enqueued evaluation. | `null` → same as above |
| `AUDIT_EVENT_CORRECTED` | researcher | A correction was appended for an earlier event. | `{event_id, event_type}` → `{event_id, corrected: true}` |
| `RUN_CREATED` / `RUN_STARTED` / `RUN_CANCELLED` / `RUN_COMPLETED` / `RUN_FAILED` / `RUN_STOPPED_BY_BUDGET` | researcher / lifecycle_manager / system_recovery | Run lifecycle transitions. | recorded per transition |
| `BUDGET_CONFIGURED` / `BUDGET_EXHAUSTED` | researcher / system | Budget configuration and exhaustion. | recorded per transition |
| `REPORT_GENERATED` / `REPORT_EXPORTED` | researcher | Reporting actions, when a reporting service records them. | recorded by caller |

Issue #15's task/oracle event types (`TASK_*`, `ORACLE_QUERY_EXECUTED`,
`OBSERVATION_RECORDED`) remain available and queryable through the same history endpoint.

---

## 4. Hypothesis revisions

Table: `hypothesis_revisions` (immutable).

| Column | Meaning |
|---|---|
| `revision_id` | Stable identifier (`rev-…`). |
| `hypothesis_id` | Parent hypothesis. |
| `revision_number` | Monotonic, per-hypothesis, starts at 1. |
| `formula` | Validated expression exactly as recorded. |
| `parameters` | Canonical parameter/coefficient mapping (family, degree, coefficients, variables). |
| `complexity` | Recorded complexity at that revision. |
| `creation_source` | `researcher`, `generator`, `refit`, `restoration`. |
| `previous_revision_id` | Reference to the preceding revision, `null` for revision 1. |
| `change_reason` | Optional explanation supplied by the researcher. |
| `actor` / `actor_id` | Who produced the revision. |
| `request_key` | Client idempotency key, unique per hypothesis. |
| `request_fingerprint` | Canonical fingerprint (`req-…`) of formula, parameters, complexity, model family and creation source of the request that created the revision. |
| `created_at` | UTC creation timestamp. |

### Numbering and integrity

- `UNIQUE (hypothesis_id, revision_number)` — two writers cannot commit the same number.
- `UNIQUE (hypothesis_id, request_key)` — a replayed edit cannot create a second revision.
- `previous_revision_id` is an **application-maintained** reference. It is deliberately
  not a self-referential foreign key, because SQLite cannot drop a self-referencing table
  during FK-enforced test teardown. The chain is written only by
  `revise_hypothesis_service`, together with the unique constraints above.

### Editing rules

- Omitted fields inherit the current revision: `parameters`-only edits validate against the
  stored formula; `formula`-only edits derive canonical coefficients from the new
  expression instead of inheriting stale ones.
- Validation happens **before** any write, so a rejected edit leaves no revision and no event.
- An edit that would not change the definition (same formula, parameters and complexity) is
  rejected with `422` rather than producing a meaningless revision.
- Optimistic concurrency: pass `expected_revision` or `expected_version_token`; a mismatch
  returns `409`.
- A lost database race (duplicate revision number or request key) rolls the transaction back
  and returns `409`; no partial revision survives.
- A replayed idempotency key is compared on `request_fingerprint`, not only on
  formula/parameters/complexity: the same formula with a different `model_family` is a
  conflicting request (`409`), not a replay. `reason`, `actor` and `actor_id` are metadata
  and may differ on a retry.

### Lifecycle actions do not create revisions

Pin, unpin, exclude and restore only flip flags. They record events with before/after values
and increment the hypothesis's `version`/`version_token`, but they do not append a revision
and do not change any stored evaluation.

---

## 5. Scientific revisions vs evaluation snapshots vs lifecycle state

| Concept | Stored in | Changes when | Historical? |
|---|---|---|---|
| Scientific definition | `hypothesis_revisions` | A new edit is committed | Immutable; every revision retained |
| Evaluation snapshot | `evaluation_results` | Each evaluation/re-evaluation | Append-only; earlier rows never updated |
| Lifecycle / presentation | `hypotheses.is_pinned`, `hypotheses.is_excluded`, `version` | Researcher flag toggle | Transitions recorded as audit events |

A researcher pinning a hypothesis therefore cannot make it look scientifically better: the
metrics, ranking inputs and evaluation rows are untouched.

---

## 6. Evaluation provenance

Table: `evaluation_results`.

| Column | Meaning |
|---|---|
| `evaluation_id` | Stable identifier (`eva-…`). |
| `revision_id` | The exact hypothesis revision evaluated (foreign key, `ON DELETE RESTRICT`). |
| `run_id` | Owning run; cross-run evaluation is rejected by the repository. |
| `status` | `pending`, `completed`, `failed`. |
| `dataset_snapshot_id` | Content-addressed fingerprint of the exact observations supplied to the pipeline. |
| `dataset_snapshot` | Canonical manifest the fingerprint was computed from (observation ids, inputs, outputs, status, partition, sequence). JSON column; not exposed by the API. |
| `dataset_version` | Number of training observations at evaluation time (retained for compatibility). |
| `sample_count` | Number of samples actually used by the pipeline. |
| `metrics` / `held_out_metrics` | Structured metrics; `{}` / `null` when unavailable. |
| `error_message` | Structured failure text for failed evaluations. |
| `evaluated_at` | UTC timestamp. |

### `dataset_snapshot_id`

Computed by `app.domain.history_contracts.build_dataset_snapshot(run_id, observations)`
(exposed as `compute_dataset_snapshot_id`):

```
dset-<first 32 hex chars of sha256(json({schema_version, run_id,
       observations: [canonical observation records sorted by observation_id]}))>
```

Each canonical record contains `observation_id`, `input_id`, `input_values`,
`output_values`, `status`, `sequence_number` and `is_held_out`. Bare observation ids are
rejected: a hash of identifiers cannot notice that the evaluated data changed.

Properties:

- **Content, not count or identity alone.** Two observation sets of the same size produce
  different ids, and changing any scientific field of an observation while keeping its id
  also changes the id.
- **Reconstructable.** The same canonical manifest is persisted in
  `evaluation_results.dataset_snapshot`, so the exact inputs behind a snapshot id can be
  re-read and re-fingerprinted later instead of being inferred.
- **Set-stable.** Reordering the same observations yields the same id; duplicate ids with
  identical content collapse, while two different records sharing an id raise an error
  (`ValueError`) rather than hashing an ambiguous dataset.
- **Run-scoped.** Ids from another run cannot collide.
- **A fingerprint, not a signature.** It proves content equality against the same inputs; it
  is not a tamper-proof commitment, and observation rows themselves are not made immutable
  by this column.

`dataset_version` and `sample_count` remain descriptive only. A count alone is never treated
as dataset identity.

### Pending evaluations

An enqueued evaluation is written with `status = "pending"`,
`dataset_snapshot_id = null` and `dataset_snapshot = null`: it has not consumed any inputs yet. The worker that
consumes it (`consume_pending_evaluation`) stamps the revision and the snapshot at
execution time, so provenance always reflects the inputs actually used rather than the
inputs that existed when the request was queued.

### Failure semantics

A failed evaluation keeps `status = "failed"`, its `error_message`, and the empty metrics the
pipeline produced. It remains distinguishable from a successful evaluation whose MAE is
legitimately 0.0, and it keeps its own `dataset_snapshot_id` and manifest so the attempted inputs are
still traceable.

### Re-evaluation

Re-evaluation always appends a new `evaluation_results` row referencing the same revision and
the current dataset snapshot. Earlier rows keep their own snapshot and metrics, so the
metric history of a revision reads as a timeline rather than a single overwritten value.

---

## 7. Transactional consistency

`revise_hypothesis_service` performs, in the caller's transaction:

1. Validate the requested change.
2. Check `expected_revision` / `expected_version_token`.
3. Insert the new revision.
4. Update the hypothesis's current revision pointer, version and version token.
5. Append the `HYPOTHESIS_REVISED` audit event.
6. Flush; the route's `session_scope` commits.

A failure at any step rolls back all of it: there is no path that commits a revision without
its event, or advances the pointer without a revision. If the database rejects the write
because another writer won the race, the transaction is rolled back and a conflict is raised.

Run cancellation and failure never delete history: revisions, evaluations and audit events
recorded before the terminal transition remain queryable afterwards.

**Not guaranteed:** cross-service distributed atomicity. There is no outbox. Any future
external event delivery must add one rather than writing to a secondary store outside the
transaction.

---

## 8. Idempotency

| Operation | Mechanism | Behaviour on replay |
|---|---|---|
| Hypothesis edit (`POST /hypotheses/{id}/revisions`) | `Idempotency-Key` header or `idempotency_key` body field, stored in `hypothesis_revisions.request_key` with `UNIQUE (hypothesis_id, request_key)`; payload compared through `hypothesis_revisions.request_fingerprint` | Same payload → the committed revision is returned with `200` and `idempotent_replay: true`; different payload (including a different `model_family`) → `409` |
| Hypothesis creation (`POST /runs/{id}/hypotheses`) | Caller-supplied `hypothesis_id` plus the Issue #14 budget reservation's `operation_id` | Duplicate id → `409`; replayed admission → `409` "already admitted" |
| Run creation (`POST /runs`) | Caller-supplied `run_id` with primary-key uniqueness | Duplicate id → `409` |
| Audit event append | Caller-supplied `event_id` with content comparison | Identical content → existing event returned; conflicting content → `409` |

A request key is only consumed by a committed revision: validation failures, conflicts and
rolled-back transactions leave it free for a retry.

**Limitations.** Idempotency is scoped per hypothesis key, not per actor. There is no global
exactly-once guarantee, no idempotency window/expiry, and evaluations (as opposed to
requests) are intentionally not deduplicated — re-evaluation always appends a new record.

---

## 9. History API

All endpoints live under `/api/v1`.

### 9.1 Run action history

```http
GET /api/v1/runs/{run_id}/history
```

Query parameters:

| Name | Type | Default | Description |
|---|---|---|---|
| `event_type` | string | – | Exact event-type filter. |
| `actor` | string | – | Actor category filter. |
| `hypothesis_id` | string | – | Only events for one hypothesis. |
| `revision_id` | string | – | Only events for one revision. |
| `limit` | int (1–200) | 50 | Page size. |
| `cursor` | string | – | Opaque cursor from `pagination.next_cursor`. |
| `offset` | int ≥ 0 | 0 | Offset pagination for compatibility. Ignored when `cursor` is supplied. |

Ordering is `(timestamp, event_id)` ascending — the event ID is the deterministic tie-breaker
for equal timestamps. Cursor pagination compares strictly after the last `(timestamp, event_id)`
pair, so pages stay stable while new events are appended; offset pagination cannot make that
guarantee and is retained only for API compatibility.

Response:

```json
{
  "run_id": "run-4c1b0e",
  "events": [
    {
      "event_id": "evt-7a91fc82d41b",
      "run_id": "run-4c1b0e",
      "event_type": "HYPOTHESIS_REVISED",
      "actor": "researcher",
      "actor_id": null,
      "hypothesis_id": "hyp-001",
      "revision_id": "rev-b2",
      "correlation_id": "req-9f2c",
      "before": {"revision_id": "rev-a1", "revision_number": 1, "formula": "y = a*x + b", "parameters": {"coefficients": {"c0": 1.0, "c1": 2.0}}, "complexity": 2},
      "after": {"revision_id": "rev-b2", "revision_number": 2, "formula": "y = a*x^2 + b", "parameters": {"coefficients": {"c0": 1.0, "c1": 0.0, "c2": 2.0}}, "complexity": 3},
      "reason": "residual structure suggested curvature",
      "corrects_event_id": null,
      "metadata": {"hypothesis_id": "hyp-001", "revision_number": 2, "previous_revision_number": 1},
      "timestamp": "2026-10-23T12:00:05.123456+00:00"
    }
  ],
  "filters": {"event_type": null, "actor": null, "hypothesis_id": null, "revision_id": null},
  "pagination": {"count": 1, "total": 42, "limit": 50, "offset": 0, "next_cursor": null, "has_more": false}
}
```

### 9.2 Hypothesis revision history

```http
GET /api/v1/hypotheses/{hypothesis_id}/revisions
```

Returns every revision in ascending `revision_number` with `previous_revision_id`,
`change_reason`, `actor`, `actor_id` and `created_at`. A hypothesis with no revisions returns
`revisions: []`; an unknown hypothesis returns `404`.

### 9.3 Evaluation history for one revision

```http
GET /api/v1/hypotheses/{hypothesis_id}/revisions/{revision_id}/evaluations?limit=50&offset=0
```

Oldest-first, each record carrying `revision_id`, `dataset_snapshot_id`, `dataset_version`,
`sample_count`, `status`, `metrics` and `error_message`. A revision that does not belong to
the hypothesis is reported as `404` rather than leaking another candidate's evaluation.

### 9.4 Appending a revision

```http
POST /api/v1/hypotheses/{hypothesis_id}/revisions
Content-Type: application/json
Idempotency-Key: 0f8c2b1e-…

{
  "formula": "y = 3*x^2 + 2",
  "reason": "quadratic mechanism fits the residual",
  "expected_revision": 1
}
```

Status codes: `201` created, `200` idempotent replay, `400` malformed body, `404` unknown
hypothesis, `409` stale revision / reused idempotency key / lost race, `422` invalid
expression or no-op edit.

### 9.5 Compatibility

Issue #15's `GET /api/v1/runs/{run_id}/audit-events` and
`GET /api/v1/hypotheses/{hypothesis_id}/evaluations` remain unchanged. `/history` is the same
append-only store with structured provenance, filtering and cursor pagination.

### 9.6 Authorization and scoping

CLARITY currently has **no authentication or per-researcher ownership model**. History is
scoped by `run_id` in the URL, and evaluation history is scoped by hypothesis and revision, so
an identifier from another run cannot be used to read that run's data through these routes
(`404`, not a partial result). `actor_id` is recorded but is not an access-control subject.
When authentication lands, these routes are the boundary where ownership checks must be added.

Responses never include credentials, connection strings, oracle ground truth, held-out
oracle internals, or unvalidated provider metadata.

---

## 10. Known limitations

1. **No cryptographic immutability.** Append-only is enforced at the ORM/session layer; direct
   SQL can still modify rows. No hash chain, signature or WORM storage.
2. **No `previous_revision_id` foreign key.** The chain is application-maintained (see §4)
   because a self-referential FK breaks SQLite test teardown.
3. **No authentication.** `actor_id` comes from a trusted header and is not verified.
4. **Budget re-configuration is not yet exposed.** `RUN_CONFIGURED` /
   `BUDGET_CONFIGURED` events exist for configuration performed at creation time; there is no
   endpoint to change a run's budget afterwards, so no audit record of that action can occur
   yet.
5. **Report generation/export events** are recorded only where a reporting service emits them;
   the reports feature is delivered separately (Issue #23), so generation is not yet audited
   by this issue.
6. **Automated discovery-loop candidates are not persisted as hypothesis rows** by the current
   engine: it keeps its candidates in memory and the oracle/evaluation path does not write
   `hypotheses` records. Consequently only proposals that go through
   `propose_hypothesis_service` (all researcher proposals today, and any automated producer
   that adopts the service) appear in revision history. Wiring engine candidates into the
   same service is a prerequisite for auditing automated hypotheses; nothing here simulates
   that.
7. **No outbox.** Events are written inside the same database transaction. Any future external
   delivery must add an outbox rather than a second, non-transactional write.
8. **Snapshot ids are fingerprints**, not proofs: they establish content equality for the
   same run and inputs, not authorship or tamper resistance. Observation rows remain
   mutable at the ORM layer; the content-addressed snapshot is what makes a mutation
   visible instead of silent.
9. **Offset pagination is unstable under concurrent appends** by design; use `cursor`.
10. **A lost write race rolls back the caller's transaction.** `revise_hypothesis_service`
    rolls back on a database conflict before raising, so compose it in its own transaction
    (as the API routes do) rather than inside a larger unit of work.

---

## 11. Tests

`backend/tests/test_researcher_history.py` covers:

- Initial revision creation; edit appends while preserving the previous definition; invalid
  and no-op edits write nothing; stale expected revision; duplicate revision number rejection.
- Revision and dataset-snapshot references on evaluations; snapshot stability and change;
  snapshot change when observation *content* changes under an unchanged id; reconstructable
  manifests; rejection of id-only snapshot input; append-only evaluation history;
  failed-evaluation fidelity; edits not rewriting earlier evaluations.
- Pin/unpin/exclude/restore events with before/after and no scientific change.
- Stable event ids, aware UTC timestamps, actor identity, sanitized payloads, append-only
  enforcement, correction linkage, duplicate-id conflict.
- Deterministic ordering, cursor pagination coverage, offset totals, filters, empty vs
  missing resources, malformed cursors.
- Transaction rollback when the audit write fails; safe retry afterwards; revision and event
  committing together.
- Idempotent replay, conflicting key reuse (including a model-family-only difference),
  canonical full-request comparison, and a validation failure not consuming the key.
- Cancellation and run failure preserving revisions, evaluations and earlier events.
- API contract: create revision, revision history, evaluation history, cursor pagination,
  filters, `404`/`400`/`409`/`422` behaviour.

Run it with:

```bash
cd backend && python -m pytest tests/test_researcher_history.py -q
```
