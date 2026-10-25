import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { makeHypothesis, makeRun } from '../test/fixtures.js';

vi.mock('../api/runs.js', () => ({
  listRuns: vi.fn(),
  getRun: vi.fn(),
  getRunStatus: vi.fn(),
  getRunBudget: vi.fn(),
  cancelRun: vi.fn(),
  createRun: vi.fn(),
  listRunHypotheses: vi.fn(),
  getRunReport: vi.fn(),
  generateRunReport: vi.fn(),
  exportRunReport: vi.fn(),
  listBenchmarkScenarios: vi.fn(),
  getRunBenchmarks: vi.fn(),
}));

import {
  exportRunReport,
  generateRunReport,
  getRunBenchmarks,
  getRunReport,
  listBenchmarkScenarios,
  listRunHypotheses,
  listRuns,
} from '../api/runs.js';
import { BenchmarksPage } from './BenchmarksPage.jsx';
import { HypothesesPage } from './HypothesesPage.jsx';
import { ReportsPage } from './ReportsPage.jsx';

const RUN_ID = 'run-4c1b0e7a2d9f';

function renderAt(path, element) {
  return render(<MemoryRouter initialEntries={[path]}>{element}</MemoryRouter>);
}

const report = {
  report_id: 'rep-001',
  run_id: RUN_ID,
  status: 'completed',
  ranked_candidates: [
    {
      rank: 1,
      hypothesis_id: 'hyp-001',
      name: 'Candidate 1',
      model_family: 'polynomial',
      formula: 'y = 2.5*x^2 + 1',
      parameters: {},
      revision_number: 2,
      evaluation_id: 'eva-001',
      status: 'completed',
      evaluation_tier: 'primary',
      primary_metric_name: 'mae',
      held_out_score: null,
      training_score: 0.045,
      complexity_score: 3,
      dataset_id: RUN_ID,
      dataset_version: 12,
      explanation: 'Quadratic curvature explained the residual.',
      supporting_evidence: [],
      contradicting_evidence: [],
      indeterminate_count: 0,
      uncertainty_and_limitations: ['Held-out metrics were not computed.'],
    },
  ],
  stop_reason: 'max_queries_reached',
  // The v1 report record returns limitations joined into one string.
  limitations:
    'Sparse Candidate Pool: Only 2 candidates were evaluated; Low Sample Count: Only 2 observations are available.',
  artifact_uri: null,
  generated_at: '2026-10-24T09:00:00+00:00',
};

describe('scientific workflow pages', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listRuns.mockResolvedValue({ runs: [makeRun({ run_id: RUN_ID })], count: 1 });
  });

  it('renders recorded hypotheses with revisions and missing held-out metrics', async () => {
    listRunHypotheses.mockResolvedValue({ hypotheses: [makeHypothesis()], count: 1 });

    renderAt(`/hypotheses?run=${RUN_ID}`, <HypothesesPage />);

    expect(await screen.findByText('y = 2.5*x^2 + 1')).toBeInTheDocument();
    expect(screen.getByText('revision 2 · updated 24 Oct 2026, 08:05 UTC')).toBeInTheDocument();
    expect(screen.getByText(/held-out metrics unavailable/i)).toBeInTheDocument();
    expect(screen.getByText('MAE 0.0450 · RMSE 0.0520')).toBeInTheDocument();
  });

  it('shows an honest empty state when no hypotheses are recorded', async () => {
    listRunHypotheses.mockResolvedValue({ hypotheses: [], count: 0 });

    renderAt(`/hypotheses?run=${RUN_ID}`, <HypothesesPage />);

    expect(await screen.findByText('No hypotheses recorded for this run')).toBeInTheDocument();
  });

  it('does not fabricate a report and can ask the backend to generate one', async () => {
    getRunReport
      .mockResolvedValueOnce({ run_id: RUN_ID, report: null, status: 'pending' })
      .mockResolvedValueOnce({ report });
    generateRunReport.mockResolvedValue({ report, message: 'Report generated successfully.' });

    renderAt(`/reports?run=${RUN_ID}`, <ReportsPage />);

    expect(await screen.findByText('No report has been generated')).toBeInTheDocument();
    expect(screen.getByText(/backend reports “pending”/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /generate report/i }));

    await waitFor(() => {
      expect(generateRunReport).toHaveBeenCalledWith(RUN_ID);
    });
    expect(await screen.findByText('Completed report')).toBeInTheDocument();
    expect(screen.getByText('y = 2.5*x^2 + 1')).toBeInTheDocument();
    expect(
      screen.getByText('Sparse Candidate Pool: Only 2 candidates were evaluated'),
    ).toBeInTheDocument();
    expect(
      screen.getByText('Low Sample Count: Only 2 observations are available.'),
    ).toBeInTheDocument();
    expect(screen.getAllByText('Unavailable').length).toBeGreaterThan(0);
  });

  it('downloads the backend-rendered report as a PDF', async () => {
    getRunReport.mockResolvedValue({ report });
    const descriptor = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
    const downloads = [];
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      writable: true,
      value: vi.fn(() => 'blob:report'),
    });
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      writable: true,
      value: vi.fn(),
    });
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function capture() {
        downloads.push(this.download);
      });
    exportRunReport.mockResolvedValue({
      blob: new Blob(['%PDF-1.4'], { type: 'application/pdf' }),
      filename: `report_${RUN_ID}.pdf`,
      contentType: 'application/pdf',
    });

    renderAt(`/reports?run=${RUN_ID}`, <ReportsPage />);

    await userEvent.click(await screen.findByRole('button', { name: /^pdf$/i }));

    await waitFor(() => {
      expect(exportRunReport).toHaveBeenCalledWith(RUN_ID, 'pdf');
    });
    expect(downloads).toEqual([`report_${RUN_ID}.pdf`]);

    clickSpy.mockRestore();
    if (descriptor) Object.defineProperty(URL, 'createObjectURL', descriptor);
  });

  it('lists registered benchmark scenarios and reports missing results honestly', async () => {
    listBenchmarkScenarios.mockResolvedValue({
      scenarios: [
        {
          scenario_id: 'polynomial_constant_benchmark',
          benchmark_name: 'polynomial_constant_benchmark',
          version: '1.0',
          description: 'Discovery of a constant function',
          challenge_type: 'simple_numerical',
          domain_bounds: { variable_name: 'x', min_val: -5, max_val: 5, domain_name: '1d_polynomial' },
          max_query_budget: 8,
          held_out_sample_count: 8,
        },
      ],
      count: 1,
    });
    getRunBenchmarks.mockResolvedValue({ benchmarks: [], count: 0 });

    renderAt(`/benchmarks?run=${RUN_ID}`, <BenchmarksPage />);

    expect(await screen.findByText('polynomial_constant_benchmark')).toBeInTheDocument();
    expect(screen.getByText('Discovery of a constant function')).toBeInTheDocument();
    expect(await screen.findByText('No benchmark results for this run')).toBeInTheDocument();
    expect(screen.getByText(/absence of a result is not a zero score/i)).toBeInTheDocument();
  });
});
