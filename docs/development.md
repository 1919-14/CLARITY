# CLARITY Development Guide

## Start here

Before an issue, read [README](../README.md), [master context](../context.md), [architecture](architecture.md), [decisions](decisions.md), and the assigned GitHub issue. Then inspect the actual branch, files, code, and tests. A documented target layout does not mean those files already exist.

## Repository layout

```text
CLARITY/
├── README.md
├── context.md
├── docs/{architecture,development,decisions}.md
├── backend/{app,tests,requirements.txt}
├── frontend/{src,package.json}
└── .github/{workflows,pull_request_template.md}
```

This is an intended shape, not a requirement to create empty directories. Adapt it consistently when the skeleton is established.

## Team ownership

- **Sai Narayana:** discovery algorithm, hypothesis lifecycle, experiment planner, scientific correctness, shared contracts, technical integration.
- **Anuj Sharma:** Nodus SDK integration, remote execution, parallel evaluation, benchmark runner, runtime/cost evidence.
- **Ram sir:** Flask integration/application wiring, backend integration tests, deployment/demo setup.
- **Barkha Rathi:** React researcher workflow, hypothesis/evidence UI, ranked reports/benchmarks, test scenarios and demo evidence.

Assign one primary owner per issue. Cross-component changes require coordination with relevant owners. A handoff states changed files/interfaces, commands and actual results, remaining work, blockers, and reproduction steps.

## Git and PR workflow

Use `main` as the shared integration branch. Avoid direct implementation commits to `main` except for an explicitly agreed emergency.

Branch names:
- `feature/<short-description>`
- `fix/<short-description>`
- `docs/<short-description>`
- `test/<short-description>`

Flow:
1. Confirm issue and owner.
2. Fetch latest `main` and create a focused branch.
3. Implement the smallest coherent change.
4. Run relevant checks.
5. Open a PR linking the issue (`Closes #N` only if the PR truly completes it).
6. Request review from the relevant owner.
7. Resolve feedback and ensure applicable checks pass.
8. Merge using the repository's configured method and verify integration.

Prefer small PRs and frequent integration checkpoints rather than a last-minute merge. If issue dependencies are circular or inaccurate, coordinate a practical order rather than waiting indefinitely.

Every PR includes scope/linked issue, summary/design notes, commands actually run and results, relevant screenshots/logs/benchmark evidence with secrets redacted, limitations/untested behavior, and confirmation that no credentials or hidden ground truth are included.

## Coding conventions

- Use clear names, small functions, and explicit interfaces.
- Keep domain/scientific logic independent of Flask routes, React components, and provider SDK internals.
- Validate inputs at system boundaries; use consistent schemas and structured errors.
- Avoid duplicated ranking, budget logic, serializers, and contracts.
- Use deterministic fixtures/seeds when appropriate.
- Define units and meanings for metrics, costs, counts, and scores.
- Missing metrics are unavailable/null with a reason, not zero.
- Preserve scientific history; do not silently overwrite meaningful evaluations.
- Comment non-obvious scientific/concurrency decisions.
- Avoid new frameworks/services without a clear MVP need and documented rationale.

## Scientific correctness rules

1. Discovery never receives hidden rules or evaluator-only labels.
2. Pinning does not alter scientific scores or prove correctness.
3. Exclusion is reversible and preserves history.
4. Re-evaluation preserves prior results when needed.
5. Active/random benchmarks use comparable conditions and query budgets.
6. Preserve failed trials and disclose sample counts.
7. Use Hit@K/MRR only with a meaningful correct target; use MAE/RMSE for suitable numeric predictions.
8. Report uncertainty only when defensibly estimated.
9. Never fabricate accuracy, confidence, speedup, savings, or benchmark outcomes.
10. A top-ranked candidate is not automatically true.
11. Mock Nodus tests do not prove real remote execution.
12. Keep estimated, live/accruing, and settled costs distinct.

## AI-assisted development rules

AI agents must read context, focused docs, and issue before coding; inspect actual files/tests; state assumptions and ask about conflicts; stay within scope; avoid unrelated rewrites; never invent SDK methods or test/benchmark results; never commit secrets or hidden ground truth; run relevant checks and report exact results; and explain work so the human owner can reproduce it.

When docs, issues, and code differ, code/tests describe current behavior, approved docs describe intended direction, and the issue defines task scope. Surface conflicts that affect interfaces, scope, or scientific correctness.

## Testing expectations

**Backend:** configured Python suite; unit tests for domain logic, validation, state transitions, budgets, and errors; Flask test-client tests; separate integration tests for MySQL/provider behavior.

**Frontend:** configured tests, lint checks if present, production build; cover loading, empty, success, error, and unavailable states; ensure displayed values come from real API responses.

**Scientific/integration:** deterministic oracle fixture; query budget under concurrency; Nodus partial failure/timeouts/malformed payloads; candidate/result identity; ground-truth isolation; active-vs-random comparison under matched conditions; real Nodus smoke evidence separated from mocks.

Example commands, subject to the actual repository scripts:

```bash
cd backend
python -m pytest -q
```

```bash
cd frontend
npm ci
npm test
npm run build
```

These are examples, not evidence that those directories/scripts already exist. Verify before relying on them.

## Environment and secrets

- Commit safe templates such as `.env.example`, never real environment files.
- Load secrets from environment variables or an approved secret manager.
- Never log credentials, hidden rules, or evaluator-only answers.
- Validate required configuration at startup without exposing secret values.
- Keep local data, build artifacts, caches, dependency folders, coverage output, and credentials out of Git.
- Document tests that require MySQL, Nodus credentials, network access, or other external services.
- Do not assume provider balance, budget enforcement, cost reporting, or SDK behavior without verification.

## Documentation and definition of done

Update docs when API contracts, lifecycle states, hypothesis forms, metrics, Nodus behavior, budget semantics, persistence/schema, setup, or architecture changes. Record durable choices in [decisions.md](decisions.md), including context, alternatives, trade-offs, status, and validation.

A task is done only when acceptance criteria are met or limitations are approved/documented; relevant checks actually ran; normal, edge, and invalid behavior is understood; interfaces/docs/tests agree; no secrets or ground truth leaked; the owner can explain/reproduce the work; and the PR is reviewed and integrated.
