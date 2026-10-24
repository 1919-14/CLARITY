import { useState } from 'react';
import {
  formatDecimal,
  formatInteger,
  UNAVAILABLE,
} from '../../utils/formatting.js';
import {
  CheckCircleIcon,
  HelpCircleIcon,
  InfoIcon,
} from '../icons.jsx';

export function MeasuredPerformanceGrid({
  measurement,
  benchmarkResult,
  selectedRun,
}) {
  const [selectedK, setSelectedK] = useState('1'); // '1' | '3' | '5'

  // Extract from measurement if available (PerformanceMeasurementService contract),
  // otherwise fallback to BenchmarkResultModel serialization.
  const accuracy = measurement?.accuracy;
  const efficiency = measurement?.query_efficiency;
  const runtime = measurement?.runtime;
  const cost = measurement?.cost;

  // 1. Hit@K
  const hit1 = accuracy?.hit_at_1?.value ?? benchmarkResult?.hit_at_1;
  const hit3 = accuracy?.hit_at_3?.value ?? benchmarkResult?.hit_at_3;
  const hit5 = accuracy?.hit_at_5?.value ?? benchmarkResult?.hit_at_5;
  const selectedHitVal = selectedK === '1' ? hit1 : selectedK === '3' ? hit3 : hit5;
  const hitSampleCount = accuracy?.hit_at_1?.sample_count ?? benchmarkResult?.sample_count ?? benchmarkResult?.evaluated_case_count ?? 0;

  // 2. MRR
  const mrrVal = accuracy?.mrr?.value ?? benchmarkResult?.reciprocal_rank ?? benchmarkResult?.mean_reciprocal_rank;
  const mrrSampleCount = accuracy?.mrr?.sample_count ?? benchmarkResult?.sample_count ?? benchmarkResult?.evaluated_case_count ?? 0;

  // 3. Held-out Error
  const maeVal = accuracy?.held_out_mae?.value ?? benchmarkResult?.held_out_mae;
  const rmseVal = accuracy?.held_out_rmse?.value ?? benchmarkResult?.held_out_rmse;

  // 4. Queries-to-success
  const queriesExecuted = efficiency?.queries_executed ?? benchmarkResult?.queries_executed ?? benchmarkResult?.queries_to_target;
  const queryBudget = efficiency?.query_budget ?? benchmarkResult?.query_budget ?? benchmarkResult?.max_query_budget;
  const queriesToTarget = efficiency?.queries_to_target ?? benchmarkResult?.queries_to_target;
  const targetReached = efficiency?.target_reached ?? (queriesToTarget !== null && queriesToTarget !== undefined);
  const successThreshold = efficiency?.success_threshold ?? benchmarkResult?.success_threshold;

  // 5. Complexity & Uncertainty
  const complexityVal = accuracy?.model_complexity?.value ?? benchmarkResult?.complexity_score;
  const uncertaintyVal = accuracy?.residual_variance?.value ?? accuracy?.residual_variance?.uncertainty ?? benchmarkResult?.uncertainty_score;

  // 6. Cost & Runtime
  const costState = cost?.cost_state ?? benchmarkResult?.cost_status;
  const settledCost = cost?.settled_cost_usd;
  const estimatedCost = cost?.estimated_cost_usd;
  const costVal = settledCost ?? estimatedCost ?? benchmarkResult?.cost_estimate_usd;
  const wallClockMs = runtime?.total_wall_clock_ms ?? benchmarkResult?.duration_ms;
  const localProcessingMs = runtime?.local_processing_ms;

  const isEvaluated = Boolean(measurement || benchmarkResult);

  return (
    <section className="panel" aria-labelledby="measured-performance-heading">
      <div className="panel__header">
        <div>
          <p className="volume-label">Empirical Discovery Metrics</p>
          <h2 id="measured-performance-heading" className="panel__title">Current Measured Performance</h2>
        </div>
        {!isEvaluated && (
          <span className="badge badge--neutral">Not evaluated</span>
        )}
      </div>

      <p className="panel-lead muted" style={{ fontSize: '0.9rem', marginBottom: 'var(--space-4)' }}>
        Empirical metrics captured directly from ground-truth benchmark executions. Values are computed
        by evaluating discovered hypotheses against held-out oracle partitions without exposing secret parameters.
      </p>

      <div className="metrics-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 'var(--space-4)' }}>
        {/* Metric 1: Hit@K */}
        <div className="card metric-card" style={{ padding: 'var(--space-4)', display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span className="stat__label" style={{ fontWeight: 600, fontSize: '0.95rem' }}>Hit@K Discovery Recall</span>
              <span className="badge badge--success" style={{ fontSize: '0.7rem' }}>Higher is better ↑</span>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', margin: 'var(--space-3) 0' }}>
              <span className="stat__value" style={{ fontSize: '2rem', fontFamily: 'var(--font-mono)' }}>
                {selectedHitVal !== null && selectedHitVal !== undefined ? formatDecimal(selectedHitVal, 3) : UNAVAILABLE}
              </span>
              <div style={{ display: 'flex', gap: 'var(--space-1)' }}>
                {['1', '3', '5'].map((k) => (
                  <button
                    key={k}
                    type="button"
                    className={`button button--small ${selectedK === k ? 'button--primary' : 'button--ghost'}`}
                    style={{ minHeight: '26px', padding: '0 8px', fontSize: '0.7rem' }}
                    onClick={() => setSelectedK(k)}
                  >
                    K={k}
                  </button>
                ))}
              </div>
            </div>

            <p className="muted" style={{ fontSize: '0.82rem', margin: 0 }}>
              Measures whether a mathematically equivalent hypothesis appears in the top <strong>K={selectedK}</strong> ranked candidates.
              {hitSampleCount > 0 && ` Evaluated across ${hitSampleCount} benchmark cases.`}
            </p>
          </div>

          <div style={{ borderTop: 'var(--border-hairline)', paddingTop: 'var(--space-2)', marginTop: 'var(--space-3)', fontSize: '0.78rem' }} className="muted">
            Hit@1: <strong>{formatDecimal(hit1, 2)}</strong> · Hit@3: <strong>{formatDecimal(hit3, 2)}</strong> · Hit@5: <strong>{formatDecimal(hit5, 2)}</strong>
          </div>
        </div>

        {/* Metric 2: Mean Reciprocal Rank (MRR) */}
        <div className="card metric-card" style={{ padding: 'var(--space-4)', display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span className="stat__label" style={{ fontWeight: 600, fontSize: '0.95rem' }}>Mean Reciprocal Rank (MRR)</span>
              <span className="badge badge--success" style={{ fontSize: '0.7rem' }}>Higher is better ↑</span>
            </div>

            <div style={{ margin: 'var(--space-3) 0' }}>
              <span className="stat__value" style={{ fontSize: '2rem', fontFamily: 'var(--font-mono)' }}>
                {mrrVal !== null && mrrVal !== undefined ? formatDecimal(mrrVal, 4) : UNAVAILABLE}
              </span>
            </div>

            <p className="muted" style={{ fontSize: '0.82rem', margin: 0 }}>
              Quantifies how near the top of the scientific ranking the first valid candidate appears: MRR = (1/N) · Σ (1 / rank_i).
              Values near 1.0 indicate correct explanations consistently rank first.
            </p>
          </div>

          <div style={{ borderTop: 'var(--border-hairline)', paddingTop: 'var(--space-2)', marginTop: 'var(--space-3)', fontSize: '0.78rem' }} className="muted">
            Sample Cases: <strong>{mrrSampleCount > 0 ? formatInteger(mrrSampleCount) : 'Single trial'}</strong> · Target: <strong>1.0000</strong>
          </div>
        </div>

        {/* Metric 3: Held-out Predictive Error */}
        <div className="card metric-card" style={{ padding: 'var(--space-4)', display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span className="stat__label" style={{ fontWeight: 600, fontSize: '0.95rem' }}>Held-out Predictive Error</span>
              <span className="badge badge--danger" style={{ fontSize: '0.7rem' }}>Lower is better ↓</span>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--space-2)', margin: 'var(--space-3) 0' }}>
              <div>
                <span className="stat__label" style={{ fontSize: '0.75rem' }}>MAE (y_units)</span>
                <span className="stat__value" style={{ fontSize: '1.4rem', fontFamily: 'var(--font-mono)', display: 'block' }}>
                  {maeVal !== null && maeVal !== undefined ? formatDecimal(maeVal, 4) : UNAVAILABLE}
                </span>
              </div>
              <div>
                <span className="stat__label" style={{ fontSize: '0.75rem' }}>RMSE (y_units)</span>
                <span className="stat__value" style={{ fontSize: '1.4rem', fontFamily: 'var(--font-mono)', display: 'block' }}>
                  {rmseVal !== null && rmseVal !== undefined ? formatDecimal(rmseVal, 4) : UNAVAILABLE}
                </span>
              </div>
            </div>

            <p className="muted" style={{ fontSize: '0.82rem', margin: 0 }}>
              Mean Absolute Error and Root Mean Squared Error on held-out validation points. Lower error indicates superior generalizability
              beyond the observations selected during discovery.
            </p>
          </div>

          <div style={{ borderTop: 'var(--border-hairline)', paddingTop: 'var(--space-2)', marginTop: 'var(--space-3)', fontSize: '0.78rem' }} className="muted">
            Units: <strong>dimensionless response (y_units)</strong> · Partition: <strong>Held-out only</strong>
          </div>
        </div>

        {/* Metric 4: Queries-to-Success */}
        <div className="card metric-card" style={{ padding: 'var(--space-4)', display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span className="stat__label" style={{ fontWeight: 600, fontSize: '0.95rem' }}>Queries-to-Success</span>
              <span className="badge badge--danger" style={{ fontSize: '0.7rem' }}>Lower is better ↓</span>
            </div>

            <div style={{ margin: 'var(--space-3) 0' }}>
              <span className="stat__value" style={{ fontSize: '2rem', fontFamily: 'var(--font-mono)' }}>
                {queriesToTarget !== null && queriesToTarget !== undefined ? formatInteger(queriesToTarget) : UNAVAILABLE}
              </span>
              <span className="muted" style={{ fontSize: '0.85rem', marginLeft: 'var(--space-2)' }}>
                / {queryBudget !== null && queryBudget !== undefined ? `${queryBudget} budget` : 'unbounded'}
              </span>
            </div>

            <p className="muted" style={{ fontSize: '0.82rem', margin: 0 }}>
              Number of oracle queries required before a hypothesis satisfied the predefined scientific accuracy target.
              {targetReached ? (
                <span style={{ color: 'var(--color-success)', display: 'block', marginTop: 'var(--space-1)' }}>
                  ✓ Target reached within query budget.
                </span>
              ) : (
                <span style={{ color: 'var(--color-warning)', display: 'block', marginTop: 'var(--space-1)' }}>
                  Target not reached within budget ceiling.
                </span>
              )}
            </p>
          </div>

          <div style={{ borderTop: 'var(--border-hairline)', paddingTop: 'var(--space-2)', marginTop: 'var(--space-3)', fontSize: '0.78rem' }} className="muted">
            Success Threshold: <strong>{successThreshold !== undefined ? `MAE ≤ ${successThreshold}` : 'MAE ≤ 0.05'}</strong> · Executed: <strong>{queriesExecuted ?? UNAVAILABLE}</strong>
          </div>
        </div>

        {/* Metric 5: Complexity & Uncertainty */}
        <div className="card metric-card" style={{ padding: 'var(--space-4)', display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span className="stat__label" style={{ fontWeight: 600, fontSize: '0.95rem' }}>Complexity & Uncertainty</span>
              <span className="badge badge--neutral" style={{ fontSize: '0.7rem' }}>Structural Parsimony</span>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--space-2)', margin: 'var(--space-3) 0' }}>
              <div>
                <span className="stat__label" style={{ fontSize: '0.75rem' }}>Model Complexity</span>
                <span className="stat__value" style={{ fontSize: '1.4rem', fontFamily: 'var(--font-mono)', display: 'block' }}>
                  {complexityVal !== null && complexityVal !== undefined ? formatInteger(complexityVal) : UNAVAILABLE}
                </span>
              </div>
              <div>
                <span className="stat__label" style={{ fontSize: '0.75rem' }}>Residual Variance</span>
                <span className="stat__value" style={{ fontSize: '1.4rem', fontFamily: 'var(--font-mono)', display: 'block' }}>
                  {uncertaintyVal !== null && uncertaintyVal !== undefined ? formatDecimal(uncertaintyVal, 4) : UNAVAILABLE}
                </span>
              </div>
            </div>

            <p className="muted" style={{ fontSize: '0.82rem', margin: 0 }}>
              Complexity reflects active free parameters and degree. Caveat: Complexity is structural within its model family
              and cannot be compared as a universal metric across incompatible model spaces.
            </p>
          </div>

          <div style={{ borderTop: 'var(--border-hairline)', paddingTop: 'var(--space-2)', marginTop: 'var(--space-3)', fontSize: '0.78rem' }} className="muted">
            Uncertainty: <strong>{uncertaintyVal !== null && uncertaintyVal !== undefined ? 'Quantified' : UNAVAILABLE}</strong>
          </div>
        </div>

        {/* Metric 6: Compute Cost & Wall-clock Runtime */}
        <div className="card metric-card" style={{ padding: 'var(--space-4)', display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
          <div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span className="stat__label" style={{ fontWeight: 600, fontSize: '0.95rem' }}>Compute Cost & Runtime</span>
              <span className={`badge badge--${costState === 'settled' ? 'success' : 'neutral'}`} style={{ fontSize: '0.7rem' }}>
                {costState ? `Cost: ${costState}` : 'Unmeasured'}
              </span>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--space-2)', margin: 'var(--space-3) 0' }}>
              <div>
                <span className="stat__label" style={{ fontSize: '0.75rem' }}>Compute Cost</span>
                <span className="stat__value" style={{ fontSize: '1.4rem', fontFamily: 'var(--font-mono)', display: 'block' }}>
                  {costVal !== null && costVal !== undefined ? `$${costVal.toFixed(4)}` : UNAVAILABLE}
                </span>
              </div>
              <div>
                <span className="stat__label" style={{ fontSize: '0.75rem' }}>Wall-clock Duration</span>
                <span className="stat__value" style={{ fontSize: '1.4rem', fontFamily: 'var(--font-mono)', display: 'block' }}>
                  {wallClockMs !== null && wallClockMs !== undefined ? `${(wallClockMs / 1000).toFixed(2)}s` : UNAVAILABLE}
                </span>
              </div>
            </div>

            <p className="muted" style={{ fontSize: '0.82rem', margin: 0 }}>
              Measured wall-clock duration of the benchmark discovery process and compute expenditure calculated from documented
              local/remote task pricing. Missing cost data is never coerced to zero.
            </p>
          </div>

          <div style={{ borderTop: 'var(--border-hairline)', paddingTop: 'var(--space-2)', marginTop: 'var(--space-3)', fontSize: '0.78rem' }} className="muted">
            Timing method: <strong>Monotonic clock</strong> · Rate: <strong>Documented rates</strong>
          </div>
        </div>
      </div>
    </section>
  );
}
