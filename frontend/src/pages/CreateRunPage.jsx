import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';

import { createRun } from '../api/runs.js';
import { ErrorState } from '../components/feedback/ErrorState.jsx';
import { Notice } from '../components/feedback/Notice.jsx';
import { Field } from '../components/forms/Field.jsx';
import { ArrowLeftIcon, PlusIcon } from '../components/icons.jsx';
import { OrnamentDivider, PageHeader } from '../components/layout/PageHeader.jsx';
import {
  DEFAULT_RUN_FORM,
  EXECUTION_MODES,
  LIMITS,
  validateRunForm,
} from '../utils/validation.js';
import { formatSeconds } from '../utils/formatting.js';

export function CreateRunPage() {
  const navigate = useNavigate();
  const [values, setValues] = useState({ ...DEFAULT_RUN_FORM });
  const [errors, setErrors] = useState({});
  const [warnings, setWarnings] = useState([]);
  const [submitting, setSubmitting] = useState(false);
  const [requestError, setRequestError] = useState(null);
  const [ambiguous, setAmbiguous] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const controllerRef = useRef(null);
  const summaryRef = useRef(null);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const update = (field) => (event) => {
    const { value } = event.target;
    setValues((previous) => ({ ...previous, [field]: value }));
    if (errors[field]) {
      setErrors((previous) => {
        const next = { ...previous };
        delete next[field];
        return next;
      });
    }
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    if (submitting || ambiguous) return;

    const validation = validateRunForm(values);
    setAttempted(true);
    setErrors(validation.errors);
    setWarnings(validation.warnings);
    setRequestError(null);

    if (!validation.payload) {
      summaryRef.current?.focus();
      return;
    }

    setSubmitting(true);
    const controller = new AbortController();
    controllerRef.current = controller;

    try {
      const response = await createRun(validation.payload, { signal: controller.signal });
      const runId = response?.run?.run_id;
      if (!runId) {
        throw new Error('The backend accepted the request but returned no run identifier.');
      }
      // The returned id is the only identifier ever shown; nothing is generated
      // in the browser. The run page confirms the persisted state.
      navigate(`/runs/${encodeURIComponent(runId)}`, {
        state: { createdRunId: runId },
      });
    } catch (error) {
      if (error?.isAborted) return;
      setSubmitting(false);
      if (error?.kind === 'timeout') {
        setAmbiguous(true);
      } else {
        setRequestError(error);
      }
    }
  };

  const selectedMode = EXECUTION_MODES.find((mode) => mode.value === values.executionMode);
  const errorEntries = Object.entries(errors);
  const runtimeHint = Number(values.maxRuntimeSeconds) >= 0 && `${values.maxRuntimeSeconds}`.trim() !== ''
    ? `Equivalent to ${formatSeconds(Number(values.maxRuntimeSeconds))}.`
    : null;

  return (
    <div className="page-inner page-inner--narrow">
      <PageHeader
        volume="Volume V · Configuration"
        title="Configure a discovery run"
        description="Set the investigation name and the resources the backend may admit. Submitting creates a queued run; it does not start discovery by itself."
        actions={
          <Link to="/" className="button button--ghost">
            <ArrowLeftIcon size={16} />
            <span>Dashboard</span>
          </Link>
        }
      />

      <OrnamentDivider />

      {ambiguous ? (
        <Notice
          tone="warning"
          title="Submission outcome unknown"
          actions={
            <button
              type="button"
              className="button button--secondary button--small"
              onClick={() => setAmbiguous(false)}
            >
              Dismiss and review the form
            </button>
          }
        >
          <p>
            The request timed out before the backend answered. A run may or may not have been
            created. Do not submit blindly: open the dashboard and check the recent runs first.
          </p>
          <p>
            <Link className="text-link" to="/">Check recent runs before trying again</Link>
          </p>
        </Notice>
      ) : null}

      {requestError ? (
        <ErrorState
          title="The run was not created"
          error={requestError}
          compact={false}
        />
      ) : null}

      {errorEntries.length > 0 ? (
        <div className="note note--danger" tabIndex={-1} ref={summaryRef} role="alert" aria-labelledby="form-errors-heading">
          <p className="note__title" id="form-errors-heading">
            <span className="note__glyph" aria-hidden="true">✕</span>
            {errorEntries.length} configuration {errorEntries.length === 1 ? 'field needs' : 'fields need'} attention
          </p>
          <ul className="state-block__list">
            {errorEntries.map(([field, message]) => (
              <li key={field}>
                <a className="text-link" href={`#field-${field}`}>
                  {field === 'name' ? 'Run name' : null}
                  {field === 'domain' ? 'Input domain' : null}
                  {field === 'executionMode' ? 'Execution mode' : null}
                  {LIMITS[field] ? LIMITS[field].label : null}
                </a>
                {': '}
                {message}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {warnings.length > 0 ? (
        <Notice tone="warning" title="This budget will not permit scientific work">
          <ul className="prose-list">
            {warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </Notice>
      ) : null}

      <form className="form-card form-card--ornate" onSubmit={handleSubmit} noValidate aria-busy={submitting || undefined}>
        <section className="form-section" aria-labelledby="identity-heading">
          <p className="volume-label">I · Identity</p>
          <h2 id="identity-heading" className="section-heading">Investigation</h2>

          <Field
            id="field-name"
            label="Run name"
            required
            hint="How this investigation is listed in the run archive."
            error={errors.name}
          >
            <input
              id="field-name"
              className="input"
              type="text"
              value={values.name}
              onChange={update('name')}
              placeholder="e.g. Nocturnal growth response"
              autoComplete="off"
            />
          </Field>

          <Field
            id="field-domain"
            label="Black-box input domain"
            required
            hint="Domain identifier passed to the backend (default: “default”). The form never exposes oracle internals."
            error={errors.domain}
          >
            <input
              id="field-domain"
              className="input"
              type="text"
              value={values.domain}
              onChange={update('domain')}
              placeholder="default"
              autoComplete="off"
            />
          </Field>

          <Field
            id="field-executionMode"
            label="Execution mode"
            hint={selectedMode?.description}
            error={errors.executionMode}
          >
            <select
              id="field-executionMode"
              className="input"
              value={values.executionMode}
              onChange={update('executionMode')}
            >
              {EXECUTION_MODES.map((mode) => (
                <option key={mode.value} value={mode.value}>{mode.label}</option>
              ))}
            </select>
          </Field>
        </section>

        <section className="form-section" aria-labelledby="budget-heading">
          <p className="volume-label">II · Resources</p>
          <h2 id="budget-heading" className="section-heading">Application-enforced limits</h2>
          <p className="form-section__intro">
            Defaults come from the backend contract: 10 oracle queries, 100 candidate evaluations,
            4 concurrent remote tasks and 3,600 seconds. Limits are admitted by the backend budget
            manager; this form only validates the request before it is sent.
          </p>

          <div className="form-grid">
            <Field
              id="field-maxOracleQueries"
              label="Oracle query budget"
              required
              hint="0–100,000. Zero permits no oracle query."
              error={errors.maxOracleQueries}
            >
              <input
                id="field-maxOracleQueries"
                className="input input--numeric"
                type="text"
                inputMode="numeric"
                value={values.maxOracleQueries}
                onChange={update('maxOracleQueries')}
              />
            </Field>

            <Field
              id="field-maxCandidateEvaluations"
              label="Candidate evaluation limit"
              required
              hint="0–1,000,000. Zero permits no candidate evaluation."
              error={errors.maxCandidateEvaluations}
            >
              <input
                id="field-maxCandidateEvaluations"
                className="input input--numeric"
                type="text"
                inputMode="numeric"
                value={values.maxCandidateEvaluations}
                onChange={update('maxCandidateEvaluations')}
              />
            </Field>

            <Field
              id="field-maxConcurrentRemoteTasks"
              label="Maximum concurrent tasks"
              required
              hint="0–32. Zero permits no active remote task."
              error={errors.maxConcurrentRemoteTasks}
            >
              <input
                id="field-maxConcurrentRemoteTasks"
                className="input input--numeric"
                type="text"
                inputMode="numeric"
                value={values.maxConcurrentRemoteTasks}
                onChange={update('maxConcurrentRemoteTasks')}
              />
            </Field>

            <Field
              id="field-maxRuntimeSeconds"
              label="Maximum run duration (seconds)"
              required
              hint={`0–604,800 (7 days). ${runtimeHint || ''}`}
              error={errors.maxRuntimeSeconds}
            >
              <input
                id="field-maxRuntimeSeconds"
                className="input input--numeric"
                type="text"
                inputMode="numeric"
                value={values.maxRuntimeSeconds}
                onChange={update('maxRuntimeSeconds')}
              />
            </Field>
          </div>

          <Field
            id="field-maxComputeSpendUsd"
            label="Compute spending limit (USD)"
            hint="Optional; leave blank to disable the application-side spending gate. This is an estimate gate, not a provider-enforced cap."
            error={errors.maxComputeSpendUsd}
          >
            <input
              id="field-maxComputeSpendUsd"
              className="input input--numeric"
              type="text"
              inputMode="decimal"
              value={values.maxComputeSpendUsd}
              onChange={update('maxComputeSpendUsd')}
              placeholder="e.g. 5.00"
            />
          </Field>

          <div className="form-section__note">
            <p className="volume-label">How spending is enforced</p>
            <p>
              Query, evaluation, concurrency and runtime limits are enforced by the CLARITY
              backend. A spending limit compares the application&apos;s own cost estimate against
              the configured ceiling. The configured provider adapter does not expose a
              provider-enforced hard cap, so this form never describes the ceiling as one.
            </p>
          </div>
        </section>

        <div className="form-actions">
          <button
            type="submit"
            className="button button--primary"
            disabled={submitting || ambiguous}
            aria-busy={submitting || undefined}
          >
            <PlusIcon size={16} />
            <span>{submitting ? 'Creating run…' : 'Create discovery run'}</span>
          </button>
          <Link className="button button--ghost" to="/">Cancel</Link>
          <p className="form-actions__note" role="status">
            {submitting
              ? 'Submitting the configuration. The button is disabled to prevent a duplicate run.'
              : attempted && errorEntries.length > 0
                ? 'Correct the highlighted fields and submit again.'
                : 'The run identifier is taken from the backend response, never generated in the browser.'}
          </p>
        </div>
      </form>
    </div>
  );
}
