import {
  formatDecimal,
  formatInteger,
  UNAVAILABLE,
} from '../../utils/formatting.js';
import {
  AlertTriangleIcon,
  CheckCircleIcon,
  TrendingDownIcon,
  TrendingUpIcon,
  WarningIcon,
} from '../icons.jsx';

export function ActiveBaselineComparison({
  comparisonReport,
  comparisonSummary,
  scenarioName,
}) {
  // Support either StrategyComparisonReport or BenchmarkComparisonSummary
  const hasComparison = Boolean(comparisonReport || comparisonSummary);

  const activeAgg =
    comparisonReport?.active_aggregates ||
    comparisonSummary?.active_stats ||
    comparisonSummary?.active_summary ||
    comparisonSummary?.active;
  const baselineAgg =
    comparisonReport?.baseline_aggregates ||
    comparisonSummary?.random_stats ||
    comparisonSummary?.baseline_summary ||
    comparisonSummary?.random;
  const metricComparisons =
    comparisonReport?.metric_comparisons ||
    comparisonSummary?.differences ||
    {};
  const efficiencyRatio =
    comparisonReport?.efficiency_gain_ratio ||
    comparisonSummary?.efficiency_gain ||
    comparisonSummary?.efficiency_gain_ratio;
  const claimsAdvantage =
    comparisonReport?.claims_advantage ??
    comparisonSummary?.claims_advantage ??
    (efficiencyRatio && efficiencyRatio !== 'N/A');
  const advantageSummary =
    comparisonReport?.advantage_summary ||
    comparisonSummary?.summary_statement ||
    comparisonSummary?.advantage_summary;
  const pairedTrials =
    comparisonReport?.paired_trials_count ||
    comparisonSummary?.paired_trials_count ||
    comparisonSummary?.trial_count ||
    activeAgg?.total_trials ||
    activeAgg?.total_runs ||
    0;
  const queryBudget =
    comparisonReport?.query_budget_per_trial ||
    comparisonSummary?.query_budget_per_trial ||
    comparisonSummary?.budget ||
    activeAgg?.max_query_budget;

  // Check if baseline is genuinely executed or missing
  const hasBaseline = Boolean(
    baselineAgg &&
      (baselineAgg.total_trials > 0 ||
        baselineAgg.total_runs > 0 ||
        baselineAgg.mean_queries_used > 0 ||
        baselineAgg.queries_to_target > 0 ||
        (baselineAgg.mean_held_out_mae !== null && baselineAgg.mean_held_out_mae !== undefined) ||
        (baselineAgg.held_out_mae !== null && baselineAgg.held_out_mae !== undefined) ||
        baselineAgg.hit_at_1 !== undefined ||
        baselineAgg.hit_at_1_rate !== undefined),
  );

  return (
    <section className="panel" aria-labelledby="active-baseline-heading">
      <div className="panel__header">
        <div>
          <p className="volume-label">Controlled Comparison</p>
          <h2 id="active-baseline-heading" className="panel__title">Active Discovery vs. Passive Baseline</h2>
        </div>
        {hasComparison && hasBaseline ? (
          <span className="badge badge--success">Controlled Evaluation</span>
        ) : (
          <span className="badge badge--warning">Baseline Missing</span>
        )}
      </div>

      <p className="panel-lead muted" style={{ fontSize: '0.9rem', marginBottom: 'var(--space-4)' }}>
        Empirical comparison of CLARITY's active experiment selection strategy against a random exploration baseline
        under identical oracle query budgets, noise levels, and evaluation criteria.
      </p>

      {!hasComparison || !hasBaseline ? (
        <div className="empty-state" style={{ padding: 'var(--space-5)', textAlign: 'center', background: 'var(--color-bg)', border: 'var(--border-hairline)', borderRadius: 'var(--radius)' }}>
          <AlertTriangleIcon size={32} style={{ color: 'var(--color-warning)', marginBottom: 'var(--space-2)' }} />
          <h3 style={{ margin: '0 0 var(--space-2)', fontSize: '1.1rem' }}>Baseline measurements unavailable</h3>
          <p className="muted" style={{ maxWidth: '36rem', margin: '0 auto var(--space-4)', fontSize: '0.9rem' }}>
            A compatible baseline evaluation has not been executed for this scenario. CLARITY never presents
            an unmeasured baseline as evidence of superior efficiency. Execute a controlled benchmark comparison
            to establish empirical performance against random sampling.
          </p>
        </div>
      ) : (
        <div>
          {/* Executive Summary Banner */}
          <div
            className={`card ${claimsAdvantage ? 'card--success' : 'card--neutral'}`}
            style={{
              padding: 'var(--space-4)',
              marginBottom: 'var(--space-4)',
              borderLeft: claimsAdvantage ? '4px solid var(--color-success)' : '4px solid var(--color-accent)',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 'var(--space-3)' }}>
              <div>
                <span className="volume-label">Advantage Assessment</span>
                <h3 style={{ margin: 'var(--space-1) 0', fontSize: '1.15rem' }}>
                  {advantageSummary || (claimsAdvantage ? 'Active Strategy Shows Measured Advantage' : 'Inconclusive / Parity Result')}
                </h3>
                <p className="muted" style={{ fontSize: '0.85rem', margin: 0 }}>
                  Evaluated across <strong>{pairedTrials} paired trial{pairedTrials === 1 ? '' : 's'}</strong> with a strict budget of{' '}
                  <strong>{queryBudget || UNAVAILABLE} queries per trial</strong>.
                </p>
              </div>

              {efficiencyRatio && (
                <div style={{ textAlign: 'right' }}>
                  <span className="stat__label" style={{ fontSize: '0.75rem' }}>Query Efficiency Ratio</span>
                  <span className="stat__value" style={{ fontSize: '1.6rem', color: 'var(--color-accent)', display: 'block', fontFamily: 'var(--font-mono)' }}>
                    {efficiencyRatio}
                  </span>
                  <span className="stat__hint muted" style={{ fontSize: '0.75rem' }}>Active vs. Random</span>
                </div>
              )}
            </div>
          </div>

          {/* Metric-by-metric Comparison Table */}
          <div className="table-scroll">
            <table className="data-table data-table--compact">
              <caption className="visually-hidden">Active strategy versus random baseline metric comparison</caption>
              <thead>
                <tr>
                  <th scope="col">Scientific Metric</th>
                  <th scope="col">Active Strategy</th>
                  <th scope="col">Random Baseline</th>
                  <th scope="col">Absolute Difference</th>
                  <th scope="col">Relative Change</th>
                  <th scope="col">Better Direction</th>
                  <th scope="col">Advantage</th>
                </tr>
              </thead>
              <tbody>
                {/* 1. Queries to Target */}
                <tr>
                  <td>
                    <strong>Queries to Target</strong>
                    <span className="block-note muted">Queries required to reach accuracy threshold</span>
                  </td>
                  <td>
                    <strong>
                      {activeAgg?.mean_queries_to_target !== null && activeAgg?.mean_queries_to_target !== undefined
                        ? formatDecimal(activeAgg.mean_queries_to_target, 1)
                        : formatDecimal(activeAgg?.mean_queries_used, 1)}
                    </strong>
                  </td>
                  <td>
                    {baselineAgg?.mean_queries_to_target !== null && baselineAgg?.mean_queries_to_target !== undefined
                      ? formatDecimal(baselineAgg.mean_queries_to_target, 1)
                      : formatDecimal(baselineAgg?.mean_queries_used, 1)}
                  </td>
                  <td>
                    {metricComparisons['queries_to_target']?.absolute_difference !== undefined && metricComparisons['queries_to_target']?.absolute_difference !== null
                      ? formatDecimal(metricComparisons['queries_to_target'].absolute_difference, 2)
                      : calculateAbsDiff(activeAgg?.mean_queries_to_target, baselineAgg?.mean_queries_to_target)}
                  </td>
                  <td>
                    {renderRelativeChange(metricComparisons['queries_to_target'], activeAgg?.mean_queries_to_target, baselineAgg?.mean_queries_to_target, 'lower_is_better')}
                  </td>
                  <td>
                    <span className="badge badge--neutral">Lower is better ↓</span>
                  </td>
                  <td>
                    {renderAdvantageBadge(metricComparisons['queries_to_target']?.better_strategy, activeAgg?.mean_queries_to_target, baselineAgg?.mean_queries_to_target, 'lower')}
                  </td>
                </tr>

                {/* 2. Success Rate / Target Reached */}
                <tr>
                  <td>
                    <strong>Success Rate</strong>
                    <span className="block-note muted">Proportion of trials satisfying target</span>
                  </td>
                  <td>
                    <strong>
                      {activeAgg?.success_rate !== undefined ? `${(activeAgg.success_rate * 100).toFixed(1)}%` : UNAVAILABLE}
                    </strong>
                    <span className="block-note muted">
                      {activeAgg?.successful_trials ?? 0}/{activeAgg?.total_trials ?? 0}
                    </span>
                  </td>
                  <td>
                    {baselineAgg?.success_rate !== undefined ? `${(baselineAgg.success_rate * 100).toFixed(1)}%` : UNAVAILABLE}
                    <span className="block-note muted">
                      {baselineAgg?.successful_trials ?? 0}/{baselineAgg?.total_trials ?? 0}
                    </span>
                  </td>
                  <td>
                    {activeAgg?.success_rate !== undefined && baselineAgg?.success_rate !== undefined
                      ? `${((activeAgg.success_rate - baselineAgg.success_rate) * 100).toFixed(1)}%`
                      : UNAVAILABLE}
                  </td>
                  <td>
                    {renderRelativeChange(metricComparisons['success_rate'], activeAgg?.success_rate, baselineAgg?.success_rate, 'higher_is_better')}
                  </td>
                  <td>
                    <span className="badge badge--neutral">Higher is better ↑</span>
                  </td>
                  <td>
                    {renderAdvantageBadge(metricComparisons['success_rate']?.better_strategy, activeAgg?.success_rate, baselineAgg?.success_rate, 'higher')}
                  </td>
                </tr>

                {/* 3. Held-out MAE */}
                <tr>
                  <td>
                    <strong>Held-out MAE</strong>
                    <span className="block-note muted">Mean Absolute Error on unobserved points</span>
                  </td>
                  <td>
                    <strong>{formatDecimal(activeAgg?.mean_held_out_mae, 4)}</strong>
                  </td>
                  <td>{formatDecimal(baselineAgg?.mean_held_out_mae, 4)}</td>
                  <td>
                    {metricComparisons['held_out_mae']?.absolute_difference !== undefined && metricComparisons['held_out_mae']?.absolute_difference !== null
                      ? formatDecimal(metricComparisons['held_out_mae'].absolute_difference, 4)
                      : calculateAbsDiff(activeAgg?.mean_held_out_mae, baselineAgg?.mean_held_out_mae)}
                  </td>
                  <td>
                    {renderRelativeChange(metricComparisons['held_out_mae'], activeAgg?.mean_held_out_mae, baselineAgg?.mean_held_out_mae, 'lower_is_better')}
                  </td>
                  <td>
                    <span className="badge badge--neutral">Lower is better ↓</span>
                  </td>
                  <td>
                    {renderAdvantageBadge(metricComparisons['held_out_mae']?.better_strategy, activeAgg?.mean_held_out_mae, baselineAgg?.mean_held_out_mae, 'lower')}
                  </td>
                </tr>

                {/* 4. Hit@1 Discovery Rate */}
                <tr>
                  <td>
                    <strong>Hit@1 Rate</strong>
                    <span className="block-note muted">Proportion with exact match at Rank 1</span>
                  </td>
                  <td>
                    <strong>
                      {activeAgg?.hit_at_1_rate !== undefined ? `${(activeAgg.hit_at_1_rate * 100).toFixed(1)}%` : UNAVAILABLE}
                    </strong>
                  </td>
                  <td>
                    {baselineAgg?.hit_at_1_rate !== undefined ? `${(baselineAgg.hit_at_1_rate * 100).toFixed(1)}%` : UNAVAILABLE}
                  </td>
                  <td>
                    {activeAgg?.hit_at_1_rate !== undefined && baselineAgg?.hit_at_1_rate !== undefined
                      ? `${((activeAgg.hit_at_1_rate - baselineAgg.hit_at_1_rate) * 100).toFixed(1)}%`
                      : UNAVAILABLE}
                  </td>
                  <td>
                    {renderRelativeChange(metricComparisons['hit_at_1'], activeAgg?.hit_at_1_rate, baselineAgg?.hit_at_1_rate, 'higher_is_better')}
                  </td>
                  <td>
                    <span className="badge badge--neutral">Higher is better ↑</span>
                  </td>
                  <td>
                    {renderAdvantageBadge(metricComparisons['hit_at_1']?.better_strategy, activeAgg?.hit_at_1_rate, baselineAgg?.hit_at_1_rate, 'higher')}
                  </td>
                </tr>

                {/* 5. Mean Reciprocal Rank (MRR) */}
                <tr>
                  <td>
                    <strong>Mean Reciprocal Rank (MRR)</strong>
                    <span className="block-note muted">Inverse rank of first valid explanation</span>
                  </td>
                  <td>
                    <strong>{formatDecimal(activeAgg?.mean_mrr, 4)}</strong>
                  </td>
                  <td>{formatDecimal(baselineAgg?.mean_mrr, 4)}</td>
                  <td>
                    {metricComparisons['mrr']?.absolute_difference !== undefined && metricComparisons['mrr']?.absolute_difference !== null
                      ? formatDecimal(metricComparisons['mrr'].absolute_difference, 4)
                      : calculateAbsDiff(activeAgg?.mean_mrr, baselineAgg?.mean_mrr)}
                  </td>
                  <td>
                    {renderRelativeChange(metricComparisons['mrr'], activeAgg?.mean_mrr, baselineAgg?.mean_mrr, 'higher_is_better')}
                  </td>
                  <td>
                    <span className="badge badge--neutral">Higher is better ↑</span>
                  </td>
                  <td>
                    {renderAdvantageBadge(metricComparisons['mrr']?.better_strategy, activeAgg?.mean_mrr, baselineAgg?.mean_mrr, 'higher')}
                  </td>
                </tr>

                {/* 6. Total Wall-clock Runtime */}
                <tr>
                  <td>
                    <strong>Wall-clock Duration</strong>
                    <span className="block-note muted">Elapsed time to discovery or budget exhaustion</span>
                  </td>
                  <td>
                    <strong>
                      {activeAgg?.mean_wall_clock_ms !== undefined ? `${(activeAgg.mean_wall_clock_ms / 1000).toFixed(2)}s` : UNAVAILABLE}
                    </strong>
                  </td>
                  <td>
                    {baselineAgg?.mean_wall_clock_ms !== undefined ? `${(baselineAgg.mean_wall_clock_ms / 1000).toFixed(2)}s` : UNAVAILABLE}
                  </td>
                  <td>
                    {activeAgg?.mean_wall_clock_ms !== undefined && baselineAgg?.mean_wall_clock_ms !== undefined
                      ? `${((activeAgg.mean_wall_clock_ms - baselineAgg.mean_wall_clock_ms) / 1000).toFixed(2)}s`
                      : UNAVAILABLE}
                  </td>
                  <td>
                    {renderRelativeChange(metricComparisons['wall_clock_ms'], activeAgg?.mean_wall_clock_ms, baselineAgg?.mean_wall_clock_ms, 'lower_is_better')}
                  </td>
                  <td>
                    <span className="badge badge--neutral">Lower is better ↓</span>
                  </td>
                  <td>
                    {renderAdvantageBadge(metricComparisons['wall_clock_ms']?.better_strategy, activeAgg?.mean_wall_clock_ms, baselineAgg?.mean_wall_clock_ms, 'lower')}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <p className="muted" style={{ fontSize: '0.8rem', marginTop: 'var(--space-3)' }}>
            ⚠️ <strong>Statistical note:</strong> Relative changes are suppressed when baseline measurements equal zero to prevent
            division-by-zero distortion. Differences are reported under identical noise seeds and budget ceilings.
          </p>
        </div>
      )}
    </section>
  );
}

function calculateAbsDiff(active, baseline) {
  if (active === null || active === undefined || baseline === null || baseline === undefined) {
    return UNAVAILABLE;
  }
  const diff = active - baseline;
  return formatDecimal(diff, 4);
}

function renderRelativeChange(detail, active, baseline, direction) {
  if (detail?.relative_change_valid && detail.relative_change_pct !== null && detail.relative_change_pct !== undefined) {
    const pct = detail.relative_change_pct;
    return `${pct > 0 ? '+' : ''}${pct.toFixed(1)}%`;
  }
  if (detail?.relative_pct_change !== undefined && detail.relative_pct_change !== null) {
    const pct = detail.relative_pct_change;
    return `${pct > 0 ? '+' : ''}${pct.toFixed(1)}%`;
  }
  if (active === null || active === undefined || baseline === null || baseline === undefined) {
    return <span className="muted">{UNAVAILABLE}</span>;
  }
  if (Math.abs(baseline) < 1e-12) {
    return <span className="muted" title="Baseline is 0; relative percentage undefined">Undefined</span>;
  }
  const diff = direction === 'lower_is_better' ? (baseline - active) / Math.abs(baseline) : (active - baseline) / Math.abs(baseline);
  const pct = diff * 100.0;
  return `${pct > 0 ? '+' : ''}${pct.toFixed(1)}%`;
}

function renderAdvantageBadge(betterStrategy, activeVal, baselineVal, lowerOrHigher) {
  if (betterStrategy) {
    if (betterStrategy === 'active') return <span className="badge badge--success">Active Strategy</span>;
    if (betterStrategy === 'baseline') return <span className="badge badge--warning">Random Baseline</span>;
    if (betterStrategy === 'tie') return <span className="badge badge--neutral">Tie / Parity</span>;
    return <span className="badge badge--neutral">Inconclusive</span>;
  }

  if (activeVal === null || activeVal === undefined || baselineVal === null || baselineVal === undefined) {
    return <span className="muted">{UNAVAILABLE}</span>;
  }

  if (lowerOrHigher === 'lower') {
    if (activeVal < baselineVal) return <span className="badge badge--success">Active Strategy</span>;
    if (activeVal > baselineVal) return <span className="badge badge--warning">Random Baseline</span>;
    return <span className="badge badge--neutral">Parity</span>;
  } else {
    if (activeVal > baselineVal) return <span className="badge badge--success">Active Strategy</span>;
    if (activeVal < baselineVal) return <span className="badge badge--warning">Random Baseline</span>;
    return <span className="badge badge--neutral">Parity</span>;
  }
}
