import { useState } from 'react';
import { ChevronDownIcon, ChevronUpIcon, InfoIcon } from '../icons.jsx';

const METRIC_DEFINITIONS = [
  {
    id: 'hit_at_k',
    name: 'Hit@K Discovery Rate',
    definition: 'Proportion of benchmark discovery trials where the ground-truth hypothesis (or a mathematically equivalent formulation) appears within the top K ranked candidates.',
    whatItMeasures: 'Discovery recall and ranking accuracy. It answers: does the true governing law make the shortlist of candidate theories?',
    direction: 'Higher is better (1.0 = perfect discovery on every case)',
    units: 'Ratio (0.0 to 1.0) or Percentage (0% to 100%)',
    thresholds: 'K ∈ {1, 3, 5}. Hit@1 requires the exact formula to be ranked #1 by predictive fit and parsimony.',
    limitations: 'Hit@K requires a known ground-truth answer in the benchmark evaluator. It cannot be computed for exploratory real-world scientific runs where nature holds the true law.',
  },
  {
    id: 'mrr',
    name: 'Mean Reciprocal Rank (MRR)',
    definition: 'MRR = (1 / N) * Σ (1 / rank_i), where rank_i is the 1-based rank position of the first valid hypothesis for trial i (and 0 if not found).',
    whatItMeasures: 'The precision with which CLARITY surfaces the true physical law near the top of the candidate hierarchy.',
    direction: 'Higher is better (1.0 = true hypothesis always ranked #1)',
    units: 'Score from 0.0 to 1.0',
    thresholds: 'MRR = 1.0 (Rank 1), MRR = 0.5 (Rank 2), MRR = 0.333 (Rank 3).',
    limitations: 'Only rewards the single highest-ranking valid hypothesis; ignores whether redundant variations appear lower in the cohort.',
  },
  {
    id: 'held_out_error',
    name: 'Held-out Predictive Error (MAE & RMSE)',
    definition: 'Mean Absolute Error: MAE = (1 / M) * Σ |y_obs - y_pred|; Root Mean Squared Error: RMSE = sqrt((1 / M) * Σ (y_obs - y_pred)^2) evaluated on held-out test points.',
    whatItMeasures: 'Generalization ability and interpolation/extrapolation fidelity beyond the experimental points queried during discovery.',
    direction: 'Lower is better (0.0 = perfect noiseless fit)',
    units: 'Physical response units (y_units)',
    thresholds: 'Success threshold in polynomial benchmarks is typically MAE ≤ 0.05 y_units.',
    limitations: 'Errors can only be compared when evaluating compatible observation domains and identical noise variances.',
  },
  {
    id: 'queries_to_success',
    name: 'Queries-to-Success (Sample Efficiency)',
    definition: 'The minimum number of oracle queries executed before the discovery engine proposed a hypothesis satisfying the success threshold (e.g. MAE ≤ 0.05).',
    whatItMeasures: 'Experimental sample efficiency and laboratory resource conservation. It measures how quickly the active agent learns without wasteful trial-and-error.',
    direction: 'Lower is better (fewer wet-lab or oracle experiments needed)',
    units: 'Integer query count (queries)',
    thresholds: 'Capped at the predefined query budget (e.g. 10 or 20 queries). If target is not reached, queries_to_target is undefined/budget-exhausted.',
    limitations: 'Valid comparison requires identical query budgets, equal noise levels, and identical target tolerances.',
  },
  {
    id: 'complexity',
    name: 'Model Complexity & Parsimony',
    definition: 'Structural complexity score derived from parameter degrees, free coefficients, and functional operator depth.',
    whatItMeasures: "Adherence to Occam's razor: penalizing needlessly convoluted explanations when simpler models fit the evidence equally well.",
    direction: 'Lower complexity is preferred among models with comparable predictive power',
    units: 'Structural complexity score (integer)',
    thresholds: 'e.g. constant = 1, linear = 2, quadratic = 3, cubic = 4.',
    limitations: 'Complexity metrics are model-family specific and cannot be treated as a universal continuous scale between disparate physical formulations.',
  },
  {
    id: 'runtime_cost',
    name: 'Compute Cost & Wall-clock Execution',
    definition: 'Empirical runtime measured via monotonic wall-clock time (seconds), and compute cost (USD) tracked across settled, accruing, and estimated states.',
    whatItMeasures: 'Computational tractability and economic expenditure for running the AI scientist loop.',
    direction: 'Lower cost and shorter duration are better for equivalent scientific output',
    units: 'USD ($) and Seconds (s)',
    thresholds: 'Local execution rates vs cloud GPU/remote worker pricing schedules.',
    limitations: 'Cost values are reported as "Unavailable" if pricing data is not configured. CLARITY never coerces unmeasured costs to $0.00.',
  },
];

export function MetricExplanationsPanel() {
  const [expandedId, setExpandedId] = useState(null);

  const toggle = (id) => {
    setExpandedId((prev) => (prev === id ? null : id));
  };

  return (
    <section className="panel" aria-labelledby="metric-explanations-heading">
      <div className="panel__header">
        <div>
          <p className="volume-label">Technical Reference & Judge Guide</p>
          <h2 id="metric-explanations-heading" className="panel__title">Metric Definitions and Interpretation</h2>
        </div>
      </div>

      <p className="panel-lead muted" style={{ fontSize: '0.9rem', marginBottom: 'var(--space-4)' }}>
        A rigorous guide for technical judges and scientific evaluators. Each metric definition establishes its
        mathematical formula, scientific rationale, direction of improvement, and documented caveats.
      </p>

      <div className="accordion-stack" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-2)' }}>
        {METRIC_DEFINITIONS.map((m) => {
          const isExpanded = expandedId === m.id;
          return (
            <div
              key={m.id}
              className="card accordion-card"
              style={{
                border: isExpanded ? '1px solid var(--color-accent)' : 'var(--border-hairline)',
                background: 'var(--color-bg)',
                borderRadius: 'var(--radius)',
                overflow: 'hidden',
              }}
            >
              <button
                type="button"
                className="accordion-trigger"
                onClick={() => toggle(m.id)}
                aria-expanded={isExpanded}
                style={{
                  width: '100%',
                  padding: 'var(--space-3) var(--space-4)',
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  background: 'none',
                  border: 'none',
                  color: 'inherit',
                  cursor: 'pointer',
                  textAlign: 'left',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)' }}>
                  <InfoIcon size={18} style={{ color: 'var(--color-accent)' }} />
                  <div>
                    <strong style={{ fontSize: '1rem', display: 'block' }}>{m.name}</strong>
                    <span className="muted" style={{ fontSize: '0.8rem' }}>{m.direction}</span>
                  </div>
                </div>
                {isExpanded ? <ChevronUpIcon size={18} /> : <ChevronDownIcon size={18} />}
              </button>

              {isExpanded && (
                <div
                  className="accordion-content"
                  style={{
                    padding: 'var(--space-4)',
                    borderTop: '1px solid rgba(201,169,98,0.2)',
                    fontSize: '0.88rem',
                    lineHeight: '1.5',
                  }}
                >
                  <div style={{ marginBottom: 'var(--space-3)' }}>
                    <span className="volume-label" style={{ fontSize: '0.7rem' }}>Formal Definition</span>
                    <p style={{ margin: 'var(--space-1) 0 0', fontFamily: 'var(--font-mono)', fontSize: '0.85rem' }}>
                      {m.definition}
                    </p>
                  </div>

                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 'var(--space-3)', margin: 'var(--space-3) 0' }}>
                    <div>
                      <span className="stat__label" style={{ fontSize: '0.75rem' }}>What it Measures</span>
                      <p style={{ margin: 'var(--space-1) 0 0' }}>{m.whatItMeasures}</p>
                    </div>

                    <div>
                      <span className="stat__label" style={{ fontSize: '0.75rem' }}>Units & Thresholds</span>
                      <p style={{ margin: 'var(--space-1) 0 0' }}>{m.units} · {m.thresholds}</p>
                    </div>
                  </div>

                  <div style={{ marginTop: 'var(--space-3)', padding: 'var(--space-2) var(--space-3)', background: 'rgba(201,169,98,0.06)', borderRadius: 'var(--radius)' }}>
                    <span className="stat__label" style={{ fontSize: '0.75rem', color: 'var(--color-accent)' }}>⚠️ Critical Limitations & Caveats</span>
                    <p className="muted" style={{ margin: 'var(--space-1) 0 0', fontSize: '0.82rem' }}>
                      {m.limitations}
                    </p>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
