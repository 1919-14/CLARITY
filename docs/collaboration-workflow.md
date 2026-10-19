# CLARITY Collaboration Workflow

## 1. Purpose

This document defines the lightweight GitHub workflow used by the CLARITY team to develop components independently while preserving code quality, traceability, and reliable integration across remote and onsite work.

The workflow applies to the discovery engine, Nodus integration, Flask API, MySQL persistence, React interface, benchmarking, tests, and documentation.

## 2. Core Rules

1. `main` is the shared integration branch and should remain usable.
2. Do not commit implementation work directly to `main`. Use a branch and pull request (PR). An emergency exception requires explicit agreement from the technical lead and must be documented in the relevant issue.
3. Every task must be tracked by a GitHub issue with one primary owner, objective acceptance criteria, and verification instructions.
4. Every task branch and PR must identify its related issue.
5. Keep changes small and focused. Review and validate them before merging.
6. Do not claim tests passed, benchmarks ran, or remote execution worked unless there is actual evidence.
7. Communicate blockers and cross-component dependencies in the relevant GitHub issue or PR so the information is visible to the team.

## 3. Branch Naming

Create branches from an up-to-date `main` unless a documented dependency requires another base branch.

Use these names:

- `feature/<short-description>` — new functionality.
- `fix/<short-description>` — bug fixes.
- `docs/<short-description>` — documentation-only changes.
- `test/<short-description>` — tests, benchmark infrastructure, and benchmark changes.

Use lowercase words separated by hyphens. Keep the name short and descriptive.

Examples:

- `feature/active-query-selector`
- `fix/run-budget-race`
- `docs/collaboration-workflow`
- `test/active-vs-random-benchmark`

Include the issue number in the branch name only if the team agrees to adopt that convention consistently. Regardless of the branch name, the issue must be linked in the PR.

## 4. Issues, Ownership, and Dependencies

Every issue should contain:

- A clear objective and scope.
- One primary owner responsible for progress and handoff.
- Objective acceptance criteria.
- Verification instructions, including relevant commands or manual checks.
- Dependencies or affected shared interfaces, where applicable.

The primary owner may ask teammates for help, but ownership should remain unambiguous. If work is blocked, update the issue with the blocker, its impact, the person or component needed, and the next action.

Record dependencies using GitHub issue links and dependency features where available. Do not rely on private messages as the only record of a blocker or interface decision.

A branch and PR must reference the issue they address. Use GitHub closing keywords such as `Closes #123` only when merging the PR should close that issue. For partial work, use a non-closing reference such as `Refs #123`.

## 5. Pull Request Workflow

1. Update the local `main` branch and create a focused branch.
2. Implement only the scope described by the issue. If scope changes materially, update the issue or split follow-up work into another issue.
3. Run the relevant tests and verification commands. Record the actual outcome, including failures or checks not run.
4. Push the branch and open a PR using `.github/pull_request_template.md`.
5. Link the issue and request review from the relevant component owner when shared interfaces or cross-component behavior are affected.
6. Address relevant review feedback and push updates to the same branch.
7. Ensure applicable automated checks pass and required approvals are present.
8. Merge only when the PR meets the review and verification requirements. Delete the branch after merging if appropriate.

Prefer small PRs that can be reviewed quickly. Avoid combining unrelated features, broad refactors, and formatting-only changes in one PR.

## 6. Review Expectations

Reviewers should check:

- Correctness and alignment with the issue's acceptance criteria.
- Readability, maintainability, and consistency with existing conventions.
- Error handling, edge cases, and resource/budget behavior where relevant.
- Compatibility with agreed API, data, and component contracts.
- Tests that demonstrate the intended behavior and protect against regressions.
- Whether claims in logs, reports, benchmarks, and documentation match the evidence.
- Whether secrets, credentials, tokens, personal data, or other sensitive information have been included.

Changes to shared interfaces require review from the relevant component owner. If the change affects multiple components, involve the owners of the affected components. The technical lead reviews and approves workflow or architecture changes and resolves disagreements about shared contracts.

Review feedback should be actionable and respectful. The PR author is responsible for resolving relevant comments or explaining why a suggestion is not being adopted. Do not merge while a blocking review concern remains unresolved.

## 7. Testing and Verification

Run checks appropriate to the change. Examples may include unit tests, integration tests, linting, type checks, migration checks, API contract checks, or manual UI verification.

In the PR, state:

- The exact commands executed.
- Whether each command passed or failed.
- Any checks that could not be run and why.
- Relevant logs, screenshots, or benchmark artifacts.
- Known limitations and follow-up work.

Never report a check as passing if it was not run. Do not treat mocked Nodus execution as proof of genuine remote execution. Benchmark claims must be backed by reproducible results and must not be invented.

## 8. Integration Checkpoints and Cross-Component Coordination

During active collaborative work, the team should synchronize approximately every **60–90 minutes**. At a checkpoint, each active contributor should share:

- Current branch and issue.
- Files or interfaces changed.
- Current test status.
- Any interface changes that other contributors need to know about.
- Blockers or integration risks.

These checkpoints are coordination targets, not a requirement to interrupt focused work every 60 minutes. Share urgent breaking-contract changes immediately rather than waiting for the next checkpoint.

Before integrating changes across components, verify that the agreed interfaces still match and run the relevant integration or contract checks. Do not merge changes known to break agreed application interfaces or critical tests.

## 9. Team Handoff Format

Use this concise format in a GitHub issue or PR comment when handing work to another teammate:

```text
Handoff
- Issue / PR:
- Owner handing off:
- Work completed:
- Files and interfaces changed:
- Tests / verification commands:
- Actual results:
- Remaining work / known issues:
- Dependencies / blockers:
- Reproduction or verification steps:
- Next owner / next action:
```

Include exact commands and meaningful output when another person needs to reproduce a result. If a command was not run, say so explicitly.

## 10. Secrets and Sensitive Data

- Never commit API keys, access tokens, passwords, private keys, `.env` files containing secrets, or provider credentials.
- Use environment variables and a documented example configuration containing placeholders only.
- Do not include secrets in screenshots, logs, PR comments, benchmark artifacts, or issue descriptions.
- If a secret is exposed, notify the technical lead promptly, revoke or rotate it, and remove it from the repository history using an agreed remediation process. Deleting it in a later commit alone may not be sufficient.

## 11. `main` Branch Rules

Where supported by the repository's GitHub plan and settings, configure a branch ruleset or branch protection rule targeting `main` with:

- Require a pull request before merging.
- Require at least one approval, if the team can provide an independent reviewer.
- Require applicable CI checks to pass once stable checks exist.
- Require conversation resolution before merging, where available.
- Block force pushes and branch deletion, where supported and appropriate.
- Restrict direct pushes to `main` where the repository configuration supports it.

Do not require a CI check by name until that check exists and reports reliably. For a small team, avoid adding rules that prevent urgent integration simply because the required check has not been configured.

Repository rules must be verified in GitHub Settings; documentation alone does not enable them. The person with repository administration permission should record which rules were enabled and any unsupported or unavailable settings. If a rule cannot be enforced, the team must follow it as a documented convention and record that limitation.

## 12. Definition of Done for a PR

A PR is ready to merge when:

- It addresses a linked issue and meets its acceptance criteria.
- Its scope is focused and its description is complete.
- Relevant tests and verification commands have been run and actual results recorded.
- Required reviewers have approved it and blocking feedback is resolved.
- Applicable automated checks pass.
- Shared contracts remain compatible, or coordinated changes are included.
- No secrets or sensitive data are included.
- Known limitations and follow-up work are documented.

## 13. Questions and Exceptions

Discuss workflow exceptions in the relevant issue or PR and get explicit agreement from the technical lead before bypassing the normal `main`-branch workflow. Keep the process lightweight: add process only when it improves traceability, quality, or coordination.
