import { useCallback, useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import {
  getRunBenchmarks,
  listBenchmarkScenarios,
  listPerformanceMeasurements,
  triggerBenchmarkComparison,
} from '../api/runs.js';
import { ActiveBaselineComparison } from '../components/benchmarks/ActiveBaselineComparison.jsx';
import { BenchmarkRunDetailModal } from '../components/benchmarks/BenchmarkRunDetailModal.jsx';
import { BenchmarkStatusBanner } from '../components/benchmarks/BenchmarkStatusBanner.jsx';
import { HistoricalBenchmarkTable } from '../components/benchmarks/HistoricalBenchmarkTable.jsx';
import { MeasuredPerformanceGrid } from '../components/benchmarks/MeasuredPerformanceGrid.jsx';
import { MetricExplanationsPanel } from '../components/benchmarks/MetricExplanationsPanel.jsx';
import { TriggerBenchmarkModal } from '../components/benchmarks/TriggerBenchmarkModal.jsx';
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

  // Filter scenario state
  const [selectedScenario, setSelectedScenario] = useState(searchParams.get('scenario') || '');

  // Modals state
  const [inspectedRun, setInspectedRun] = useState(null);
  const [triggerModalOpen, setTriggerModalOpen] = useState(false);
  const [triggerBusy, setTriggerBusy] = useState(false);
  const [triggerError, setTriggerError] = useState(null);

  // Active Comparison state from live triggers or most recent run
  const [liveComparison, setLiveComparison] = useState(null);

  // 1. Scenarios Resource
  const scenariosLoader = useCallback(({ signal }) => listBenchmarkScenarios({ signal }), []);
  const {
    data: scenarioData,
    isLoading: scenariosLoading,
    error: scenariosError,
    reload: reloadScenarios,
  } = useResource(scenariosLoader);

  // 2. Measurements History Resource
  const measurementsLoader = useCallback(
    ({ signal }) => {
      if (typeof listPerformanceMeasurements !== 'function') {
        return Promise.resolve({ measurements: [], count: 0 });
      }
      return listPerformanceMeasurements({ scenarioId: selectedScenario || null, limit: 50, signal }).catch(() => ({
        measurements: [],
        count: 0,
      }));
    },
    [selectedScenario],
  );
  const {
    data: measurementsData,
    isLoading: measurementsLoading,
    error: measurementsError,
    reload: reloadMeasurements,
  } = useResource(measurementsLoader);

  // 3. Run Benchmarks Resource (specific to selected run)
  const resultsLoader = useCallback(({ signal }) => getRunBenchmarks(runId, { signal }), [runId]);
  const {
    data: resultData,
    isLoading: resultsLoading,
    isRefreshing: resultsRefreshing,
    error: resultsError,
    reload: reloadResults,
  } = useResource(resultsLoader, { enabled: Boolean(runId) });

  const scenarios = scenarioData?.scenarios ?? [];
  const historicalMeasurements = measurementsData?.measurements ?? [];
  const runBenchmarks = resultData?.benchmarks ?? [];

  // Determine latest successful and attempted evaluations
  const latestSuccessful = historicalMeasurements.find((m) => m.has_measurements || m.hit_at_1 !== undefined) || runBenchmarks[0] || null;
  const latestAttempted = historicalMeasurements[0] || runBenchmarks[0] || null;

  // Selected measurement / benchmark for current performance display
  const activeBenchmarkResult = runBenchmarks[0] || historicalMeasurements[0] || null;

  const changeRun = (nextRunId) => {
    setSelectedRunId(nextRunId);
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (nextRunId) next.set('run', nextRunId);
        else next.delete('run');
        return next;
      },
      { replace: true },
    );
  };

  const handleSelectScenario = (scId) => {
    setSelectedScenario(scId);
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (scId) next.set('scenario', scId);
        else next.delete('scenario');
        return next;
      },
      { replace: true },
    );
  };

  const handleRefreshAll = () => {
    reloadScenarios();
    reloadMeasurements();
    if (runId) reloadResults();
  };

  const handleTriggerBenchmark = async (payload) => {
    setTriggerBusy(true);
    setTriggerError(null);
    try {
      const response = await triggerBenchmarkComparison(payload);
      if (response?.comparison) {
        setLiveComparison(response.comparison);
      }
      setTriggerModalOpen(false);
      handleRefreshAll();
    } catch (err) {
      setTriggerError(err);
    } finally {
      setTriggerBusy(false);
    }
  };

  return (
    <div className="page-inner">
      <PageHeader
        volume="Volume VIII · Agent Evaluation"
        title="Agent Performance & Scientific Benchmarks"
        description="Empirical quantification of CLARITY's hypothesis discovery accuracy, oracle query efficiency, compute expenditure, and execution runtime. Ground-truth secrets are held exclusively by the evaluator and are never leaked. Unmeasured values show 'Not evaluated' or 'Unavailable' rather than assumed zero scores."
      />
      <OrnamentDivider />

      {/* 1. Overall Benchmark Status Banner */}
      <BenchmarkStatusBanner
        latestSuccessful={latestSuccessful}
        latestAttempted={latestAttempted}
        totalRunsCount={historicalMeasurements.length}
        scenarios={scenarios}
        selectedScenario={selectedScenario}
        onSelectScenario={handleSelectScenario}
        onTriggerBenchmark={() => setTriggerModalOpen(true)}
        onRefresh={handleRefreshAll}
        isLoading={scenariosLoading || measurementsLoading || resultsRefreshing}
      />

      {/* 2. Current Measured Performance */}
      <MeasuredPerformanceGrid
        measurement={null}
        benchmarkResult={activeBenchmarkResult}
        selectedRun={runs?.find((r) => r.run_id === runId)}
      />

      {/* 3. Active Discovery vs. Passive Baseline */}
      <ActiveBaselineComparison
        comparisonReport={liveComparison?.report || null}
        comparisonSummary={liveComparison || (activeBenchmarkResult?.baseline_type ? activeBenchmarkResult : null)}
        scenarioName={selectedScenario || activeBenchmarkResult?.scenario_id || 'linear_polynomial'}
      />

      {/* 4. Metric Definitions and Technical Interpretation Guide */}
      <MetricExplanationsPanel />

      {/* 5. Historical Benchmark Runs */}
      <HistoricalBenchmarkTable
        runs={historicalMeasurements}
        scenarios={scenarios}
        onInspectRun={(item) => setInspectedRun(item)}
      />

      {/* 6. Scenario Register (Maintained for full system transparency) */}
      <section className="panel" aria-labelledby="scenarios-heading" style={{ marginTop: 'var(--space-5)' }}>
        <div className="panel__header">
          <div>
            <p className="volume-label">Evaluator Register</p>
            <h2 id="scenarios-heading" className="panel__title">Registered Benchmark Scenarios</h2>
          </div>
          <button
            type="button"
            className="button button--ghost button--small"
            onClick={reloadScenarios}
            disabled={scenariosLoading}
          >
            <RefreshIcon size={16} />
            <span>Refresh scenarios</span>
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

      {/* 7. Results Recorded for Run (Ledger Drilldown) */}
      <section className="panel" aria-labelledby="results-heading" style={{ marginTop: 'var(--space-5)' }}>
        <div className="panel__header">
          <div>
            <p className="volume-label">Discovery Run Audit</p>
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

        {runId && !resultsLoading && !resultsError && runBenchmarks.length === 0 ? (
          <EmptyState
            title="No benchmark results for this run"
            message="No benchmark evaluation has been stored for this run. Absence of a result is not a zero score."
            action={<Link className="text-link" to={`/runs/${encodeURIComponent(runId)}`}>Open the run ledger</Link>}
          />
        ) : null}

        {runId && !resultsLoading && !resultsError && runBenchmarks.length > 0 ? (
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
                  <th scope="col">Inspect</th>
                </tr>
              </thead>
              <tbody>
                {runBenchmarks.map((result) => (
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
                    <td>
                      <button
                        type="button"
                        className="button button--secondary button--small"
                        onClick={() => setInspectedRun(result)}
                        aria-label={`Inspect ${result.benchmark_id}`}
                      >
                        Details
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      {/* Modals */}
      <BenchmarkRunDetailModal
        open={Boolean(inspectedRun)}
        runItem={inspectedRun}
        onClose={() => setInspectedRun(null)}
      />

      <TriggerBenchmarkModal
        open={triggerModalOpen}
        scenarios={scenarios}
        busy={triggerBusy}
        onSubmit={handleTriggerBenchmark}
        onClose={() => setTriggerModalOpen(false)}
        backendError={triggerError}
      />
    </div>
  );
}
