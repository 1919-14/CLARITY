# CLARITY — Autonomous Black-Box Scientist

CLARITY aims to discover an unknown system's behavior through **active experimentation**. It maintains competing explanations, chooses informative experiments, observes the black box, and updates hypotheses under explicit query and compute budgets.

> **Status:** These documents describe intended MVP scope, not proof that features are implemented. Check the code, tests, and provider evidence before claiming implementation.

## Problem

When a system's internal rule is unknown, a researcher must choose which input to try next. Random or passive sampling can waste a limited query budget. CLARITY aims to maintain plausible hypotheses and select experiments that help distinguish them.

## Intended capabilities

- Generate, fit, evaluate, and rank competing hypotheses.
- Choose experiments using prediction disagreement and defensible uncertainty estimates.
- Query a black-box oracle and update hypotheses from observations.
- Use Nodus Compute for genuine remote parallel evaluation.
- Enforce per-run query/resource limits and track usage honestly.
- Support researcher actions: add, pin/unpin, exclude/restore, and re-evaluate hypotheses.
- Persist runs, observations, evaluations, revisions, usage, and audit events.
- Produce evidence-backed ranked reports and reproducible active-versus-random benchmarks.

## Technology stack

| Area | Technology |
|---|---|
| Backend/API | Python, Flask |
| Research interface | React |
| Persistence | MySQL |
| Remote parallel execution | Nodus Compute SDK/service, after verifying actual SDK capabilities |
| Automated checks | GitHub Actions, backend tests, frontend checks/build |
| Initial progress updates | REST and polling; streaming only if justified |

Exact libraries, Nodus SDK capabilities, schema migration tooling, and deployment target remain subject to implementation verification.

## High-level workflow

1. Researcher configures a run, oracle interface, query budget, and resource limits.
2. CLARITY generates candidate hypotheses and evaluates them against permitted observations.
3. The planner proposes valid candidate experiments and scores their information value.
4. The budget manager checks limits before work is scheduled.
5. The oracle returns an observation; the discovery engine sees only permitted inputs and outputs.
6. Candidate evaluations and rankings are updated.
7. The run stops when a configured limit or documented stopping condition is reached.
8. A report presents ranked hypotheses, evidence, measured metrics, usage, stop reason, and limitations.
9. A separate evaluator may use hidden ground truth for benchmark metrics. The discovery agent must not access it.

## Architecture at a glance

- **React:** run configuration, progress, hypothesis/evidence views, researcher actions, reports, benchmarks.
- **Flask:** validation, run lifecycle, orchestration, researcher actions, reporting, persistence access.
- **Discovery engine:** hypothesis generation/fitting/ranking, experiment generation, active selection, stopping logic.
- **Oracle adapter:** narrow interface for querying the unknown system.
- **Nodus adapter:** remote task submission/results, timeouts, and failures behind a testable boundary.
- **Budget/usage services:** query/evaluation limits, concurrency/runtime controls, cost-state tracking.
- **MySQL:** runs, observations, hypotheses/revisions, evaluations, usage, audit events.
- **Evaluator:** isolated access to reference answers and controlled comparisons.

## Scientific integrity

- Keep the oracle, discovery engine, and evaluator separate; ground truth belongs only to the evaluator.
- Pinning is a researcher preference, not proof of correctness.
- Exclusion is reversible and must not delete history.
- Missing metrics are unavailable, not zero.
- Estimated cost is not a provider-confirmed charge; account balance and per-run budget are distinct.
- A mocked Nodus test does not prove remote execution.
- Do not claim discovery succeeded without independent evidence.
- Never fabricate accuracy, confidence, speedup, savings, or benchmark outcomes.

## Documentation

- [Master project context](context.md)
- [Architecture](docs/architecture.md)
- [Scientific data contracts](docs/contracts.md)
- [Discovery loop](docs/discovery-loop.md)
- [LLM Scientist (optional)](docs/llm-scientist.md)
- [Ranking and evidence](docs/ranking-and-evidence.md)
- [Persistence](docs/persistence.md)
- [Development guide](docs/development.md)
- [Decision record](docs/decisions.md)
- [GitHub Issues](https://github.com/1919-14/CLARITY/issues)

## Quickstart and Local Setup

### Backend (Python 3.11+)

1. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   # On Windows:
   .\venv\Scripts\activate
   # On macOS/Linux:
   source venv/bin/activate
   ```
2. Install dependencies:
   ```bash
   pip install -r backend/requirements.txt
   ```
3. Run backend tests (from repository root):
   ```bash
   python -m pytest backend/tests -q
   ```
   *(Or navigate into `backend` and run `python -m pytest -q`)*
4. Start development server (from repository root):
   ```bash
   python backend/run.py
   ```
   *(Or from within `backend/`, run `python run.py`)*
   Verify health endpoint at `http://127.0.0.1:5000/api/health`.

   The optional LLM Scientist is **disabled by default**. Deterministic discovery
   needs no credentials and no network. A local, gitignored `backend/.env` already
   contains five ordered Groq credential slots (`GROQ_API_KEY` … `GROQ_API_KEY_5`)
   and `LLM_SCIENTIST_MODEL`. Fill at least one slot, set
   `LLM_SCIENTIST_ENABLED=true`, and the provider rotates to the next slot when a
   key is rate limited. See [docs/llm-scientist.md](docs/llm-scientist.md).

### Frontend (Node.js 20+)

1. Install dependencies:
   ```bash
   cd frontend
   npm ci
   ```
2. Run lint and production build:
   ```bash
   npm run lint
   npm run build
   ```
3. Start development server:
   ```bash
   npm run dev
   ```
   Open `http://localhost:5173` in your browser.

## Development status

The application skeleton and automated CI checks (Issue #3), the scientific data contracts and black-box oracle interface (Issue #4), bounded candidate hypothesis representation/generation (Issue #5), the fitting/evaluation pipeline (Issue #6), hypothesis ranking and evidence tracking (Issue #7), candidate experiment generation (Issue #9), the end-to-end discovery loop (Issue #8), disagreement-driven active experiment selection with a seeded random baseline (Issue #10), and an optional, provider-independent LLM Scientist with offline tests and a synthetic comparison (Issue #41) are in place. The Flask backend, React status interface, GitHub Actions CI workflow, domain contracts, deterministic local oracle, isolated benchmark evaluator, polynomial model family, prediction interface, seeded candidate generator, least-squares fitter, train/held-out evaluation pipeline, ranking/evidence engine, experiment generator, budgeted discovery orchestrator, and opt-in active experiment selector are established. The LLM Scientist is disabled by default: it proposes bounded, locally validated hypotheses from permitted evidence, while CLARITY retains fitting, evaluation, ranking, experiment selection, oracle access, budget, and stopping. Next phase focuses on information-gain selection and stopping controls (Issue #11).
