import {
  formatCostUsd,
  formatInteger,
  formatLimit,
  formatRemaining,
  formatSeconds,
  titleize,
  UNAVAILABLE,
} from '../../utils/formatting.js';
import { Stat } from './Stat.jsx';

function CostEnforcementNote({ cost }) {
  const hardCap = cost?.provider_hard_cap_supported === true;
  return (
    <p className="panel-note">
      {hardCap
        ? 'The configured provider adapter reports a provider-enforced spending cap.'
        : 'No provider-enforced spending cap is available from the configured adapter. Every monetary limit here is enforced by the application against its own estimates.'}
    </p>
  );
}

/**
 * Renders the `GET /runs/{id}/budget` contract.
 *
 * Every value comes from the backend. Null monetary values render as
 * “Unavailable”, never as zero, and the application-enforcement/estimated-cost
 * distinction is stated in words rather than implied by a badge.
 */
export function BudgetPanel({ budget }) {
  if (!budget) {
    return <p className="panel-note">Budget information is not available for this run.</p>;
  }

  const usage = budget.usage || {};
  const runtime = budget.runtime || {};
  const cost = budget.cost || {};
  const limits = budget.limits || {};
  const queries = usage.oracle_queries;
  const evaluations = usage.candidate_evaluations;

  return (
    <div className="budget-panel">
      {budget.is_exhausted ? (
        <div className="note note--warning" role="status">
          <p className="note__title">
            <span className="note__glyph" aria-hidden="true">◑</span>
            Work stopped: {budget.stop_reason ? titleize(budget.stop_reason) : 'budget exhausted'}
          </p>
          <p className="note__body">
            Resource exhaustion ended this run. Completed observations and evaluations are
            preserved; no additional work is admitted under this budget.
          </p>
        </div>
      ) : null}

      <section className="budget-panel__section" aria-labelledby="budget-limits-heading">
        <h3 id="budget-limits-heading" className="section-heading">
          Application-enforced limits
        </h3>
        <dl className="stat-grid">
          <Stat label="Oracle queries" value={formatLimit(queries)} hint={`remaining ${formatRemaining(queries)}`} />
          <Stat
            label="Candidate evaluations"
            value={formatLimit(evaluations)}
            hint={`remaining ${formatRemaining(evaluations)}`}
          />
          <Stat
            label="Concurrent remote tasks"
            value={`${formatInteger(usage.active_remote_tasks)} / ${formatInteger(usage.max_concurrent_remote_tasks)}`}
            hint="active / maximum"
          />
          <Stat
            label="Elapsed / duration limit"
            value={`${formatSeconds(runtime.elapsed_seconds)} / ${formatInteger(limits.max_runtime_seconds)} s`}
            hint={`remaining ${formatSeconds(runtime.remaining_seconds)}`}
          />
          <Stat
            label="Generated hypotheses"
            value={formatLimit(usage.generated_hypotheses)}
            hint={`remaining ${formatRemaining(usage.generated_hypotheses)}`}
          />
          <Stat
            label="Execution mode"
            value={budget.execution_mode ? titleize(budget.execution_mode) : UNAVAILABLE}
            hint="as recorded by the backend"
          />
        </dl>
        <p className="panel-note">
          Remaining capacity is reported by the backend budget manager. It is not recalculated
          in the browser, and an attempted query is not assumed to have succeeded.
        </p>
      </section>

      <section className="budget-panel__section" aria-labelledby="budget-cost-heading">
        <h3 id="budget-cost-heading" className="section-heading">
          Compute spending
        </h3>
        <dl className="stat-grid">
          <Stat
            label="Estimated cost"
            value={formatCostUsd(cost.estimated_cost_usd)}
            hint="application-side estimate"
          />
          <Stat
            label="Accruing cost"
            value={formatCostUsd(cost.accruing_cost_usd)}
            hint="live provider-reported state, if any"
          />
          <Stat
            label="Settled cost"
            value={formatCostUsd(cost.settled_cost_usd)}
            hint="finalised provider charge, if any"
          />
          <Stat
            label="Cost data state"
            value={titleize(cost.data_state)}
            hint="as reported by the backend"
          />
        </dl>
        <CostEnforcementNote cost={cost} />
      </section>

      {Array.isArray(budget.limitations) && budget.limitations.length > 0 ? (
        <section className="budget-panel__section" aria-labelledby="budget-limitations-heading">
          <h3 id="budget-limitations-heading" className="section-heading">
            Known limitations
          </h3>
          <ul className="prose-list">
            {budget.limitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
