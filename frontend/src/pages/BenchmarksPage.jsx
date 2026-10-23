import { useCallback, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import {
  getRunBenchmarks,
  listBenchmarkScenarios,
} from '../api/runs.js';
import { EmptyState } from '../components/feedback/EmptyState.jsx';
import { ErrorState } from '../components/feedback/ErrorState.jsx';
import { LoadingState } from '../components/feedback/LoadingState.jsx';
import { RefreshIcon } from '../components/icons.jsx';
import { OrnamentDivider, PageHeader } from '../components/layout/PageHeader.jsx';
import { RunPicker } from '../components/runs/RunPicker.jsx';
import { useResource } from '../hooks/useResource.js';
import { useRuns } from '../hooks/useRuns.js';
import {
  formatDateTime,
  formatDecimal,
  formatInteger,
  isUnavailable,
  UNAVAILABLE,
} from '../utils/formatting.js';

export function BenchmarksPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { runs, isLoading: runsLoading, error: runsError, reload: reloadRuns } = useRuns({ limit: 50 });
  const [selectedRunId, setSelectedRunId] = useState(searchParams.get('run') || '');
  const runId = selectedRunId || runs?.[0]?.run_id || '';

  const scenariosLoader = useCallback(({ signal }) => listBenchmarkScenarios({ signal }), []);
  const {
    data: scenarioData,
    isLoading: scenariosLoading,
    error: scenariosError,
    reload: reloadScenarios,
  } = useResource(scenariosLoader);

  const resultsLoader = useCallback(({ signal }) => getRunBenchmarks(runId, { signal }), [runId]);
  const {
    data: resultData,
    isLoading: resultsLoading,
    isRefreshing: resultsRefreshing,
    error: resultsError,
    reload: reloadResults,
  } = useResource(resultsLoader, { enabled: Boolean(runId) });

  const scenarios = scenarioData?.scenarios ?? [];
  const results = resultData?.benchmarks ?? [];

  const changeRun = (nextRunId) => {
    setSelectedRunId(nextRunId);
    setSearchParams(nextRunId ? { run: nextRunId } : {}, { replace: true });
  };

  return (
    <div className="page-inner">
      <PageHeader
        volume="Volume VIII · Benchmarks"
        title="Benchmarks"
        description="Registered benchmark scenarios and the results the backend stored for a run. Ground-truth targets are held by the evaluator and are never requested or displayed here."
      />
      <OrnamentDivider />

      <section className="panel" aria-labelledby="scenarios-heading">
        <div className="panel__header">
          <div>
            <p className="volume-label">I · Scenario register</p>
            <h2 id="scenarios-heading" className="panel__title">Registered scenarios</h2>
          </div>
          <button
            type="button"
            className="button button--ghost button--small"
            onClick={reloadScenarios}
            disabled={scenariosLoading}
          >
            <RefreshIcon size={16} />
            <span>Refresh</span>
          </button>
        </div>

        {scenariosLoading ? <LoadingState label="Reading benchmark scenarios…" compact /> : null}
        {!scenariosLoading && scenariosError ? (
          <ErrorState compact title="Scenarios could not be retrieved" error={scenariosError} onRetry={reloadScenarios} />
        ) : null}
        {!scenariosLoading && !scenariosError && scenarios.length === 0 ? (
          <EmptyState title="No benchmark scenarios" message="The backend returned an empty scenario register." />
        ) : null}
        {!scenariosLoading && !scenariosError && scenarios.length > 0 ? (
          <div className="table-scroll">
            <table className="data-table">
              <caption className="visually-hidden">Registered benchmark scenarios</caption>
              <thead>
                <tr>
                  <th scope="col">Scenario</th>
                  <th scope="col">Challenge</th>
                  <th scope="col">Domain</th>
                  <th scope="col">Query budget</th>
                  <th scope="col">Held-out samples</th>
                </tr>
              </thead>
              <tbody>
                {scenarios.map((scenario) => (
                  <tr key={scenario.scenario_id}>
                    <td data-label="Scenario">
                      <code className="id-text">{scenario.scenario_id}</code>
                      <span className="block-note muted">{scenario.description}</span>
                    </td>
                    <td data-label="Challenge">{scenario.challenge_type}</td>
                    <td data-label="Domain">
                      {scenario.domain_bounds?.domain_name}
                      <span className="block-note muted">
                        {scenario.domain_bounds?.variable_name} ∈ [{scenario.domain_bounds?.min_val},{' '}
                        {scenario.domain_bounds?.max_val}]
                      </span>
                    </td>
                    <td data-label="Query budget">{formatInteger(scenario.max_query_budget)}</td>
                    <td data-label="Held-out samples">{formatInteger(scenario.held_out_sample_count)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      {runsError ? (
        <ErrorState title="Runs could not be loaded" error={runsError} onRetry={reloadRuns} compact />
      ) : null}

      <section className="panel" aria-labelledby="results-heading">
        <div className="panel__header">
          <div>
            <p className="volume-label">II · Recorded results</p>
            <h2 id="results-heading" className="panel__title">Benchmark results for a run</h2>
          </div>
          {runId ? (
            <button
              type="button"
              className="button button--ghost button--small"
              onClick={reloadResults}
              disabled={resultsRefreshing}
            >
              <RefreshIcon size={16} />
              <span>{resultsRefreshing ? 'Refreshing…' : 'Refresh'}</span>
            </button>
          ) : null}
        </div>

        <div className="toolbar">
          <RunPicker runs={runs} value={runId} onChange={changeRun} isLoading={runsLoading} label="Run to inspect" />
        </div>

        {runId && resultsLoading ? <LoadingState label="Retrieving benchmark results…" compact /> : null}

        {runId && !resultsLoading && resultsError ? (
          <ErrorState
            compact
            title="Benchmark results could not be retrieved"
            error={resultsError}
            onRetry={reloadResults}
          />
        ) : null}

        {runId && !resultsLoading && !resultsError && results.length === 0 ? (
          <EmptyState
            title="No benchmark results for this run"
            message="No benchmark evaluation has been stored for this run. Absence of a result is not a zero score."
            action={<Link className="text-link" to={`/runs/${encodeURIComponent(runId)}`}>Open the run ledger</Link>}
          />
        ) : null}

        {runId && !resultsLoading && !resultsError && results.length > 0 ? (
          <div className="table-scroll">
            <table className="data-table data-table--wide">
              <caption className="visually-hidden">Benchmark results recorded for the selected run</caption>
              <thead>
                <tr>
                  <th scope="col">Benchmark</th>
                  <th scope="col">Scenario</th>
                  <th scope="col">Hit@1 / Hit@3 / Hit@5</th>
                  <th scope="col">Reciprocal rank</th>
                  <th scope="col">Held-out error</th>
                  <th scope="col">Baseline</th>
                  <th scope="col">Executed</th>
                </tr>
              </thead>
              <tbody>
                {results.map((result) => (
                  <tr key={result.benchmark_id}>
                    <td data-label="Benchmark">
                      <code className="id-text">{result.benchmark_id}</code>
                      <span className="block-note muted">
                        Best candidate {result.best_candidate_id || UNAVAILABLE}
                      </span>
                    </td>
                    <td data-label="Scenario">{result.benchmark_name}</td>
                    <td data-label="Hit@1 / Hit@3 / Hit@5">
                      {formatDecimal(result.hit_at_1, 2)} / {formatDecimal(result.hit_at_3, 2)} /{' '}
                      {formatDecimal(result.hit_at_5, 2)}
                    </td>
                    <td data-label="Reciprocal rank">{formatDecimal(result.reciprocal_rank)}</td>
                    <td data-label="Held-out error">
                      MAE {formatDecimal(result.held_out_mae)}
                      <span className="block-note muted">
                        RMSE {formatDecimal(result.held_out_rmse)}
                      </span>
                    </td>
                    <td data-label="Baseline">{result.baseline_type}</td>
                    <td data-label="Executed">
                      {formatDateTime(result.executed_at)}
                      {isUnavailable(result.queries_to_target) ? null : (
                        <span className="block-note muted">
                          {formatInteger(result.queries_to_target)} queries to target
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>
    </div>
  );
}
