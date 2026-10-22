# LLM Scientist (Issue #41)

The LLM Scientist is an **optional, bounded proposal source** inside CLARITY.
It suggests candidate hypotheses and follow-up explanations from permitted
observations and evidence. CLARITY itself validates, fits, evaluates, ranks,
selects experiments, queries the oracle, accounts for budget, and decides
whether to stop.

> **The LLM proposes. CLARITY validates and measures.**

The component is disabled by default. Deterministic discovery requires no
provider credentials, no network access, and no provider package at import time.

## 1. Responsibilities and boundaries

| The LLM Scientist owns | The LLM Scientist does **not** own |
| --- | --- |
| Proposing candidate model structures | Deciding that a hypothesis is correct |
| Explaining why a structure may fit the evidence | Fitting parameters or computing metrics |
| Listing assumptions and competing explanations | Ranking, evidence classification, or acceptance |
| Suggesting advisory experiment inputs | Choosing experiments, querying the oracle, or spending budget |
| Producing a structured, auditable proposal round | Writing committed observations, counters, or run status |

A self-reported confidence is stored as **uncalibrated** metadata. It never
influences ranking, selection, or stopping, and it is never presented as
statistical uncertainty.

## 2. Architecture

```text
DiscoveryEngine
   |  permitted observations, candidates, evaluations, ranking, domain
   v
LLMScientist.build_request  -> LLMProposalRequest (bounded, leakage-checked)
   |
   v
LLMProposalProvider (Protocol)
   |  GroqProposalProvider (runtime)  |  fake provider (tests / benchmark)
   v
ProviderCallResult (structured status, bounded retries)
   |
   v
local JSON schema + domain + safety validation
   |
   v
canonicalization + deduplication
   |
   v
HypothesisRepresentation (existing Issue #5 contract)
   |
   v
existing fitting / evaluation / ranking / evidence pipeline
   |
   v
next proposal round (newly committed observations included)
```

Advisory experiment inputs follow a separate path:

```text
validated suggested inputs
   -> CandidateExperimentGenerator (same bounds/exclusion/dedup checks)
   -> ExperimentPlanner (Issue #10 policy)
   -> oracle input validation -> duplicate guard -> query budget -> oracle
```

The scientist never receives an oracle instance and never executes a query.

## 3. Contracts

All contracts live in `backend/app/domain/llm_scientist_contracts.py`.

### 3.1 Configuration (`LLMScientistConfig`)

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `False` | Master switch; deterministic-only when false. |
| `provider` | `groq` | Runtime provider identifier. |
| `model` | `llama-3.3-70b-versatile` | Provider model identifier, recorded as provenance. |
| `credential_env_var` | `GROQ_API_KEY` | **Name** of the credential variable; the value is never stored. |
| `max_proposals_per_round` | `3` | Hard ceiling per round (`1..10`). |
| `timeout_seconds` | `20.0` | Per-request timeout (`0 < x <= 120`). |
| `max_retries` | `1` | Bounded retries for transient failures only (`0..3`). |
| `max_tokens` | `1024` | Response token bound requested from the provider. |
| `max_response_characters` | `20000` | Local response-size bound checked before parsing. |
| `max_context_observations` | `20` | Most recent committed observations sent. |
| `max_context_hypotheses` | `8` | Ranked/selected candidate summaries sent. |
| `max_rounds` | `3` | Proposal rounds per run (`1..20`). |
| `temperature` | `0.2` | Sampling temperature (provenance only; output is not reproducible). |
| `fallback_policy` | `continue_deterministic` | Explicit behavior when a round yields nothing usable. |

`LLMScientistConfig.from_environment()` is the single source of truth for
environment variables and raises `ValueError` on invalid values.
`Config.llm_scientist_config()` exposes the same object to application wiring.

### 3.2 Proposal request (`LLMProposalRequest`)

Frozen, JSON-serializable, and leakage-checked on construction. It contains only:

- request/round identifiers, prompt and schema versions;
- supported domain, variable names, numeric bounds;
- supported model family and maximum polynomial degree;
- `max_proposals` and the canonical keys of existing candidates;
- bounded committed observations: successful, **non-held-out** rows only, with
  `sequence_number` and just the declared numeric input/output task fields
  (`x`/`y`). Reserved held-out rows and any extra payload keys never cross the
  provider boundary;
- bounded candidate summaries (formula, parameters, complexity, evaluation
  metrics/status, ranking status/scores, evidence counts);
- a ranking summary;
- prior round outcomes (status/counts/rejection codes);
- structured failure summaries.

It never contains oracle internals, evaluator labels, hidden rules, benchmark
answers, credentials, prompts from previous rounds, or raw discovery objects.

### 3.3 Provider protocol

```python
class LLMProposalProvider(Protocol):
    provider_id: str
    model_id: str
    is_live: bool

    def propose(self, request: LLMProposalRequest) -> ProviderCallResult: ...
```

`ProviderCallResult` always carries a structured `ProviderOutcomeStatus`,
attempt count, duration, and — only when the provider genuinely returned it —
model and token usage. Provider implementations must not raise across this
boundary; the scientist also converts any escaped exception into
`PROVIDER_ERROR` without copying its message.

### 3.4 Proposal schema

The provider must return one JSON object:

```json
{
  "hypotheses": [
    {
      "model_family": "polynomial",
      "degree": 2,
      "coefficients": [1.0, -0.5, 0.25],
      "rationale": "Measured facts separated from conjectures.",
      "assumptions": ["..."],
      "competing_explanations": ["..."],
      "distinguishing_evidence": ["..."],
      "suggested_experiment_inputs": [0.5],
      "self_reported_confidence": 0.4
    }
  ]
}
```

- `model_family`, `degree`, `coefficients`, and `rationale` are required.
- Coefficients are ascending powers and must number `degree + 1`.
- Optional lists are bounded to eight short strings each; optional confidence is
  `[0, 1]` and recorded with `confidence_is_calibrated: false`.
- Unknown fields are rejected. Fields implying code execution or tool calls
  (`code`, `python`, `exec`, `eval`, `expression`, `script`, `shell`, `command`,
  `tool`, `tool_call`, `import`) are rejected with `unsafe_field`.
- No proposal text is ever executed, imported, or evaluated.

### 3.5 Round record (`ScientistRoundRecord`)

Recorded for **every** round, including failures and empty rounds:
`round_id`, `round_index`, `run_id`, `provider_id`, `model_id`,
`prompt_version`, `schema_version`, `status`, timestamps, measured duration,
proposal/accepted/rejected counts, accepted hypothesis IDs, structured
rejections, provider outcome, structured error, attempts, real usage (when
reported), `is_live_provider`, `fallback_applied`, `fallback_reason`, and an
evidence summary. It is leakage-checked on construction and on serialization.

## 4. Supported hypothesis space

- One numeric input variable and one numeric output variable (the existing
  engine domain).
- The registered polynomial family only, degrees `0..max_degree` where
  `max_degree` is the run's `hypothesis_config.max_degree`.
- Leading coefficient must be non-zero for `degree >= 1` (family contract).
- Unsupported families, out-of-range degrees, and non-finite coefficients are
  rejected with structured diagnostics instead of being approximated.

## 5. Validation and deduplication

Every proposal passes, in order:

1. response size bound (before parsing);
2. strict JSON parsing (`malformed_response`);
3. top-level schema (`hypotheses` list);
4. proposal count bound (`too_many_proposals` rejects the whole round);
5. unsafe/unknown field rejection;
6. required field and type checks;
7. supported family, degree range, coefficient count, finiteness;
8. suggestion bounds (`out_of_domain`);
9. constructor validation through the registered model family;
10. canonical-key deduplication against existing candidates and earlier
    proposals in the same round (`duplicate_proposal`).

Rejection codes: `malformed_response`, `schema_invalid`, `missing_field`,
`invalid_type`, `unsupported_model_family`, `unsupported_representation`,
`unsafe_field`, `non_finite_value`, `out_of_domain`, `too_many_proposals`,
`duplicate_proposal`, `invalid_suggestion`.

## 6. Discovery-loop integration

`DiscoveryEngine(..., scientist=...)` accepts an optional `LLMScientist`.

A round runs:

1. after the initial evaluation/ranking pass, when at least one observation is
   committed and the query budget is non-zero, and
2. after each successful oracle query, once the new observation and refreshed
   evaluation/ranking are committed.

Rules:

- Rounds never consume oracle query budget.
- `max_rounds` bounds the number of provider calls per run.
- Accepted hypotheses are re-validated by the engine, appended with stable IDs,
  then fitted/evaluated/ranked by the existing pipeline.
- Duplicate or invalid proposals are skipped with `SCIENTIST_PROPOSAL_SKIPPED`
  warnings even if a scientist contract were bypassed.
- Advisory suggestions are consumed once by the next generated experiment batch
  through `ExperimentGenerationConfig.suggested_inputs` (same bounds, exclusion,
  and deduplication path as generated candidates) and are then cleared. The
  planner, oracle input validation, duplicate-query guard, and query budget stay
  authoritative.
- Rounds are recorded in `DiscoveryRunResult.scientist_history`.

## 7. Modes and fallback

| Mode | Behavior |
| --- | --- |
| Deterministic-only (default) | No scientist object, no provider call, no credentials, no network. `scientist_history` is empty. |
| LLM-assisted | Explicitly enabled configuration with a configured provider. Accepted proposals join the existing pipeline; provenance records provider/model/prompt/schema/round. |
| Fallback | Any failed/empty round records a non-success status with `fallback_applied: true`, emits `SCIENTIST_FALLBACK`, and lets the existing stopping policy continue deterministic discovery. `disable_further_rounds` additionally stops provider calls for the rest of the run. |

A provider failure is never represented as an LLM success, and deterministic
fallback hypotheses are never reported as LLM-generated.

## 8. Groq provider

`backend/app/adapters/llm/groq_provider.py` implements Groq behind the protocol
using the official `groq` SDK (`requirements.txt`). The SDK import is lazy.

### 8.1 Setup

`backend/.env` is gitignored and is loaded by `backend/run.py`. It ships with
five ordered credential slots and the model name:

```bash
# backend/.env (never commit real values)
LLM_SCIENTIST_ENABLED=true
LLM_SCIENTIST_PROVIDER=groq
LLM_SCIENTIST_MODEL=llama-3.3-70b-versatile
LLM_SCIENTIST_CREDENTIAL_ENV_VARS=GROQ_API_KEY,GROQ_API_KEY_2,GROQ_API_KEY_3,GROQ_API_KEY_4,GROQ_API_KEY_5
GROQ_API_KEY=
GROQ_API_KEY_2=
GROQ_API_KEY_3=
GROQ_API_KEY_4=
GROQ_API_KEY_5=
```

Fill at least one slot; empty slots are skipped. Keep `LLM_SCIENTIST_ENABLED=false`
until you intend to use a provider.

Environment variables:

| Variable | Default |
| --- | --- |
| `LLM_SCIENTIST_ENABLED` | `false` |
| `LLM_SCIENTIST_PROVIDER` | `groq` |
| `LLM_SCIENTIST_MODEL` | `llama-3.3-70b-versatile` |
| `LLM_SCIENTIST_CREDENTIAL_ENV_VARS` | `GROQ_API_KEY,GROQ_API_KEY_2,GROQ_API_KEY_3,GROQ_API_KEY_4,GROQ_API_KEY_5` |
| `LLM_SCIENTIST_CREDENTIAL_ENV_VAR` (legacy, single slot) | unset |
| `LLM_SCIENTIST_MAX_PROPOSALS` | `3` |
| `LLM_SCIENTIST_TIMEOUT_SECONDS` | `20` |
| `LLM_SCIENTIST_MAX_RETRIES` | `1` |
| `LLM_SCIENTIST_MAX_TOKENS` | `1024` |
| `LLM_SCIENTIST_MAX_RESPONSE_CHARS` | `20000` |
| `LLM_SCIENTIST_MAX_CONTEXT_OBSERVATIONS` | `20` |
| `LLM_SCIENTIST_MAX_CONTEXT_HYPOTHESES` | `8` |
| `LLM_SCIENTIST_MAX_ROUNDS` | `3` |
| `LLM_SCIENTIST_TEMPERATURE` | `0.2` |
| `LLM_SCIENTIST_FALLBACK_POLICY` | `continue_deterministic` |

Build the component with:

```python
from app.adapters.llm import build_llm_scientist_from_environment

scientist = build_llm_scientist_from_environment()  # None when disabled
```

### 8.2 Credential slots and rotation

- Credentials are read from the ordered environment-variable names in
  `LLM_SCIENTIST_CREDENTIAL_ENV_VARS` (or the legacy single
  `LLM_SCIENTIST_CREDENTIAL_ENV_VAR`). Only names live in configuration; values
  are read once to build clients and are never stored, logged, or serialized.
- Each call starts at the next slot in round-robin order. If a slot is rate
  limited, the adapter moves to the next configured slot within the same bounded
  attempt sequence (each slot gets at most `1 + max_retries` attempts).
- Authentication, permission, timeout, provider, malformed, and oversized
  failures return immediately instead of silently switching credentials; only a
  rate limit rotates.
- If every configured slot is rate limited, the call reports `rate_limited` with
  the number of slots tried. Diagnostics record the 1-based `credential_slot`,
  never the key.
- Rotation is bounded: at most `slots × (1 + max_retries)` provider attempts per
  round, and `max_rounds` bounds rounds per run.

### 8.3 Bounds, retries, and error taxonomy

- One explicit request timeout; the SDK's own retry loop is disabled.
- Bounded retries (`max_retries`) for `rate_limited`, `timeout`, and transient
  connection failures, with capped exponential backoff.
- Authentication and permission failures are never retried.
- Malformed/oversized responses are never retried (a retry only masks the same
  failure and spends budget).
- Structured statuses: `success`, `disabled`, `not_configured`,
  `missing_credentials`, `sdk_unavailable`, `authentication_failed`,
  `permission_denied`, `rate_limited`, `timeout`, `transient_failure`,
  `provider_error`, `malformed_response`, `response_too_large`.
- Token usage is reported only when the provider returned it. Cost and latency
  beyond the measured call duration are never invented.

### 8.4 Troubleshooting

| Symptom | Meaning |
| --- | --- |
| `LLM_PROVIDER_NOT_CONFIGURED` | LLM mode enabled but no provider adapter was supplied. |
| `LLM_MISSING_CREDENTIALS` | LLM mode enabled but every configured credential slot is empty. |
| `LLM_SDK_UNAVAILABLE` | Provider package/client could not be created. |
| `LLM_AUTHENTICATION_FAILED` | Credential rejected; not retried. |
| `LLM_RATE_LIMITED` | Retries exhausted at `max_retries`. |
| `LLM_TIMEOUT` | Request exceeded `timeout_seconds`. |
| `LLM_SCHEMA_VALIDATION_FAILED` | Response JSON did not match the proposal schema. |
| `LLM_NO_VALID_HYPOTHESES` | All proposals were rejected; deterministic fallback applied. |
| `SCIENTIST_ROUND_FAILED` warning | Scientist raised; run continued deterministically. |

## 9. Provenance and observability

Each accepted proposal's `generation_metadata` records: generator and version,
proposal round ID and index, acceptance timestamp, provider and model
identifiers, configured model, prompt and schema versions,
rationale/assumptions/competing explanations, suggested inputs, uncalibrated
confidence flag, and whether the call was live. `DiscoveryRunResult.scientist_history`
holds every round record, including the 1-based `credential_slot` that produced
the outcome (never the key).
`ExperimentExecutionRecord` continues to record the planner's decision for each
experiment.

Nothing records credentials, authorization headers, raw provider payloads, or
full prompts.

## 10. Ground-truth isolation

- The scientist receives only explicit public fields; there is no generic
  serializer over discovery objects. `build_request()` excludes observations
  flagged `is_held_out` and reduces each observation to the declared task
  variables, so evaluator-reserved rows and incidental payload keys cannot
  reach a provider.
- `LLMProposalRequest` and `ScientistRoundRecord` run
  `verify_no_ground_truth_leakage`, so a forbidden key fails closed.
- `DiscoveryRunResult.to_dict()` re-checks the whole payload.
- Tests assert that a canary ground-truth payload and the oracle's evaluator-only
  coefficients never appear in the provider request, round record, warnings, or
  serialized result.
- Provider output is data: it cannot execute code, call the oracle, mutate
  committed observations, change rankings, or alter counters/terminal state.

## 11. Tests and benchmark

```bash
cd backend
python -m pytest tests/test_llm_scientist.py -q     # 67 offline tests
python -m pytest -q                                 # full backend suite
python -m compileall -q app tests
python -m benchmarks.llm_scientist_benchmark        # offline synthetic comparison
python -m benchmarks.llm_scientist_benchmark --json # full report
```

All tests use a deterministic fake provider injected through the same protocol
as Groq. **No test claims live provider execution.** A real-provider smoke test
is optional and must be recorded separately with redacted metadata:

```bash
cd backend
LLM_SCIENTIST_ENABLED=true GROQ_API_KEY=... python - <<'PY'
from app.adapters.llm import build_llm_scientist_from_environment
scientist = build_llm_scientist_from_environment()
print("configured:", scientist is not None, "live:", scientist.provider.is_live)
# Run a discovery loop and print only round status/counts, never prompts or keys.
PY
```

### 11.1 Measured synthetic comparison (offline fake provider)

Command: `python -m benchmarks.llm_scientist_benchmark` (seeds 1–5, 5 initial
observations, query budget 6, active Issue #10 planner, held-out MAE scored
afterwards against the oracle's evaluator-only coefficients).

| Seed | Deterministic held-out MAE | LLM-assisted held-out MAE | Queries (both) | Accepted proposals |
| --- | --- | --- | --- | --- |
| 1 | 4.144e-08 | 8.572e-08 | 6 | 3 |
| 2 | 5.510e-08 | 5.510e-08 | 6 | 3 |
| 3 | 1.422e-07 | 1.422e-07 | 6 | 3 |
| 4 | 7.654e-08 | 7.654e-08 | 6 | 3 |
| 5 | 8.180e-08 | 8.180e-08 | 6 | 3 |
| **Mean** | **7.942e-08** | **8.828e-08** | **6.00** | **15 total** |

Interpretation: both arms reach essentially the same held-out error because the
deterministic generator already contains a cubic candidate that fits the
noiseless synthetic oracle. The benchmark demonstrates the end-to-end path
(proposal → validation → fit → evaluate → rank → select → query → feedback)
and reports queries used and acceptance counts; it does **not** demonstrate a
performance advantage, and none is claimed. All runs terminated with
`terminated_budget_exhausted`; no round fell back.

## 12. Known limitations

- One-dimensional numeric domain and the polynomial family only; other
  structures are rejected, not approximated.
- Proposal quality depends on provider/model/prompt and is not reproducible at
  non-zero temperature; fixed fake responses are reproducible.
- Self-reported confidence is uncalibrated and is never used as evidence.
- Provider dependence: rate limits, outages, and latency affect LLM-assisted
  runs, which then fall back deterministically.
- No live Groq execution was performed in this repository's verification; only
  the offline fake provider and the adapter's classification/retry logic were
  tested.
- Rounds are triggered after the initial ranking and after each successful
  query, not per iteration, and the count is bounded by `max_rounds`.
- Suggestion handling is advisory: suggestions can only enter a generated batch;
  they cannot force a query.
- Query-efficiency benefits are not established; Issue #11 owns stronger query
  validation, information-gain selection, and stopping controls.
