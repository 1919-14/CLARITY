import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '../api/client.js';
import { makeBudget, makeProgress, makeRun } from '../test/fixtures.js';

vi.mock('../api/runs.js', () => ({
  listRuns: vi.fn(),
  getRun: vi.fn(),
  getRunStatus: vi.fn(),
  getRunBudget: vi.fn(),
  cancelRun: vi.fn(),
  createRun: vi.fn(),
}));

import { getRun, getRunBudget, getRunStatus, listRuns } from '../api/runs.js';
import { DashboardPage } from './DashboardPage.jsx';

function renderDashboard() {
  return render(
    <MemoryRouter>
      <DashboardPage />
    </MemoryRouter>,
  );
}

describe('DashboardPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getRun.mockResolvedValue({ run: makeRun() });
    getRunStatus.mockResolvedValue({ status: makeProgress() });
    getRunBudget.mockResolvedValue({ run_id: 'run-1', budget: makeBudget() });
  });

  it('shows a loading state while runs are being retrieved', () => {
    listRuns.mockReturnValue(new Promise(() => {}));
    renderDashboard();
    expect(screen.getByText(/reading active runs from the backend/i)).toBeInTheDocument();
    expect(screen.getByText(/retrieving persisted runs/i)).toBeInTheDocument();
  });

  it('lists actual persisted runs returned by the API', async () => {
    listRuns.mockResolvedValue({
      runs: [
        makeRun(),
        makeRun({ run_id: 'run-completed', name: 'Completed study', status: 'completed', completed_at: '2026-10-24T09:00:00+00:00' }),
      ],
      count: 2,
    });

    renderDashboard();

    const link = await screen.findByRole('link', { name: makeRun().run_id });
    expect(link).toHaveAttribute('href', `/runs/${makeRun().run_id}`);
    expect(screen.getByText('2 runs loaded', { exact: false })).toBeInTheDocument();
    expect(screen.getAllByText('Running').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Completed').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Local').length).toBeGreaterThan(0);
  });

  it('shows the active run with backend phase and resource usage', async () => {
    listRuns.mockResolvedValue({ runs: [makeRun()], count: 1 });

    renderDashboard();

    expect(await screen.findByText('Fitting candidate 5 of 10')).toBeInTheDocument();
    expect(screen.getByText('2 / 10')).toBeInTheDocument();
    expect(screen.getByText('5 / 100')).toBeInTheDocument();
    expect(screen.getByText(/auto-refreshing every few seconds/i)).toBeInTheDocument();
    expect(screen.getByText(/application-side estimate, not an invoice/i)).toBeInTheDocument();
  });

  it('renders an empty state for a new installation', async () => {
    listRuns.mockResolvedValue({ runs: [], count: 0 });

    renderDashboard();

    expect(await screen.findByText('No runs recorded yet')).toBeInTheDocument();
    expect(screen.getByText('No run is active')).toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: /create/i }).length).toBeGreaterThan(0);
  });

  it('renders a backend error and retries on request', async () => {
    listRuns
      .mockRejectedValueOnce(new ApiError('The CLARITY backend could not be reached.', { kind: 'network' }))
      .mockResolvedValueOnce({ runs: [], count: 0 });

    renderDashboard();

    expect(await screen.findByText('The run archive could not be opened')).toBeInTheDocument();
    expect(screen.getByText(/could not be reached/i)).toBeInTheDocument();
    expect(screen.getByText(/start the flask backend/i)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /try again/i }));

    await waitFor(() => {
      expect(screen.getByText('No runs recorded yet')).toBeInTheDocument();
    });
    expect(listRuns).toHaveBeenCalledTimes(2);
  });

  it('shows missing optional fields as unavailable rather than guessing', async () => {
    listRuns.mockResolvedValue({
      runs: [
        makeRun({
          run_id: 'run-sparse',
          name: 'Sparse run',
          status: null,
          created_at: null,
          execution_mode: null,
        }),
      ],
      count: 1,
    });

    renderDashboard();

    const link = await screen.findByRole('link', { name: 'run-sparse' });
    const row = link.closest('tr');
    expect(row).not.toBeNull();
    expect(row.textContent).toMatch(/Unavailable/);
  });
});
