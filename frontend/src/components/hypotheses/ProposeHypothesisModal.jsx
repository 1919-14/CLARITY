import { useEffect, useId, useRef, useState } from 'react';
import { useModalFocusTrap } from '../../hooks/useModalFocusTrap.js';
import { Field } from '../forms/Field.jsx';

const FAMILY_OPTIONS = [
  { value: '1d_polynomial', label: '1D Polynomial (e.g. y = 2*x + 1)' },
  { value: 'linear_regression', label: 'Linear Regression (e.g. y = w1*x + w0)' },
  { value: 'rational_function', label: 'Rational Function' },
  { value: 'exponential_decay', label: 'Exponential Decay' },
  { value: 'custom', label: 'Custom / Other' },
];

export function ProposeHypothesisModal({
  open,
  runId,
  busy = false,
  onSubmit,
  onCancel,
  backendError = null,
}) {
  const titleId = useId();
  const formRef = useRef(null);
  const containerRef = useModalFocusTrap({ open, onClose: onCancel, busy });

  const [modelFamily, setModelFamily] = useState('1d_polynomial');
  const [customFamily, setCustomFamily] = useState('');
  const [formula, setFormula] = useState('');
  const [parametersJson, setParametersJson] = useState('');
  const [complexity, setComplexity] = useState('');
  const [customId, setCustomId] = useState('');
  const [clientErrors, setClientErrors] = useState({});

  useEffect(() => {
    if (!open) return;
    setModelFamily('1d_polynomial');
    setCustomFamily('');
    setFormula('');
    setParametersJson('');
    setComplexity('');
    setCustomId('');
    setClientErrors({});
  }, [open]);

  if (!open) return null;

  const handleSubmit = (e) => {
    e.preventDefault();
    const errors = {};

    const cleanFormula = formula.trim();
    if (!cleanFormula) {
      errors.formula = 'Formula is required (e.g. y = 2*x + 1 or y = a*x + b).';
    }

    const effectiveFamily = modelFamily === 'custom' ? customFamily.trim() : modelFamily;
    if (!effectiveFamily) {
      errors.model_family = 'Model family is required.';
    }

    let parsedParameters = undefined;
    if (parametersJson.trim()) {
      try {
        parsedParameters = JSON.parse(parametersJson.trim());
        if (typeof parsedParameters !== 'object' || Array.isArray(parsedParameters) || parsedParameters === null) {
          errors.parameters = 'Parameters must be a JSON object, e.g. {"a": 2.0, "b": 1.0}.';
        }
      } catch (err) {
        errors.parameters = 'Invalid JSON: ' + err.message;
      }
    }

    let parsedComplexity = undefined;
    if (complexity.trim()) {
      const num = Number(complexity.trim());
      if (!Number.isInteger(num) || num < 0) {
        errors.complexity = 'Complexity must be a non-negative integer.';
      } else {
        parsedComplexity = num;
      }
    }

    if (Object.keys(errors).length > 0) {
      setClientErrors(errors);
      return;
    }

    setClientErrors({});
    const payload = {
      model_family: effectiveFamily,
      formula: cleanFormula,
      ...(parsedParameters !== undefined ? { parameters: parsedParameters } : {}),
      ...(parsedComplexity !== undefined ? { complexity: parsedComplexity } : {}),
      ...(customId.trim() ? { hypothesis_id: customId.trim() } : {}),
    };

    onSubmit(payload);
  };

  return (
    <div
      className="dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onCancel();
      }}
    >
      <div
        ref={containerRef}
        tabIndex={-1}
        className="dialog dialog--large"
        style={{ width: 'min(100%, 42rem)' }}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <p className="volume-label">Researcher Contribution</p>
        <h2 className="dialog__title" id={titleId}>Propose Hypothesis</h2>
        <p className="dialog__body">
          Introduce a researcher-created hypothesis into this discovery run. The candidate will be
          recorded with an origin of <code>researcher</code> and queued for scientific evaluation
          against observed evidence.
        </p>

        {backendError && (
          <div className="error-banner" role="alert" style={{ margin: 'var(--space-4) 0' }}>
            <p className="error-banner__message">{backendError.message || String(backendError)}</p>
          </div>
        )}

        <form ref={formRef} onSubmit={handleSubmit} noValidate style={{ marginTop: 'var(--space-4)' }}>
          <Field
            id="hyp-family"
            label="Model Family"
            required
            error={clientErrors.model_family}
            hint="Choose a model family matching the mathematical form of the explanation."
          >
            <select
              id="hyp-family"
              className="select"
              value={modelFamily}
              onChange={(e) => setModelFamily(e.target.value)}
              disabled={busy}
            >
              {FAMILY_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </Field>

          {modelFamily === 'custom' && (
            <Field
              id="hyp-custom-family"
              label="Custom Family Identifier"
              required
              error={clientErrors.model_family}
            >
              <input
                id="hyp-custom-family"
                type="text"
                className="input"
                placeholder="e.g., custom_oscillator"
                value={customFamily}
                onChange={(e) => setCustomFamily(e.target.value)}
                disabled={busy}
              />
            </Field>
          )}

          <Field
            id="hyp-formula"
            label="Formula"
            required
            error={clientErrors.formula}
            hint="Mathematical expression for the hypothesis (e.g. y = 2*x + 1 or y = a*x + b)."
          >
            <input
              id="hyp-formula"
              type="text"
              className="input formula-text"
              placeholder="e.g. y = 2*x + 1"
              value={formula}
              onChange={(e) => setFormula(e.target.value)}
              disabled={busy}
            />
          </Field>

          <Field
            id="hyp-parameters"
            label="Parameters (optional JSON)"
            error={clientErrors.parameters}
            hint="If parameter names appear in the formula, supply their numeric values as JSON (e.g. { &quot;a&quot;: 2.0, &quot;b&quot;: 1.0 }). If omitted, polynomial coefficients are automatically derived."
          >
            <textarea
              id="hyp-parameters"
              className="textarea formula-text"
              rows={3}
              placeholder='{"w1": 2.0, "w0": 1.0}'
              value={parametersJson}
              onChange={(e) => setParametersJson(e.target.value)}
              disabled={busy}
            />
          </Field>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--space-4)' }}>
            <Field
              id="hyp-complexity"
              label="Complexity (optional)"
              error={clientErrors.complexity}
              hint="Structural or term complexity measure."
            >
              <input
                id="hyp-complexity"
                type="number"
                min="0"
                step="1"
                className="input"
                placeholder="e.g. 2"
                value={complexity}
                onChange={(e) => setComplexity(e.target.value)}
                disabled={busy}
              />
            </Field>

            <Field
              id="hyp-custom-id"
              label="Custom ID (optional)"
              hint="Leave blank to have CLARITY assign an identifier."
            >
              <input
                id="hyp-custom-id"
                type="text"
                className="input"
                placeholder="e.g. hyp-researcher-1"
                value={customId}
                onChange={(e) => setCustomId(e.target.value)}
                disabled={busy}
              />
            </Field>
          </div>

          <div className="dialog__actions">
            <button
              type="button"
              className="button button--secondary"
              onClick={onCancel}
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
              {busy ? 'Proposing…' : 'Propose hypothesis'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
