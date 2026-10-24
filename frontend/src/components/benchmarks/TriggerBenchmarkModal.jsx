import { useEffect, useId, useState } from 'react';
import { useModalFocusTrap } from '../../hooks/useModalFocusTrap.js';
import { Field } from '../forms/Field.jsx';
import { CloseIcon, PlayIcon } from '../icons.jsx';

export function TriggerBenchmarkModal({
  open,
  scenarios = [],
  busy = false,
  onSubmit,
  onClose,
  backendError = null,
}) {
  const titleId = useId();
  const containerRef = useModalFocusTrap({ open, onClose, busy });

  const [scenarioId, setScenarioId] = useState(scenarios[0]?.scenario_id || '');
  const [seeds, setSeeds] = useState('42');
  const [maxBudget, setMaxBudget] = useState('10');
  const [clientErrors, setClientErrors] = useState({});

  useEffect(() => {
    if (!scenarioId && scenarios.length > 0) {
      setScenarioId(scenarios[0].scenario_id);
    }
  }, [scenarios, scenarioId]);

  if (!open) return null;

  const handleSubmit = (e) => {
    e.preventDefault();
    const errors = {};

    if (!scenarioId) {
      errors.scenarioId = 'Please select a benchmark scenario.';
    }

    const parsedSeeds = seeds
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
      .map((s) => Number(s));

    if (parsedSeeds.length === 0 || parsedSeeds.some((s) => Number.isNaN(s) || !Number.isInteger(s))) {
      errors.seeds = 'Seeds must be a comma-separated list of integers (e.g. 42, 123).';
    }

    const parsedBudget = Number(maxBudget);
    if (!parsedBudget || Number.isNaN(parsedBudget) || parsedBudget < 1 || parsedBudget > 100) {
      errors.maxBudget = 'Query budget must be an integer between 1 and 100.';
    }

    if (Object.keys(errors).length > 0) {
      setClientErrors(errors);
      return;
    }

    setClientErrors({});
    onSubmit({
      scenario_id: scenarioId,
      seeds: parsedSeeds,
      max_query_budget: parsedBudget,
    });
  };

  return (
    <div
      className="dialog-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        ref={containerRef}
        tabIndex={-1}
        className="dialog dialog--large"
        style={{ width: 'min(100%, 38rem)' }}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', borderBottom: '1px solid rgba(201,169,98,0.25)', paddingBottom: 'var(--space-3)' }}>
          <div>
            <p className="volume-label">Reproducible Benchmark Execution</p>
            <h2 className="dialog__title" id={titleId} style={{ margin: 0 }}>Run Scientific Benchmark</h2>
          </div>
          <button
            type="button"
            className="button button--ghost button--small"
            onClick={onClose}
            disabled={busy}
            aria-label="Close modal"
          >
            <CloseIcon size={18} />
          </button>
        </div>

        <p className="dialog__body" style={{ marginTop: 'var(--space-3)' }}>
          Executes a controlled benchmark comparing CLARITY's active hypothesis discovery against a passive random baseline.
          All oracle ground-truth formulas remain strictly hidden inside the evaluator.
        </p>

        {backendError && (
          <div className="error-banner" role="alert" style={{ margin: 'var(--space-3) 0' }}>
            <p className="error-banner__message">{backendError.message || String(backendError)}</p>
          </div>
        )}

        <form onSubmit={handleSubmit} noValidate style={{ marginTop: 'var(--space-3)' }}>
          <Field
            id="bm-scenario"
            label="Benchmark Scenario"
            required
            error={clientErrors.scenarioId}
            hint="Choose a registered benchmark case from the evaluator scenario register."
          >
            <select
              id="bm-scenario"
              className="select"
              value={scenarioId}
              onChange={(e) => setScenarioId(e.target.value)}
              disabled={busy || scenarios.length === 0}
            >
              {scenarios.length === 0 && <option value="">No scenarios available</option>}
              {scenarios.map((s) => (
                <option key={s.scenario_id} value={s.scenario_id}>
                  {s.scenario_id} ({s.challenge_type})
                </option>
              ))}
            </select>
          </Field>

          <Field
            id="bm-seeds"
            label="Random Seeds (comma-separated)"
            required
            error={clientErrors.seeds}
            hint="Seeds determine the pseudorandom noise and initial observation points for reproducible evaluation."
          >
            <input
              id="bm-seeds"
              type="text"
              className="input"
              value={seeds}
              onChange={(e) => setSeeds(e.target.value)}
              placeholder="42, 101, 777"
              disabled={busy}
            />
          </Field>

          <Field
            id="bm-budget"
            label="Oracle Query Budget"
            required
            error={clientErrors.maxBudget}
            hint="Maximum number of oracle query experiments allowed per strategy trial (1 - 100)."
          >
            <input
              id="bm-budget"
              type="number"
              className="input"
              min="1"
              max="100"
              value={maxBudget}
              onChange={(e) => setMaxBudget(e.target.value)}
              disabled={busy}
            />
          </Field>

          <div className="dialog__actions" style={{ borderTop: '1px solid rgba(201,169,98,0.25)', paddingTop: 'var(--space-4)', marginTop: 'var(--space-4)' }}>
            <button
              type="button"
              className="button button--secondary"
              onClick={onClose}
              disabled={busy}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="button button--primary"
              disabled={busy}
              aria-busy={busy || undefined}
            >
              <PlayIcon size={14} />
              <span>{busy ? 'Evaluating…' : 'Execute Benchmark'}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
