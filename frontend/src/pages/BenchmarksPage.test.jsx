import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getBenchmarkComparison,
  getRunBenchmarks,
  listBenchmarkScenarios,
  listPerformanceMeasurements,
  listRuns,
  triggerBenchmarkComparison,
} from '../api/runs.js';
import { BenchmarksPage } from './BenchmarksPage.jsx';

vi.mock('../api/runs.js', () => ({
  listRuns: vi.fn(),
  listBenchmarkScenarios: vi.fn(),
  getRunBenchmarks: vi.fn(),
  listPerformanceMeasurements: vi.fn(),
  getBenchmarkComparison: vi.fn(),
  triggerBenchmarkComparison: vi.fn(),
}));

function renderPage(path = '/benchmarks') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <BenchmarksPage />
    </MemoryRouter>,
  );
}

const mockScenario = {
  scenario_id: 'linear_polynomial',
  benchmark_name: 'linear_polynomial',
  version: '1.0.0',
  description: 'First order polynomial discovery with noisy observations',
  challenge_type: 'noisy_discovery',
  domain_bounds: {
    variable_name: 'x',
    min_val: -10,
    max_val: 10,
    domain_name: '1d_reals',
  },
  max_query_budget: 15,
  held_out_sample_count: 20,
};

const mockCompletedMeasurement = {
  benchmark_id: 'bm-success-001',
  scenario_id: 'linear_polynomial',
  run_id: 'run-alpha-42',
  has_measurements: true,
  status: 'completed',
  hit_at_1: 1.0,
  hit_at_3: 1.0,
  hit_at_5: 1.0,
  mean_reciprocal_rank: 0.85,
  held_out_mae: 0.042,
  held_out_rmse: 0.058,
  y_units: 'arbitrary_units',
  queries_to_target: 7,
  max_query_budget: 15,
  target_reached: true,
  complexity_score: 3,
  uncertainty_score: 0.12,
  cost_estimate_usd: 0.035,
  cost_status: 'settled',
  duration_ms: 1240,
  executed_at: '2026-10-24T12:00:00Z',
  dataset_version: '2.1.0',
  metric_definition_version: '1.0.0',
  measurement_method_version: '1.0.0',
  execution_mode: 'active_guided',
  evaluated_case_count: 25,
};

const mockFailedMeasurement = {
  benchmark_id: 'bm-failed-002',
  scenario_id: 'linear_polynomial',
  run_id: 'run-beta-99',
  has_measurements: false,
  status: 'failed',
  executed_at: '2026-10-24T14:30:00Z',
  evaluated_case_count: 0,
};

describe('Agent Performance & Scientific Benchmarks Page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listRuns.mockResolvedValue({
      runs: [{ run_id: 'run-alpha-42', name: 'Alpha Active Discovery' }],
      count: 1,
    });
    listBenchmarkScenarios.mockResolvedValue({
      scenarios: [mockScenario],
      count: 1,
    });
    getRunBenchmarks.mockResolvedValue({
      benchmarks: [mockCompletedMeasurement],
      count: 1,
    });
    listPerformanceMeasurements.mockResolvedValue({
      measurements: [mockFailedMeasurement, mockCompletedMeasurement],
      count: 2,
    });
  });

  it('renders the dedicated benchmark page title and header volume', async () => {
    renderPage();
    expect(await screen.findByRole('heading', { level: 1, name: /Agent Performance & Scientific Benchmarks/i })).toBeInTheDocument();
    expect(screen.getByText(/Volume VIII · Agent Evaluation/i)).toBeInTheDocument();
  });

  it('distinguishes the latest successful evaluation from the latest attempted evaluation', async () => {
    renderPage();

    // Latest successful is bm-success-001 (linear_polynomial)
    expect(await screen.findByText('Latest Successful Evaluation')).toBeInTheDocument();
    // Latest attempted is bm-failed-002 (failed)
    expect(screen.getByText('Latest Attempted Evaluation')).toBeInTheDocument();
    expect(screen.getByText('Status:')).toBeInTheDocument();
  });

  it('displays measured scientific discovery metrics with directions and units', async () => {
    renderPage('/benchmarks?run=run-alpha-42');

    // Hit@K section
    expect(await screen.findByText('Hit@K Discovery Recall')).toBeInTheDocument();
    expect(screen.getAllByText(/Higher is better/i).length).toBeGreaterThan(0);
    expect(screen.getByText('1.000')).toBeInTheDocument();

    // MRR section
    expect(screen.getAllByText('Mean Reciprocal Rank (MRR)').length).toBeGreaterThan(0);
    expect(screen.getByText('0.8500')).toBeInTheDocument();

    // Held-out error
    expect(screen.getByText('Held-out Predictive Error')).toBeInTheDocument();
    expect(screen.getAllByText(/0\.0420/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/0\.0580/).length).toBeGreaterThan(0);

    // Queries to success
    expect(screen.getByText('Queries-to-Success')).toBeInTheDocument();
    expect(screen.getAllByText('7').length).toBeGreaterThan(0);
    expect(screen.getByText(/15 budget/i)).toBeInTheDocument();

    // Complexity & uncertainty
    expect(screen.getByText('Complexity & Uncertainty')).toBeInTheDocument();
    expect(screen.getAllByText('3').length).toBeGreaterThan(0);

    // Cost & Runtime
    expect(screen.getByText('Compute Cost & Runtime')).toBeInTheDocument();
    expect(screen.getByText('$0.0350')).toBeInTheDocument();
  });

  it('allows switching K values on Hit@K (K=1, K=3, K=5)', async () => {
    const user = userEvent.setup();
    renderPage('/benchmarks?run=run-alpha-42');

    expect(await screen.findByText('Hit@K Discovery Recall')).toBeInTheDocument();

    const hit3Button = screen.getByRole('button', { name: 'K=3' });
    await user.click(hit3Button);
    expect(hit3Button).toHaveClass('button--primary');

    const hit5Button = screen.getByRole('button', { name: 'K=5' });
    await user.click(hit5Button);
    expect(hit5Button).toHaveClass('button--primary');
  });

  it('shows explicit "Unavailable" when cost or uncertainty metrics are unmeasured', async () => {
    const measurementWithoutCost = {
      ...mockCompletedMeasurement,
      cost_estimate_usd: null,
      uncertainty_score: null,
    };
    getRunBenchmarks.mockResolvedValue({
      benchmarks: [measurementWithoutCost],
      count: 1,
    });
    listPerformanceMeasurements.mockResolvedValue({
      measurements: [measurementWithoutCost],
      count: 1,
    });

    renderPage('/benchmarks?run=run-alpha-42');

    expect(await screen.findByText('Hit@K Discovery Recall')).toBeInTheDocument();
    // Unavailable badges/values should be present
    expect(screen.getAllByText('Unavailable').length).toBeGreaterThan(0);
  });

  it('displays an honest "Not evaluated" state when no benchmarks have been executed', async () => {
    getRunBenchmarks.mockResolvedValue({ benchmarks: [], count: 0 });
    listPerformanceMeasurements.mockResolvedValue({ measurements: [], count: 0 });

    renderPage();

    expect(await screen.findByText(/Not evaluated:/i)).toBeInTheDocument();
    expect(screen.getByText(/No scientific discovery benchmarks have been run yet/i)).toBeInTheDocument();
  });

  it('shows an honest warning when baseline measurements are unavailable', async () => {
    renderPage('/benchmarks?run=run-alpha-42');

    expect(await screen.findByText('Active Discovery vs. Passive Baseline')).toBeInTheDocument();
    expect(screen.getByText('Baseline measurements unavailable')).toBeInTheDocument();
    expect(
      screen.getByText(/A compatible baseline evaluation has not been executed/i),
    ).toBeInTheDocument();
  });

  it('renders comparative results when active vs baseline benchmark comparison exists', async () => {
    const comparisonData = {
      comparison: {
        comparison_id: 'cmp-9988',
        scenario_id: 'linear_polynomial',
        active_summary: {
          hit_at_1: 1.0,
          mean_reciprocal_rank: 0.9,
          held_out_mae: 0.03,
          held_out_rmse: 0.04,
          queries_to_target: 6,
          max_query_budget: 15,
          cost_estimate_usd: 0.03,
          duration_ms: 1100,
          total_runs: 5,
        },
        baseline_summary: {
          hit_at_1: 0.4,
          mean_reciprocal_rank: 0.45,
          held_out_mae: 0.12,
          held_out_rmse: 0.18,
          queries_to_target: 14,
          max_query_budget: 15,
          cost_estimate_usd: 0.028,
          duration_ms: 980,
          total_runs: 5,
        },
        differences: {
          hit_at_1: { absolute_diff: 0.6, relative_pct_change: 150.0, direction: 'higher_is_better' },
          mean_reciprocal_rank: { absolute_diff: 0.45, relative_pct_change: 100.0, direction: 'higher_is_better' },
          held_out_mae: { absolute_diff: -0.09, relative_pct_change: -75.0, direction: 'lower_is_better' },
          queries_to_target: { absolute_diff: -8, relative_pct_change: -57.1, direction: 'lower_is_better' },
        },
      },
    };

    triggerBenchmarkComparison.mockResolvedValue(comparisonData);

    const user = userEvent.setup();
    renderPage('/benchmarks?run=run-alpha-42');

    // Click "Run Benchmark" button
    const runBtn = await screen.findByRole('button', { name: /Run Benchmark/i });
    await user.click(runBtn);

    // Modal opens
    expect(screen.getByRole('dialog', { name: /Run Scientific Benchmark/i })).toBeInTheDocument();

    // Submit form
    const triggerSubmit = screen.getByRole('button', { name: /Execute Benchmark/i });
    await user.click(triggerSubmit);

    // Table should now render with comparison
    await waitFor(() => {
      expect(screen.getByText('Controlled Evaluation')).toBeInTheDocument();
      expect(screen.getByText(/Hit@1 Rate/i)).toBeInTheDocument();
      expect(screen.getByText('+150.0%')).toBeInTheDocument();
    });
  });

  it('expands and collapses metric explanations in the technical guide', async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText('Metric Definitions and Interpretation')).toBeInTheDocument();

    const hitAtKButton = screen.getByRole('button', { name: /Hit@K Discovery Rate/i });
    expect(hitAtKButton).toHaveAttribute('aria-expanded', 'false');

    await user.click(hitAtKButton);
    expect(hitAtKButton).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText(/Formal Definition/i)).toBeInTheDocument();
    expect(screen.getByText(/Higher is better \(1\.0 = perfect discovery on every case\)/i)).toBeInTheDocument();

    // Collapse
    await user.click(hitAtKButton);
    expect(hitAtKButton).toHaveAttribute('aria-expanded', 'false');
  });

  it('inspects a historical benchmark run in the detail modal and closes on Escape', async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText('Historical Benchmark Runs')).toBeInTheDocument();

    // Click "Inspect" button on the first historical row
    const inspectButtons = screen.getAllByRole('button', { name: /Inspect/i });
    await user.click(inspectButtons[0]);

    // Detail modal should be visible
    const modal = screen.getByRole('dialog');
    expect(modal).toBeInTheDocument();
    expect(screen.getByText(/Methodology & Execution Metadata/i)).toBeInTheDocument();
    expect(screen.getByText(/Evaluator Security Invariant/i)).toBeInTheDocument();

    // Close on Escape
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    });
  });

  it('displays API error state when benchmark scenarios fail to load', async () => {
    listBenchmarkScenarios.mockRejectedValue(new Error('Backend connection refused'));
    renderPage();

    expect(await screen.findByText('Scenarios could not be retrieved')).toBeInTheDocument();
    expect(screen.getByText(/Backend connection refused/i)).toBeInTheDocument();
  });

  it('renders ErrorState in HistoricalBenchmarkTable when measurements request fails', async () => {
    listPerformanceMeasurements.mockRejectedValue(new Error('500 Internal Server Error: database connection timeout'));
    renderPage();

    expect(await screen.findByText(/Historical benchmark runs could not be loaded/i)).toBeInTheDocument();
    expect(screen.getByText(/500 Internal Server Error/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Try again|Retry/i })).toBeInTheDocument();
    // It must NOT show the empty state
    expect(screen.queryByText(/No historical benchmark runs/i)).not.toBeInTheDocument();
  });

  it('correctly handles actual backend response shape with boolean hit_at_1/3/5 values', async () => {
    // Real BenchmarkResultModel shape returned by backend serialize_benchmark
    const realBackendBenchmark = {
      benchmark_id: 'bm-real-backend-001',
      run_id: 'run-alpha-42',
      benchmark_name: 'polynomial_constant_benchmark',
      hit_at_1: true,
      hit_at_3: true,
      hit_at_5: false,
      reciprocal_rank: 1.0,
      held_out_mae: 0.0123,
      held_out_rmse: 0.0245,
      queries_to_target: 4,
      baseline_type: 'random_uniform',
      efficiency_gain: 0.65,
      executed_at: '2026-10-24T18:00:00Z',
      has_measurements: true,
      status: 'completed',
    };

    getRunBenchmarks.mockResolvedValue({
      benchmarks: [realBackendBenchmark],
      count: 1,
    });
    listPerformanceMeasurements.mockResolvedValue({
      measurements: [realBackendBenchmark],
      count: 1,
    });

    renderPage('/benchmarks?run=run-alpha-42');

    // Boolean hit_at_1: true must format to 1.000, NOT 'Unavailable'
    expect(await screen.findByText('Hit@K Discovery Recall')).toBeInTheDocument();
    expect(screen.getByText('1.000')).toBeInTheDocument();

    // In the ledger
    expect(screen.getAllByText('polynomial_constant_benchmark').length).toBeGreaterThan(0);
    // Boolean hit_at_1 should display in ledger as 1.00 or 1.000
    expect(screen.getAllByText('1.00').length).toBeGreaterThan(0);
  });

  it('never displays failed or unknown evaluations as completed or successful', async () => {
    const failedOnlyList = [
      {
        benchmark_id: 'bm-only-failed',
        scenario_id: 'linear_polynomial',
        run_id: 'run-alpha-42',
        has_measurements: false,
        status: 'failed',
        executed_at: '2026-10-24T15:00:00Z',
      },
    ];

    listPerformanceMeasurements.mockResolvedValue({
      measurements: failedOnlyList,
      count: 1,
    });
    getRunBenchmarks.mockResolvedValue({
      benchmarks: failedOnlyList,
      count: 1,
    });

    renderPage();

    // Banner should show latest attempted status as failed with danger badge
    expect(await screen.findByText('Latest Attempted Evaluation')).toBeInTheDocument();
    const statusBadges = screen.getAllByText('failed');
    expect(statusBadges.length).toBeGreaterThan(0);
    expect(statusBadges[0]).toHaveClass('badge--danger');

    // "Latest Successful Evaluation" must NOT display bm-only-failed as successful
    expect(screen.getByText(/No completed benchmark evaluations recorded yet/i)).toBeInTheDocument();
  });

  it('renders Unavailable when provenance metadata is not supplied by the API', async () => {
    const bareMeasurement = {
      benchmark_id: 'bm-bare-001',
      scenario_id: 'linear_polynomial',
      run_id: 'run-alpha-42',
      has_measurements: true,
      status: 'completed',
      hit_at_1: 1.0,
      executed_at: '2026-10-24T12:00:00Z',
      // None of the versions or thresholds are provided:
      metric_definition_version: undefined,
      dataset_version: undefined,
      execution_mode: undefined,
      success_threshold: undefined,
    };

    listPerformanceMeasurements.mockResolvedValue({
      measurements: [bareMeasurement],
      count: 1,
    });
    getRunBenchmarks.mockResolvedValue({
      benchmarks: [bareMeasurement],
      count: 1,
    });

    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText('Historical Benchmark Runs')).toBeInTheDocument();

    // Inspect the run
    const inspectBtn = screen.getByRole('button', { name: /Inspect/i });
    await user.click(inspectBtn);

    // In modal: versions should be 'Unavailable', not hardcoded '1.0.0'
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    const unavailables = screen.getAllByText('Unavailable');
    expect(unavailables.length).toBeGreaterThan(0);
    // Ensure '1.0.0' is NOT shown when unsupplied
    expect(screen.queryByText('1.0.0')).not.toBeInTheDocument();
  });
});

