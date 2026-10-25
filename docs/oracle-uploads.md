# Uploaded black boxes (oracle library)

A researcher can point CLARITY at their own simulator or trained model instead
of the built-in polynomial oracle. The artifact is uploaded once, verified by a
registration probe, and then queried through the same `OracleInterface` as any
other black box. The discovery loop, ranking and reports are unchanged: they see
only inputs and scalar responses.

What CLARITY does **not** do is inspect the artifact. It never reads the source,
never decompiles the model and never claims a ground-truth formula for it. A run
bound to an uploaded artifact produces observations that came from that
artifact's answers, and hypotheses are ranked on how well they reproduce those
answers — nothing more.

---

## 1. Artifact kinds

| Kind | Extensions | Invocation |
|---|---|---|
| `python_function` | `.py` | `entrypoint(x)` — the named function receives one number and must return one number |
| `joblib_model` | `.joblib`, `.pkl`, `.pickle` | `model.predict([[x]])`, or the loaded object itself when it is callable |
| `onnx_model` | `.onnx` | A graph with exactly one input taking a single float feature (`[1, 1]` or `[1]`) and one output |

Rules that apply to every kind:

* one scalar input → one scalar output; arrays of length other than one, booleans,
  complex values and non-finite results are contract errors, not observations;
* the declared `min_value … max_value` range is the only domain CLARITY probes;
* the upload limit is 25 MiB.

Invalid artifacts are rejected before anything is stored:

* unsupported extension, empty file, oversized file → `422`;
* declared `kind` disagreeing with the extension → `422`;
* Python artifact without an `entrypoint`, or an entrypoint that is missing or
  not callable → `422`;
* a model that cannot be loaded, or one that answers no probe input with a
  usable scalar → `422`.

When registration succeeds, the three probe answers are persisted. Partial probe
failures are kept honestly rather than being reported as success.

---

## 2. Trust boundary

**Uploaded artifacts execute inside the backend process.** Importing a `.py`
module and unpickling a model can run arbitrary code with the backend user's
privileges. This is a deliberate choice for the current feature: uploads are
treated as trusted research inputs. There is no sandbox, no subprocess isolation
and no per-query timeout.

Consequences for deployments that accept uploads:

* keep `CLARITY_ORACLES_DIR` outside the repository and outside any directory
  served statically;
* restrict who can reach `POST /api/v1/oracles` at the network or gateway layer;
* expect a misbehaving artifact to be able to hang or crash the backend process —
  execution failures are contained as observations only once the call returns.

Failed calls that do return are contained as `oracle_failure` observations
carrying a sanitized message; they never propagate an exception into the
discovery loop.

---

## 3. Storage

```
backend/oracle_uploads/<oracle_id>/<stored_filename>
```

* The directory is configurable with `CLARITY_ORACLES_DIR`; the default is
  anchored to the backend package so any working directory resolves the same
  path.
* Only the sanitized stored filename is used; the client-supplied name never
  determines the path, and an artifact cannot be resolved outside the storage
  root.
* `backend/oracle_uploads/` is gitignored — uploaded code is never committed.
* Deleting an artifact removes both its row and its directory. An artifact bound
  to at least one run cannot be deleted (`409`): historical runs stay
  reproducible.

The database row records provenance only: original filename, kind, SHA-256,
size, entrypoint, input/output names, declared bounds, creator and the probe
summary. Absolute server paths and artifact bytes are never serialized back to a
client.

---

## 4. API

### Register an artifact

```bash
curl -X POST http://127.0.0.1:5000/api/v1/oracles \
  -F "file=@simulate.py" \
  -F "entrypoint=simulate" \
  -F "min_value=-2" \
  -F "max_value=2" \
  -F "name=Reactor pressure simulator"
```

`multipart/form-data` fields:

| Field | Required | Notes |
|---|---|---|
| `file` | yes | The artifact |
| `kind` | no | Inferred from the extension; a mismatch is rejected |
| `name` | no | Library label; defaults to the filename stem |
| `entrypoint` | for `.py` | Function to call |
| `input_name` | no | Variable label in observations (default `x`) |
| `output_name` | no | Response label in observations (default `y`) |
| `min_value`, `max_value` | no | Probe/search bounds (default `-10 … 10`), must be finite and ordered |
| `actor` | no | Recorded as `created_by` (default `researcher`) |

Response `201`:

```json
{
  "oracle": {
    "oracle_id": "oracle-307db2ae0999",
    "name": "Reactor pressure simulator",
    "kind": "python_function",
    "kind_label": "Python function",
    "original_filename": "simulate.py",
    "sha256": "7c236da0…",
    "size_bytes": 42,
    "entrypoint": "simulate",
    "input_name": "x",
    "output_name": "y",
    "min_value": -2.0,
    "max_value": 2.0,
    "probe": {
      "samples": [
        {"x": -2.0, "y": -5.0, "error": null},
        {"x": 0.0, "y": 1.0, "error": null},
        {"x": 2.0, "y": 7.0, "error": null}
      ],
      "ok_count": 3,
      "failed_count": 0
    },
    "created_by": "researcher",
    "created_at": "2026-10-25T08:56:28.041083+00:00",
    "run_count": 0
  }
}
```

### Library management

| Method | Endpoint | Notes |
|---|---|---|
| `GET` | `/api/v1/oracles` | Newest first, with `run_count` per artifact |
| `GET` | `/api/v1/oracles/{oracle_id}` | One artifact and its run count |
| `DELETE` | `/api/v1/oracles/{oracle_id}` | `409` while any run references it |

### Bind a run to an artifact

```http
POST /api/v1/runs
{ "name": "Distil my simulator", "oracle_id": "oracle-307db2ae0999" }
```

* Omitting `oracle_id` keeps the built-in local simulator.
* An unknown id is rejected with `422` and `details[0].field == "oracle_id"`;
  a run is never created against a missing artifact.
* `GET /api/v1/runs/{run_id}` adds an `oracle` summary (id, name, kind, kind
  label, entrypoint, input/output names, bounds) alongside `oracle_id`.

---

## 5. Execution

The engine factory (`backend/app/services/engine_factory.py`) composes the run's
oracle:

* no artifact → `PolynomialLocalOracle` on the historical `[-2, 2]` domain;
* bound artifact → `UploadedOracle` using the artifact's declared bounds,
  variable name and output label. Candidate generation widens to degree 3 so a
  black box that is not quadratic can still be distilled, and every claim in the
  report remains the ranking engine's, not a claimed ground truth.

If a bound artifact cannot be loaded when the run starts, the run fails loudly
(`RunOracleUnavailable`). CLARITY never silently substitutes the built-in
simulator, because that would attribute fabricated observations to the
researcher's artifact.

Every successful call records the artifact's own rounded response
(`output_values[output_name]`). A call that raises is recorded as
`oracle_failure` with a sanitized message, and an input outside the declared
range is recorded as `invalid_input` without invoking the artifact.

---

## 6. Frontend

* **`/oracles` — Oracle library:** upload form (file, detected kind,
  entrypoint, bounds, labels), the trust notice, the live library table with each
  artifact's registration probe, and deletion when unused.
* **`/runs/new` — Create run:** an oracle selector listing registered artifacts;
  the built-in simulator stays the default. `?oracle=<id>` preselects one, so the
  library's “Create run” action lands on a pre-bound form.
* **`/runs/{run_id}` — Run ledger:** a “Black box” panel naming the artifact, its
  contract and bounds, and stating that every observation came from it.

---

## 7. Verification

```bash
cd backend && python -m pytest tests/test_oracle_library.py -q
cd frontend && npm test -- --run src/pages/RunDetailsPage.test.jsx
```

`backend/tests/test_oracle_library.py` covers the contract validation, all three
adapters (including a real ONNX graph in `backend/tests/fixtures/`), the upload
and library endpoints, the probe record, deletion rules, and a bound run whose
recorded observations are checked against the artifact's own formula.
